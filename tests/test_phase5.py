"""
阶段 5 验收测试：FastAPI 接口流程。
运行：py tests/test_phase5.py  或  py -m pytest tests/test_phase5.py -v
"""
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"phase5_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _fake_skeleton_volumes(n: int) -> str:
    chapters = [
        {"title": f"第{i + 1}章", "beat": f"第{i + 1}章核心推进", "points": []} for i in range(n)
    ]
    return json.dumps({"volumes": [{"volume_title": "第一卷", "chapters": chapters}]}, ensure_ascii=False)


def _fake_expand_batch(prompt: str) -> str:
    m = re.search(r"本批 global_index 列表：([\d,]+)", prompt)
    raw = (m.group(1) if m else "").strip()
    indices = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    chapters = []
    for g in indices:
        chapters.append(
            {
                "global_index": g,
                "points": [f"要点A-{g}", f"要点B-{g}", f"要点C-{g}"],
            }
        )
    return json.dumps({"chapters": chapters}, ensure_ascii=False)


class ApiPlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp(
                '{"plot_ideas":["概要A：雨城连环失踪案。","概要B：机械城阴谋。"]}'
            )
        if "【plan_outline_expand_batch】" in prompt:
            return _Resp(_fake_expand_batch(prompt))
        if "【plan_outline_skeleton】" in prompt:
            m = re.search(r"目标章节数[：:]\s*(\d+)", prompt)
            n = int(m.group(1)) if m else 12
            return _Resp(_fake_skeleton_volumes(n))
        if "【plan_outline_single】" in prompt and '"volumes"' in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章 雨夜","points":["案件发生","主角入局"]},{"title":"第二章 追踪","points":["线索扩展","对手现身"]}]}]}'
            )
        if "根据用户反馈和重写后章节，更新大纲要点" in prompt:
            return _Resp(
                '{"current_chapter_points":["结尾改为悬疑停顿"],'
                '"next_chapters_updates":[{"chapter_index":1,"points":["围绕悬疑线索追踪"]}]}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp(
                '{"nodes":[{"id":"hero","name":"主角"}],'
                '"edges":[{"from_id":"hero","to_id":"case","relation":"调查"}]}'
            )
        return _Resp("本章摘要：案件推进，悬疑增强。")


class ApiWriterLLM:
    async def ainvoke(self, prompt: str):
        if "根据用户反馈重写" in prompt:
            return _Resp("# 第一章 雨夜\n\n重写后结尾更悬疑，门外传来第二次敲门声。")
        if "润色" in prompt:
            # 反馈重写后会再跑一次 refine：润色稿需保留「悬疑」以满足接口验收断言
            if "悬疑" in prompt:
                return _Resp("# 第一章 雨夜\n\n润色后仍保留悬疑收束，氛围更紧。")
            return _Resp("# 第一章 雨夜\n\n润色后的章节内容，氛围更紧张。")
        return _Resp("# 第一章 雨夜\n\n初稿章节内容。")


def test_phase5_api_flow():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=ApiPlannerLLM(),
        writer_llm=ApiWriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    # 创建项目
    r = client.post("/projects", json={"instruction": "都市悬疑", "total_chapters": 2})
    assert r.status_code == 200
    project_id = r.json()["project_id"]

    # 列表与项目详情
    r = client.get("/projects")
    assert r.status_code == 200
    assert any(item["project_id"] == project_id for item in r.json()["projects"])

    r = client.get(f"/projects/{project_id}")
    assert r.status_code == 200
    assert r.json()["project_id"] == project_id

    # 生成剧情概要（可刷新）
    r = client.post(f"/projects/{project_id}/plot-ideas", json={"instruction": "都市悬疑"})
    assert r.status_code == 200
    ideas = r.json()["plot_ideas"]
    assert len(ideas) >= 1

    # 生成大纲
    r = client.post(
        f"/projects/{project_id}/outline",
        json={"selected_plot_summary": ideas[0], "total_chapters": 2},
    )
    assert r.status_code == 200
    assert r.json()["outline_structure"]["volumes"]

    # 写第一章
    r = client.post(f"/projects/{project_id}/chapters/next", json={})
    assert r.status_code == 200
    assert r.json()["chapter_index"] == 0
    assert r.json()["chapter"].startswith("# ")

    # 续写第二章
    r = client.post(f"/projects/{project_id}/chapters/next", json={})
    assert r.status_code == 200
    assert r.json()["chapter_index"] == 1

    # 章节列表与正文查询
    r = client.get(f"/projects/{project_id}/chapters")
    assert r.status_code == 200
    assert len(r.json()["chapters"]) >= 2

    r = client.get(f"/projects/{project_id}/chapters/0")
    assert r.status_code == 200
    assert r.json()["content"].startswith("# ")

    # 非最新章重写应被拒绝
    r = client.post(
        f"/projects/{project_id}/chapters/0/rewrite",
        json={"user_feedback": "把结尾改得更悬疑", "update_outline": True},
    )
    assert r.status_code == 400

    # 反馈重写 + 可选更新大纲（仅最新章）
    r = client.post(
        f"/projects/{project_id}/chapters/1/rewrite",
        json={"user_feedback": "把结尾改得更悬疑", "update_outline": True},
    )
    assert r.status_code == 200
    assert "悬疑" in r.json()["chapter"]

    # 再次查询第二章为修订版
    r = client.get(f"/projects/{project_id}/chapters/1")
    assert r.status_code == 200
    assert "悬疑" in r.json()["content"]

    # 回滚到第一章，删除后续章节
    r = client.delete(f"/projects/{project_id}/chapters/0/tail")
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1

    r = client.get(f"/projects/{project_id}/chapters")
    assert r.status_code == 200
    assert len(r.json()["chapters"]) == 1

    shutil.rmtree(root, ignore_errors=True)


def test_phase5_outline_multi_phase_api():
    from graph.nodes.plan_outline import PLAN_OUTLINE_SINGLE_CALL_MAX
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=ApiPlannerLLM(),
        writer_llm=ApiWriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)
    n = PLAN_OUTLINE_SINGLE_CALL_MAX + 3
    r = client.post("/projects", json={"instruction": "长篇 API", "total_chapters": n})
    assert r.status_code == 200
    pid = r.json()["project_id"]
    r = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "长篇 API"})
    assert r.status_code == 200
    ideas = r.json()["plot_ideas"]
    r = client.post(
        f"/projects/{pid}/outline",
        json={"selected_plot_summary": ideas[0], "total_chapters": n},
    )
    assert r.status_code == 200
    vols = r.json()["outline_structure"]["volumes"]
    total = sum(len(v.get("chapters") or []) for v in vols)
    assert total == n
    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_phase5_api_flow()
    test_phase5_outline_multi_phase_api()
    print("Phase 5 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
