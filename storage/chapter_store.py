"""
章节存储：按项目/章号将正文保存为 Markdown 文件。
"""
from pathlib import Path
from typing import Optional

from config import PROJECTS_ROOT


class ChapterStore:
    """按章读写 Markdown 正文。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, project_id: str, chapter_index: int) -> Path:
        chapters_dir = self.root / project_id / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        return chapters_dir / f"{chapter_index:03d}.md"

    def ref_for(self, project_id: str, chapter_index: int) -> str:
        return f"{project_id}/chapters/{chapter_index:03d}.md"

    def save(self, project_id: str, chapter_index: int, content: str) -> str:
        path = self.path_for(project_id, chapter_index)
        path.write_text(content, encoding="utf-8")
        return self.ref_for(project_id, chapter_index)

    def load(self, project_id: str, chapter_index: int) -> str:
        path = self.path_for(project_id, chapter_index)
        if not path.exists():
            raise FileNotFoundError(f"Chapter file not found: {path}")
        return path.read_text(encoding="utf-8")

    def load_by_ref(self, ref: str) -> str:
        path = self.root / ref
        if not path.exists():
            raise FileNotFoundError(f"Chapter file not found by ref: {ref}")
        return path.read_text(encoding="utf-8")
