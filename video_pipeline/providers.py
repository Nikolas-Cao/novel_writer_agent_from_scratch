"""
云端视频/TTS 提供方适配（generic HTTP），支持重试与降级占位输出。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import httpx

from config import (
    TTS_API_BASE_URL,
    TTS_API_KEY,
    TTS_MAX_RETRIES,
    TTS_MODEL,
    TTS_TIMEOUT_S,
    VIDEO_API_BASE_URL,
    VIDEO_API_KEY,
    VIDEO_MAX_RETRIES,
    VIDEO_MODEL,
    VIDEO_TIMEOUT_S,
)
from .storage import VideoAssetStore

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class GenericCloudVideoProvider:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or VIDEO_API_BASE_URL).strip()
        self.api_key = (api_key or VIDEO_API_KEY).strip()
        self.model = model or VIDEO_MODEL
        self.timeout_s = float(timeout_s or VIDEO_TIMEOUT_S)
        self.max_retries = int(max_retries or VIDEO_MAX_RETRIES)

    async def generate_shot(
        self,
        *,
        project_id: str,
        chapter_index: int,
        shot: Dict[str, Any],
        store: VideoAssetStore,
    ) -> Dict[str, Any]:
        shot_id = str(shot.get("shot_id") or "shot")
        payload = {
            "model": self.model,
            "prompt": shot.get("prompt", ""),
            "duration_s": float(shot.get("duration_s", 6.0)),
            "seed": int(shot.get("seed", 0)),
            "references": list(shot.get("references") or []),
        }
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._generate_once(project_id, chapter_index, shot_id, payload, store)
            except Exception as exc:
                last_err = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(3.0, 0.5 * (attempt + 1)))
        raise ProviderError(f"video shot generation failed shot={shot_id}: {last_err}")

    async def _generate_once(
        self,
        project_id: str,
        chapter_index: int,
        shot_id: str,
        payload: Dict[str, Any],
        store: VideoAssetStore,
    ) -> Dict[str, Any]:
        # 未配置云端地址时，回落为可追踪占位资产，便于本地联调。
        if not self.base_url:
            ref = store.write_text(
                project_id,
                chapter_index,
                f"{shot_id}.video.txt",
                f"[MOCK VIDEO]\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
            )
            return {"shot_id": shot_id, "asset_ref": ref, "duration_s": payload["duration_s"], "mock": True}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url.rstrip('/')}/generate-video"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # 兼容返回 url/base64/content 三种格式。
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            raw = str(data["content"]).encode("utf-8")
            ref = store.write_bytes(project_id, chapter_index, f"{shot_id}.mp4", raw)
            return {"shot_id": shot_id, "asset_ref": ref, "duration_s": payload["duration_s"], "mock": False}
        if isinstance(data, dict) and isinstance(data.get("video_url"), str):
            ref = store.write_text(project_id, chapter_index, f"{shot_id}.video.url.txt", data["video_url"])
            return {"shot_id": shot_id, "asset_ref": ref, "duration_s": payload["duration_s"], "mock": False}
        raise ProviderError(f"unsupported video response for shot={shot_id}")


class GenericCloudTTSProvider:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or TTS_API_BASE_URL).strip()
        self.api_key = (api_key or TTS_API_KEY).strip()
        self.model = model or TTS_MODEL
        self.timeout_s = float(timeout_s or TTS_TIMEOUT_S)
        self.max_retries = int(max_retries or TTS_MAX_RETRIES)

    async def synthesize_line(
        self,
        *,
        project_id: str,
        chapter_index: int,
        line: Dict[str, Any],
        voice_profile: Dict[str, Any],
        store: VideoAssetStore,
    ) -> Dict[str, Any]:
        line_id = str(line.get("line_id") or "line")
        payload = {
            "model": self.model,
            "text": line.get("text", ""),
            "speaker": line.get("speaker", "narrator"),
            "emotion": line.get("emotion", "neutral"),
            "target_seconds": float(line.get("target_seconds", 2.0)),
            "voice_id": voice_profile.get("voice_id", "default"),
            "speed": float(voice_profile.get("speed", 1.0)),
            "pitch": float(voice_profile.get("pitch", 0.0)),
            "style": voice_profile.get("style", "cinematic"),
        }
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._synthesize_once(project_id, chapter_index, line_id, payload, store)
            except Exception as exc:
                last_err = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(2.5, 0.4 * (attempt + 1)))
        raise ProviderError(f"tts generation failed line={line_id}: {last_err}")

    async def _synthesize_once(
        self,
        project_id: str,
        chapter_index: int,
        line_id: str,
        payload: Dict[str, Any],
        store: VideoAssetStore,
    ) -> Dict[str, Any]:
        if not self.base_url:
            ref = store.write_text(
                project_id,
                chapter_index,
                f"{line_id}.tts.txt",
                f"[MOCK AUDIO]\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
            )
            return {
                "line_id": line_id,
                "asset_ref": ref,
                "duration_s": payload["target_seconds"],
                "sample_rate": 48000,
                "channels": 2,
                "mock": True,
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url.rstrip('/')}/generate-tts"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("audio_content"), str):
            ref = store.write_bytes(project_id, chapter_index, f"{line_id}.wav", data["audio_content"].encode("utf-8"))
            return {
                "line_id": line_id,
                "asset_ref": ref,
                "duration_s": payload["target_seconds"],
                "sample_rate": int(data.get("sample_rate") or 48000),
                "channels": int(data.get("channels") or 2),
                "mock": False,
            }
        if isinstance(data, dict) and isinstance(data.get("audio_url"), str):
            ref = store.write_text(project_id, chapter_index, f"{line_id}.audio.url.txt", data["audio_url"])
            return {
                "line_id": line_id,
                "asset_ref": ref,
                "duration_s": payload["target_seconds"],
                "sample_rate": int(data.get("sample_rate") or 48000),
                "channels": int(data.get("channels") or 2),
                "mock": False,
            }
        raise ProviderError(f"unsupported tts response for line={line_id}")
