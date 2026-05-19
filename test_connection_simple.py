#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""简单连接测试：读取 .env，不再在脚本中硬编码 API Key。"""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                str(ROOT / "scripts" / "test_endpoints.py"),
                "--skip-ground",
            ],
            cwd=ROOT,
        )
    )
