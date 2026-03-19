"""
阶段 7 节点：识别章节中适合插图的位置与关键词。
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, get_message_text, invoke_and_parse_with_retry
from state import NovelProjectState
from storage import ChapterStore


def _fallback_points(markdown_text: str, limit: int = 2) -> List[Dict[str, str]]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", markdown_text) if p.strip()]
    points: List[Dict[str, str]] = []
    for p in parts:
        if p.startswith("#"):
            continue
        anchor = p[:80]
        query = (p[:24] or "小说场景") + " 插图"
        points.append(
            {
                "position_describe": "段落后插图",
                "anchor_text": anchor,
                "image_query": query,
                "alt": "章节插图",
            }
        )
        if len(points) >= limit:
            break
    return points


async def identify_illustration_points_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    store = chapter_store or ChapterStore()

    if not state.get("enable_chapter_illustrations", False):
        return {"illustration_points": []}

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not project_id:
        raise ValueError("project_id is required.")

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    prompt = (
        "你是小说插图策划。请从以下 Markdown 章节中找出适合插图的位置。\n"
        "仅输出 JSON：\n"
        '{"illustration_points":[{"position_describe":"位置说明","anchor_text":"用于定位插入的原文片段","image_query":"检索或生图关键词","alt":"图片替代文本"}]}\n'
        "最多给出 2 个插图点。\n\n"
        f"{chapter_text}"
    )
    points: List[Dict[str, str]] = []
    t0 = time.monotonic()
    logger.info("[identify_illustration_points] llm_invoke_begin project=%s chapter_index=%s", project_id, current_idx)
    try:
        obj = await invoke_and_parse_with_retry(
            planner, prompt, extract_json_object, max_retries=3
        )
        points = list(obj.get("illustration_points", []))
        logger.info(
            "[identify_illustration_points] llm_invoke_done project=%s points=%s elapsed_s=%.2f",
            project_id,
            len(points),
            time.monotonic() - t0,
        )
    except Exception:
        points = _fallback_points(chapter_text)
        logger.warning(
            "[identify_illustration_points] fallback project=%s elapsed_s=%.2f",
            project_id,
            time.monotonic() - t0,
        )

    if not points:
        points = _fallback_points(chapter_text, limit=1)

    normalized = []
    for p in points[:2]:
        normalized.append(
            {
                "position_describe": str(p.get("position_describe") or "段落后插图"),
                "anchor_text": str(p.get("anchor_text") or "")[:120],
                "image_query": str(p.get("image_query") or "小说场景插图"),
                "alt": str(p.get("alt") or "章节插图"),
            }
        )

    return {"illustration_points": normalized}
