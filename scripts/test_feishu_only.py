#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run only the Feishu GUI archive step, without watcher or GPT classification."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def make_demo_file() -> Path:
    from file_archive_watcher import get_watch_dir

    watch = get_watch_dir()
    watch.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = watch / f"feishu_only_test_{ts}.txt"
    path.write_text(
        "Feishu-only Agent-S3 test file\n"
        "This file is generated to test GUI upload/archive only.\n"
        "No GPT classification is performed by this test entry.\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test only the Feishu Agent-S3 archive operation."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional local file path to upload. If omitted, a demo text file is created in WATCH_DIR.",
    )
    parser.add_argument(
        "--project",
        default="",
        help=(
            "Project name to locate/create in column 1. Required in this Feishu-only "
            "mode because GPT classification is not run."
        ),
    )
    parser.add_argument(
        "--kind",
        default="",
        help="Archive type: 发票, 进货单, or 其他. Default: FEISHU_TEST_KIND or 发票.",
    )
    parser.add_argument(
        "--doc-title",
        default="",
        help="Feishu cloud doc title. Default: FEISHU_ARCHIVE_DOC_TITLE or 文档归档处.",
    )
    parser.add_argument(
        "--print-task-only",
        action="store_true",
        help="Print the generated Agent-S3 task text without running Agent-S3.",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")

    from file_archive_watcher import (  # noqa: E402
        _feishu_archive_task_text,
        _feishu_iteration_safety_cap,
        _normalize_feishu_doc_title,
        _run_feishu_subprocess,
    )

    if args.file:
        target = Path(os.path.expandvars(args.file)).expanduser().resolve()
        if not target.is_file():
            print("ERROR: file does not exist:", target)
            return 1
    else:
        target = make_demo_file()
        print("Created demo file:", target)

    project = args.project.strip()
    if not project:
        print("ERROR: --project is required in Feishu-only mode.")
        print("       Full archive modes use the project name recognized by GPT classification.")
        return 1
    kind = args.kind.strip() or env_first("FEISHU_TEST_KIND", default="发票")
    raw_doc_title = args.doc_title.strip() or env_first(
        "FEISHU_ARCHIVE_DOC_TITLE", default="文档归档处"
    )
    doc_title, doc_title_notes = _normalize_feishu_doc_title(raw_doc_title)

    print("=" * 60)
    print("Feishu-only Agent-S3 test")
    print("=" * 60)
    print("Doc title:", doc_title)
    for note in doc_title_notes:
        print("[config warning]", note)
    print("Project:", project)
    print("Kind:", kind)
    print("File:", target)
    print("=" * 60)

    task = _feishu_archive_task_text(doc_title, target, project, kind)
    if args.print_task_only:
        print(task)
        return 0

    timeout_s = int(env_first("AGENT_S_RUN_TIMEOUT", default="900") or "900")
    max_steps = _feishu_iteration_safety_cap(1)

    def record(message: str) -> None:
        print(message, flush=True)

    _run_feishu_subprocess(task, record, max_steps, timeout_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
