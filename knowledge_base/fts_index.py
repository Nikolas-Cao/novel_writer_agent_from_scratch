"""SQLite FTS5 关键词索引（按知识集一份库）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_schema(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                doc_id UNINDEXED,
                content,
                tokenize = 'unicode61'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def fts_upsert_batch(db_path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema(db_path)
    ids = [r["chunk_id"] for r in rows]
    fts_delete_chunk_ids(db_path, ids)
    conn = _connect(db_path)
    try:
        for r in rows:
            conn.execute(
                "INSERT INTO kb_chunks_fts (chunk_id, doc_id, content) VALUES (?, ?, ?)",
                (r["chunk_id"], r["doc_id"], r["content"]),
            )
        conn.commit()
    finally:
        conn.close()


def fts_delete_by_doc(db_path: Path, doc_id: str) -> None:
    if not db_path.exists():
        return
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM kb_chunks_fts WHERE doc_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


def fts_delete_chunk_ids(db_path: Path, chunk_ids: List[str]) -> None:
    if not chunk_ids or not db_path.exists():
        return
    conn = _connect(db_path)
    try:
        ph = ",".join("?" * len(chunk_ids))
        conn.execute(f"DELETE FROM kb_chunks_fts WHERE chunk_id IN ({ph})", chunk_ids)
        conn.commit()
    finally:
        conn.close()


def fts_search(db_path: Path, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    if not db_path.exists() or not query.strip():
        return []
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        # 简单转义：FTS5 对特殊字符敏感，用户查询做引号包裹
        q = query.strip().replace('"', '""')
        if not q:
            return []
        cur = conn.execute(
            "SELECT chunk_id, doc_id FROM kb_chunks_fts WHERE kb_chunks_fts MATCH ? LIMIT ?",
            (q, int(limit)),
        )
        return [{"chunk_id": row[0], "doc_id": row[1], "rank": 0.0} for row in cur.fetchall()]
    except sqlite3.OperationalError:
        # MATCH 语法失败时回退 LIKE
        cur = conn.execute(
            "SELECT chunk_id, doc_id FROM kb_chunks_fts WHERE content LIKE ? LIMIT ?",
            (f"%{query[:200]}%", int(limit)),
        )
        return [{"chunk_id": r[0], "doc_id": r[1], "rank": 0.0} for r in cur.fetchall()]
    finally:
        conn.close()


def fts_get_content_map(db_path: Path, chunk_ids: List[str]) -> Dict[str, str]:
    if not chunk_ids or not db_path.exists():
        return {}
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" * len(chunk_ids))
        cur = conn.execute(
            f"SELECT chunk_id, content FROM kb_chunks_fts WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        return {str(r[0]): str(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()
