#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Connectivity test for the main model and the remote UI-TARS endpoint."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]


def first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def make_client(base_url: str, api_key: str) -> OpenAI:
    kwargs = {"api_key": api_key or "dummy-key", "timeout": 30.0}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def exception_details(exc: Exception) -> list[str]:
    lines = [f"{type(exc).__name__}: {exc}"]
    status_code = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = status_code or getattr(response, "status_code", None)
        try:
            lines.append(f"response_text={response.text[:500]}")
        except Exception:
            pass
    body = getattr(exc, "body", None)
    if status_code is not None:
        lines.append(f"http_status={status_code}")
    if request_id:
        lines.append(f"request_id={request_id}")
    if body:
        lines.append(f"body={str(body)[:500]}")
    return lines


def endpoint_hint(name: str, base_url: str, exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = status_code or getattr(response, "status_code", None)
    local_endpoint = "127.0.0.1" in base_url or "localhost" in base_url
    if status_code == 502:
        if name == "ground" or local_endpoint:
            return (
                "Grounding/UI-TARS 502: check SSH tunnel, remote vLLM process, "
                ".windows/agent_s_paramiko_tunnel*.log, and ~/agent_s_vllm.log."
            )
        return "Main model 502: upstream/provider gateway issue; retry or switch provider endpoint."
    if status_code == 401:
        return "Authentication failed: check API key for this endpoint."
    if status_code == 403:
        return "Permission denied: check token/model access for this endpoint."
    if status_code == 404:
        return "Not found: check base_url and model/served-model name."
    if status_code == 429:
        return "Rate limited: wait or reduce request frequency."
    if name == "ground" or local_endpoint:
        return "Grounding/UI-TARS failed: verify tunnel, local port, and remote vLLM health."
    return "Main model failed: verify provider URL, API key, model name, and network."


def chat_completion(client: OpenAI, model: str):
    messages = [{"role": "user", "content": "Reply with OK only."}]
    attempts = [
        {"max_tokens": 16, "temperature": 0},
        {"max_completion_tokens": 16, "temperature": 0},
        {"max_completion_tokens": 16},
        {},
    ]
    last_error: Exception | None = None
    for params in attempts:
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                **params,
            )
        except Exception as exc:
            text = str(exc)
            retryable = (
                "max_tokens" in text
                or "max_completion_tokens" in text
                or "temperature" in text
                or "Unsupported parameter" in text
            )
            if not retryable:
                raise
            last_error = exc
    assert last_error is not None
    raise last_error


def test_chat(name: str, base_url: str, api_key: str, model: str) -> bool:
    print(f"\n[{name}] URL: {base_url or '(default OpenAI endpoint)'}")
    print(f"[{name}] Model: {model}")
    print(f"[{name}] API key: {mask(api_key)}")
    if not model:
        print(f"[{name}] FAIL: model is empty")
        return False
    if not api_key:
        print(f"[{name}] FAIL: API key is empty")
        return False

    client = make_client(base_url, api_key)
    start = time.time()
    response = chat_completion(client, model)
    elapsed = time.time() - start
    content = (response.choices[0].message.content or "").strip()
    print(f"[{name}] OK in {elapsed:.2f}s: {content[:120]}")
    return True


def test_grounding() -> bool:
    base_url = first_env(
        "AGENT_S_GROUND_URL", "HF_ENDPOINT_URL", default="http://127.0.0.1:8000/v1"
    )
    api_key = first_env("AGENT_S_GROUND_API_KEY", "HF_TOKEN", default="dummy-key")
    model = first_env("AGENT_S_GROUND_MODEL", "GROUND_MODEL", default="UI-TARS-1.5-7B")

    print(f"\n[ground] URL: {base_url}")
    print(f"[ground] Model: {model}")
    client = make_client(base_url, api_key)
    models = client.models.list()
    model_ids = [item.id for item in models.data]
    print(f"[ground] /models: {', '.join(model_ids) if model_ids else '(empty)'}")
    if not model_ids:
        print("[ground] WARN: /models returned no served models; continuing to chat/completions.")
    if model_ids and model not in model_ids:
        print(f"[ground] WARN: configured model {model!r} is not listed by /models.")
        print("  hint=If chat/completions fails with 404, set AGENT_S_GROUND_MODEL/GROUND_MODEL to one of /models or restart vLLM with --served-model-name UI-TARS-1.5-7B.")
    return test_chat("ground", base_url, api_key, model)


def test_main() -> bool:
    provider = first_env("AGENT_S_PROVIDER", default="openai")
    if provider != "openai":
        print(f"\n[main] SKIP: test script currently checks OpenAI-compatible providers; provider={provider}")
        return True
    base_url = first_env("AGENT_S_MODEL_URL", "OPENAI_BASE_URL")
    api_key = first_env("AGENT_S_MODEL_API_KEY", "OPENAI_API_KEY")
    model = first_env("AGENT_S_MODEL", "ARCHIVE_LLM_MODEL", default="gpt-5.4")
    return test_chat("main", base_url, api_key, model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test Agent-S3 model endpoints.")
    parser.add_argument("--skip-main", action="store_true", help="Only test grounding endpoint.")
    parser.add_argument("--skip-ground", action="store_true", help="Only test main model endpoint.")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")

    print("=" * 60)
    print("Agent-S3 endpoint test")
    print("=" * 60)

    ok = True
    if not args.skip_main:
        try:
            ok = test_main() and ok
        except Exception as exc:
            ok = False
            print("[main] FAIL:")
            for line in exception_details(exc):
                print(f"  {line}")
            print(f"  hint={endpoint_hint('main', first_env('AGENT_S_MODEL_URL', 'OPENAI_BASE_URL'), exc)}")

    if not args.skip_ground:
        try:
            ok = test_grounding() and ok
        except Exception as exc:
            ok = False
            ground_url = first_env(
                "AGENT_S_GROUND_URL",
                "HF_ENDPOINT_URL",
                default="http://127.0.0.1:8000/v1",
            )
            print("[ground] FAIL:")
            for line in exception_details(exc):
                print(f"  {line}")
            print(f"  hint={endpoint_hint('ground', ground_url, exc)}")

    print("\n" + "=" * 60)
    print("Result:", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
