"""Run Agent-S3 CLI from project-local environment variables."""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent


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


def add_optional_flag(cmd: list[str], flag: str, value: str) -> None:
    if value:
        cmd.extend([flag, value])


def main() -> int:
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")

    provider = first_env("AGENT_S_PROVIDER", default="openai")
    model = first_env("AGENT_S_MODEL", "ARCHIVE_LLM_MODEL", default="gpt-5.4")
    model_url = first_env("AGENT_S_MODEL_URL", "OPENAI_BASE_URL")
    model_api_key = first_env("AGENT_S_MODEL_API_KEY", "OPENAI_API_KEY")

    ground_provider = first_env("AGENT_S_GROUND_PROVIDER", default="huggingface")
    ground_url = first_env(
        "AGENT_S_GROUND_URL", "HF_ENDPOINT_URL", default="http://127.0.0.1:8000/v1"
    )
    ground_api_key = first_env("AGENT_S_GROUND_API_KEY", "HF_TOKEN", default="dummy-key")
    ground_model = first_env("AGENT_S_GROUND_MODEL", "GROUND_MODEL", default="UI-TARS-1.5-7B")
    grounding_width = first_env("AGENT_S_GROUNDING_WIDTH", default="1920")
    grounding_height = first_env("AGENT_S_GROUNDING_HEIGHT", default="1080")
    max_steps = first_env("AGENT_S_MAX_AGENT_STEPS", "AGENT_GUI_MAX_ITERATIONS")

    print("=" * 60)
    print("Agent-S3 CLI 启动")
    print("=" * 60)
    print("\n配置:")
    print(f"  主模型 Provider: {provider}")
    print(f"  主模型: {model}")
    print(f"  主模型 URL: {model_url or '(default OpenAI endpoint)'}")
    print(f"  主模型 Key: {mask(model_api_key)}")
    print(f"  Grounding Provider: {ground_provider}")
    print(f"  Grounding 模型: {ground_model}")
    print(f"  Grounding URL: {ground_url}")
    print("\n提示:")
    print("  - 输入你的任务指令")
    print("  - 按 Ctrl+C 可暂停/退出 Agent-S3")
    print("=" * 60)
    print()

    cmd = [
        sys.executable,
        "-m",
        "gui_agents.s3.cli_app",
        "--provider",
        provider,
        "--model",
        model,
        "--model_url",
        model_url,
        "--model_api_key",
        model_api_key,
        "--ground_provider",
        ground_provider,
        "--ground_url",
        ground_url,
        "--ground_api_key",
        ground_api_key,
        "--ground_model",
        ground_model,
        "--grounding_width",
        grounding_width,
        "--grounding_height",
        grounding_height,
    ]

    add_optional_flag(cmd, "--model_temperature", first_env("AGENT_S_MODEL_TEMPERATURE"))
    add_optional_flag(cmd, "--max_trajectory_length", first_env("AGENT_S_MAX_TRAJECTORY_LENGTH"))
    add_optional_flag(cmd, "--max-agent-steps", max_steps)

    if first_env("AGENT_S_ENABLE_LOCAL_ENV", default="0").lower() in {"1", "true", "yes", "on"}:
        cmd.append("--enable_local_env")

    enable_reflection = first_env("AGENT_S_ENABLE_REFLECTION", default="1")
    if enable_reflection.lower() in {"0", "false", "no", "off"}:
        cmd.append("--no-enable_reflection")

    cmd.extend(sys.argv[1:])

    try:
        return subprocess.run(cmd, cwd=ROOT).returncode
    except KeyboardInterrupt:
        print("\n\n[退出] Agent-S3 已停止")
        return 130
    except Exception as e:
        print(f"\n[错误] 运行失败: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
