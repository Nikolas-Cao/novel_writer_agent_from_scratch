"""
阶段 7 节点：根据插图点，先搜索后生图，返回本地图片路径。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import PROJECTS_ROOT
from illustration import generate_image, search_image
from state import NovelProjectState


async def fetch_or_generate_images_node(
    state: NovelProjectState,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    if not state.get("enable_chapter_illustrations", False):
        return {"illustration_assets": []}

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    points = list(state.get("illustration_points", []))
    if not project_id:
        raise ValueError("project_id is required.")

    override_root = state.get("projects_root_override")
    root = Path(project_root or override_root or PROJECTS_ROOT)
    assets: List[Dict[str, Any]] = []
    for i, p in enumerate(points, 1):
        query = str(p.get("image_query") or "小说插图")
        local_path = search_image(query)
        source = "search"
        if not local_path:
            source = "generate"
            local_path = generate_image(
                project_root=root,
                project_id=project_id,
                chapter_index=current_idx,
                image_index=i,
                prompt=query,
            )
        assets.append(
            {
                "position_describe": p.get("position_describe", "段落后插图"),
                "anchor_text": p.get("anchor_text", ""),
                "image_query": query,
                "alt": p.get("alt", "章节插图"),
                "image_path": local_path,
                "source": source,
            }
        )

    return {"illustration_assets": assets}
