from __future__ import annotations

from typing import Any, Dict, Optional

from graph.llm import create_planner_llm
import graph.nodes.plan_outline as po
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from state import NovelProjectState


async def outline_short_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    kb_context: Optional[str] = None,
    target_chapters: Optional[int] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    selected_plot_summary = str(state.get("selected_plot_summary") or "").strip()
    total_chapters = int(state.get("total_chapters", 0) or 0) or 12
    run_target = int(target_chapters if target_chapters is not None else total_chapters)
    run_target = max(1, min(run_target, total_chapters))

    kb_suffix = ""
    if (kb_context or "").strip():
        kb_suffix = (
            "\n\n【参考知识库（原著/设定；若与概要冲突，以概要与本作二创为准）】\n"
            + (kb_context or "").strip()[:16000]
        )

    prompt = po._single_call_prompt(selected_plot_summary, run_target) + kb_suffix
    obj = await invoke_and_parse_with_retry(planner, prompt, extract_json_object, max_retries=3)
    outline_structure = {"volumes": obj.get("volumes", [])}
    return {
        "outline_mode": "short",
        "outline_structure": outline_structure,
        "outline_generated_until": po._count_chapters(outline_structure) - 1,
    }

