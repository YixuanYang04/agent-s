#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
监视「文件自动归档」目录，在出现新文件时用 GPT 判断所属项目、以及是发票或进货单。
若开启飞书开关，分类完成后会启动一次 GUI Agent-S3，将文件归档到飞书云文档《文档归档处》对应列（第1列项目名，第2列发票，第3列进货单）。

环境变量（可在项目根 .env 中配置，需 python-dotenv 时: pip install python-dotenv）:
  WATCH_DIR, OPENAI_API_KEY, OPENAI_BASE_URL, ARCHIVE_LLM_MODEL, ARCHIVE_PROJECT_NAMES, ARCHIVE_LOG_FILE
  HTTPX_NO_PROXY, ARCHIVE_BOOTSTRAP_EXISTING
  ARCHIVE_ENABLE_FEISHU_AGENT=1  启用：分类后调 Agent-S3 飞书归档
  FEISHU_ARCHIVE_DOC_TITLE       云文档标题，默认「文档归档处」
  ARCHIVE_BATCH_SETTLE_SEC       批量模式：检测到新文件后，安静等待该秒数内不再增加/变化再整批处理，默认 8；设为 0 则每文件单独处理（旧行为）
  ARCHIVE_AGENT_MAX_STEPS        飞书 Agent 单任务最大步数，默认按批次数计算；也可手动设大数
  AGENT_S_* 见下方 run_feishu_agent_if_enabled
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False, encoding="utf-8-sig")
except ImportError:
    pass

from gui_agents.s3.core.engine import LMMEngineOpenAI
from openai import OpenAI
import httpx

DEFAULT_WATCH = r"C:\Users\GFFFT\Desktop\文件自动归档"
POLL_INTERVAL_SEC = 2.0
STABLE_ROUNDS = 3
STABLE_SLEEP_SEC = 0.4
STATE_NAME = ".file_archive_watcher_state.json"
VISION_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTS = {".txt", ".md", ".csv", ".json"}
TMP_SUFFIXES = (".tmp", ".crdownload", ".part")


def _gather_pending_candidates(watch: Path, state: dict, log_name: str) -> list:
    out: list = []
    for p in watch.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("~") or p.name == STATE_NAME:
            continue
        if p.name == log_name:
            continue
        if p.suffix.lower() in TMP_SUFFIXES:
            continue
        sig = _file_signature(p)
        if not sig:
            continue
        prev = state.get(p.name)
        if prev and prev.get("mtime_ns") == sig[0] and prev.get("size") == sig[1]:
            continue
        out.append(p)
    out.sort(key=lambda x: x.name.lower())
    return out


def _settle_pending_batch(
    watch: Path,
    state: dict,
    log_name: str,
    settle_sec: float,
    record: Callable[[str], None],
) -> list:
    """待处理集合在 settle_sec 秒内不变后，返回最终列表。"""
    if settle_sec <= 0:
        return _gather_pending_candidates(watch, state, log_name)

    last_key = None
    stable_since = None
    while True:
        cand = _gather_pending_candidates(watch, state, log_name)
        keyed = []
        for p in cand:
            s = _file_signature(p)
            if s:
                keyed.append((p.name, s[0], s[1]))
        key = tuple(sorted(keyed))
        now = time.time()
        if key != last_key:
            last_key = key
            stable_since = now
            record(
                f"[批量] 待处理 {len(cand)} 个文件，清单变化则重新计时；需安静约 {settle_sec:.0f}s 后继续…"
            )
        elif stable_since is not None and (now - stable_since) >= settle_sec:
            return cand
        time.sleep(1.0)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def get_watch_dir() -> Path:
    p = os.getenv("WATCH_DIR", DEFAULT_WATCH).strip()
    return Path(os.path.expandvars(p)).expanduser().resolve()


def _setup_engine() -> LMMEngineOpenAI:
    api_key = _env_first("OPENAI_API_KEY", "AGENT_S_MODEL_API_KEY")
    if not api_key:
        raise SystemExit("请设置环境变量 OPENAI_API_KEY 或 AGENT_S_MODEL_API_KEY")

    base_url = _env_first("OPENAI_BASE_URL", "AGENT_S_MODEL_URL") or None
    model = _env_first("ARCHIVE_LLM_MODEL", "AGENT_S_MODEL") or "gpt-5.4"

    engine = LMMEngineOpenAI(base_url=base_url, api_key=api_key, model=model)

    if _env_bool("HTTPX_NO_PROXY"):
        transport = httpx.HTTPTransport()
        client = httpx.Client(transport=transport)
        engine.llm_client = OpenAI(
            base_url=base_url or "https://api.openai.com/v1",
            api_key=api_key,
            http_client=client,
        )
    return engine


