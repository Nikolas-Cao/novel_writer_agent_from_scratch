import sys
import uuid
import re
import json
from pathlib import Path

from fastapi.testclient import TestClient

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _PlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["剧情概要A"]}')
        # 注意：extend_window 的 prompt 含有 "plan_outline_extend_window"，
        # 若先匹配通用 "plan_outline" 会误返回 volumes 结构，导致校验失败。
        if "plan_outline_extend_window" in prompt:
            m = re.search(r"本次新增区间：(\d+)\.\.(\d+)", prompt)
            s = int(m.group(1)) if m else 0
            e = int(m.group(2)) if m else s
            chapters = []
            for g in range(s, e + 1):
                chapters.append(
                    {
                        "global_index": g,
                        "title": f"第{g + 1}章",
                        "beat": f"beat{g}",
                        "points": [f"p1-{g}", f"p2-{g}", f"p3-{g}"],
                        "depends_on": [g - 1] if g > 0 else [],
                        "carry_forward": [],
                        "new_threads": [],
                        "resolved_threads": [],
                    }
                )
            return _Resp(json.dumps({"chapters": chapters, "repairs": []}, ensure_ascii=False))
        if "plan_outline" in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章","points":["A"]}]}]}'
            )
        return _Resp("本章摘要")


class _WriterLLM:
    async def ainvoke(self, prompt: str):
        if "根据用户反馈重写" in prompt:
            return _Resp("# 第一章\n\n重写版本。")
        if "润色" in prompt:
            return _Resp("# 第一章\n\n润色版本。")
        return _Resp("# 第一章\n\n初稿。")


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"events_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_event_logs_api_and_truncation():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=_PlannerLLM(),
        writer_llm=_WriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    create_resp = client.post("/projects", json={"instruction": "测试", "total_chapters": 1})
    assert create_resp.status_code == 200
    pid = create_resp.json()["project_id"]

    long_instruction = "意图" * 80
    resp = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": long_instruction})
    assert resp.status_code == 200
    assert len(resp.json().get("plot_ideas") or []) >= 1

    events_resp = client.get(f"/projects/{pid}/events")
    assert events_resp.status_code == 200
    events = events_resp.json().get("events") or []
    assert events, "应记录至少一条事件"
    names = [e.get("event_name") for e in events]
    assert "generate_plot_ideas" in names

    first = events[0]
    assert isinstance(first.get("event_content"), str)
    assert len(first.get("event_content")) <= 300


def test_event_logs_filter_by_chapter():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=_PlannerLLM(),
        writer_llm=_WriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    create_resp = client.post("/projects", json={"instruction": "测试", "total_chapters": 1})
    pid = create_resp.json()["project_id"]

    ideas = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "都市悬疑"}).json()["plot_ideas"]
    outline_resp = client.post(
        f"/projects/{pid}/outline",
        json={"selected_plot_summary": ideas[0], "total_chapters": 1},
    )
    assert outline_resp.status_code == 200
    next_resp = client.post(f"/projects/{pid}/chapters/next", json={})
    assert next_resp.status_code == 200

    all_events = client.get(f"/projects/{pid}/events").json().get("events") or []
    chapter_events = client.get(f"/projects/{pid}/events?chapter_index=0").json().get("events") or []
    assert len(all_events) >= len(chapter_events)
    assert chapter_events, "应能筛到章节事件"
    assert all(int(e.get("chapter_index")) == 0 for e in chapter_events)


def test_outline_window_event_logs():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=_PlannerLLM(),
        writer_llm=_WriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    create_resp = client.post("/projects", json={"instruction": "测试", "total_chapters": 20})
    assert create_resp.status_code == 200
    pid = create_resp.json()["project_id"]

    ideas = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "都市悬疑"}).json()["plot_ideas"]
    outline_resp = client.post(
        f"/projects/{pid}/outline",
        json={"selected_plot_summary": ideas[0], "total_chapters": 20},
    )
    assert outline_resp.status_code == 200

    # 默认初始窗口为 10 章，扩窗一次应触发 11~20 章。
    window_resp = client.post(f"/projects/{pid}/outline/window", json={})
    assert window_resp.status_code == 200

    events = client.get(f"/projects/{pid}/events").json().get("events") or []
    window_events = [e for e in events if e.get("event_name") == "generate_outline_window"]
    assert window_events, "扩窗应记录 generate_outline_window 事件"
    statuses = {str(e.get("status")) for e in window_events}
    assert "start" in statuses
    assert "success" in statuses
