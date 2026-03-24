from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from graph.llm import create_planner_llm
import graph.nodes.plan_outline as po
from rag import LocalRagIndexer
from state import NovelProjectState, outline_structure_to_string

logger = logging.getLogger(__name__)


async def outline_finalize_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    kb_context: Optional[str] = None,
) -> Dict[str, Any]:
    t0 = time.monotonic()
    planner = llm or create_planner_llm()
    project_id = (state.get("project_id") or "").strip() or "(no_project)"
    outline_structure = state.get("outline_structure") or {"volumes": []}
    outline_str = outline_structure_to_string(outline_structure)

    if project_id != "(no_project)" and rag_indexer is not None and outline_structure.get("volumes"):
        po._index_outline_chunks(rag_indexer, project_id, outline_structure)

    canon_overrides: List[Dict[str, Any]] = list(state.get("canon_overrides") or [])
    if (kb_context or "").strip():
        new_ov = await po._extract_canon_overrides(
            planner,
            str(state.get("selected_plot_summary") or ""),
            outline_str,
            (kb_context or "").strip()[:12000],
        )
        seen = {str(o.get("subject")) for o in canon_overrides}
        for o in new_ov:
            subj = str(o.get("subject") or "")
            if subj and subj not in seen:
                seen.add(subj)
                canon_overrides.append(o)

    logger.info(
        "[outline_finalize] done project=%s chapters=%s elapsed_s=%.3f",
        project_id,
        po._count_chapters(outline_structure),
        time.monotonic() - t0,
    )
    return {
        "outline": outline_str,
        "outline_structure": outline_structure,
        "outline_generated_until": int(state.get("outline_generated_until", -1) or -1),
        "canon_overrides": canon_overrides,
    }

