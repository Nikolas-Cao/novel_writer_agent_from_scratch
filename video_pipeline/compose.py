"""
时间线拼接与导出描述（EDL/Manifest）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from config import VIDEO_MAX_BUDGET, VIDEO_TARGET_FPS, VIDEO_TARGET_RESOLUTION


def build_timeline_manifest(
    *,
    project_id: str,
    chapter_index: int,
    shots: List[Dict[str, Any]],
    lines: List[Dict[str, Any]],
    qc_report: Dict[str, Any],
) -> Dict[str, Any]:
    clips: List[Dict[str, Any]] = []
    cursor = 0.0
    for s in shots:
        dur = float(s.get("duration_s") or 0.0)
        clips.append(
            {
                "clip_id": str(s.get("shot_id")),
                "video_ref": s.get("asset_ref"),
                "start_sec": round(cursor, 2),
                "end_sec": round(cursor + dur, 2),
                "duration_s": round(dur, 2),
                "camera": s.get("camera"),
            }
        )
        cursor += dur

    audio_tracks: List[Dict[str, Any]] = []
    for ln in lines:
        audio_tracks.append(
            {
                "line_id": ln.get("line_id"),
                "speaker": ln.get("speaker"),
                "audio_ref": ln.get("asset_ref"),
                "duration_s": float(ln.get("duration_s") or 0.0),
                "target_lufs": -16.0,
            }
        )

    total_seconds = round(sum(float(c.get("duration_s") or 0.0) for c in clips), 2)
    estimated_cost = round(total_seconds * 0.03 + len(audio_tracks) * 0.002, 3)
    budget_ok = estimated_cost <= float(VIDEO_MAX_BUDGET)

    return {
        "project_id": project_id,
        "chapter_index": chapter_index,
        "timeline": {
            "fps": VIDEO_TARGET_FPS,
            "resolution": VIDEO_TARGET_RESOLUTION,
            "duration_s": total_seconds,
            "clips": clips,
            "audio_tracks": audio_tracks,
        },
        "mixing": {
            "voice_bus": {"deesser": True, "compressor": "light", "target_lufs": -16.0},
            "bgm_bus": {"ducking": True, "sidechain_to": "voice_bus"},
            "sfx_bus": {"target_lufs": -22.0},
        },
        "qc": qc_report,
        "budget": {
            "estimated_cost": estimated_cost,
            "max_budget": float(VIDEO_MAX_BUDGET),
            "within_budget": budget_ok,
        },
    }
