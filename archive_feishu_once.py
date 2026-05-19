#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
单次真实运行：对指定本地文件做 GPT 分类，并按 .env 启动飞书 Agent-S3 归档（不启动长期监视）。

长期监视整个「文件自动归档」文件夹请用：run_archive_once.bat → file_archive_watcher.py

用法:
  python archive_feishu_once.py "C:\\path\\to\\文件.pdf"   # 直接指定文件
  python archive_feishu_once.py                             # 未指定时弹出「选择文件」对话框（需图形界面）
  双击 run_archive_once.bat — 同上，会弹出选文件窗口

只分类、不打开飞书 Agent:
  python archive_feishu_once.py "路径" --no-feishu

.env 需配置 OPENAI_*；飞书步骤需 ARCHIVE_ENABLE_FEISHU_AGENT=1，且 Grounding、飞书已就绪。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent


def _pick_file_dialog() -> Path | None:
    """无参数启动时让用户选择文件。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        path = filedialog.askopenfilename(title="选择要归档的文件")
        root.destroy()
        if not path:
            return None
        p = Path(path).expanduser().resolve()
        return p if p.is_file() else None
    except Exception as e:
        print("无法打开文件选择窗口:", e)
        print('请改用命令行传入路径，例如: python archive_feishu_once.py "D:\\文件.pdf"')
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="单次归档：分类 + 可选飞书 Agent（非监视模式）"
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="要处理的本地文件完整路径",
    )
    parser.add_argument(
        "--no-feishu",
        action="store_true",
        help="只执行 GPT 分类，不启动飞书 Agent",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="在监视目录生成一条示例文本再处理（一般请直接传真实 file）",
    )
    args = parser.parse_args()

    os.chdir(_REPO)
    sys.path.insert(0, str(_REPO))

    try:
        from dotenv import load_dotenv
    except ImportError:
        print("请安装: pip install python-dotenv")
        return 1
    load_dotenv(_REPO / ".env", override=False, encoding="utf-8-sig")

    if args.no_feishu:
        os.environ["ARCHIVE_ENABLE_FEISHU_AGENT"] = "0"

    from file_archive_watcher import (  # noqa: E402
        classify_file,
        get_watch_dir,
        run_feishu_agent_if_enabled,
        _setup_engine,
    )

    if args.demo:
        watch = get_watch_dir()
        watch.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = watch / f"sample_{ts}.txt"
        target.write_text(
            "浙江增值税电子普通发票\n"
            "购买方：示例公司\n"
            "项目：自动识别测试项目\n"
            "价税合计：128.00元\n",
            encoding="utf-8",
        )
        print("已生成示例文件:", target)
    elif args.file:
        target = Path(args.file).expanduser().resolve()
        if not target.is_file():
            print("错误：不是有效文件:", target)
            return 1
    else:
        print("未指定路径，请在下方的文件选择窗口中选择要归档的文件…")
        picked = _pick_file_dialog()
        if picked is None:
            print("未选择文件，已退出。")
            return 0
        target = picked

    print("=" * 60)
    print("处理文件:", target)
    print("=" * 60)

    eng = _setup_engine()
    out = classify_file(eng, target)
    print("\n【1/2】GPT 分类结果:\n", out, "\n")

    if args.no_feishu:
        print("已指定 --no-feishu，不启动飞书 Agent。")
        return 0

    feishu_on = os.getenv("ARCHIVE_ENABLE_FEISHU_AGENT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not feishu_on:
        print("【2/2】未启动飞书：请在 .env 中设置 ARCHIVE_ENABLE_FEISHU_AGENT=1")
        return 0

    print("【2/2】正在启动飞书 Agent-S3（将新开控制台）。请勿操作鼠标键盘直至结束。\n")

    def record(msg: str) -> None:
        print(msg, flush=True)

    run_feishu_agent_if_enabled(target, out or "", record)
    _t = os.getenv("FEISHU_ARCHIVE_DOC_TITLE", "文档归档处")
    print(f"\n单次归档流程已结束。请在飞书中核对云文档「{_t}」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
