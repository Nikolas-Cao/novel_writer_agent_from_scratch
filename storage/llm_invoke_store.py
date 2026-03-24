"""
DEBUG 模式下的 LLM 调用结果落盘（按项目维度）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import PROJECTS_ROOT

_PURPOSE_BY_NODE = {
    "outline_short_node": ("outline_short", "outline_skeleton"),
    "outline_skeleton_lite_node": ("outline_skeleton_lite", "outline_skeleton"),
    "outline_extend_window_node": ("outline_extend_window", "outline_skeleton"),
    "outline_finalize_node": ("outline_finalize", "outline_skeleton"),
    "plan_outline_node": ("plan_outline", "outline_skeleton"),
    "write_chapter_node": ("write_chapter", "chapter_generation"),
    "refine_chapter_node": ("refine_chapter", "chapter_refinement"),
    "rewrite_with_feedback_node": ("rewrite_chapter", "chapter_refinement"),
    "post_chapter_node": ("post_chapter_analysis", "postprocess"),
    "generate_plot_ideas_node": ("generate_plot_ideas", "outline_skeleton"),
    "update_outline_from_feedback_node": ("update_outline", "outline_skeleton"),
    "identify_illustration_points_node": ("identify_illustration_points", "illustration"),
}


def normalize_token(value: str) -> str:
    raw = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def resolve_purpose(node_name: str) -> Dict[str, str]:
    if node_name in _PURPOSE_BY_NODE:
        purpose, purpose_group = _PURPOSE_BY_NODE[node_name]
        return {"purpose": purpose, "purpose_group": purpose_group}
    normalized = normalize_token(node_name)
    return {"purpose": normalized, "purpose_group": "unknown"}


def utc_ts_for_filename(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z").replace(":", "")


class LLMInvokeStore:
    """将每次调用写为单独 JSON 文件。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir_for_project(self, project_id: str) -> Path:
        path = self.root / project_id / "llm_invoke_results"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_record(self, project_id: str, record: Dict[str, Any]) -> Path:
        invoke_id = str(record.get("invoke_id") or "")
        short_id = normalize_token(invoke_id[:8] or "noid")
        status = normalize_token(str(record.get("status") or "unknown"))
        purpose = normalize_token(str(record.get("purpose") or "unknown"))
        ts = utc_ts_for_filename(datetime.now(timezone.utc))
        path = self._dir_for_project(project_id) / f"{ts}_{status}_{purpose}_{short_id}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
