"""
章节视频流水线的产物存储。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import PROJECTS_ROOT


class VideoAssetStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def chapter_video_dir(self, project_id: str, chapter_index: int) -> Path:
        d = self.root / project_id / "videos" / f"{int(chapter_index):03d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, project_id: str, chapter_index: int, name: str, payload: Dict[str, Any]) -> str:
        out = self.chapter_video_dir(project_id, chapter_index) / name
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.to_ref(out)

    def write_text(self, project_id: str, chapter_index: int, name: str, text: str) -> str:
        out = self.chapter_video_dir(project_id, chapter_index) / name
        out.write_text(text or "", encoding="utf-8")
        return self.to_ref(out)

    def write_bytes(self, project_id: str, chapter_index: int, name: str, data: bytes) -> str:
        out = self.chapter_video_dir(project_id, chapter_index) / name
        out.write_bytes(data)
        return self.to_ref(out)

    def to_ref(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")
