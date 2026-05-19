#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Password-capable SSH local port forward for the remote UI-TARS vLLM server."""

from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

import paramiko
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def log(message: str) -> None:
    print(message, flush=True)


def connect_ssh(host: str, port: int, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
        banner_timeout=30,
        auth_timeout=30,
    )
    transport = client.get_transport()
    if not transport or not transport.is_active():
        raise RuntimeError("SSH transport is not active after login")
    transport.set_keepalive(30)
    return client


def remote_start_script(args: argparse.Namespace) -> str:
    auto_start = "1" if args.auto_start else "0"
    return f"""set -e
AUTO_START="{auto_start}"
CONDA_ENV="{args.conda_env}"
CUDA_DEVICES="{args.cuda_devices}"
MODEL_PATH="{args.model_path}"
SERVED_MODEL="{args.served_model}"
TP_SIZE="{args.tp_size}"
VLLM_PORT="{args.remote_vllm_port}"

if command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)"
  conda activate "$CONDA_ENV"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  . "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate "$CONDA_ENV"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
  . "$HOME/anaconda3/etc/profile.d/conda.sh"
  conda activate "$CONDA_ENV"
fi

if curl -fsS --max-time 3 "http://127.0.0.1:$VLLM_PORT/v1/models" >/dev/null; then
  echo "[remote] vLLM is already ready on port $VLLM_PORT."
elif [ "$AUTO_START" = "1" ]; then
  if pgrep -af "vllm.entrypoints.openai.api_server.*$SERVED_MODEL" >/dev/null 2>&1; then
    echo "[remote] vLLM process exists; waiting for API..."
  else
    echo "[remote] starting vLLM: $SERVED_MODEL"
    nohup env CUDA_VISIBLE_DEVICES="$CUDA_DEVICES" python -m vllm.entrypoints.openai.api_server \\
      --model "$MODEL_PATH" \\
      --tensor-parallel-size "$TP_SIZE" \\
      --served-model-name "$SERVED_MODEL" \\
      --trust-remote-code \\
      --host 0.0.0.0 \\
      --port "$VLLM_PORT" > "$HOME/agent_s_vllm.log" 2>&1 &
  fi

  for i in $(seq 1 180); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$VLLM_PORT/v1/models" >/dev/null; then
      echo "[remote] vLLM API is ready."
      exit 0
    fi
    sleep 2
  done
  echo "[remote] timed out waiting for vLLM; tail ~/agent_s_vllm.log for details." >&2
  exit 1
else
  echo "[remote] vLLM is not ready and AGENT_S_REMOTE_AUTO_START=0." >&2
  exit 1
fi
"""


def ensure_remote_vllm(client: paramiko.SSHClient, args: argparse.Namespace) -> None:
    log("[ssh] Checking remote vLLM service...")
    stdin, stdout, stderr = client.exec_command(f"bash -lc {quote_bash(remote_start_script(args))}")
    stdin.close()
    for line in iter(stdout.readline, ""):
        if line:
            log(line.rstrip())
    err = stderr.read().decode("utf-8", errors="replace").strip()
    code = stdout.channel.recv_exit_status()
    if err:
        log(err)
    if code != 0:
        raise RuntimeError(f"remote vLLM check/start failed with exit code {code}")


def quote_bash(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def relay(local_sock: socket.socket, transport: paramiko.Transport, remote_host: str, remote_port: int) -> None:
    peer = local_sock.getpeername()
    try:
        channel = transport.open_channel(
            "direct-tcpip",
            (remote_host, remote_port),
            peer,
        )
    except Exception as exc:
        log(f"[ssh] Could not open channel to {remote_host}:{remote_port}: {exc}")
        local_sock.close()
        return

    if channel is None:
        log(f"[ssh] SSH channel rejected for {remote_host}:{remote_port}")
        local_sock.close()
        return

    try:
        while True:
            readable, _, _ = select.select([local_sock, channel], [], [])
            if local_sock in readable:
                data = local_sock.recv(65536)
                if not data:
                    break
                channel.sendall(data)
            if channel in readable:
                data = channel.recv(65536)
                if not data:
                    break
                local_sock.sendall(data)
    finally:
        channel.close()
        local_sock.close()


def serve_forward(client: paramiko.SSHClient, args: argparse.Namespace) -> None:
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not available")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.local_host, args.local_port))
    server.listen(100)
    log(
        f"[ssh] Forwarding http://{args.local_host}:{args.local_port}/v1 "
        f"-> {args.remote_bind_host}:{args.remote_vllm_port} via {args.remote_user}@{args.remote_host}:{args.remote_ssh_port}"
    )

    try:
        while True:
            local_sock, _ = server.accept()
            thread = threading.Thread(
                target=relay,
                args=(local_sock, transport, args.remote_bind_host, args.remote_vllm_port),
                daemon=True,
            )
            thread.start()
    finally:
        server.close()


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
    parser = argparse.ArgumentParser(description="Password-capable SSH tunnel for Agent-S UI-TARS.")
    parser.add_argument("--local-host", default="127.0.0.1")
    parser.add_argument("--local-port", type=int, default=int(first_env("AGENT_S_LOCAL_TUNNEL_PORT", default="8000")))
    parser.add_argument("--remote-host", default=first_env("AGENT_S_REMOTE_HOST", default="111.0.130.56"))
    parser.add_argument("--remote-ssh-port", type=int, default=int(first_env("AGENT_S_REMOTE_SSH_PORT", default="10023")))
    parser.add_argument("--remote-user", default=first_env("AGENT_S_REMOTE_USER", default="lcwt"))
    parser.add_argument("--remote-password", default=first_env("AGENT_S_REMOTE_PASSWORD"))
    parser.add_argument("--remote-bind-host", default="127.0.0.1")
    parser.add_argument("--remote-vllm-port", type=int, default=int(first_env("AGENT_S_REMOTE_VLLM_PORT", default="8000")))
    parser.add_argument("--conda-env", default=first_env("AGENT_S_REMOTE_CONDA_ENV", default="vllm"))
    parser.add_argument("--cuda-devices", default=first_env("AGENT_S_REMOTE_CUDA_VISIBLE_DEVICES", default="1,3"))
    parser.add_argument("--model-path", default=first_env("AGENT_S_REMOTE_MODEL_PATH", default="/mnt/data/Models/UI-TARS-1.5-7B"))
    parser.add_argument("--served-model", default=first_env("AGENT_S_GROUND_MODEL", "GROUND_MODEL", default="UI-TARS-1.5-7B"))
    parser.add_argument("--tp-size", default=first_env("AGENT_S_REMOTE_TP_SIZE", default="2"))
    parser.add_argument("--auto-start", action="store_true", default=first_env("AGENT_S_REMOTE_AUTO_START", default="1").lower() in {"1", "true", "yes", "on"})
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.remote_password:
        log("[ssh] AGENT_S_REMOTE_PASSWORD is empty; cannot use password tunnel.")
        return 2

    try:
        log(f"[ssh] Connecting to {args.remote_user}@{args.remote_host}:{args.remote_ssh_port} with configured password...")
        client = connect_ssh(args.remote_host, args.remote_ssh_port, args.remote_user, args.remote_password)
        ensure_remote_vllm(client, args)
        serve_forward(client, args)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log(f"[ssh] FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
