"""
视频质量评估：角色一致性、音频质量、音画同步。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _ratio(ok: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return round(max(0.0, min(1.0, ok / float(total))), 4)


def evaluate_visual_consistency(
    *,
    plan: Dict[str, Any],
    generated_shots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    expected = 0
    passed = 0
    snapshots = {str(x.get("character_id")): x for x in plan.get("versioned_characters") or []}
    for shot in generated_shots:
        speakers = shot.get("speakers") or []
        refs = " ".join(list(shot.get("references") or []))
        for sp in speakers:
            if sp in ("narrator", "", None):
                continue
            expected += 1
            traits = " ".join(list((snapshots.get(str(sp)) or {}).get("visual_traits") or []))
            if not traits:
                passed += 1
                continue
            if any(t and t in refs for t in traits.split(" ")):
                passed += 1
    score = _ratio(passed, expected)
    return {
        "metric": "visual_consistency",
        "score": score,
        "pass": score >= 0.75,
        "details": {"matched_pairs": passed, "expected_pairs": expected},
    }


def evaluate_voice_consistency(
    *,
    line_assets: List[Dict[str, Any]],
    voice_profiles: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    expected = 0
    passed = 0
    for item in line_assets:
        speaker = str(item.get("speaker") or "narrator")
        if speaker == "narrator":
            continue
        expected += 1
        vp = voice_profiles.get(speaker) or {}
        vid = str(vp.get("voice_id") or "")
        # 在无声纹模型时，用 voice_id 存在性做第一层保障。
        if vid:
            passed += 1
    score = _ratio(passed, expected)
    return {
        "metric": "voice_consistency",
        "score": score,
        "pass": score >= 0.9,
        "details": {"with_voice_id": passed, "expected_dialogues": expected},
    }


def evaluate_av_sync(
    *,
    scene_plans: List[Dict[str, Any]],
    line_assets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    expected = 0
    passed = 0
    line_duration = {str(x.get("line_id")): float(x.get("duration_s") or 0.0) for x in line_assets}
    for scene in scene_plans:
        budget = float(scene.get("duration_s") or 0.0)
        target = 0.0
        for line in scene.get("dialogues") or []:
            expected += 1
            target += float(line_duration.get(str(line.get("line_id")), line.get("target_seconds", 0.0)))
        if budget <= 0:
            continue
        drift = abs(target - budget) / budget
        if drift <= 0.2:
            passed += len(scene.get("dialogues") or [])
    score = _ratio(passed, expected)
    return {
        "metric": "av_sync",
        "score": score,
        "pass": score >= 0.8,
        "details": {"within_tolerance_lines": passed, "total_lines": expected},
    }


def summarize_qc(*reports: Dict[str, Any]) -> Dict[str, Any]:
    rs = list(reports)
    avg = round(sum(float(r.get("score") or 0.0) for r in rs) / max(len(rs), 1), 4)
    return {
        "overall_score": avg,
        "pass": all(bool(r.get("pass")) for r in rs),
        "reports": rs,
    }
