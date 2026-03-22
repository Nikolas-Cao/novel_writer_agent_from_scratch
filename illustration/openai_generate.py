"""
使用 OpenAI Images API 生成章节插图并写入章节目录。

失败时返回 None（调用方跳过插图，不使用占位图）。
"""
from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx

from config import (
    IMAGE_GEN_API_KEY,
    IMAGE_GEN_BASE_URL,
    IMAGE_GEN_MODEL,
    IMAGE_GEN_SIZE,
    IMAGE_GEN_TIMEOUT_S,
)

logger = logging.getLogger(__name__)


def _safe_filename_part(text: str, max_len: int = 20) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (text or "").strip())
    t = (t[:max_len] or "img").strip("_")
    return t or "img"


def _usage_from_images_response(resp: Any) -> Tuple[int, int]:
    """
    OpenAI Images 响应中的 usage（gpt-image-1 等会返回；dall-e-2/3 可能为 None）。
    返回 (input_tokens, output_tokens)。若仅有 total_tokens 则记入 output 侧便于展示。
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    inp = int(getattr(u, "input_tokens", None) or 0)
    out = int(getattr(u, "output_tokens", None) or 0)
    total = int(getattr(u, "total_tokens", None) or 0)
    if inp or out:
        return inp, out
    if total:
        return 0, total
    return 0, 0


def chapter_images_dir(project_root: Path, project_id: str, chapter_index: int) -> Path:
    d = Path(project_root) / project_id / "chapters" / f"{int(chapter_index):03d}" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_openai_chapter_image(
    *,
    project_root: Path,
    project_id: str,
    chapter_index: int,
    image_index: int,
    prompt: str,
) -> Optional[Tuple[str, str, int, int]]:
    """
    调用 OpenAI 生图并保存到 projects/{project_id}/chapters/{idx}/images/。

    Returns:
        (relative_path_from_project_root, mime, input_tokens, output_tokens) 或失败时 None。
        relative_path 形如 project_id/chapters/000/images/xxx.png
        token 来自 API 的 usage 字段；无则 (0, 0)。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        logger.warning("[openai_generate] empty prompt, skip")
        return None
    if not project_id.strip():
        return None

    try:
        from openai import OpenAI
    except ImportError:
        logger.error("[openai_generate] openai package not available")
        return None

    client_kwargs = {"api_key": IMAGE_GEN_API_KEY or None}
    if IMAGE_GEN_BASE_URL:
        client_kwargs["base_url"] = IMAGE_GEN_BASE_URL

    client = OpenAI(**client_kwargs)
    ts = int(time.time())
    suffix = _safe_filename_part(prompt, 16)

    out_dir = chapter_images_dir(project_root, project_id, chapter_index)
    out_path: Optional[Path] = None

    try:
        # 兼容不同模型参数（gpt-image-1 / dall-e-3 等）
        kwargs = {
            "model": IMAGE_GEN_MODEL,
            "prompt": prompt[:4000],
            "n": 1,
        }
        if str(IMAGE_GEN_MODEL).lower().startswith("dall-e"):
            kwargs["size"] = IMAGE_GEN_SIZE

        try:
            resp = client.images.generate(**kwargs, timeout=IMAGE_GEN_TIMEOUT_S)
        except TypeError:
            resp = client.images.generate(**kwargs)

        in_tok, out_tok = _usage_from_images_response(resp)
        if in_tok or out_tok:
            logger.info(
                "[openai_generate] usage input=%s output=%s project=%s",
                in_tok,
                out_tok,
                project_id,
            )

        data0 = resp.data[0] if resp.data else None
        if not data0:
            logger.warning("[openai_generate] empty response data")
            return None

        mime = "image/png"
        ext = "png"
        raw: bytes

        if getattr(data0, "b64_json", None):
            raw = base64.b64decode(data0.b64_json)
        elif getattr(data0, "url", None):
            u = str(data0.url)
            with httpx.Client(timeout=IMAGE_GEN_TIMEOUT_S) as h:
                r = h.get(u)
                r.raise_for_status()
                raw = r.content
                ct = (r.headers.get("content-type") or "").lower()
                if "jpeg" in ct or "jpg" in ct:
                    ext = "jpg"
                    mime = "image/jpeg"
                elif "webp" in ct:
                    ext = "webp"
                    mime = "image/webp"
        else:
            logger.warning("[openai_generate] no url or b64 in response")
            return None

        filename = f"{int(chapter_index):03d}_{int(image_index):02d}_{ts}_{suffix}.{ext}"
        out_path = out_dir / filename
        out_path.write_bytes(raw)

        rel = f"{project_id}/chapters/{int(chapter_index):03d}/images/{out_path.name}"
        logger.info("[openai_generate] saved project=%s path=%s", project_id, rel)
        return rel, mime, in_tok, out_tok
    except Exception as exc:
        logger.warning("[openai_generate] failed project=%s: %s", project_id, exc)
        if out_path is not None and out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return None