def _project_list_instruction() -> str:
    names = os.getenv("ARCHIVE_PROJECT_NAMES", "").strip()
    if not names:
        return "候选项目表未提供，请仅根据文件名称与内容归纳一个最合理的「项目」名称（可简短如代号）。"
    parts = [x.strip() for x in names.split(",") if x.strip()]
    return "请从以下候选项目中选出最匹配的一项作为「项目」；若均不匹配，可给出你认为更合适的项目名，并在理由中说明：\n" + "\n".join(
        f"- {p}" for p in parts
    )


def _read_state(watch: Path) -> dict:
    f = watch / STATE_NAME
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(watch: Path, state: dict) -> None:
    f = watch / STATE_NAME
    try:
        f.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError as e:
        logging.warning("无法写入状态文件: %s", e)


def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), st.st_size)
    except OSError:
        return None


def _wait_until_stable(path: Path) -> bool:
    last = _file_signature(path)
    if last is None:
        return False
    stable = 0
    while stable < STABLE_ROUNDS:
        time.sleep(STABLE_SLEEP_SEC)
        cur = _file_signature(path)
        if cur is None:
            return False
        if cur == last:
            stable += 1
        else:
            stable = 0
            last = cur
    return True


def _pdf_text_excerpt(path: Path, max_pages: int = 5) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        out = []
        for _, page in enumerate(reader.pages[:max_pages]):
            t = page.extract_text() or ""
            if t.strip():
                out.append(t)
        return "\n".join(out) if out else None
    except Exception:
        return None


def _read_text_limited(path: Path, max_chars: int = 12000) -> Optional[str]:
    try:
        raw = path.read_bytes()
        for enc in ("utf-8", "gbk", "gb2312", "utf-8-sig"):
            try:
                s = raw.decode(enc)
                return s[:max_chars]
            except UnicodeDecodeError:
                continue
    except OSError:
        return None
    return None


def _mime_and_b64_image(path: Path) -> Optional[Tuple[str, str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        sfx = path.suffix.lower()
        if sfx in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif sfx == ".png":
            mime = "image/png"
        elif sfx == ".webp":
            mime = "image/webp"
        elif sfx == ".gif":
            mime = "image/gif"
        else:
            mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return mime, b64


def _build_user_payload(path: Path) -> Tuple[str, list]:
    name = path.name
    ext = path.suffix.lower()

    if ext in TEXT_EXTS:
        t = _read_text_limited(path)
        if t:
            return f"文件名：{name}\n\n以下是从文件中读取的文本（可能截断）：\n\n{t}", []
        return f"文件名：{name}\n（无法以文本方式读取，请仅根据文件名判断。）", []

    if ext == ".pdf":
        excerpt = _pdf_text_excerpt(path)
        if excerpt:
            return f"文件名：{name}\n\n以下是从 PDF 提取的文本：\n\n{excerpt[:12000]}", []
        return (
            f"文件名：{name}\n（可 pip install pypdf 以提取 PDF 文字；否则请据文件名判断。）",
            [],
        )

    if ext in VISION_EXTS:
        img = _mime_and_b64_image(path)
        if not img:
            return f"文件名：{name}\n（无法读取图像数据。）", []
        mime, b64 = img
        parts = [
            {
                "type": "text",
                "text": f"请阅读下列图像（原文件：{name}），判断项目与类型。",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"},
            },
        ]
        return "", parts

    t = _read_text_limited(path)
    if t and len(t) > 50:
        return f"文件名：{name}\n\n文件开头文本：\n\n{t[:4000]}", []
    return (
        f"文件名：{name}\n\n无法可靠解析该扩展名，请据文件名与常识判断。",
        [],
    )


def _system_prompt() -> str:
    return f"""你是财务与项目归档助手。{_project_list_instruction()}

对文件判断并仅输出**一段合法 JSON 对象**（不要 Markdown 代码块、不要其他文字），字段如下：
"项目": string，
"类型": 必须是 "发票" 或 "进货单" 或 "其他" 三者之一，
"理由": string（简短说明依据）。

若无法从内容判断项目，"项目" 可为 "未确定"。"""


def classify_file(engine: LMMEngineOpenAI, file_path: Path) -> str:
    sys_prompt = _system_prompt()
    text_part, extra_parts = _build_user_payload(file_path)
    has_vision = bool(
        extra_parts
        and any(
            p.get("type") == "image_url" for p in extra_parts if isinstance(p, dict)
        )
    )
    if has_vision:
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": extra_parts},
        ]
    else:
        user_text = f"请根据下列信息分析并只输出要求格式的 JSON：\n\n{text_part}"
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ]
    return engine.generate(
        messages=messages,
        temperature=0.1,
        max_new_tokens=1024,
    )


