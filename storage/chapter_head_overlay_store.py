"""
最新章节派生状态（head overlay）存储。

用途：
1) 把“当前最新章可被重写推翻”的派生分析从主 state JSON 中拆分出来；
2) 在续写下一章前再合并进主 state，随后清空 overlay。
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import PROJECTS_ROOT


class ChapterHeadOverlayStore:
    """按项目存储 chapter_head_overlay.json。"""

    FILE_NAME = "chapter_head_overlay.json"

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        d = self.root / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, project_id: str) -> Path:
        return self._project_dir(project_id) / self.FILE_NAME

    def load(self, project_id: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(project_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data

    def save(self, project_id: str, payload: Dict[str, Any]) -> Path:
        path = self.path_for(project_id)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def clear(self, project_id: str) -> None:
        path = self.path_for(project_id)
        if path.exists():
            path.unlink()
