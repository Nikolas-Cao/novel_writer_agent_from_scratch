"""
分层摘要资产：流式读取原文窗口 -> leaf 摘要 -> section 汇总 -> 全局结构化 JSON。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

from charset_normalizer import from_bytes

from config import KB_ASSET_LEAF_BATCH_CHARS, KB_ASSET_MAX_LEAF_WINDOWS, VECTOR_STORE_DIR
from graph.utils import extract_json_object, get_message_text

logger = logging.getLogger(__name__)


def _normalize_token(value: str) -> str:
    raw = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _utc_ts_for_filename(dt_value: dt.datetime) -> str:
    return dt_value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z").replace(":", "")


def _extract_token_usage(resp: Any) -> Dict[str, int]:
    inp = out = 0
    usage_metadata = getattr(resp, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        inp = int(usage_metadata.get("input_tokens") or usage_metadata.get("prompt_tokens") or 0)
        out = int(usage_metadata.get("output_tokens") or usage_metadata.get("completion_tokens") or 0)
    if inp or out:
        return {"input_tokens": inp, "output_tokens": out}
    response_metadata = getattr(resp, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            inp = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
            out = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0)
    return {"input_tokens": inp, "output_tokens": out}


def _model_name(planner: Any) -> str:
    inner = getattr(planner, "_inner", None)
    if inner is not None:
        return str(getattr(inner, "model_name", None) or getattr(inner, "model", None) or "unknown")
    return str(getattr(planner, "model_name", None) or getattr(planner, "model", None) or "unknown")


def _write_kb_invoke_record(
    *,
    kb_id: str,
    record: Dict[str, Any],
) -> None:
    jobs_dir = Path(VECTOR_STORE_DIR) / "global_kb" / kb_id / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    invoke_id = str(record.get("invoke_id") or "")
    short_id = _normalize_token(invoke_id[:8] or "noid")
    status = _normalize_token(str(record.get("status") or "unknown"))
    purpose = _normalize_token(str(record.get("purpose") or "unknown"))
    ts = _utc_ts_for_filename(dt.datetime.now(dt.timezone.utc))
    path = jobs_dir / f"{ts}_{status}_{purpose}_{short_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


async def _invoke_with_audit(
    *,
    planner: Any,
    prompt: str,
    kb_id: str,
    doc_id: str,
    job_id: str,
    phase: str,
) -> str:
    invoke_id = str(uuid.uuid4())
    started_at = dt.datetime.now(dt.timezone.utc)
    response_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    error_obj = None
    status = "success"
    try:
        resp = await planner.ainvoke(prompt)
        response_text = get_message_text(resp)
        usage = _extract_token_usage(resp)
    except Exception as exc:
        status = "error"
        error_obj = {"type": exc.__class__.__name__, "message": str(exc)}
        raise
    finally:
        finished_at = dt.datetime.now(dt.timezone.utc)
        # 思路：
        # 1) 每次 LLM ainvoke 都落盘独立记录，便于按阶段(phase)回溯问题；
        # 2) 与项目侧 llm_invoke_results 保持同字段风格，降低排障心智负担；
        # 3) 失败分支同样持久化 prompt + error，避免“线上失败不可复现”。
        record = {
            "invoke_id": invoke_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "job_id": job_id,
            "node_name": "build_hierarchical_assets",
            "purpose": "summarize_assets",
            "purpose_group": "knowledge_base_ingest",
            "phase": phase,
            "model_name": _model_name(planner),
            "is_stream": False,
            "status": status,
            "prompt": prompt,
            "response_text": response_text,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "usage": usage,
            "error": error_obj,
        }
        try:
            _write_kb_invoke_record(kb_id=kb_id, record=record)
        except Exception:
            logger.exception("failed to persist kb llm invoke record")
    return response_text.strip()


def _detect_enc(sample: bytes) -> str:
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    r = from_bytes(sample).best()
    return str(r.encoding) if r else "utf-8"


async def build_hierarchical_assets(
    *,
    raw_path: Path,
    planner: Any,
    cancel_check: Callable[[], bool],
    kb_id: str,
    doc_id: str,
    job_id: str,
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
                summary = await _invoke_with_audit(
                    planner=planner,
                    prompt=(
                        "你是读书摘要助手。请将下列小说原文片段压缩为要点摘要（保留人名、关系、关键事件），"
                        f"300-600 字，纯文本。\n片段编号：W{window_idx}\n\n{piece[:50000]}"
                    ),
                    kb_id=kb_id,
                    doc_id=doc_id,
                    job_id=job_id,
                    phase=f"leaf_summary_W{window_idx}",
                )
                leaf_summaries.append(
                    {
                        "id": f"leaf-{window_idx}",
                        "char_approx_end": char_carry,
                        "summary": summary,
                    }
                )

    if buf.strip() and window_idx < max_leaf_windows and not cancel_check():
        window_idx += 1
        summary = await _invoke_with_audit(
            planner=planner,
            prompt=(
                "你是读书摘要助手。请将下列小说原文片段压缩为要点摘要（保留人名、关系、关键事件），"
                f"300-600 字，纯文本。\n片段编号：W{window_idx}\n\n{buf.strip()[:50000]}"
            ),
            kb_id=kb_id,
            doc_id=doc_id,
            job_id=job_id,
            phase=f"leaf_summary_W{window_idx}",
        )
        leaf_summaries.append(
            {
                "id": f"leaf-{window_idx}",
                "char_approx_end": char_carry + len(buf),
                "summary": summary,
            }
        )

    section_summaries: List[Dict[str, Any]] = []
    # 只有一个 leaf 时，section 层不再二次调用 LLM：
    # 1) 避免把 300-600 字“硬扩写”为 600-1000 字导致幻觉信息；
    # 2) 减少一次无必要的模型调用成本与延迟。
    if len(leaf_summaries) == 1:
        section_summaries.append(
            {
                "id": "section-1",
                "summary": str(leaf_summaries[0].get("summary") or ""),
            }
        )

    batch: List[str] = []
    sec_i = 0
    if len(leaf_summaries) != 1:
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
                section_summaries.append(
                    {
                        "id": f"section-{sec_i}",
                        "summary": await _invoke_with_audit(
                            planner=planner,
                            prompt=prompt,
                            kb_id=kb_id,
                            doc_id=doc_id,
                            job_id=job_id,
                            phase=f"section_summary_{sec_i}",
                        ),
                    }
                )
                batch = []
        if batch and not cancel_check():
            sec_i += 1
            joined = "\n---\n".join(batch)
            section_summaries.append(
                {
                    "id": f"section-{sec_i}",
                    "summary": await _invoke_with_audit(
                        planner=planner,
                        prompt="将下列摘要合并为一段卷摘要（600-1000字），纯文本。\n\n" + joined[:45000],
                        kb_id=kb_id,
                        doc_id=doc_id,
                        job_id=job_id,
                        phase=f"section_summary_{sec_i}",
                    ),
                }
            )

    merged = "\n\n".join(s["summary"] for s in section_summaries)[:24000]
    global_summary = ""
    characters: List[Any] = []
    timeline: List[Any] = []
    world_rules: List[Any] = []
    core_facts: List[Any] = []

    if merged.strip() and not cancel_check():
        # 思路：
        # 1) 仅 1 个 section 且由 leaf 直拷时，merged 往往只有 300–600 字，再要求 global_summary 500–900 字会逼模型“注水/胡编”；
        # 2) 因此 global_summary 上限必须严格低于 merged 总字数(merged_len)，长文仍保留 500–900 的常规区间；
        # 3) 下限随上限收缩，避免出现 low>high 的无效区间。
        merged_len = len(merged.strip())
        gs_ceiling = max(1, merged_len - 1)
        if merged_len >= 901:
            global_summary_field_hint = (
                f"全书概要约500-900字，且必须短于下方输入摘要总字数（约{merged_len}字），不得超出输入编造"
            )
        else:
            gs_low = max(1, min(400, (gs_ceiling * 2) // 3))
            gs_low = min(gs_low, gs_ceiling)
            global_summary_field_hint = (
                f"全书概要约{gs_low}-{gs_ceiling}字（须严格少于输入摘要总字数约{merged_len}字），"
                "只能压缩提炼，禁止扩写凑字数或编造"
            )
        extract_prompt = (
            "根据下列小说摘要，提取结构化知识资产。仅输出 JSON：\n"
            f'{{"global_summary":"{global_summary_field_hint}",'
            '"characters":[{"name":"","aliases":[],"role":"","relations":""}],'
            '"timeline":[{"order":1,"event":"","actors":""}],'
            '"world_rules":[{"rule":"","note":""}],'
            '"core_facts":[{"fact":"","importance":"high|medium"}]}\n\n'
            f"{merged}"
        )
        try:
            # 思路：
            # 1) 这里不能直接复用 invoke_and_parse_with_retry，否则无法记录每次重试的原始响应；
            # 2) 因此手写 2 次重试循环，并且每次都通过 _invoke_with_audit 落盘；
            # 3) 这样即使最终解析失败，也能在 jobs 目录中看到每轮响应内容用于排障。
            obj: Dict[str, Any] = {}
            for attempt in range(2):
                response_text = await _invoke_with_audit(
                    planner=planner,
                    prompt=extract_prompt,
                    kb_id=kb_id,
                    doc_id=doc_id,
                    job_id=job_id,
                    phase=f"extract_structured_assets_attempt_{attempt + 1}",
                )
                try:
                    obj = extract_json_object(response_text)
                    break
                except Exception as exc:
                    if attempt == 1:
                        raise
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
    kb_id: str,
    doc_id: str,
    job_id: str,
) -> Dict[str, Any]:
    return await build_hierarchical_assets(
        raw_path=raw_path,
        planner=planner,
        cancel_check=cancel_check,
        kb_id=kb_id,
        doc_id=doc_id,
        job_id=job_id,
    )