def _parse_classification_json(text: str) -> Optional[Dict[str, Any]]:
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(t[a : b + 1])
            except json.JSONDecodeError:
                pass
    return None


def _env_first(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


_ENV_ASSIGNMENT_IN_VALUE_RE = re.compile(r"[A-Z_][A-Z0-9_]{2,}\s*=")
_MOJIBAKE_MARKERS = ("鏂", "褰", "澶", "�", "æ", "å", "Ã")


def _text_quality_score(value: str) -> int:
    cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
    marker_hits = sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)
    bad_chars = value.count("?") + value.count("\ufffd")
    return cjk * 3 - marker_hits * 8 - bad_chars * 12


def _repair_mojibake(value: str) -> str:
    candidates = [value]
    for encoding in ("gbk", "cp936", "latin1"):
        try:
            repaired = value.encode(encoding, errors="replace").decode(
                "utf-8", errors="replace"
            )
        except (LookupError, UnicodeError):
            continue
        if repaired and repaired != value:
            candidates.append(repaired)
    return max(candidates, key=_text_quality_score)


def _normalize_feishu_doc_title(raw: str) -> Tuple[str, List[str]]:
    """Keep malformed .env values from becoming Feishu search text."""
    default = "文档归档处"
    value = (raw or "").strip()
    notes: List[str] = []
    if not value:
        return default, notes

    assignment = _ENV_ASSIGNMENT_IN_VALUE_RE.search(value)
    if assignment and assignment.start() > 0:
        stripped = value[: assignment.start()].rstrip(" \t\r\n;；,，?？")
        notes.append(
            f"FEISHU_ARCHIVE_DOC_TITLE 含有疑似拼接的其他配置，已只保留标题部分：{stripped!r}"
        )
        value = stripped

    repaired = _repair_mojibake(value)
    if repaired != value:
        notes.append(
            f"FEISHU_ARCHIVE_DOC_TITLE 疑似编码乱码，已尝试修正：{value!r} -> {repaired!r}"
        )
        value = repaired

    if value.startswith("文档归档") and ("?" in value or "\ufffd" in value):
        notes.append("FEISHU_ARCHIVE_DOC_TITLE 末尾含乱码占位符，已恢复为默认标题「文档归档处」。")
        value = default

    if any(marker in value for marker in _MOJIBAKE_MARKERS) or "\ufffd" in value:
        notes.append("FEISHU_ARCHIVE_DOC_TITLE 仍像乱码，已回退为默认标题「文档归档处」。")
        value = default

    return value.strip() or default, notes


def _feishu_doc_title_from_env(record: Optional[Callable[[str], None]] = None) -> str:
    raw = os.getenv("FEISHU_ARCHIVE_DOC_TITLE", "文档归档处")
    title, notes = _normalize_feishu_doc_title(raw)
    if record:
        for note in notes:
            record(f"飞书 Agent 配置提示：{note}")
    return title


def _extract_classified_project_kind(
    data: Dict[str, Any],
    record: Optional[Callable[[str], None]] = None,
    label: str = "本文件",
) -> Optional[Tuple[str, str]]:
    project = str(data.get("项目", data.get("project", ""))).strip()
    if project in {"", "未确定", "未知", "无法确定", "不确定", "未识别", "无法识别"}:
        if record:
            record(
                f"飞书 Agent：{label} 未识别到有效项目名，跳过飞书归档，避免写入错误项目行。"
            )
        return None
    kind = str(data.get("类型", data.get("type", ""))).strip() or "其他"
    return project, kind


def _feishu_open_step(step_name: str) -> str:
    return (
        f"{step_name}：打开飞书必须通过代码动作完成：下一条动作应调用 "
        '`agent.open("飞书")`（或等价的系统启动代码）。禁止双击桌面图标，'
        "禁止用鼠标在桌面上寻找/打开飞书快捷方式，禁止点击任务栏图标来切换飞书。"
        "若飞书已经打开，则仍应通过 `agent.open(\"飞书\")` 的代码动作激活窗口；"
        "如果只出现开始菜单/搜索面板而未进入飞书，先按 Esc 关闭面板，再重新调用代码动作打开或激活飞书。"
    )


