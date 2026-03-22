"""
分层摘要资产：流式读取原文窗口 -> leaf 摘要 -> section 汇总 -> 全局结构化 JSON。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List

from charset_normalizer import from_bytes

from config import KB_ASSET_LEAF_BATCH_CHARS, KB_ASSET_MAX_LEAF_WINDOWS
from graph.utils import extract_json_object, get_message_text, invoke_and_parse_with_retry

logger = logging.getLogger(__name__)


def _detect_enc(sample: bytes) -> str:
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    r = from_bytes(sample).best()
    return str(r.encoding) if r else "utf-8"


async def _summarize_window(planner: Any, text: str, window_label: str) -> str:
    prompt = (
        "你是读书摘要助手。请将下列小说原文片段压缩为要点摘要（保留人名、关系、关键事件），"
        f"300-600 字，纯文本。\n片段编号：{window_label}\n\n{text[:50000]}"
    )
    resp = await planner.ainvoke(prompt)
    return get_message_text(resp).strip() or text[:400]


async def build_hierarchical_assets(
    *,
    raw_path: Path,
    planner: Any,
    cancel_check: Callable[[], bool],
    max_leaf_windows: int = KB_ASSET_MAX_LEAF_WINDOWS,
) -> Dict[str, Any]:
    file_size = raw_path.stat().st_size
    head = raw_path.read_bytes()[: min(262144, file_size)]
    enc = _detect_enc(head)

    leaf_summaries: List[Dict[str, Any]] = []
    buf = ""
    window_idx = 0
    char_carry = 0

    with raw_path.open("r", encoding=enc, errors="replace") as f:
        while window_idx < max_leaf_windows:
            if cancel_check():
                break
            chunk = f.read(100000)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= KB_ASSET_LEAF_BATCH_CHARS and window_idx < max_leaf_windows:
                if cancel_check():
                    break
                piece = buf[:KB_ASSET_LEAF_BATCH_CHARS]
                buf = buf[KB_ASSET_LEAF_BATCH_CHARS // 2 :]
                window_idx += 1
                char_carry += len(piece)
                summary = await _summarize_window(planner, piece, f"W{window_idx}")
                leaf_summaries.append(
                    {
                        "id": f"leaf-{window_idx}",
                        "char_approx_end": char_carry,
                        "summary": summary,
                    }
                )

    if buf.strip() and window_idx < max_leaf_windows and not cancel_check():
        window_idx += 1
        summary = await _summarize_window(planner, buf.strip(), f"W{window_idx}")
        leaf_summaries.append(
            {
                "id": f"leaf-{window_idx}",
                "char_approx_end": char_carry + len(buf),
                "summary": summary,
            }
        )

    section_summaries: List[Dict[str, Any]] = []
    batch: List[str] = []
    sec_i = 0
    for leaf in leaf_summaries:
        batch.append(leaf.get("summary") or "")
        if len(batch) >= 8:
            sec_i += 1
            if cancel_check():
                break
            joined = "\n---\n".join(batch)
            prompt = (
                "将下列多段摘要合并为一段结构化卷摘要（800-1200字），保留人物关系与主线事件，纯文本。\n\n"
                f"{joined[:45000]}"
            )
            resp = await planner.ainvoke(prompt)
            section_summaries.append(
                {"id": f"section-{sec_i}", "summary": get_message_text(resp).strip()}
            )
            batch = []
    if batch and not cancel_check():
        sec_i += 1
        joined = "\n---\n".join(batch)
        resp = await planner.ainvoke(
            "将下列摘要合并为一段卷摘要（600-1000字），纯文本。\n\n" + joined[:45000]
        )
        section_summaries.append({"id": f"section-{sec_i}", "summary": get_message_text(resp).strip()})

    merged = "\n\n".join(s["summary"] for s in section_summaries)[:24000]
    global_summary = ""
    characters: List[Any] = []
    timeline: List[Any] = []
    world_rules: List[Any] = []
    core_facts: List[Any] = []

    if merged.strip() and not cancel_check():
        extract_prompt = (
            "根据下列小说摘要，提取结构化知识资产。仅输出 JSON：\n"
            '{"global_summary":"全书概要500-900字",'
            '"characters":[{"name":"","aliases":[],"role":"","relations":""}],'
            '"timeline":[{"order":1,"event":"","actors":""}],'
            '"world_rules":[{"rule":"","note":""}],'
            '"core_facts":[{"fact":"","importance":"high|medium"}]}\n\n'
            f"{merged}"
        )
        try:
            obj = await invoke_and_parse_with_retry(planner, extract_prompt, extract_json_object, max_retries=2)
            global_summary = str(obj.get("global_summary") or "")
            characters = list(obj.get("characters") or [])
            timeline = list(obj.get("timeline") or [])
            world_rules = list(obj.get("world_rules") or [])
            core_facts = list(obj.get("core_facts") or [])
        except Exception as exc:
            logger.warning("assets extract json failed: %s", exc)
            global_summary = merged[:2000]

    return {
        "characters": characters,
        "timeline": timeline,
        "world_rules": world_rules,
        "core_facts": core_facts,
        "leaf_summaries": leaf_summaries,
        "section_summaries": section_summaries,
        "global_summary": global_summary or (leaf_summaries[0]["summary"] if leaf_summaries else ""),
        "status": "ready",
    }


async def build_assets_task(
    raw_path: Path,
    planner: Any,
    cancel_check: Callable[[], bool],
) -> Dict[str, Any]:
    return await build_hierarchical_assets(raw_path=raw_path, planner=planner, cancel_check=cancel_check)
