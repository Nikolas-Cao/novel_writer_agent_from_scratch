from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from graph.llm import create_planner_llm
import graph.nodes.plan_outline as po
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from state import NovelProjectState


OUTLINE_SKELETON_BATCHES = max(1, int(os.environ.get("OUTLINE_SKELETON_BATCHES", "5")))
OUTLINE_SKELETON_RECENT_CONTEXT = max(0, int(os.environ.get("OUTLINE_SKELETON_RECENT_CONTEXT", "60")))


def _split_batches(total_chapters: int, batches: int) -> List[Tuple[int, int]]:
    # 思路：
    # 1) 先用整除得到每批基础章数 base；
    # 2) 余数 rem 分配到前 rem 批（每批 +1）；
    # 3) 这样即使 total_chapters 不能被 batches 整除，也能保证区间总和严格等于 total_chapters。
    #
    # 例：total_chapters=202, batches=5
    # base=40, rem=2 -> [41,41,40,40,40]，总和=202。
    base = total_chapters // batches
    rem = total_chapters % batches
    out: List[Tuple[int, int]] = []
    start = 0
    for i in range(batches):
        size = base + (1 if i < rem else 0)
        if size <= 0:
            continue
        end = start + size - 1
        out.append((start, end))
        start = end + 1
    return out


def _recent_skeleton_context(index_map: Dict[int, Dict[str, str]], recent_k: int) -> str:
    if not index_map:
        return "（无，本批为开篇）"
    keys = sorted(index_map.keys())
    tail = keys[-recent_k:] if recent_k > 0 else keys
    rows: List[str] = []
    for g in tail:
        item = index_map[g]
        title = str(item.get("title") or f"第{g + 1}章").strip()
        desc = str(item.get("description") or "").strip() or "（无）"
        rows.append(f"- {g}|{title}|{desc}")
    return "\n".join(rows) if rows else "（无）"


def _skeleton_lite_batch_prompt(
    selected_plot_summary: str,
    total_chapters: int,
    batch_start: int,
    batch_end: int,
    generated_context: str,
) -> str:
    return (
        "你是一名长篇小说策划。【plan_outline_skeleton_lite】请根据剧情概要生成全书目录骨架的当前批次。\n"
        "输出要求：\n"
        f"1) 本次仅生成 global_index={batch_start}..{batch_end} 的章节骨架；不可越界、不可遗漏；\n"
        "2) 每章仅保留 g/t/d（分别代表 global_index/title/description，description 约20字，建议18~25字）；不要写 beat/points；\n"
        "3) description 要体现该章核心推进，禁止空字符串；\n"
        "4) 仅输出 JSON，不要解释。\n"
        "JSON 格式：\n"
        '{"chapters":[{"g":0,"t":"章名","d":"约20字章节简述"}]}\n\n'
        f"全书目标章节数：{total_chapters}\n"
        f"本批区间：{batch_start}..{batch_end}\n"
        f"已生成骨架摘要（global_index|title|description）：\n{generated_context}\n"
        f"剧情概要：{selected_plot_summary}"
    )


def _parse_batch_payload(data: Dict[str, Any], batch_start: int, batch_end: int) -> Dict[int, Dict[str, str]]:
    chapters = data.get("chapters") or []
    if not isinstance(chapters, list):
        raise ValueError("skeleton_lite batch: chapters must be list")
    out: Dict[int, Dict[str, str]] = {}
    for item in chapters:
        if not isinstance(item, dict):
            continue
        # 兼容长/短键：
        # - 新格式：g/t/d（降低输出 token）
        # - 旧格式：global_index/title/description（兼容历史提示词与缓存）
        g_raw = item.get("global_index", item.get("g", -1))
        g = int(g_raw)
        if g < batch_start or g > batch_end:
            continue
        title = str(item.get("title", item.get("t")) or "").strip()
        desc = str(item.get("description", item.get("d")) or "").strip()
        if not title or not desc:
            raise ValueError(f"skeleton_lite batch: chapter {g} title/description empty")
        out[g] = {"title": title, "description": desc}
    missing = [i for i in range(batch_start, batch_end + 1) if i not in out]
    if missing:
        raise ValueError(f"skeleton_lite batch: missing chapters {missing}")
    return out


def _build_outline_from_index_map(total_chapters: int, index_map: Dict[int, Dict[str, str]]) -> Dict[str, Any]:
    chapters: List[Dict[str, Any]] = []
    for g in range(total_chapters):
        item = index_map.get(g) or {}
        chapters.append(
            {
                "title": str(item.get("title") or f"第{g + 1}章"),
                "description": str(item.get("description") or "（待补充章节简述）"),
                "points": [],
            }
        )
    return {"volumes": [{"volume_title": "第一卷", "chapters": chapters}]}


async def outline_skeleton_lite_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    kb_context: Optional[str] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    selected_plot_summary = str(state.get("selected_plot_summary") or "").strip()
    total_chapters = int(state.get("total_chapters", 0) or 0) or 12
    total_chapters = max(1, total_chapters)

    kb_suffix = ""
    if (kb_context or "").strip():
        kb_suffix = (
            "\n\n【参考知识库（原著/设定；若与概要冲突，以概要与本作二创为准）】\n"
            + (kb_context or "").strip()[:12000]
        )

    index_map: Dict[int, Dict[str, str]] = {}
    batch_ranges = _split_batches(total_chapters, OUTLINE_SKELETON_BATCHES)
    for batch_start, batch_end in batch_ranges:
        generated_context = _recent_skeleton_context(index_map, OUTLINE_SKELETON_RECENT_CONTEXT)
        prompt = _skeleton_lite_batch_prompt(
            selected_plot_summary=selected_plot_summary,
            total_chapters=total_chapters,
            batch_start=batch_start,
            batch_end=batch_end,
            generated_context=generated_context,
        )
        try:
            parse_fn = lambda text: _parse_batch_payload(
                extract_json_object(text),
                batch_start,
                batch_end,
            )
            got = await invoke_and_parse_with_retry(
                planner,
                prompt + kb_suffix,
                parse_fn,
                max_retries=3,
            )
            index_map.update(got)
        except Exception:
            # 连续/单批失败都按区间占位兜底：保证骨架结构完整，后续仍可扩窗细化。
            for g in range(batch_start, batch_end + 1):
                if g not in index_map:
                    index_map[g] = {
                        "title": f"第{g + 1}章",
                        "description": "（待补充章节简述）",
                    }

    outline_structure = _build_outline_from_index_map(total_chapters, index_map)
    po._normalize_skeleton_chapter_count(outline_structure, total_chapters)
    for _v, _c, ch in po._flatten_chapter_refs(outline_structure):
        ch.pop("beat", None)
        if not str(ch.get("description") or "").strip():
            ch["description"] = "（待补充章节简述）"
        ch["points"] = []
    return {
        "outline_mode": "long",
        "outline_structure": outline_structure,
        "outline_seed_done": False,
        "outline_generated_until": -1,
    }