def _feishu_doc_search_step(step_name: str, doc_title: str) -> str:
    return f"""{step_name}：用键盘快捷键打开搜索框并输入文档标题，等待搜索结果出现。必须严格按下面的流程操作：
1. 必须直接使用快捷键 Ctrl+J 打开云文档搜索框；下一条动作应调用 `agent.hotkey(["ctrl", "j"])`（或等价的 `pyautogui.hotkey("ctrl", "j")`）。
2. 禁止使用鼠标点击任何搜索/放大镜入口来打开搜索框，禁止再根据「主页」位置判断搜索框，禁止点击屏幕最外侧细窄全局边栏里的搜索/放大镜。
3. Ctrl+J 打开搜索框后，搜索输入框应已自动聚焦；下一条输入动作必须调用 `agent.type(text="{doc_title}", overwrite=True)`，注意：不要传 enter=True，不要传入元素描述，不要再点击输入框，不要再调用视觉模型定位搜索框。
4. 该动作必须形成这样的执行顺序：`pyperclip.copy("{doc_title}")` → `pyautogui.hotkey('ctrl', 'v')`。输入完成后不按 Enter，等待搜索结果自动出现。
5. 即使右侧主页、最近、推荐、文档列表中已经能看到「{doc_title}」，也禁止直接点击该可见文档；本步骤必须通过 Ctrl+J 搜索。
6. 本步骤完成后，搜索面板中应出现与「{doc_title}」匹配的搜索结果。下一步将点击搜索结果打开文档。"""


def _feishu_doc_open_search_result_step(step_name: str, doc_title: str) -> str:
    return f"""{step_name}：在搜索结果中点击目标文档以打开它。
1. 上一步已在 Ctrl+J 搜索框中输入了「{doc_title}」，搜索结果应已出现在搜索面板中。
2. 在搜索结果列表中，找到标题包含「{doc_title}」的文档条目，用鼠标单击该条目以打开文档。下一条动作应调用 `agent.click("搜索结果中标题为{doc_title}的文档条目", 1, "left")`。
3. 点击后等待文档加载；如果页面空白、正在加载或尚未出现表格，使用 `agent.wait(3)` 等待。
4. 若搜索面板中没有出现匹配「{doc_title}」的结果，输出 fail 并说明"搜索结果中未找到目标文档"。"""


def _feishu_table_schema_note() -> str:
    return (
        "表格判定规则：只要求前 3 列依次为「项目名称 / 发票 / 进货单」。"
        "右侧允许存在「其他」、备注等额外列，这属于正常情况，不得因此判定文档或表格结构错误。"
        "处理「发票」和「进货单」时只使用第 2、3 列。"
    )


def _feishu_clear_overlay_instruction() -> str:
    return (
        "确认上传成功后，必须按一次 Esc，用于收起附件弹窗/选择浮层并恢复无遮挡的表格视图；"
        "不要点击空白处、其他项目行、其他单元格或功能按钮来恢复界面。"
    )


