"""
RAG 索引器：使用 Chroma 本地持久化目录存储章节摘要/大纲片段。
"""
import shutil
import time
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.api.models.Collection import Collection

from config import VECTOR_STORE_DIR
from rag.embedding import LocalHashEmbeddingFunction


class LocalRagIndexer:
    """面向项目维度的本地 RAG 索引。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or VECTOR_STORE_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.embedding_fn = LocalHashEmbeddingFunction()

    def _project_path(self, project_id: str) -> Path:
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _collection(self, project_id: str) -> Collection:
        client = chromadb.PersistentClient(path=str(self._project_path(project_id)))
        return client.get_or_create_collection(
            name="novel_rag",
            embedding_function=self.embedding_fn,
        )

    def add_chapter_summary(self, project_id: str, chapter_index: int, summary_text: str) -> str:
        coll = self._collection(project_id)
        doc_id = f"summary-{chapter_index}"
        coll.upsert(
            ids=[doc_id],
            documents=[summary_text],
            metadatas=[
                {
                    "project_id": project_id,
                    "kind": "chapter_summary",
                    "chapter_index": int(chapter_index),
                }
            ],
        )
        return doc_id

    def add_outline_chunk(
        self,
        project_id: str,
        chunk_text: str,
        volume_idx: int,
        chapter_idx: int,
    ) -> str:
        coll = self._collection(project_id)
        doc_id = f"outline-v{volume_idx}-c{chapter_idx}"
        updated_at = time.time()
        coll.upsert(
            ids=[doc_id],
            documents=[chunk_text],
            metadatas=[
                {
                    "project_id": project_id,
                    "kind": "outline_chunk",
                    "volume_index": int(volume_idx),
                    "chapter_index": int(chapter_idx),
                    "updated_at": float(updated_at),
                }
            ],
        )
        return doc_id

    def upsert_outline_chunks_for_chapters(
        self,
        project_id: str,
        outline_structure: dict,
        chapter_indices: set[int],
    ) -> int:
        """将指定章的 outline chunk 回写到 RAG（用于反馈后大纲同步）。"""
        targets = {int(i) for i in chapter_indices if int(i) >= 0}
        if not targets:
            return 0

        global_idx = 0
        upserted = 0
        for vol_idx, volume in enumerate(outline_structure.get("volumes", [])):
            if not isinstance(volume, dict):
                continue
            for chapter in volume.get("chapters", []):
                if not isinstance(chapter, dict):
                    global_idx += 1
                    continue
                if global_idx in targets:
                    title = chapter.get("title") or f"第{global_idx + 1}章"
                    points = chapter.get("points") if isinstance(chapter.get("points"), list) else []
                    chunk_text = title + "\n" + "\n".join(f"- {p}" for p in points)
                    self.add_outline_chunk(project_id, chunk_text, volume_idx=vol_idx, chapter_idx=global_idx)
                    upserted += 1
                global_idx += 1
        return upserted

    def upsert_outline_chunks_range(
        self,
        project_id: str,
        outline_structure: dict,
        start_index: int,
        end_index: int,
    ) -> int:
        """将 [start_index, end_index] 区间的大纲 chunk 回写到 RAG。"""
        s = int(start_index)
        e = int(end_index)
        if e < s:
            return 0
        return self.upsert_outline_chunks_for_chapters(
            project_id=project_id,
            outline_structure=outline_structure,
            chapter_indices=set(range(s, e + 1)),
        )

    def delete_chapter_summaries_from(self, project_id: str, start_index: int) -> None:
        coll = self._collection(project_id)
        begin = int(start_index)
        records = coll.get(include=["metadatas"])
        ids = records.get("ids") or []
        metadatas = records.get("metadatas") or []
        to_delete = []
        for doc_id, meta in zip(ids, metadatas):
            if not meta:
                continue
            if meta.get("kind") != "chapter_summary":
                continue
            chapter_idx = int(meta.get("chapter_index", -1))
            if chapter_idx >= begin:
                to_delete.append(doc_id)
        if to_delete:
            coll.delete(ids=to_delete)

    def delete_project(self, project_id: str) -> None:
        project_path = self.root / project_id
        if not project_path.exists():
            return
        # Windows 下 Chroma 可能短暂持有文件句柄，做一次短重试并最终忽略删除失败。
        for _ in range(3):
            try:
                shutil.rmtree(project_path)
                return
            except PermissionError:
                time.sleep(0.2)
