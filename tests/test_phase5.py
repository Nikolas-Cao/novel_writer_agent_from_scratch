"""
阶段 5 验收测试：FastAPI 接口流程。
运行：py tests/test_phase5.py  或  py -m pytest tests/test_phase5.py -v
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
    base = _root / "tests_tmp" / f"phase5_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class ApiPlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp(
                '{"plot_ideas":["概要A：雨城连环失踪案。","概要B：机械城阴谋。"]}'
            )
        if '"volumes"' in prompt and "剧情概要" in prompt:
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


def run_all():
    test_phase5_api_flow()
    print("Phase 5 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
