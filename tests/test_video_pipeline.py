"""
章节视频流水线基础测试：scene/shot 切分、产物落盘、质检报告。
"""
import sys
import uuid
from pathlib import Path


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    p = _root / "tests_tmp" / f"video_pipeline_{uuid.uuid4().hex}"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _run_case(root: Path):
    from video_pipeline import build_default_character_bible, run_chapter_video_pipeline
    from video_pipeline.storage import VideoAssetStore

    project_id = "p-video-test"
    text = (
        "夜雨打在窗上。男主低声说：“我们没有退路了。”\n\n"
        "女主看向远处灯火：“那就把真相带回来。”\n\n"
        "旁白：这场追逐，才刚刚开始。"
    )
    store = VideoAssetStore(root=root / "projects")
    res = await run_chapter_video_pipeline(
        project_id=project_id,
        chapter_index=1,
        chapter_title="追逐",
        chapter_text=text,
        character_bible=build_default_character_bible(project_id),
        store=store,
    )
    assert res["plan"]["scenes"]
    assert res["shot_assets"]
    assert res["line_assets"]
    assert "overall_score" in res["qc_report"]
    assert res["timeline_manifest"]["timeline"]["clips"]
    plan_path = root / "projects" / str(res["plan_ref"])
    manifest_path = root / "projects" / str(res["timeline_manifest_ref"])
    assert plan_path.exists()
    assert manifest_path.exists()


def test_video_pipeline_basic():
    import asyncio
    import shutil

    root = _tmp_root()
    try:
        asyncio.run(_run_case(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)