def _feishu_archive_task_text(
    doc_title: str,
    file_path: Path,
    project: str,
    kind: str,
) -> str:
    doc_title, _ = _normalize_feishu_doc_title(doc_title)
    abs_path = str(file_path.resolve())

    # 与用户约定严格一致的类型→列映射说明（供步骤 5）
    row_col_rule = (
        "若为「发票」：目标是该行与第 2 列（发票）交叉的单元格；"
        "若为「进货单」或等价表述：目标是该行与第 3 列（进货单）交叉的单元格；"
        "若为「其他」且表格有「其他」列：目标是该行与「其他」列交叉的单元格；"
        "若没有「其他」列，再在第 2 列与第 3 列中择一最合适的单元格。"
    )

    return f"""【飞书云文档归档任务——单文件】你必须严格按下列「步骤 1～8」的内容与顺序执行，不得调换或跳步。
{_feishu_table_schema_note()}

本次分类结果：
- FEISHU_ARCHIVE_DOC_TITLE（云文档标题）=「{doc_title}」
- 已识别项目名称 =「{project}」
- 已识别类型 =「{kind}」
- 本地文件绝对路径 =「{abs_path}」（步骤 7 必须选用此路径的文件）

────────────────
{_feishu_open_step("步骤 1")}

步骤 2：观察飞书界面左侧导航栏，如果「云文档」已经高亮选中，则跳过本步骤直接进入步骤 3；否则点击左侧导航中的「云文档」。

{_feishu_doc_search_step("步骤 3", doc_title)}

{_feishu_doc_open_search_result_step("步骤 4", doc_title)}

步骤 5：确认当前截图中能看到目标表格视图，且前 3 列依次为「项目名称 / 发票 / 进货单」。本轮先假设单页包含全部信息：只在当前可见表格中查找项目「{project}」，不要滚动、不要 Ctrl+F、不要新建行。匹配项目名时使用宽松匹配：忽略首尾空格、标点符号、全角半角差异，只要核心汉字序列相同即视为匹配成功。重要：不要对项目名进行过度比较——如果你在表格中看到的项目名称读起来和目标名称一样，那就是同一个项目，直接匹配，不得以"措辞不同""顺序不同""字形差异"等理由拒绝匹配。如果当前截图中确实找不到任何与该项目名核心文字匹配的行，直接输出 fail 并说明“当前单页未看到项目行”。

步骤 6：按类型「{kind}」确定目标单元格：{row_col_rule}

步骤 7：根据步骤 5 和步骤 6 确定的目标单元格，用自然语言描述该单元格的位置，格式必须是 `[{{"cell_description": "表格中项目名称为{project}的行与{kind}列交叉的数据单元格", "file_path": r"{abs_path}"}}]`。cell_description 必须是一句完整的、精确描述目标单元格位置的中文语句，系统会通过视觉定位模型自动精确定位该单元格的中心坐标，不需要你估算 x/y 数值。下一条动作必须调用 `agent.paste_images_to_cells([...])`，由代码对每张图片严格逐项执行“先单击 click 选中目标单元格 → 再 Ctrl+V 粘贴图片”。禁止直接在未选中单元格时粘贴，禁止调用 `agent.upload_file_via_dialog`，禁止点击「添加本地文件」，禁止打开 Windows 文件选择框，禁止逐步视觉定位上传弹窗。

步骤 8：批量粘贴动作完成后，等待云文档自动保存；成功后输出 done。若粘贴动作报错或图片未能写入，输出 fail 并说明原因。

────────────────
结束约定：若以上步骤 1～8 全部成功完成，你的下一条动作代码中须体现任务结束并输出 done；若任一步无法完成，输出 fail 并简要说明卡在哪一步及原因。"""


def _feishu_batch_task_text(doc_title: str, items: List[Tuple[Path, str, str]]) -> str:
    doc_title, _ = _normalize_feishu_doc_title(doc_title)
    n = len(items)
    parts = []
    for i, (fp, project, kind) in enumerate(items, 1):
        ap = str(fp.resolve())
        parts.append(
            f"""
—— 文件 {i}/{n} ——
• 分类结果：项目「{project}」；类型「{kind}」
• 本地绝对路径：{ap}"""
        )
    body = "\n".join(parts)

    return f"""【飞书云文档归档任务——多文件批量】你必须严格按下述「步骤 A → B → C」与用户约定执行。
云文档标题 doc_title（即环境 FEISHU_ARCHIVE_DOC_TITLE）=「{doc_title}」。共需处理 {n} 个本地文件。
{_feishu_table_schema_note()}

────────────────
步骤 A（只做一次：打开文档后保持该文档前台打开，不要关闭后再从列表重进）：
{_feishu_open_step("步骤 A1")}
步骤 A2：观察飞书界面左侧导航栏，如果「云文档」已经高亮选中，则跳过本步骤直接进入步骤 A3；否则点击左侧导航中的「云文档」。
{_feishu_doc_search_step("步骤 A3", doc_title)}
{_feishu_doc_open_search_result_step("步骤 A4", doc_title)}
步骤 A5：确认当前界面中能看到目标表格视图，且前 3 列依次为「项目名称 / 发票 / 进货单」；右侧多出「其他」等额外列是正常情况，不要因此重新找文档或反思为结构错误。

────────────────
步骤 B（只基于当前这一张表格截图，一次性规划全部粘贴位置）：
{body}

本轮先假设单页包含全部信息：只在当前可见表格中查找上述项目行，不要滚动、不要 Ctrl+F、不要新建行。匹配项目名时使用宽松匹配：忽略首尾空格、标点符号、全角半角差异，只要核心汉字序列相同即视为匹配成功。重要：不要对项目名进行过度比较——如果你在表格中看到的项目名称读起来和目标名称一样，那就是同一个项目，直接匹配，不得以"措辞不同""顺序不同""字形差异"等理由拒绝匹配。若任一项目行在当前截图中确实不可见，直接输出 fail 并说明“当前单页未看到第几个文件的项目行”。

列映射规则：发票→第 2 列「发票」；进货单→第 3 列「进货单」；其他→优先使用「其他」列，若当前表格没有「其他」列再在第 2 或第 3 列中择一。

你必须根据上述每个文件的项目名和类型，用自然语言描述每个目标单元格的位置，并构造下面格式的 Python 列表，顺序保持为文件 1 → 文件 2 → … → 文件 {n}：
[
  {{"cell_description": "表格中项目名称为XXX的行与YYY列交叉的数据单元格", "file_path": r"对应文件绝对路径"}},
  ...
]

cell_description 必须是一句完整的、精确描述目标单元格位置的中文语句，系统会通过视觉定位模型自动精确定位该单元格的中心坐标，不需要你估算 x/y 数值。

下一条动作必须只调用一次，并且要把上面的列表字面量直接传入调用，例如：
`agent.paste_images_to_cells([{{"cell_description": "表格中项目名称为华东仓储系统升级项目的行与发票列交叉的数据单元格", "file_path": r"C:\\Users\\...\\1.png"}}, ...])`
不要只写未定义变量 `placements`。

该动作会由代码对每张图片严格逐项执行“先单击 click 选中目标单元格 → 再 Ctrl+V 粘贴图片”。禁止直接在未选中单元格时粘贴，禁止调用 `agent.upload_file_via_dialog`，禁止点击「添加本地文件」，禁止打开 Windows 文件选择框，禁止对每张图片重复截图理解或逐个走上传弹窗。

────────────────
步骤 C：批量粘贴动作完成后，等待云文档自动保存。成功后你的下一条动作须体现输出 done；
若批量粘贴动作报错或任一文件无法写入：输出 fail，并写明是「第几个文件」卡住及原因摘要。
"""


