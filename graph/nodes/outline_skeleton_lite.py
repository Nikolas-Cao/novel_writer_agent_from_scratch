from __future__ import annotations

from typing import Any, Dict, Optional

from graph.llm import create_planner_llm
import graph.nodes.plan_outline as po
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from state import NovelProjectState


def _skeleton_lite_prompt(selected_plot_summary: str, total_chapters: int) -> str:
    return (
        "你是一名长篇小说策划。【plan_outline_skeleton_lite】请根据剧情概要生成全书极简目录骨架。\n"
        "输出要求：\n"
        "1) 至少一卷；章节总数必须等于目标章节数；\n"
        "2) 每章仅保留 title，不要写 beat/points；\n"
        "3) 仅输出 JSON，不要解释。\n"
        "JSON 格式：\n"
        '{"volumes":[{"volume_title":"卷名","chapters":[{"title":"章名"}]}]}\n\n'
        f"目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}"
    )


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

    raw = await invoke_and_parse_with_retry(
        planner,
        _skeleton_lite_prompt(selected_plot_summary, total_chapters) + kb_suffix,
        extract_json_object,
        max_retries=3,
    )
    outline_structure: Dict[str, Any] = {"volumes": list(raw.get("volumes", []))}
    po._normalize_skeleton_chapter_count(outline_structure, total_chapters)
    for _v, _c, ch in po._flatten_chapter_refs(outline_structure):
        ch.pop("beat", None)
        ch["points"] = []
    return {
        "outline_mode": "long",
        "outline_structure": outline_structure,
        "outline_seed_done": False,
        "outline_generated_until": -1,
    }

