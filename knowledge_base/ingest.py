"""
流式摄取 txt/md：按字节边界 checkpoint，分批写入 Chroma + FTS。
"""
from __future__ import annotations

import asyncio
import codecs
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from charset_normalizer import from_bytes

from config import (
    KB_CHUNK_OVERLAP_CHARS,
    KB_CHUNK_TARGET_CHARS,
    KB_INGEST_BATCH_CHUNKS,
    KB_MAX_CHUNKS_PER_DOCUMENT,
    KB_READ_BLOCK_BYTES,
)
from knowledge_base.fts_index import fts_delete_by_doc, fts_upsert_batch
from knowledge_base.store import KnowledgeBaseStore
from rag.global_kb_chroma import GlobalKbChroma

logger = logging.getLogger(__name__)

ALLOWED_SUFFIX = {".txt", ".md"}


class IngestCancelled(Exception):
    """用户取消构建任务。"""


def detect_text_encoding(sample: bytes) -> str:
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    res = from_bytes(sample).best()
    if res:
        return str(res.encoding)
    return "utf-8"


def _split_chunk(text: str, target: int, overlap: int) -> Tuple[str, str]:
    """从 text 头部切出约 target 字，返回 (chunk, remainder)。尽量在段落边界切。"""
    if len(text) <= target:
        return text, ""
    window = text[: target + 200]
    cut = target
    for sep in ("\n\n", "\n", "。", "；", "，"):
        idx = window.rfind(sep, max(0, target // 2), target + 100)
        if idx != -1 and idx >= target // 3:
            cut = idx + len(sep)
            break
    chunk = text[:cut].strip()
    rest = text[cut:].strip()
    if overlap > 0 and rest:
        tail = chunk[-overlap:] if len(chunk) > overlap else chunk
        rest = tail + rest
    return chunk, rest


def run_ingest_sync(
    *,
    kb_id: str,
    doc_id: str,
    raw_path: Path,
    store: KnowledgeBaseStore,
    chroma: GlobalKbChroma,
    fts_db: Path,
    job_id: str,
    cancel_check: Callable[[], bool],
) -> Dict[str, Any]:
    suffix = raw_path.suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise ValueError(f"不支持的文件类型：{suffix}，仅支持 .txt / .md")

    # 每次任务全量重建该文档索引（千万字流式读取，不整文件载入内存）
    chroma.delete_by_doc(kb_id, doc_id)
    fts_delete_by_doc(fts_db, doc_id)
    file_pos = 0
    next_seq = 0
    total_chunks = 0

    file_size = raw_path.stat().st_size
    head = raw_path.read_bytes()[: min(262144, file_size)]
    encoding = detect_text_encoding(head)
    decoder_cls = codecs.getincrementaldecoder(encoding)
    decoder = decoder_cls(errors="replace")

    buffer = ""
    batch_ids: List[str] = []
    batch_docs: List[str] = []
    batch_metas: List[Dict[str, Any]] = []
    batch_fts: List[Dict[str, Any]] = []

    def flush_batch() -> None:
        nonlocal batch_ids, batch_docs, batch_metas, batch_fts
        if not batch_ids:
            return
        chroma.upsert_chunks(kb_id, batch_ids, batch_docs, batch_metas)
        fts_upsert_batch(fts_db, batch_fts)
        batch_ids = []
        batch_docs = []
        batch_metas = []
        batch_fts = []

    with raw_path.open("rb") as f:
        f.seek(file_pos)
        while True:
            if cancel_check():
                raise IngestCancelled()
            block = f.read(KB_READ_BLOCK_BYTES)
            if block:
                buffer += decoder.decode(block)
                file_pos = f.tell()
            else:
                buffer += decoder.decode(b"", final=True)
            while len(buffer) >= KB_CHUNK_TARGET_CHARS:
                if total_chunks >= KB_MAX_CHUNKS_PER_DOCUMENT:
                    raise ValueError(
                        f"单文档 chunk 数超过上限 {KB_MAX_CHUNKS_PER_DOCUMENT}，请拆分文件或调高环境变量"
                    )
                chunk_text, buffer = _split_chunk(buffer, KB_CHUNK_TARGET_CHARS, KB_CHUNK_OVERLAP_CHARS)
                if not chunk_text.strip():
                    break
                cid = f"{doc_id}-c{next_seq}"
                meta = {
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "chunk_index": next_seq,
                    "char_len": len(chunk_text),
                    "byte_offset_approx": file_pos,
                }
                batch_ids.append(cid)
                batch_docs.append(chunk_text)
                batch_metas.append(meta)
                batch_fts.append({"chunk_id": cid, "doc_id": doc_id, "content": chunk_text})
                next_seq += 1
                total_chunks += 1
                if len(batch_ids) >= KB_INGEST_BATCH_CHUNKS:
                    flush_batch()
                    store.save_job(
                        kb_id,
                        {
                            "job_id": job_id,
                            "kb_id": kb_id,
                            "doc_id": doc_id,
                            "status": "indexing",
                            "byte_offset": file_pos,
                            "next_chunk_seq": next_seq,
                            "processed_chunks": total_chunks,
                            "file_size": file_size,
                            "cancel_requested": False,
                            "error_message": None,
                        },
                    )
            if not block:
                break

    if buffer.strip() and total_chunks < KB_MAX_CHUNKS_PER_DOCUMENT:
        cid = f"{doc_id}-c{next_seq}"
        chunk_text = buffer.strip()
        batch_ids.append(cid)
        batch_docs.append(chunk_text)
        batch_metas.append(
            {
                "kb_id": kb_id,
                "doc_id": doc_id,
                "chunk_index": next_seq,
                "char_len": len(chunk_text),
                "byte_offset_approx": file_pos,
            }
        )
        batch_fts.append({"chunk_id": cid, "doc_id": doc_id, "content": chunk_text})
        next_seq += 1
        total_chunks += 1

    flush_batch()

    store.save_job(
        kb_id,
        {
            "job_id": job_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "status": "summarizing_assets",
            "byte_offset": file_size,
            "next_chunk_seq": next_seq,
            "processed_chunks": total_chunks,
            "file_size": file_size,
            "cancel_requested": False,
            "error_message": None,
        },
    )
    return {
        "doc_id": doc_id,
        "chunks": total_chunks,
        "encoding": encoding,
    }


async def run_document_ingest(
    *,
    kb_id: str,
    doc_id: str,
    raw_path: Path,
    store: KnowledgeBaseStore,
    chroma: GlobalKbChroma,
    fts_db: Path,
    job_id: str,
    cancel_check: Callable[[], bool],
) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: run_ingest_sync(
            kb_id=kb_id,
            doc_id=doc_id,
            raw_path=raw_path,
            store=store,
            chroma=chroma,
            fts_db=fts_db,
            job_id=job_id,
            cancel_check=cancel_check,
        ),
    )
