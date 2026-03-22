"""
根据插图点调用 OpenAI 生图；失败则跳过插图（不生成占位图）。
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from config import IMAGE_GEN_MODEL, PROJECTS_ROOT
from graph.llm import accumulate_usage_into_state
from illustration.openai_generate import generate_openai_chapter_image
from state import NovelProjectState


async def fetch_or_generate_images_node(
    state: NovelProjectState,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    if not state.get("enable_chapter_illustrations", False):
        return {"illustration_assets": [], "illustration_asset": {}}

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not project_id:
        raise ValueError("project_id is required.")

    override_root = state.get("projects_root_override")
    root = Path(project_root or override_root or PROJECTS_ROOT)

    ill_prompt = (state.get("illustration_prompt") or "").strip()
    points = list(state.get("illustration_points", []))
    point_meta: Dict[str, Any] = dict(state.get("illustration_point") or {})

    if not ill_prompt and points:
        ill_prompt = str(points[0].get("image_query") or "").strip()
    if not point_meta and points:
        p0 = points[0]
        point_meta = {
            "position_describe": p0.get("position_describe", "段落后插图"),
            "anchor_text": p0.get("anchor_text", ""),
            "alt": p0.get("alt", "章节插图"),
        }

    if not ill_prompt:
        logger.info(
            "[fetch_or_generate_images] skip: no prompt project=%s chapter_index=%s",
            project_id,
            current_idx,
        )
        return {"illustration_assets": [], "illustration_asset": {}}

    logger.info(
        "[fetch_or_generate_images] openai_generate start project=%s chapter_index=%s",
        project_id,
        current_idx,
    )
    result = generate_openai_chapter_image(
        project_root=root,
        project_id=project_id,
        chapter_index=current_idx,
        image_index=1,
        prompt=ill_prompt,
    )
    if not result:
        logger.warning(
            "[fetch_or_generate_images] skipped (generation failed) project=%s",
            project_id,
        )
        return {"illustration_assets": [], "illustration_asset": {}}

    rel_path, mime, in_tok, out_tok = result
    accumulate_usage_into_state(
        state,
        str(IMAGE_GEN_MODEL),
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    alt = str(point_meta.get("alt") or "章节插图")
    anchor = str(point_meta.get("anchor_text") or "")
    asset: Dict[str, Any] = {
        "position_describe": point_meta.get("position_describe", "段落后插图"),
        "anchor_text": anchor,
        "image_query": ill_prompt,
        "generation_prompt": ill_prompt,
        "alt": alt,
        "image_path": rel_path,
        "source": "openai",
        "mime": mime,
        "usage_input_tokens": in_tok,
        "usage_output_tokens": out_tok,
    }
    assets: List[Dict[str, Any]] = [asset]
    logger.info(
        "[fetch_or_generate_images] done project=%s path=%s tokens in=%s out=%s",
        project_id,
        rel_path,
        in_tok,
        out_tok,
    )
    return {
        "illustration_assets": assets,
        "illustration_asset": asset,
        "token_usage": state.get("token_usage") or {},
    }


# 与计划文档中的节点名对齐（同一实现）
generate_chapter_illustration_node = fetch_or_generate_images_node
