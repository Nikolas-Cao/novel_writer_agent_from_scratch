"""
阶段 7 节点：将图片 markdown 语法插入章节正文并写回存储。
"""
from typing import Any, Dict, List, Optional

from state import NovelProjectState
from storage import ChapterStore


def _insert_after_anchor(text: str, anchor: str, image_md: str) -> str:
    if anchor and anchor in text:
        return text.replace(anchor, f"{anchor}\n\n{image_md}\n", 1)
    return text + f"\n\n{image_md}\n"


async def insert_illustrations_into_chapter_node(
    state: NovelProjectState,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    if not state.get("enable_chapter_illustrations", False):
        return {}

    store = chapter_store or ChapterStore()
    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    assets = list(state.get("illustration_assets", []))
    if not project_id:
        raise ValueError("project_id is required.")
    if not assets:
        return {}

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    inserted_paths: List[str] = []
    for a in assets:
        alt = str(a.get("alt") or "章节插图")
        path = str(a.get("image_path") or "")
        anchor = str(a.get("anchor_text") or "")
        if not path:
            continue
        image_md = f"![{alt}]({path})"
        chapter_text = _insert_after_anchor(chapter_text, anchor, image_md)
        inserted_paths.append(path)

    ref = store.save(project_id, current_idx, chapter_text)

    chapters = list(state.get("chapters", []))
    for item in chapters:
        if int(item.get("index", -1)) == current_idx:
            item["path_or_content_ref"] = ref
            item["images_refs"] = inserted_paths
            item["summary"] = chapter_text[:120]
            item["word_count"] = len(chapter_text.replace("\n", ""))

    return {
        "current_chapter_final": chapter_text,
        "chapters": chapters,
    }
