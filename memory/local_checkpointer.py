"""
本地 checkpoint 工具：
1) FileSaver 风格 JSON 状态存储（本地文件）
2) 可选 LangGraph SqliteSaver（若环境已安装 sqlite checkpointer 包）
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from config import CHECKPOINT_DIR


class LocalFileCheckpointer:
    """最小可用本地状态持久化，按 thread_id 写 JSON。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or CHECKPOINT_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    def _state_path(self, thread_id: str) -> Path:
        return self.root / f"{thread_id}.json"

    def save_state(self, thread_id: str, state: Dict[str, Any]) -> Path:
        path = self._state_path(thread_id)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        path = self._state_path(thread_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def try_create_sqlite_checkpointer(
    sqlite_file: Optional[Path] = None,
) -> Optional[Any]:
    """
    尝试创建 LangGraph SqliteSaver。
    若未安装相关包，返回 None，由调用方回退到 LocalFileCheckpointer。
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
    except Exception:
        return None

    checkpoint_root = Path(CHECKPOINT_DIR)
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    db_file = Path(sqlite_file or (checkpoint_root / "langgraph_checkpoint.sqlite"))
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    return SqliteSaver(conn)
