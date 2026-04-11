import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"outline_resume_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _PlannerResumeLLM:
    def __init__(self) -> None:
        self.skeleton_calls = 0
        self.extend_calls = 0
        self.plot_calls = 0

    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            self.plot_calls += 1
            return _Resp('{"plot_ideas":["概要A"]}')
        if "【plan_outline_skeleton_lite】" in prompt or "【plan_outline_skeleton】" in prompt:
            self.skeleton_calls += 1
            m = re.search(r"本批区间[：:]\s*(\d+)\.\.(\d+)", prompt)
            s = int(m.group(1)) if m else 0
            e = int(m.group(2)) if m else s
            chapters = [
                {"global_index": i, "title": f"第{i + 1}章", "description": f"第{i + 1}章约20字简述"}
                for i in range(s, e + 1)
            ]
            return _Resp(json.dumps({"chapters": chapters}, ensure_ascii=False))
        if "【plan_outline_extend_window】" in prompt:
            self.extend_calls += 1
            # outline_extend_window 内部有 3 次重试；前 3 次都失败，确保首轮请求整体失败，
            # 第 4 次（第二轮请求）开始成功，用于验证 checkpoint 续跑。
            if self.extend_calls <= 3:
                raise RuntimeError("intentional extend failure")
            m = re.search(r"本次新增区间：(\d+)\.\.(\d+)", prompt)
            s = int(m.group(1)) if m else 0
            e = int(m.group(2)) if m else s
            chapters = []
            for g in range(s, e + 1):
                chapters.append(
                    {
                        "global_index": g,
                        "title": f"第{g + 1}章",
                        "description": f"第{g + 1}章约20字简述",
                        "beat": f"beat-{g}",
                        "points": [f"p1-{g}", f"p2-{g}", f"p3-{g}"],
                        "depends_on": [g - 1] if g > 0 else [],
                        "carry_forward": [],
                        "new_threads": [],
                        "resolved_threads": [],
                    }
                )
            return _Resp(json.dumps({"chapters": chapters, "repairs": []}, ensure_ascii=False))
        if "【plan_outline_single】" in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章","points":["a","b"]}]}]}'
            )
        return _Resp("{}")


class _WriterLLM:
    async def ainvoke(self, prompt: str):
        if "润色" in prompt:
            return _Resp("# 第一章\n\n润色稿")
        return _Resp("# 第一章\n\n初稿")


def _patch_state_file(state_root: Path, project_id: str, patcher) -> None:
    fp = state_root / f"{project_id}.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    patcher(data)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_outline_checkpoint_resume_from_skeleton_stage():
    from server import create_app

    root = _tmp_root()
    planner = _PlannerResumeLLM()
    app = create_app(
        planner_llm=planner,
        writer_llm=_WriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        pid = client.post("/projects", json={"instruction": "测试", "total_chapters": 20}).json()["project_id"]
        ideas = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "测试"}).json()["plot_ideas"]

        first = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": ideas[0], "total_chapters": 20})
        assert first.status_code >= 500

        p1 = client.get(f"/projects/{pid}").json()
        assert p1["outline_checkpoint"]["phase"] == "skeleton_done"
        assert p1["outline_job"]["status"] == "idle"
        assert planner.skeleton_calls == 5
        assert planner.extend_calls == 3

        second = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": ideas[0], "total_chapters": 20})
        assert second.status_code == 200
        p2 = client.get(f"/projects/{pid}").json()
        assert p2["outline_checkpoint"]["phase"] is None
        assert p2["outline_job"]["status"] == "idle"
        # 续跑应跳过 skeleton，仅补执行 extend/finalize。
        assert planner.skeleton_calls == 5
        assert planner.extend_calls == 4
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_outline_running_job_rejected_but_stale_job_can_takeover():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=_PlannerResumeLLM(),
        writer_llm=_WriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        pid = client.post("/projects", json={"instruction": "测试", "total_chapters": 2}).json()["project_id"]
        ideas = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "测试"}).json()["plot_ideas"]

        now = int(time.time())
        _patch_state_file(
            root / "states",
            pid,
            lambda s: s.update(
                {
                    "selected_plot_summary": ideas[0],
                    "outline_job": {
                        "status": "running",
                        "job_id": "oj-fresh",
                        "started_at": now,
                        "last_heartbeat_at": now,
                    },
                }
            ),
        )
        r_fresh = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": ideas[0], "total_chapters": 2})
        assert r_fresh.status_code == 409

        stale = now - (10 * 60 + 1)
        _patch_state_file(
            root / "states",
            pid,
            lambda s: s.update(
                {
                    "outline_job": {
                        "status": "running",
                        "job_id": "oj-stale",
                        "started_at": stale,
                        "last_heartbeat_at": stale,
                    },
                }
            ),
        )
        r_stale = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": ideas[0], "total_chapters": 2})
        assert r_stale.status_code == 200
    finally:
        shutil.rmtree(root, ignore_errors=True)
