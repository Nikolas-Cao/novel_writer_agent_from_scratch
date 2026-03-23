"""
章节视频域模型：scene/shot、角色圣经、声音档案与成长曲线。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, TypedDict


class VoiceProfile(TypedDict, total=False):
    role: str
    voice_id: str
    style: str
    speed: float
    pitch: float
    embedding_ref: str


class CharacterIdentity(TypedDict, total=False):
    character_id: str
    display_name: str
    locked_visual_traits: List[str]
    locked_voice_traits: List[str]
    default_costume: str
    default_seed: int
    reference_images: List[str]
    voice_profile: VoiceProfile


class GrowthArcPoint(TypedDict, total=False):
    chapter_start: int
    chapter_end: int
    visual_delta: List[str]
    voice_delta: List[str]
    emotion_bias: str
    maturity_level: int


class CharacterVersionSnapshot(TypedDict, total=False):
    character_id: str
    version: str
    chapter_index: int
    visual_traits: List[str]
    voice_style: str
    prompt_guardrails: List[str]


class CharacterBible(TypedDict, total=False):
    project_id: str
    protagonists: List[CharacterIdentity]
    growth_curve: Dict[str, List[GrowthArcPoint]]
    snapshots: List[CharacterVersionSnapshot]


class DialogueLine(TypedDict, total=False):
    line_id: str
    speaker: str
    track: str  # narration | dialogue
    text: str
    emotion: str
    target_seconds: float


class ShotPlan(TypedDict, total=False):
    shot_id: str
    scene_id: str
    prompt: str
    duration_s: float
    camera: str
    references: List[str]
    seed: int
    speakers: List[str]


class ScenePlan(TypedDict, total=False):
    scene_id: str
    summary: str
    start_sec: float
    duration_s: float
    dialogues: List[DialogueLine]
    shots: List[ShotPlan]


class ChapterVideoPlan(TypedDict, total=False):
    project_id: str
    chapter_index: int
    chapter_title: str
    target_minutes: float
    scenes: List[ScenePlan]
    versioned_characters: List[CharacterVersionSnapshot]
    budget_estimate: float


def _hash_seed(*parts: str) -> int:
    joined = "|".join(parts).encode("utf-8")
    return int(hashlib.sha1(joined).hexdigest()[:8], 16)


def build_default_growth_curve() -> Dict[str, List[GrowthArcPoint]]:
    return {
        "male_lead": [
            {
                "chapter_start": 0,
                "chapter_end": 10,
                "visual_delta": ["稚气更重", "服装偏朴素"],
                "voice_delta": ["语速稍快", "语气更冲动"],
                "emotion_bias": "热血",
                "maturity_level": 1,
            },
            {
                "chapter_start": 11,
                "chapter_end": 30,
                "visual_delta": ["轮廓更硬朗", "服装更克制"],
                "voice_delta": ["停顿增多", "语气更沉稳"],
                "emotion_bias": "克制",
                "maturity_level": 2,
            },
        ],
        "female_lead": [
            {
                "chapter_start": 0,
                "chapter_end": 10,
                "visual_delta": ["神态明快", "配色更轻盈"],
                "voice_delta": ["音色更明亮", "语速自然偏快"],
                "emotion_bias": "乐观",
                "maturity_level": 1,
            },
            {
                "chapter_start": 11,
                "chapter_end": 30,
                "visual_delta": ["表情更内敛", "配色更冷静"],
                "voice_delta": ["音色更厚实", "句尾更稳"],
                "emotion_bias": "理性",
                "maturity_level": 2,
            },
        ],
    }


def build_default_character_bible(project_id: str) -> CharacterBible:
    return {
        "project_id": project_id,
        "protagonists": [
            {
                "character_id": "male_lead",
                "display_name": "男主",
                "locked_visual_traits": ["短黑发", "左眉细小疤痕", "高鼻梁", "偏瘦体型"],
                "locked_voice_traits": ["低中音", "吐字清晰", "句尾偏稳"],
                "default_costume": "深色风衣",
                "default_seed": _hash_seed(project_id, "male_lead"),
                "reference_images": [],
                "voice_profile": {
                    "role": "dialogue",
                    "voice_id": "male_lead_default",
                    "style": "cinematic",
                    "speed": 1.0,
                    "pitch": 0.0,
                    "embedding_ref": "",
                },
            },
            {
                "character_id": "female_lead",
                "display_name": "女主",
                "locked_visual_traits": ["长发", "眼神锐利", "清晰下颌线", "中等体型"],
                "locked_voice_traits": ["中高音", "有颗粒感", "情绪表达更明显"],
                "default_costume": "浅色长外套",
                "default_seed": _hash_seed(project_id, "female_lead"),
                "reference_images": [],
                "voice_profile": {
                    "role": "dialogue",
                    "voice_id": "female_lead_default",
                    "style": "cinematic",
                    "speed": 1.0,
                    "pitch": 0.1,
                    "embedding_ref": "",
                },
            },
        ],
        "growth_curve": build_default_growth_curve(),
        "snapshots": [],
    }


def resolve_character_snapshots(
    bible: CharacterBible,
    chapter_index: int,
) -> List[CharacterVersionSnapshot]:
    out: List[CharacterVersionSnapshot] = []
    curves = bible.get("growth_curve") or {}
    for ch in bible.get("protagonists") or []:
        cid = str(ch.get("character_id") or "")
        if not cid:
            continue
        matched: Optional[GrowthArcPoint] = None
        for p in curves.get(cid, []):
            s = int(p.get("chapter_start", 0))
            e = int(p.get("chapter_end", s))
            if s <= chapter_index <= e:
                matched = p
                break
        delta_v = list((matched or {}).get("visual_delta") or [])
        delta_voice = list((matched or {}).get("voice_delta") or [])
        maturity = int((matched or {}).get("maturity_level", 1))
        out.append(
            {
                "character_id": cid,
                "version": f"v{maturity}.{chapter_index}",
                "chapter_index": chapter_index,
                "visual_traits": list(ch.get("locked_visual_traits") or []) + delta_v,
                "voice_style": ", ".join(list(ch.get("locked_voice_traits") or []) + delta_voice),
                "prompt_guardrails": [
                    "保持角色核心五官一致",
                    "避免更换年龄段与体型",
                    "服装变化仅在成长曲线允许范围内",
                ],
            }
        )
    return out


def estimate_minutes(text: str, line_count: int) -> float:
    # 旁白约 240-300 字/分钟，对白更慢；这里保守估算，偏向视频更长以减少截断。
    words = max(len((text or "").strip()), 1)
    base = words / 250.0
    dialogue_penalty = min(max(line_count / 18.0, 0.0), 4.0)
    return max(1.0, round(base + dialogue_penalty * 0.15, 2))


def deep_copy_json(data: Any) -> Any:
    if isinstance(data, dict):
        return {str(k): deep_copy_json(v) for k, v in data.items()}
    if isinstance(data, list):
        return [deep_copy_json(v) for v in data]
    return data
