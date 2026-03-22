"""全局知识库 Chroma 集合（每知识集独立持久化目录）。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.api.models.Collection import Collection

from rag.embedding import LocalHashEmbeddingFunction


class GlobalKbChroma:
    def __init__(self, vector_root: Path) -> None:
        self.vector_root = Path(vector_root)
        self.embedding_fn = LocalHashEmbeddingFunction()

    def _kb_chroma_path(self, kb_id: str) -> Path:
        return self.vector_root / "global_kb" / kb_id / "chroma"

    def _collection(self, kb_id: str) -> Collection:
        path = self._kb_chroma_path(kb_id)
        path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(path))
        return client.get_or_create_collection(
            name="global_kb_chunks",
            embedding_function=self.embedding_fn,
        )

    def upsert_chunks(
        self,
        kb_id: str,
        ids: List[str],
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ) -> None:
        if not ids:
            return
        coll = self._collection(kb_id)
        now = time.time()
        metas = []
        for m in metadatas:
            mm = dict(m)
            mm["updated_at"] = float(now)
            metas.append(mm)
        coll.upsert(ids=ids, documents=documents, metadatas=metas)

    def delete_by_doc(self, kb_id: str, doc_id: str) -> None:
        path = self._kb_chroma_path(kb_id)
        if not path.exists():
            return
        coll = self._collection(kb_id)
        records = coll.get(include=["metadatas"])
        ids = records.get("ids") or []
        metas = records.get("metadatas") or []
        to_del = [i for i, m in zip(ids, metas) if m and str(m.get("doc_id")) == doc_id]
        if to_del:
            coll.delete(ids=to_del)

    def query(self, kb_id: str, query_text: str, k: int = 12, doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not query_text.strip():
            return []
        path = self._kb_chroma_path(kb_id)
        if not path.exists():
            return []
        coll = self._collection(kb_id)
        where = {"doc_id": doc_id} if doc_id else None
        try:
            res = coll.query(
                query_texts=[query_text],
                n_results=int(k),
                where=where,
            )
        except Exception:
            res = coll.query(query_texts=[query_text], n_results=int(k))
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        out = []
        for i, doc, meta in zip(ids, docs, metas):
            out.append(
                {
                    "chunk_id": str(i),
                    "text": doc or "",
                    "metadata": meta or {},
                }
            )
        return out
