"""
知识库上下文编排（激进路线）：仅注入分层摘要，不拼接证据摘录。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from rag.global_kb_retriever import GlobalKbRetriever

async def build_kb_context_for_writing(
    *,
    kb_ids: List[str],
    title: str,
    points: List[str],
    retriever: GlobalKbRetriever,
    planner: Optional[Any] = None,
    doc_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not kb_ids:
        return {
            "kb_assets_text": "",
            "kb_evidence_text": "",
            "kb_evidence": [],
            "kb_confidence": 1.0,
        }

    # 激进路线：
    # 1) 仅注入知识库分层摘要（角色/时间线/设定/核心事实等）；
    # 2) 不执行证据检索与补查询，避免 token 膨胀；
    # 3) 保持兼容字段（kb_evidence_text/kb_evidence/kb_confidence）存在，避免历史状态与前端读取断裂。
    assets_text = retriever.assets_layer_text(kb_ids)
    return {
        "kb_assets_text": assets_text,
        "kb_evidence_text": "",
        "kb_evidence": [],
        "kb_confidence": 1.0,
    }


async def build_kb_context_for_outline(
    *,
    kb_ids: List[str],
    plot_summary: str,
    retriever: GlobalKbRetriever,
) -> str:
    if not kb_ids:
        return ""
    assets = retriever.assets_layer_text(kb_ids)
    return ("【知识库分层摘要】\n" + assets).strip() if assets else ""


def format_canon_overrides(overrides: Optional[List[Dict[str, Any]]]) -> str:
    if not overrides:
        return ""
    lines = []
    for o in overrides[-30:]:
        subj = o.get("subject") or ""
        fan = o.get("fanfic_fact") or ""
        lines.append(f"- {subj}：二创设定 → {fan}")
    return "【二创设定覆盖（优先于原著）】\n" + "\n".join(lines)
