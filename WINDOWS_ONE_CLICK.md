# Windows 一键启动与测试

这套脚本面向任意 Windows 10/11 客户端：首次运行会自动创建项目内 `.venv`、安装依赖、通过 SSH 隧道连接服务器上的 UI-TARS vLLM，然后启动 Agent-S3。

## 入口

- `test_windows.bat`：只测试主模型和远端 UI-TARS grounding endpoint。
- `run_cli.bat`：完整启动 Agent-S3 CLI。
- `start_remote_model_tunnel.bat`：只打开 SSH 隧道，适合单独排查服务器连接。
- `run_file_archive_watcher.bat` / `run_archive_once.bat`：长期监视 `WATCH_DIR`，新文件完成分类后可调用 Agent-S3 归档到飞书。
- `run_archive_single.bat`：选择或传入单个文件，执行一次分类和可选飞书归档。
- `test_feishu_only.bat`：只测试飞书 GUI 归档步骤，不跑文件夹监视，也不跑 GPT 分类。

这些入口都会使用项目内 `.venv`。如果环境不存在，会自动创建并安装 `requirements.txt`。

## 首次使用

1. 确认 Windows 已安装 Python 3.9-3.12，推荐 3.10 或 3.11。
2. 双击 `test_windows.bat`。
3. 如果项目根目录没有 `.env`，脚本会从 `.env.windows.example` 创建。
4. 按提示填写主模型 API Key。服务器 SSH 密码会在单独弹出的隧道窗口中提示输入。
5. 测试通过后，双击 `run_cli.bat` 输入任务。

## 默认远端模型配置

Windows 端默认访问 `http://127.0.0.1:8000/v1`。这个地址由 SSH 隧道转发到服务器：

```text
ssh -p 10023 lcwt@111.0.130.56
remote vLLM: 127.0.0.1:8000
model: UI-TARS-1.5-7B
```

如果服务器 vLLM 没启动，隧道脚本会在远端尝试运行：

```bash
conda activate vllm
CUDA_VISIBLE_DEVICES=1,3 python -m vllm.entrypoints.openai.api_server \
  --model /mnt/data/Models/UI-TARS-1.5-7B \
  --tensor-parallel-size 2 \
  --served-model-name UI-TARS-1.5-7B \
  --trust-remote-code \
  --host 0.0.0.0 \
  --port 8000
```

## 常见调整

所有配置都在 `.env`：

- `AGENT_S_MODEL_API_KEY`：主模型 API Key。
- `AGENT_S_MODEL_URL`：主模型 OpenAI-compatible URL。
- `AGENT_S_MODEL`：主模型名称。
- `WATCH_DIR`：长期监视的新文件目录。
- `ARCHIVE_ENABLE_FEISHU_AGENT=1`：分类后启动 Agent-S3 操作飞书；设为 `0` 则只分类不归档。
- `FEISHU_ARCHIVE_DOC_TITLE`：Agent-S3 要打开的飞书云文档标题。
- `FEISHU_TEST_PROJECT` / `FEISHU_TEST_KIND`：`test_feishu_only.bat` 默认使用的测试项目名和类型。
- `AGENT_S_LOCAL_TUNNEL_PORT` / `AGENT_S_GROUND_URL`：默认使用本机 8000；如果被其他服务占用，启动器会临时切到 18000-18100 的空闲端口。
- `AGENT_S_FORCE_INSTALL=1`：强制重装 Python 依赖。
- `AGENT_S_USE_SSH_TUNNEL=0`：如果远端 8000 端口已经可直连，可关闭 SSH 隧道并直接配置 `AGENT_S_GROUND_URL`。
- `AGENT_S_REMOTE_PASSWORD`：非空时使用 Python/Paramiko 自动建立 SSH 隧道，不再弹窗要求手动输入服务器密码。隧道日志写入 `.windows/agent_s_paramiko_tunnel.out.log` 和 `.windows/agent_s_paramiko_tunnel.err.log`。
