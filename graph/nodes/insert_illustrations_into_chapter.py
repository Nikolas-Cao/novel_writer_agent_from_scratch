"""
阶段 7 节点：将图片 markdown 语法插入章节正文并写回存储。
"""
import logging
import re
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple

from state import NovelProjectState
from storage import ChapterStore

logger = logging.getLogger(__name__)


def _find_anchor_span(text: str, anchor: str) -> Optional[Tuple[int, int]]:
    """
    在正文中定位锚点起止下标。优先精确匹配；失败则尝试空白/换行与英文分词上的宽松匹配，
    避免 LLM 输出的 anchor 与正文略有出入时整图被追加到章节末尾。
    """
    if not anchor or not anchor.strip():
        return None
    anchor = anchor.strip()
    if anchor in text:
        i = text.index(anchor)
        return (i, i + len(anchor))

    collapsed = re.sub(r"\s+", " ", anchor)
    # 多「词」片段（含中英文空格分隔）：允许任意空白连接
    parts = collapsed.split()
    if len(parts) >= 2:
        pattern = r"\s+".join(re.escape(p) for p in parts)
        m = re.search(pattern, text, re.DOTALL)
        if m:
            return (m.start(), m.end())

    # 锚点含空格但正文为连续汉字（模型常在汉字间插空格）；避免对英文做去空白逐字匹配
    compact = re.sub(r"\s+", "", collapsed)
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", compact))
    if (
        len(compact) >= 8
        and compact != collapsed.strip()
        and cjk_chars >= 4
    ):
        pattern = r"\s*".join(re.escape(c) for c in compact)
        m = re.search(pattern, text)
        if m:
            return (m.start(), m.end())

    # 多行锚点：按行拼接，行间允许任意空白
    lines = [ln.strip() for ln in anchor.splitlines() if ln.strip()]
    if len(lines) >= 2:
        pattern = r"\s+".join(re.escape(ln) for ln in lines)
        m = re.search(pattern, text, re.DOTALL)
        if m:
            return (m.start(), m.end())

    # 长单句（多为中文连续文本）：允许字与字之间夹空白/换行
    if len(collapsed) >= 8 and len(parts) <= 1:
        pattern = r"\s*".join(re.escape(c) for c in collapsed)
        m = re.search(pattern, text)
        if m:
            return (m.start(), m.end())

    # 最长前缀命中（处理尾部标点/引号与模型多字）
    for L in range(min(len(collapsed), 400), 7, -1):
        prefix = collapsed[:L]
        if prefix in text:
            i = text.index(prefix)
            return (i, i + len(prefix))

    return None


def _illustration_img_html(alt: str, path: str, generation_prompt: str) -> str:
    """单行 HTML img：持久化生图提示词到 title，供前端 hover 展示。"""
    alt_e = html_escape(alt, quote=True)
    path_e = html_escape(path, quote=True)
    title_attr = ""
    if (generation_prompt or "").strip():
        title_attr = f' title="{html_escape(generation_prompt.strip(), quote=True)}"'
    return (
        f'<img class="chapter-illustration" alt="{alt_e}" src="{path_e}"{title_attr} />'
    )


def _insert_after_anchor(text: str, anchor: str, image_md: str) -> str:
    span = _find_anchor_span(text, anchor)
    if span is not None:
        end = span[1]
        return text[:end] + f"\n\n{image_md}\n" + text[end:]
    logger.warning(
        "[insert_illustrations] anchor not resolved; appending image at chapter end (preview=%r)",
        (anchor or "")[:100],
    )
    return text + f"\n\n{image_md}\n"


async def insert_illustrations_into_chapter_node(
    state: NovelProjectState,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    if not state.get("enable_chapter_illustrations", False):
        return {}

    store = chapter_store or ChapterStore()
    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    assets = list(state.get("illustration_assets", []))
    if not assets:
        single = state.get("illustration_asset") or {}
        if isinstance(single, dict) and single.get("image_path"):
            assets = [single]
    if not project_id:
        raise ValueError("project_id is required.")
    if not assets:
        return {}

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    inserted_paths: List[str] = []
    for a in assets:
        alt = str(a.get("alt") or "章节插图")
        path = str(a.get("image_path") or "")
        anchor = str(a.get("anchor_text") or "")
        if not path:
            continue
        prompt = str(
            a.get("generation_prompt")
            or a.get("image_query")
            or a.get("illustration_prompt")
            or ""
        ).strip()
        image_md = _illustration_img_html(alt, path, prompt)
        chapter_text = _insert_after_anchor(chapter_text, anchor, image_md)
        inserted_paths.append(path)

    ref = store.save(project_id, current_idx, chapter_text)

    chapters = list(state.get("chapters", []))
    for item in chapters:
        if int(item.get("index", -1)) == current_idx:
            item["path_or_content_ref"] = ref
            item["images_refs"] = inserted_paths
            item["word_count"] = len(chapter_text.replace("\n", ""))

    return {
        "current_chapter_final": chapter_text,
        "chapters": chapters,
    }