def _feishu_iteration_safety_cap(n_items: int) -> int:
    """仅作死循环防护；正常在 agent 输出 done/fail 时即结束，不依赖本数值。"""
    manual = os.getenv("ARCHIVE_AGENT_MAX_STEPS", "").strip()
    if manual.isdigit() and int(manual) > 0:
        return int(manual)
    return min(30000, max(5000, 2000 + n_items * 900))


def _archive_agent_log_root() -> Path:
    raw = os.getenv("ARCHIVE_AGENT_LOG_DIR", "").strip()
    if raw:
        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = _REPO_ROOT / path
        return path
    return _REPO_ROOT / "agent_execution_logs"


def _new_agent_run_log_dir() -> Path:
    root = _archive_agent_log_root()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i in range(100):
        suffix = f"_{i:02d}" if i else ""
        run_dir = root / f"feishu_agent_{stamp}_{os.getpid()}{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    run_dir = root / f"feishu_agent_{stamp}_{os.getpid()}_{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _redact_agent_cmd(cmd: List[str]) -> List[str]:
    redacted = list(cmd)
    secret_flags = {"--model_api_key", "--ground_api_key"}
    for i, part in enumerate(redacted[:-1]):
        if part in secret_flags:
            redacted[i + 1] = "***"
    return redacted


def _run_process_tee(
    cmd: List[str],
    timeout_s: int,
    log_fp: Any,
    popen_kwargs: Dict[str, Any],
) -> int:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **popen_kwargs,
    )
    output_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + max(1, int(timeout_s))

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout_s)
        try:
            line = output_queue.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue
        if line is None:
            break
        log_fp.write(line)
        log_fp.flush()
        print(line, end="", flush=True)

    return proc.wait()


