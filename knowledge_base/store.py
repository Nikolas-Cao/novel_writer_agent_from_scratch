"""
全局知识库存储：registry、每知识集目录、原始文件、任务状态、分层 assets。
根目录：{VECTOR_STORE_DIR}/global_kb/
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REGISTRY_FILENAME = "registry.json"


def _now_ts() -> int:
    return int(time.time())


class KnowledgeBaseStore:
    def __init__(self, vector_root: Path) -> None:
        self.root = Path(vector_root) / "global_kb"
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.root / REGISTRY_FILENAME

    def _load_registry(self) -> Dict[str, Any]:
        if not self._registry_path.exists():
            return {"bases": []}
        try:
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"bases": []}

    def _save_registry(self, data: Dict[str, Any]) -> None:
        self._registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def kb_dir(self, kb_id: str) -> Path:
        return self.root / kb_id

    def ensure_kb_dir(self, kb_id: str) -> Path:
        d = self.kb_dir(kb_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "raw").mkdir(exist_ok=True)
        (d / "jobs").mkdir(exist_ok=True)
        (d / "assets").mkdir(exist_ok=True)
        return d

    def list_bases(self) -> List[Dict[str, Any]]:
        reg = self._load_registry()
        bases = list(reg.get("bases") or [])
        # 合并磁盘上存在但未登记的目录（容错）
        known = {b.get("kb_id") for b in bases if b.get("kb_id")}
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in known:
                if (p / "meta.json").exists():
                    try:
                        meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
                        bases.append(meta)
                    except (json.JSONDecodeError, OSError):
                        pass
        bases.sort(key=lambda x: int(x.get("created_at", 0)))
        return bases

    def get_base(self, kb_id: str) -> Optional[Dict[str, Any]]:
        for b in self.list_bases():
            if b.get("kb_id") == kb_id:
                return b
        meta_path = self.kb_dir(kb_id) / "meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def create_base(self, name: str) -> Dict[str, Any]:
        kb_id = f"kb-{uuid.uuid4().hex[:12]}"
        self.ensure_kb_dir(kb_id)
        rec = {
            "kb_id": kb_id,
            "name": name.strip() or kb_id,
            "created_at": _now_ts(),
            "updated_at": _now_ts(),
            "status": "empty",
            "document_count": 0,
        }
        (self.kb_dir(kb_id) / "meta.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        reg = self._load_registry()
        bases = list(reg.get("bases") or [])
        bases.append(rec)
        reg["bases"] = bases
        self._save_registry(reg)
        return rec

    def update_base_meta(self, kb_id: str, **kwargs: Any) -> None:
        path = self.kb_dir(kb_id) / "meta.json"
        if not path.exists():
            return
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {"kb_id": kb_id}
        meta.update(kwargs)
        meta["updated_at"] = _now_ts()
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        reg = self._load_registry()
        bases = []
        found = False
        for b in reg.get("bases") or []:
            if b.get("kb_id") == kb_id:
                bases.append({**b, **kwargs, "updated_at": meta["updated_at"]})
                found = True
            else:
                bases.append(b)
        if not found:
            bases.append(meta)
        reg["bases"] = bases
        self._save_registry(reg)

    def list_documents(self, kb_id: str) -> List[Dict[str, Any]]:
        idx_path = self.kb_dir(kb_id) / "documents.json"
        if not idx_path.exists():
            return []
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            return list(data.get("documents") or [])
        except (json.JSONDecodeError, OSError):
            return []

    def upsert_document_record(self, kb_id: str, doc: Dict[str, Any]) -> None:
        idx_path = self.kb_dir(kb_id) / "documents.json"
        docs = self.list_documents(kb_id)
        doc_id = doc.get("doc_id")
        replaced = False
        out = []
        for d in docs:
            if d.get("doc_id") == doc_id:
                out.append({**d, **doc})
                replaced = True
            else:
                out.append(d)
        if not replaced:
            out.append(doc)
        idx_path.write_text(
            json.dumps({"documents": out}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.update_base_meta(kb_id, document_count=len(out), updated_at=_now_ts())

    def get_document(self, kb_id: str, doc_id: str) -> Optional[Dict[str, Any]]:
        for d in self.list_documents(kb_id):
            if d.get("doc_id") == doc_id:
                return d
        return None

    def job_path(self, kb_id: str, job_id: str) -> Path:
        return self.kb_dir(kb_id) / "jobs" / f"{job_id}.json"

    def save_job(self, kb_id: str, job: Dict[str, Any]) -> None:
        self.ensure_kb_dir(kb_id)
        p = self.job_path(kb_id, job["job_id"])
        p.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_job(self, kb_id: str, job_id: str) -> Optional[Dict[str, Any]]:
        p = self.job_path(kb_id, job_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def assets_path(self, kb_id: str) -> Path:
        """兼容旧版单文件 layers.json。"""
        return self.kb_dir(kb_id) / "assets" / "layers.json"

    def user_edited_assets_path(self, kb_id: str) -> Path:
        """用户手工编辑后的整库摘要文件。"""
        return self.kb_dir(kb_id) / "assets" / "user_edited_layers.json"

    def assets_doc_path(self, kb_id: str, doc_id: str) -> Path:
        return self.kb_dir(kb_id) / "assets" / f"layers_{doc_id}.json"

    def list_asset_layer_paths(self, kb_id: str) -> List[Path]:
        d = self.kb_dir(kb_id) / "assets"
        if not d.exists():
            return []
        return sorted(d.glob("layers_*.json"))

    def load_assets(self, kb_id: str) -> Dict[str, Any]:
        """优先读取用户手工编辑摘要；否则按文档合并或回退旧版单文件。"""
        user_path = self.user_edited_assets_path(kb_id)
        if user_path.exists():
            try:
                return self._normalize_assets_payload(json.loads(user_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                # 仅在手工覆盖文件损坏时回退自动构建资产，避免写作链路被单点文件阻断。
                pass
        paths = self.list_asset_layer_paths(kb_id)
        if paths:
            merged = self._merge_asset_files(paths)
            merged["status"] = merged.get("status") or "ready"
            return self._normalize_assets_payload(merged)
        p = self.assets_path(kb_id)
        if not p.exists():
            return self._normalize_assets_payload({"status": "none"})
        try:
            return self._normalize_assets_payload(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return {"status": "invalid"}

    @staticmethod
    def _merge_asset_files(paths: List[Path]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {
            "characters": [],
            "timeline": [],
            "world_rules": [],
            "core_facts": [],
            "leaf_summaries": [],
            "section_summaries": [],
            "global_summary": "",
            "by_doc": {},
        }
        gparts: List[str] = []
        for p in paths:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            doc_key = p.stem.replace("layers_", "", 1)
            merged["by_doc"][doc_key] = {
                "status": data.get("status"),
                "global_summary": data.get("global_summary", ""),
            }
            merged["characters"].extend(list(data.get("characters") or []))
            merged["timeline"].extend(list(data.get("timeline") or []))
            merged["world_rules"].extend(list(data.get("world_rules") or []))
            merged["core_facts"].extend(list(data.get("core_facts") or []))
            merged["leaf_summaries"].extend(list(data.get("leaf_summaries") or []))
            merged["section_summaries"].extend(list(data.get("section_summaries") or []))
            if data.get("global_summary"):
                gparts.append(str(data["global_summary"]))
        merged["global_summary"] = "\n\n".join(gparts)[:15000]
        return merged

    def load_assets_doc(self, kb_id: str, doc_id: str) -> Dict[str, Any]:
        p = self.assets_doc_path(kb_id, doc_id)
        if not p.exists():
            return {"status": "none"}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"status": "invalid"}

    def save_assets_doc(self, kb_id: str, doc_id: str, data: Dict[str, Any]) -> None:
        self.ensure_kb_dir(kb_id)
        p = self.assets_doc_path(kb_id, doc_id)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_user_edited_assets(self, kb_id: str, data: Dict[str, Any]) -> None:
        self.ensure_kb_dir(kb_id)
        p = self.user_edited_assets_path(kb_id)
        payload = self._normalize_assets_payload(data)
        payload["status"] = "ready"
        payload["by_doc"] = {}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _normalize_assets_payload(data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(data or {})
        normalized.setdefault("characters", [])
        normalized.setdefault("timeline", [])
        normalized.setdefault("world_rules", [])
        normalized.setdefault("core_facts", [])
        normalized.setdefault("leaf_summaries", [])
        normalized.setdefault("section_summaries", [])
        normalized.setdefault("global_summary", "")
        normalized.setdefault("status", "ready")
        normalized.setdefault("by_doc", {})
        return normalized

