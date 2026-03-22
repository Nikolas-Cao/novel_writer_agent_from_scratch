"""全局知识库混合检索：分层资产 + Chroma + FTS。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from config import KB_RETRIEVE_CHROMA_K, KB_RETRIEVE_FINAL_K, KB_RETRIEVE_FTS_K
from knowledge_base.fts_index import fts_get_content_map, fts_search
from knowledge_base.store import KnowledgeBaseStore
from rag.global_kb_chroma import GlobalKbChroma


def _format_assets_brief(assets: Dict[str, Any], max_chars: int = 6000) -> str:
    parts: List[str] = []
    gs = (assets.get("global_summary") or "").strip()
    if gs:
        parts.append("【全书概要】\n" + gs)
    ch = assets.get("characters") or []
    if ch:
        lines = []
        for it in ch[:40]:
            if isinstance(it, dict):
                lines.append(
                    f"- {it.get('name','')}：{it.get('role','')} {it.get('relations','')}"
                )
            else:
                lines.append(f"- {it}")
        parts.append("【人物】\n" + "\n".join(lines))
    tl = assets.get("timeline") or []
    if tl:
        lines = []
        for it in tl[:30]:
            if isinstance(it, dict):
                lines.append(f"- {it.get('event','')}")
            else:
                lines.append(f"- {it}")
        parts.append("【时间线】\n" + "\n".join(lines))
    wr = assets.get("world_rules") or []
    if wr:
        lines = []
        for it in wr[:25]:
            if isinstance(it, dict):
                lines.append(f"- {it.get('rule','')}")
            else:
                lines.append(f"- {it}")
        parts.append("【设定规则】\n" + "\n".join(lines))
    cf = assets.get("core_facts") or []
    if cf:
        lines = []
        for it in cf[:40]:
            if isinstance(it, dict):
                lines.append(f"- {it.get('fact','')}")
            else:
                lines.append(f"- {it}")
        parts.append("【核心事实】\n" + "\n".join(lines))
    text = "\n\n".join(parts).strip()
    return text[:max_chars]


class GlobalKbRetriever:
    def __init__(self, vector_root: Path, kb_store: KnowledgeBaseStore) -> None:
        self.vector_root = Path(vector_root)
        self.store = kb_store
        self.chroma = GlobalKbChroma(self.vector_root)

    def _fts_path(self, kb_id: str) -> Path:
        return self.store.kb_dir(kb_id) / "search.sqlite"

    def hybrid_retrieve(
        self,
        kb_ids: List[str],
        query: str,
        *,
        doc_id: Optional[str] = None,
        k_chroma: int = KB_RETRIEVE_CHROMA_K,
        k_fts: int = KB_RETRIEVE_FTS_K,
        k_final: int = KB_RETRIEVE_FINAL_K,
    ) -> List[Dict[str, Any]]:
        if not kb_ids or not query.strip():
            return []
        seen = set()
        scored: List[tuple[float, Dict[str, Any]]] = []
        for kb_id in kb_ids:
            fts_db = self._fts_path(kb_id)
            fts_hits = fts_search(fts_db, query, limit=k_fts)
            chunk_ids = [h["chunk_id"] for h in fts_hits]
            texts = fts_get_content_map(fts_db, chunk_ids)
            for h in fts_hits:
                cid = h["chunk_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                txt = texts.get(cid) or ""
                rank = float(h.get("rank") or 0.0)
                scored.append((2.0 - min(rank, 2.0), {"kb_id": kb_id, "chunk_id": cid, "text": txt, "source": "fts"}))

            chroma_hits = self.chroma.query(kb_id, query, k=k_chroma, doc_id=doc_id)
            for h in chroma_hits:
                cid = h["chunk_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                scored.append((1.0, {"kb_id": kb_id, "chunk_id": cid, "text": h.get("text") or "", "source": "chroma"}))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:k_final]]

    def assets_layer_text(self, kb_ids: List[str]) -> str:
        blocks = []
        for kb_id in kb_ids:
            assets = self.store.load_assets(kb_id)
            if assets.get("status") in ("none", "invalid"):
                continue
            b = _format_assets_brief(assets)
            if b.strip():
                blocks.append(f"=== 知识集 {kb_id} ===\n{b}")
        return "\n\n".join(blocks).strip()
