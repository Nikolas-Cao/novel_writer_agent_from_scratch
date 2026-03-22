"""
知识库上下文编排：分层资产 + 混合检索 + 可选补查询（受限工具循环）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from config import KB_TOOL_LOOP_MAX_CALLS
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from rag.global_kb_retriever import GlobalKbRetriever

logger = logging.getLogger(__name__)


def _build_primary_query(title: str, points: List[str], extra: str = "") -> str:
    pts = " ".join(points[:12])
    return f"{title}\n{pts}\n{extra}".strip()


def _evidence_diversity(evidence: List[Dict[str, Any]]) -> int:
    docs = {e.get("kb_id") for e in evidence}
    return len(docs)


def _compress_evidence(evidence: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    lines = []
    n = 0
    for i, e in enumerate(evidence, 1):
        t = (e.get("text") or "").strip()
        if not t:
            continue
        t = t[:2500]
        lines.append(f"[{i}] ({e.get('source')}) {t}")
        n += len(t)
        if n >= max_chars:
            break
    return "\n\n".join(lines)


async def _planner_extra_queries(planner: Any, title: str, points: List[str], brief: str) -> List[str]:
    prompt = (
        "根据章节标题与要点，生成 1~2 个用于检索同人知识库的短查询词（专名、关系、事件），"
        "严格输出 JSON：{\"queries\":[\"...\",\"...\"]}\n\n"
        f"标题：{title}\n要点：{points}\n已有证据摘要：{brief[:1500]}"
    )
    try:
        obj = await invoke_and_parse_with_retry(planner, prompt, extract_json_object, max_retries=2)
        qs = list(obj.get("queries") or [])
        return [str(q).strip() for q in qs if str(q).strip()][: KB_TOOL_LOOP_MAX_CALLS]
    except Exception as exc:
        logger.warning("extra kb queries failed: %s", exc)
        return []


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

    assets_text = retriever.assets_layer_text(kb_ids)
    q1 = _build_primary_query(title, points)
    evidence = retriever.hybrid_retrieve(kb_ids, q1, doc_id=doc_id)

    # 第二轮：从要点抽取疑似专名（2~4 字连续汉字）补检索
    blob = title + "\n" + "\n".join(points)
    names = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,4}", blob)))[:8]
    if names:
        q2 = " ".join(names[:5])
        extra = retriever.hybrid_retrieve(kb_ids, q2, doc_id=doc_id)
        seen = {e.get("chunk_id") for e in evidence}
        for e in extra:
            if e.get("chunk_id") not in seen:
                seen.add(e.get("chunk_id"))
                evidence.append(e)

    confidence = 0.85
    if len(evidence) < 3 or _evidence_diversity(evidence) < 1:
        confidence = 0.45
        if planner is not None:
            brief = _compress_evidence(evidence, max_chars=2000)
            more_qs = await _planner_extra_queries(planner, title, points, brief)
            for mq in more_qs:
                add = retriever.hybrid_retrieve(kb_ids, mq, doc_id=doc_id)
                seen = {e.get("chunk_id") for e in evidence}
                for e in add:
                    if e.get("chunk_id") not in seen:
                        evidence.append(e)
                        seen.add(e.get("chunk_id"))
            if len(evidence) >= 3:
                confidence = 0.7

    ev_text = _compress_evidence(evidence)
    return {
        "kb_assets_text": assets_text,
        "kb_evidence_text": ev_text,
        "kb_evidence": evidence,
        "kb_confidence": confidence,
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
    ev = retriever.hybrid_retrieve(kb_ids, plot_summary[:2000])
    ev_txt = _compress_evidence(ev, max_chars=8000)
    return "\n\n".join(
        [
            "【知识库分层摘要】\n" + assets if assets else "",
            "【与概要相关的原文证据摘录】\n" + ev_txt if ev_txt else "",
        ]
    ).strip()


def format_canon_overrides(overrides: Optional[List[Dict[str, Any]]]) -> str:
    if not overrides:
        return ""
    lines = []
    for o in overrides[-30:]:
        subj = o.get("subject") or ""
        fan = o.get("fanfic_fact") or ""
        lines.append(f"- {subj}：二创设定 → {fan}")
    return "【二创设定覆盖（优先于原著）】\n" + "\n".join(lines)