def _run_feishu_subprocess(
    task: str,
    record: Callable[[str], None],
    max_agent_steps: int,
    timeout_s: int,
) -> None:
    run_log_dir = _new_agent_run_log_dir()
    tpath = run_log_dir / "task.txt"
    console_log = run_log_dir / "console.log"
    agent_log_dir = run_log_dir / "agent_s3_logs"
    agent_log_dir.mkdir(parents=True, exist_ok=True)
    try:
        tpath.write_text(task, encoding="utf-8")
        cmd = _build_agent_s3_cmd(tpath, max_agent_steps)
        (run_log_dir / "command.txt").write_text(
            " ".join(str(x) for x in _redact_agent_cmd(cmd)), encoding="utf-8"
        )
        record(
            f"飞书 Agent：启动 Agent-S3（正常在输出 done/fail 时结束；安全硬上限 {max_agent_steps} 步，超时 {timeout_s} 秒）…"
        )
        record(f"飞书 Agent：执行日志目录 {run_log_dir}")
        record(f"飞书 Agent：内部日志目录 {agent_log_dir}")
        popen_kwargs: Dict[str, Any] = {"cwd": str(_REPO_ROOT)}
        env = os.environ.copy()
        env["AGENT_S_LOG_DIR"] = str(agent_log_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        popen_kwargs["env"] = env
        with console_log.open("w", encoding="utf-8", errors="replace") as log_fp:
            returncode = _run_process_tee(
                cmd,
                timeout_s=timeout_s,
                log_fp=log_fp,
                popen_kwargs=popen_kwargs,
            )
        record(f"飞书 Agent：Agent-S3 已结束，退出码 {returncode}。")
        record(f"飞书 Agent：控制台日志 {console_log}")
    except subprocess.TimeoutExpired:
        record("飞书 Agent：执行超时，已终止子进程。")
        record(f"飞书 Agent：超时前日志保存在 {run_log_dir}")
    except ValueError as e:
        record(f"飞书 Agent：{e} 跳过。")
    except OSError as e:
        record(f"飞书 Agent：无法启动: {e}")


def _build_agent_s3_cmd(task_file: Path, max_agent_steps: int) -> list:
    model = _env_first("AGENT_S_MODEL", "ARCHIVE_LLM_MODEL") or "gpt-5.4"
    murl = _env_first("AGENT_S_MODEL_URL", "OPENAI_BASE_URL")
    mkey = _env_first("AGENT_S_MODEL_API_KEY", "OPENAI_API_KEY")
    if not mkey:
        raise ValueError("需要 AGENT_S_MODEL_API_KEY 或 OPENAI_API_KEY")
    gprov = os.getenv("AGENT_S_GROUND_PROVIDER", "huggingface").strip() or "huggingface"
    gurl = _env_first("AGENT_S_GROUND_URL", "HF_ENDPOINT_URL")
    if not gurl:
        gurl = "http://127.0.0.1:8000/v1"
    gkey = _env_first("AGENT_S_GROUND_API_KEY", "HF_TOKEN")
    if not gkey:
        gkey = "dummy-key"
    gmodel = _env_first("AGENT_S_GROUND_MODEL", "GROUND_MODEL")
    if not gmodel:
        gmodel = "UI-TARS-1.5-7B"
    gw = os.getenv("AGENT_S_GROUNDING_WIDTH", "1920").strip() or "1920"
    gh = os.getenv("AGENT_S_GROUNDING_HEIGHT", "1080").strip() or "1080"
    prov = os.getenv("AGENT_S_PROVIDER", "openai").strip() or "openai"

    return [
        sys.executable,
        "-m",
        "gui_agents.s3.cli_app",
        "--provider",
        prov,
        "--model",
        model,
        "--model_url",
        murl,
        "--model_api_key",
        mkey,
        "--ground_provider",
        gprov,
        "--ground_url",
        gurl,
        "--ground_api_key",
        gkey,
        "--ground_model",
        gmodel,
        "--grounding_width",
        gw,
        "--grounding_height",
        gh,
        "--max-agent-steps",
        str(max(1, int(max_agent_steps))),
        "--task-file",
        str(task_file.resolve()),
    ]


def run_feishu_agent_if_enabled(
    file_path: Path,
    model_output: str,
    record: Callable[[str], None],
) -> None:
    on = os.getenv("ARCHIVE_ENABLE_FEISHU_AGENT", "").strip().lower()
    if on not in ("1", "true", "yes", "on"):
        return
    data = _parse_classification_json(model_output)
    if not data:
        record("飞书 Agent：未从模型输出解析到 JSON，跳过自动归档。")
        return
    classified = _extract_classified_project_kind(data, record)
    if not classified:
        return
    project, kind = classified
    doc_title = _feishu_doc_title_from_env(record)
    task = _feishu_archive_task_text(doc_title, file_path, project, kind)
    base_to = int(os.getenv("AGENT_S_RUN_TIMEOUT", "900") or "900")
    max_steps = _feishu_iteration_safety_cap(1)
    _run_feishu_subprocess(task, record, max_steps, base_to)


def run_feishu_batch_if_enabled(
    parsed_items: List[Tuple[Path, str, str]],
    record: Callable[[str], None],
) -> None:
    """parsed_items: (path, project, kind) 仅包含分类 JSON 解析成功的条目。"""
    if not parsed_items:
        return
    on = os.getenv("ARCHIVE_ENABLE_FEISHU_AGENT", "").strip().lower()
    if on not in ("1", "true", "yes", "on"):
        return
    doc_title = _feishu_doc_title_from_env(record)
    task = _feishu_batch_task_text(doc_title, parsed_items)
    n = len(parsed_items)
    base_to = int(os.getenv("AGENT_S_RUN_TIMEOUT", "900") or "900")
    timeout_s = max(base_to, 90 * n)
    steps = _feishu_iteration_safety_cap(n)
    record(f"飞书 Agent（批量 {n} 个文件）：一次打开文档后依次上传…")
    _run_feishu_subprocess(task, record, steps, timeout_s)


def run_forever() -> None:
    watch = get_watch_dir()
    watch.mkdir(parents=True, exist_ok=True)

    log_path = os.getenv("ARCHIVE_LOG_FILE", "").strip()
    if not log_path:
        log_path = str(watch / "classification_log.txt")
    else:
        log_path = str(Path(log_path).expanduser())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    engine = _setup_engine()
    state_path = watch / STATE_NAME
    bootstrap = os.getenv("ARCHIVE_BOOTSTRAP_EXISTING", "1").strip() != "0"
    if not state_path.is_file():
        state = {}
        if bootstrap:
            for p in watch.iterdir():
                if not p.is_file():
                    continue
                if p.name.startswith("~") or p.name == STATE_NAME:
                    continue
                if p.suffix.lower() in (".tmp", ".crdownload", ".part"):
                    continue
                st = _file_signature(p)
                if st:
                    state[p.name] = {"mtime_ns": st[0], "size": st[1]}
            _write_state(watch, state)
            logging.info("首次运行：已有文件已记入状态，仅后续新文件会分析。")
        else:
            logging.info("首次运行且 ARCHIVE_BOOTSTRAP_EXISTING=0：将分析目录内已有文件。")
    else:
        state = _read_state(watch)

    logging.info("监视目录: %s", watch)
    logging.info("轮询: %s 秒", POLL_INTERVAL_SEC)
    logging.info("日志: %s", log_path)
    bs = os.getenv("ARCHIVE_BATCH_SETTLE_SEC", "8")
    logging.info("批量安静等待 ARCHIVE_BATCH_SETTLE_SEC: %s 秒（0=不合并批次）", bs)
    print("已启动。新文件将自动分析（支持批量）。Ctrl+C 结束。")

    def record(line: str) -> None:
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logging.warning("写日志失败: %s", e)

    while True:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            log_bn = Path(log_path).name
            cand0 = _gather_pending_candidates(watch, state, log_bn)
            if not cand0:
                continue

            settle_sec = float(os.getenv("ARCHIVE_BATCH_SETTLE_SEC", "8") or "8")
            if settle_sec > 0:
                record("\n[批量] 检测到新文件，等待是否还有文件继续加入…")
            batch_paths = _settle_pending_batch(
                watch, state, log_bn, settle_sec, record
            )

            stable_batch: List[Path] = []
            for p in batch_paths:
                record(f"[批量] 等待写入稳定: {p.name}")
                if _wait_until_stable(p):
                    stable_batch.append(p)
                else:
                    record(f"[批量] 跳过(仍不稳定): {p}")

            if not stable_batch:
                continue

            outs: Dict[str, str] = {}
            parsed_feishu: List[Tuple[Path, str, str]] = []

            for p in stable_batch:
                try:
                    record(f"\n---- 分类: {p} ----")
                    out = classify_file(engine, p)
                    outs[p.name] = out or ""
                    record("模型输出:\n" + (outs[p.name]).strip())
                    data = _parse_classification_json(outs[p.name])
                    if data:
                        classified = _extract_classified_project_kind(
                            data, record, label=p.name
                        )
                        if classified:
                            pr, kd = classified
                            parsed_feishu.append((p, pr, kd))
                    else:
                        record("本文件未能解析 JSON，飞书本轮跳过该项。")
                except Exception as e:
                    record(f"分析失败: {type(e).__name__}: {e}")
                finally:
                    st = _file_signature(p)
                    if st:
                        state[p.name] = {"mtime_ns": st[0], "size": st[1]}
                        _write_state(watch, state)

            if not parsed_feishu:
                continue

            if len(parsed_feishu) == 1:
                fp, _, _ = parsed_feishu[0]
                run_feishu_agent_if_enabled(fp, outs[fp.name], record)
            else:
                run_feishu_batch_if_enabled(parsed_feishu, record)

        except Exception as e:
            logging.exception("主循环: %s", e)
            time.sleep(5.0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        raise SystemExit(0)
    run_forever()
