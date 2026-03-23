"""
章节视频 API 测试：同步生成、任务状态、产物查询。
"""
import shutil
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"video_api_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _Planner:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["概要A"]}')
        if "【plan_outline_single】" in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章","points":["p1","p2"]}]}]}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp('{"nodes":[],"edges":[]}')
        return _Resp("摘要")


class _Writer:
    async def ainvoke(self, prompt: str):
        if "润色" in prompt:
            return _Resp('# 第一章\n\n男主说：“走吧。”\n\n女主回应：“好。”')
        return _Resp('# 第一章\n\n男主说：“走吧。”\n\n女主回应：“好。”')


def test_video_api_sync_generation():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=_Planner(),
        writer_llm=_Writer(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)
    try:
        r = client.post("/projects", json={"instruction": "测试", "total_chapters": 1})
        assert r.status_code == 200
        pid = r.json()["project_id"]
        r = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "测试"})
        assert r.status_code == 200
        idea = r.json()["plot_ideas"][0]
        r = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": idea, "total_chapters": 1})
        assert r.status_code == 200
        r = client.post(f"/projects/{pid}/chapters/next", json={})
        assert r.status_code == 200

        r = client.post(
            f"/projects/{pid}/videos/chapters/0",
            json={"async_mode": False, "use_latest_character_bible": True},
        )
        assert r.status_code == 200
        out = r.json()["output"]
        assert out["timeline_manifest_ref"]
        assert "qc_report" in out

        r = client.get(f"/projects/{pid}/videos/chapters/0")
        assert r.status_code == 200
        assert r.json()["output"]["timeline_manifest_ref"] == out["timeline_manifest_ref"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
