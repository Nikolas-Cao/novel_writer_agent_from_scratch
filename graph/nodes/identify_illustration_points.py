"""
识别章节中最适合插入的一张插图位置，并生成文生图用的详细描述。
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from state import NovelProjectState
from storage import ChapterStore


def _flatten_outline_chapters(outline_structure: Any) -> List[Dict[str, Any]]:
    if not outline_structure or not isinstance(outline_structure, dict):
        return []
    out: List[Dict[str, Any]] = []
    for vol in outline_structure.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        for ch in vol.get("chapters") or []:
            if isinstance(ch, dict):
                out.append(ch)
    return out


def _current_chapter_outline_context(state: NovelProjectState) -> str:
    chs = _flatten_outline_chapters(state.get("outline_structure"))
    idx = int(state.get("current_chapter_index", 0) or 0)
    if not (0 <= idx < len(chs)):
        return ""
    ch = chs[idx]
    title = str(ch.get("title") or "").strip()
    points = ch.get("points") or []
    conflict = ch.get("conflict")
    lines: List[str] = []
    if title:
        lines.append(f"本章大纲标题：{title}")
    for p in points:
        lines.append(f"- {p}")
    if conflict:
        lines.append(f"冲突/伏笔：{conflict}")
    return "\n".join(lines).strip()


def _snap_anchor_to_chapter(chapter_text: str, anchor: str) -> str:
    """若模型多写或少写尾部字符，截成能在正文中唯一或首次命中的前缀，便于插入节点定位。"""
    if not anchor:
        return anchor
    a = anchor.strip()
    if a in chapter_text:
        return a
    collapsed = re.sub(r"\s+", " ", a)
    if collapsed in chapter_text:
        return collapsed
    for L in range(min(len(a), 400), 7, -1):
        prefix = a[:L]
        if chapter_text.count(prefix) == 1:
            return prefix
    for L in range(min(len(a), 400), 7, -1):
        prefix = a[:L]
        if prefix in chapter_text:
            return prefix
    return a


def _paragraph_candidates(markdown_text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", markdown_text) if p.strip()]
    return [p for p in parts if not p.startswith("#")]


def _fallback_single_point(markdown_text: str) -> Tuple[Dict[str, str], str]:
    parts = _paragraph_candidates(markdown_text)
    if not parts:
        anchor = (markdown_text[:120] or "正文开头")[:120]
        point = {
            "position_describe": "章节末尾",
            "anchor_text": anchor,
            "alt": "章节插图",
        }
        prompt = "A literary novel illustration, evocative scene, soft lighting, no text in image."
        return point, prompt
    # 取中后段优先，避免插图总落在开篇；长章时更接近情节推进处
    n = len(parts)
    mid = n // 2
    if n >= 4:
        mid = min(max(n // 3, 1), n - 2)
    p = parts[mid]
    anchor = p[:120]
    prompt = (
        "A cinematic novel illustration, atmospheric scene: "
        + (p[:280].replace("\n", " ") or "literary fiction moment")
        + ". Detailed, coherent composition, soft dramatic lighting, no text in image."
    )
    point = {
        "position_describe": "段落后插图（中段情节）",
        "anchor_text": anchor[:120],
        "alt": "章节插图",
    }
    return point, prompt


async def identify_illustration_points_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    store = chapter_store or ChapterStore()

    if not state.get("enable_chapter_illustrations", False):
        return {
            "illustration_point": {},
            "illustration_prompt": "",
            "illustration_points": [],
        }

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not project_id:
        raise ValueError("project_id is required.")

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    outline_ctx = _current_chapter_outline_context(state)
    outline_block = (
        f"\n\n【本章大纲要点（仅供选题，勿写入 anchor）】\n{outline_ctx}\n"
        if outline_ctx
        else ""
    )
    prompt = (
        "你是小说插图策划。请从以下 Markdown 章节中选出**一张**插图插入点。\n"
        "选题原则（重要）：\n"
        "1）优先选择**情节高潮、转折、冲突爆发、关键动作或强情绪**的段落，使画面与故事强相关；"
        "不要仅因排版方便就选全文最后一段或结尾总结句。\n"
        "2）`illustration_prompt` 必须具体描绘**锚点所在场景**里肉眼可见的画面（人物动作、环境、道具、光线），"
        "与 `anchor_text` 描写的是同一件事；避免泛泛的「氛围图」或与正文无关的意象。\n"
        "3）`anchor_text` 必须从章节正文**原样复制**一段连续文字（含标点与换行），长度建议约 20～90 字，"
        "且在整章中**只出现一次**，便于程序定位；不要改写、不要摘要、不要拼错字。\n"
        "仅输出 JSON：\n"
        '{"illustration_point":{"position_describe":"在文中何处插入（一句话）",'
        '"anchor_text":"从正文复制的定位片段",'
        '"alt":"图片替代文本（简短）"},'
        '"illustration_prompt":"详细画面描述：主体、环境、构图、光线、情绪、风格；避免版权角色名；不要出现文字水印"}'
        f"{outline_block}\n\n章节正文：\n{chapter_text}"
    )

    point: Dict[str, str] = {}
    ill_prompt = ""
    t0 = time.monotonic()
    logger.info(
        "[identify_illustration_points] llm_invoke_begin project=%s chapter_index=%s",
        project_id,
        current_idx,
    )
    try:
        obj = await invoke_and_parse_with_retry(
            planner, prompt, extract_json_object, max_retries=3
        )
        raw_pt = obj.get("illustration_point") or {}
        if isinstance(raw_pt, dict):
            point = {
                "position_describe": str(raw_pt.get("position_describe") or "段落后插图"),
                "anchor_text": str(raw_pt.get("anchor_text") or "")[:200],
                "alt": str(raw_pt.get("alt") or "章节插图"),
            }
        if point.get("anchor_text"):
            point["anchor_text"] = _snap_anchor_to_chapter(chapter_text, point["anchor_text"])
        ill_prompt = str(obj.get("illustration_prompt") or "").strip()
        logger.info(
            "[identify_illustration_points] llm_invoke_done project=%s elapsed_s=%.2f",
            project_id,
            time.monotonic() - t0,
        )
    except Exception:
        point, ill_prompt = _fallback_single_point(chapter_text)
        logger.warning(
            "[identify_illustration_points] fallback project=%s elapsed_s=%.2f",
            project_id,
            time.monotonic() - t0,
        )

    if not point or not ill_prompt:
        point, ill_prompt = _fallback_single_point(chapter_text)

    # 兼容旧节点：单元素列表，image_query 供旧代码路径使用
    legacy: List[Dict[str, str]] = [
        {
            "position_describe": point.get("position_describe", "段落后插图"),
            "anchor_text": point.get("anchor_text", ""),
            "image_query": ill_prompt,
            "alt": point.get("alt", "章节插图"),
        }
    ]

    return {
        "illustration_point": point,
        "illustration_prompt": ill_prompt,
        "illustration_points": legacy,
    }
