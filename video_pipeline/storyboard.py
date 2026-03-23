"""
将章节文本切分为 scene/shot，并生成对白与旁白分轨台本。
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Tuple

from .models import (
    ChapterVideoPlan,
    DialogueLine,
    ScenePlan,
    ShotPlan,
    estimate_minutes,
    resolve_character_snapshots,
)

_CN_QUOTE_RE = re.compile(r"[“\"]([^”\"]{3,120})[”\"]")


def _split_paragraphs(text: str) -> List[str]:
    chunks = [seg.strip() for seg in re.split(r"\n\s*\n+", text or "") if seg.strip()]
    return chunks or [text.strip() or "（空章节）"]


def _infer_speaker(dialogue: str) -> str:
    t = dialogue.lower()
    if "男主" in dialogue or "他" in dialogue:
        return "male_lead"
    if "女主" in dialogue or "她" in dialogue:
        return "female_lead"
    if any(key in t for key in ["旁白", "narration", "解说"]):
        return "narrator"
    return "narrator"


def _dialogues_from_paragraph(paragraph: str, scene_id: str, base_idx: int) -> List[DialogueLine]:
    lines: List[DialogueLine] = []
    matches = list(_CN_QUOTE_RE.finditer(paragraph))
    if not matches:
        lines.append(
            {
                "line_id": f"{scene_id}-n{base_idx}",
                "speaker": "narrator",
                "track": "narration",
                "text": paragraph[:280],
                "emotion": "calm",
                "target_seconds": round(max(len(paragraph) / 45.0, 2.0), 2),
            }
        )
        return lines
    cursor = 0
    seq = 0
    for m in matches:
        pre = paragraph[cursor:m.start()].strip()
        if pre:
            lines.append(
                {
                    "line_id": f"{scene_id}-n{base_idx}-{seq}",
                    "speaker": "narrator",
                    "track": "narration",
                    "text": pre[:220],
                    "emotion": "calm",
                    "target_seconds": round(max(len(pre) / 50.0, 1.8), 2),
                }
            )
            seq += 1
        spoken = (m.group(1) or "").strip()
        lines.append(
            {
                "line_id": f"{scene_id}-d{base_idx}-{seq}",
                "speaker": _infer_speaker(spoken),
                "track": "dialogue",
                "text": spoken[:180],
                "emotion": "neutral",
                "target_seconds": round(max(len(spoken) / 35.0, 1.5), 2),
            }
        )
        seq += 1
        cursor = m.end()
    post = paragraph[cursor:].strip()
    if post:
        lines.append(
            {
                "line_id": f"{scene_id}-n{base_idx}-{seq}",
                "speaker": "narrator",
                "track": "narration",
                "text": post[:220],
                "emotion": "calm",
                "target_seconds": round(max(len(post) / 50.0, 1.8), 2),
            }
        )
    return lines


def _build_shots(
    scene_id: str,
    scene_summary: str,
    duration_s: float,
    references: List[str],
    seed_base: int,
    speakers: List[str],
) -> List[ShotPlan]:
    pieces = max(2, min(6, int(math.ceil(duration_s / 8.0))))
    each = round(duration_s / pieces, 2)
    shots: List[ShotPlan] = []
    for i in range(pieces):
        camera = ["广角建立镜头", "中景推进", "近景情绪特写"][i % 3]
        shots.append(
            {
                "shot_id": f"{scene_id}-s{i + 1:02d}",
                "scene_id": scene_id,
                "prompt": (
                    f"{scene_summary[:220]}。镜头：{camera}。"
                    f"保持角色形象一致，电影级打光，避免画面闪烁。"
                ),
                "duration_s": each,
                "camera": camera,
                "references": list(references),
                "seed": int(seed_base + i),
                "speakers": list(speakers),
            }
        )
    return shots


def build_chapter_video_plan(
    *,
    project_id: str,
    chapter_index: int,
    chapter_title: str,
    chapter_text: str,
    character_bible: Dict[str, object],
    scene_target_seconds: int,
) -> ChapterVideoPlan:
    paragraphs = _split_paragraphs(chapter_text)
    scenes: List[ScenePlan] = []
    snapshots = resolve_character_snapshots(character_bible, chapter_index)
    snapshot_map = {str(s.get("character_id")): s for s in snapshots}
    current_t = 0.0
    total_lines = 0

    for i, para in enumerate(paragraphs):
        sid = f"sc{i + 1:02d}"
        dlines = _dialogues_from_paragraph(para, sid, i + 1)
        total_lines += len(dlines)
        speakers = sorted({str(l.get("speaker", "narrator")) for l in dlines})
        scene_duration = max(
            8.0,
            min(float(scene_target_seconds), sum(float(x.get("target_seconds", 2.0)) for x in dlines) * 1.15),
        )

        refs: List[str] = []
        for sp in speakers:
            snap = snapshot_map.get(sp)
            if snap:
                refs.extend(list(snap.get("visual_traits") or []))
        shot_seed = 1000 + chapter_index * 131 + i * 17
        shots = _build_shots(
            sid,
            para,
            scene_duration,
            refs,
            shot_seed,
            speakers,
        )
        scenes.append(
            {
                "scene_id": sid,
                "summary": para[:260],
                "start_sec": round(current_t, 2),
                "duration_s": round(scene_duration, 2),
                "dialogues": dlines,
                "shots": shots,
            }
        )
        current_t += scene_duration

    return {
        "project_id": project_id,
        "chapter_index": chapter_index,
        "chapter_title": chapter_title or f"第{chapter_index + 1}章",
        "target_minutes": estimate_minutes(chapter_text, total_lines),
        "scenes": scenes,
        "versioned_characters": snapshots,
        "budget_estimate": round(sum(float(s.get("duration_s", 0.0)) for s in scenes) * 0.03, 2),
    }


def extract_dialogue_tracks(plan: ChapterVideoPlan) -> Tuple[List[DialogueLine], List[DialogueLine]]:
    narration: List[DialogueLine] = []
    dialogue: List[DialogueLine] = []
    for scene in plan.get("scenes") or []:
        for line in scene.get("dialogues") or []:
            if str(line.get("track")) == "dialogue":
                dialogue.append(line)
            else:
                narration.append(line)
    return narration, dialogue
