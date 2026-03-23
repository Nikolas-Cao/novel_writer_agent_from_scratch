"""
项目事件日志存储：按项目写入 NDJSON。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import PROJECTS_ROOT


class EventLogStore:
    """按项目记录与查询事件日志。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, project_id: str) -> Path:
        project_dir = self.root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "event_logs.ndjson"

    def append_event(self, project_id: str, event: Dict[str, Any]) -> None:
        path = self.path_for(project_id)
        payload = json.dumps(event, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")

    def list_events(self, project_id: str, chapter_index: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        path = self.path_for(project_id)
        if not path.exists():
            return []
        items: List[Dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if chapter_index is not None and obj.get("chapter_index") != int(chapter_index):
                continue
            items.append(obj)
        items.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
        if limit <= 0:
            return items
        return items[: int(limit)]
