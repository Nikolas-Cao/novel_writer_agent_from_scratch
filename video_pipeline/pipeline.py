"""
章节视频总流水线：分镜 -> 生成 -> 质检 -> 合成描述。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import VIDEO_DEFAULT_SCENE_SECONDS, VIDEO_MAX_BUDGET
from .compose import build_timeline_manifest
from .models import CharacterBible, build_default_character_bible, deep_copy_json
from .providers import GenericCloudTTSProvider, GenericCloudVideoProvider
from .qc import evaluate_av_sync, evaluate_visual_consistency, evaluate_voice_consistency, summarize_qc
from .storage import VideoAssetStore
from .storyboard import build_chapter_video_plan, extract_dialogue_tracks

ProgressFn = Callable[[str, str], Awaitable[None]]


async def _noop_progress(_stage: str, _message: str) -> None:
    return None


def _voice_profile_map(bible: CharacterBible) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {
        "narrator": {"voice_id": "narrator_default", "speed": 1.0, "pitch": 0.0, "style": "neutral"}
    }
    for p in bible.get("protagonists") or []:
        cid = str(p.get("character_id") or "")
        if not cid:
            continue
        out[cid] = deep_copy_json(p.get("voice_profile") or {})
    return out


async def _generate_shots(
    *,
    project_id: str,
    chapter_index: int,
    scenes: List[Dict[str, Any]],
    provider: GenericCloudVideoProvider,
    store: VideoAssetStore,
    emit: ProgressFn,
) -> List[Dict[str, Any]]:
    shots_flat: List[Dict[str, Any]] = []
    for scene in scenes:
        for shot in scene.get("shots") or []:
            enriched = {
                **shot,
                "speakers": list(shot.get("speakers") or []),
                "references": list(shot.get("references") or []),
            }
            shots_flat.append(enriched)

    results: List[Dict[str, Any]] = []
    for idx, shot in enumerate(shots_flat):
        await emit("video_shot", f"生成镜头 {idx + 1}/{len(shots_flat)}：{shot.get('shot_id')}")
        out = await provider.generate_shot(
            project_id=project_id,
            chapter_index=chapter_index,
            shot=shot,
            store=store,
        )
        results.append({**shot, **out})
    return results


async def _generate_audio_lines(
    *,
    project_id: str,
    chapter_index: int,
    narration: List[Dict[str, Any]],
    dialogue: List[Dict[str, Any]],
    tts_provider: GenericCloudTTSProvider,
    voice_profiles: Dict[str, Dict[str, Any]],
    store: VideoAssetStore,
    emit: ProgressFn,
) -> List[Dict[str, Any]]:
    lines = list(narration) + list(dialogue)
    out: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        speaker = str(line.get("speaker") or "narrator")
        profile = voice_profiles.get(speaker) or voice_profiles.get("narrator") or {}
        await emit("audio_tts", f"生成配音 {idx + 1}/{len(lines)}：{line.get('line_id')}")
        audio = await tts_provider.synthesize_line(
            project_id=project_id,
            chapter_index=chapter_index,
            line=line,
            voice_profile=profile,
            store=store,
        )
        out.append({**line, **audio, "speaker": speaker})
    return out


async def run_chapter_video_pipeline(
    *,
    project_id: str,
    chapter_index: int,
    chapter_title: str,
    chapter_text: str,
    character_bible: Optional[CharacterBible] = None,
    emit: Optional[ProgressFn] = None,
    store: Optional[VideoAssetStore] = None,
    video_provider: Optional[GenericCloudVideoProvider] = None,
    tts_provider: Optional[GenericCloudTTSProvider] = None,
) -> Dict[str, Any]:
    _emit = emit or _noop_progress
    _store = store or VideoAssetStore()
    _video = video_provider or GenericCloudVideoProvider()
    _tts = tts_provider or GenericCloudTTSProvider()
    bible = deep_copy_json(character_bible or build_default_character_bible(project_id))

    await _emit("plan_storyboard", "正在拆分 scene/shot 与对白分轨…")
    plan = build_chapter_video_plan(
        project_id=project_id,
        chapter_index=chapter_index,
        chapter_title=chapter_title,
        chapter_text=chapter_text,
        character_bible=bible,
        scene_target_seconds=VIDEO_DEFAULT_SCENE_SECONDS,
    )
    if float(plan.get("budget_estimate") or 0.0) > float(VIDEO_MAX_BUDGET):
        await _emit("budget_guard", "预算超限，自动缩短镜头时长并降级复杂镜头。")
        for scene in plan.get("scenes") or []:
            scene["duration_s"] = max(8.0, round(float(scene.get("duration_s", 0.0)) * 0.8, 2))
            for shot in scene.get("shots") or []:
                shot["duration_s"] = max(3.0, round(float(shot.get("duration_s", 0.0)) * 0.8, 2))
        plan["budget_estimate"] = round(float(plan.get("budget_estimate", 0.0)) * 0.8, 2)

    plan_ref = _store.write_json(project_id, chapter_index, "chapter_video_plan.json", plan)

    await _emit("video_generation", "正在按 shot 粒度生成视频镜头…")
    shot_assets = await _generate_shots(
        project_id=project_id,
        chapter_index=chapter_index,
        scenes=list(plan.get("scenes") or []),
        provider=_video,
        store=_store,
        emit=_emit,
    )

    narration, dialogue = extract_dialogue_tracks(plan)
    await _emit("audio_generation", "正在按 line 粒度生成旁白与对白…")
    voices = _voice_profile_map(bible)
    line_assets = await _generate_audio_lines(
        project_id=project_id,
        chapter_index=chapter_index,
        narration=narration,
        dialogue=dialogue,
        tts_provider=_tts,
        voice_profiles=voices,
        store=_store,
        emit=_emit,
    )

    await _emit("quality_check", "正在执行一致性与音频质量检测…")
    qc_visual = evaluate_visual_consistency(plan=plan, generated_shots=shot_assets)
    qc_voice = evaluate_voice_consistency(line_assets=line_assets, voice_profiles=voices)
    qc_sync = evaluate_av_sync(scene_plans=list(plan.get("scenes") or []), line_assets=line_assets)
    qc = summarize_qc(qc_visual, qc_voice, qc_sync)
    qc_ref = _store.write_json(project_id, chapter_index, "qc_report.json", qc)

    await _emit("compose_export", "正在生成时间线与导出清单…")
    manifest = build_timeline_manifest(
        project_id=project_id,
        chapter_index=chapter_index,
        shots=shot_assets,
        lines=line_assets,
        qc_report=qc,
    )
    manifest_ref = _store.write_json(project_id, chapter_index, "timeline_manifest.json", manifest)

    return {
        "plan": plan,
        "plan_ref": plan_ref,
        "shot_assets": shot_assets,
        "line_assets": line_assets,
        "qc_report": qc,
        "qc_ref": qc_ref,
        "timeline_manifest": manifest,
        "timeline_manifest_ref": manifest_ref,
    }
