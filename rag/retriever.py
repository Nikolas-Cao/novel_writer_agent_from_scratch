"""
RAG 检索器：读取本地 Chroma 索引，返回写章所需上下文。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb

from config import VECTOR_STORE_DIR
from rag.embedding import LocalHashEmbeddingFunction


class LocalRagRetriever:
    """按项目检索摘要与当前章大纲片段。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or VECTOR_STORE_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.embedding_fn = LocalHashEmbeddingFunction()

    def _project_path(self, project_id: str) -> Path:
        return self.root / project_id

    def _collection(self, project_id: str):
        project_path = self._project_path(project_id)
        if not project_path.exists():
            return None
        client = chromadb.PersistentClient(path=str(project_path))
        return client.get_or_create_collection(
            name="novel_rag",
            embedding_function=self.embedding_fn,
        )

    def retrieve_for_chapter(
        self,
        project_id: str,
        current_chapter_index: int,
        k_chapters: int = 5,
        k_outline: int = 1,
    ) -> Dict[str, Any]:
        coll = self._collection(project_id)
        if coll is None:
            return {"summaries": [], "outline_chunk": ""}

        records = coll.get(include=["documents", "metadatas"])
        ids = records.get("ids") or []
        documents = records.get("documents") or []
        metadatas = records.get("metadatas") or []

        summaries: List[Dict[str, Any]] = []
        outline_candidates: List[Dict[str, Any]] = []

        for doc_id, doc, meta in zip(ids, documents, metadatas):
            if not meta:
                continue
            kind = meta.get("kind")
            chapter_idx = int(meta.get("chapter_index", -1))
            if kind == "chapter_summary" and chapter_idx < int(current_chapter_index):
                summaries.append({"chapter_index": chapter_idx, "text": doc})
            elif kind == "outline_chunk" and chapter_idx == int(current_chapter_index):
                outline_candidates.append(
                    {
                        "chapter_index": chapter_idx,
                        "text": doc,
                        "doc_id": str(doc_id),
                        "updated_at": float(meta.get("updated_at", 0.0) or 0.0),
                    }
                )

        summaries.sort(key=lambda x: x["chapter_index"])
        summaries = summaries[-int(k_chapters):] if k_chapters > 0 else []

        outline_text = ""
        if k_outline > 0 and outline_candidates:
            # 兼容历史脏数据：同一章可能存在多条 outline_chunk，优先取最新写入。
            outline_candidates.sort(key=lambda item: (item["updated_at"], item["doc_id"]), reverse=True)
            picked: List[str] = []
            seen_text = set()
            for item in outline_candidates:
                text = str(item.get("text") or "").strip()
                if not text or text in seen_text:
                    continue
                seen_text.add(text)
                picked.append(text)
                if len(picked) >= int(k_outline):
                    break
            outline_text = "\n\n".join(picked)

        return {
            "summaries": summaries,
            "outline_chunk": outline_text,
        }
