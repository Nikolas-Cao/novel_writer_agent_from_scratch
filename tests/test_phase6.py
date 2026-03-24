"""
阶段 6 验收测试：前端页面与静态资源、前后端联动。
运行：py tests/test_phase6.py  或  py -m pytest tests/test_phase6.py -v
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
    base = _root / "tests_tmp" / f"phase6_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def _front_fake_skeleton_volumes(n: int) -> str:
    chapters = [{"title": f"第{i + 1}章", "beat": f"节拍{i}", "points": []} for i in range(n)]
    return json.dumps({"volumes": [{"volume_title": "第一卷", "chapters": chapters}]}, ensure_ascii=False)


def _front_fake_expand_batch(prompt: str) -> str:
    m = re.search(r"本批 global_index 列表：([\d,]+)", prompt)
    raw = (m.group(1) if m else "").strip()
    indices = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    chapters = [{"global_index": g, "points": [f"p1-{g}", f"p2-{g}"]} for g in indices]
    return json.dumps({"chapters": chapters}, ensure_ascii=False)


def _front_fake_extend_window(prompt: str) -> str:
    m = re.search(r"本次新增区间[：:]\s*(\d+)\.\.(\d+)", prompt)
    s = int(m.group(1)) if m else 0
    e = int(m.group(2)) if m else s
    chapters = []
    for g in range(s, e + 1):
        chapters.append(
            {
                "global_index": g,
                "title": f"第{g + 1}章",
                "beat": f"beat{g}",
                "points": [f"p1-{g}", f"p2-{g}"],
                "depends_on": [g - 1] if g > 0 else [],
                "carry_forward": [],
                "new_threads": [],
                "resolved_threads": [],
            }
        )
    return json.dumps({"chapters": chapters, "repairs": []}, ensure_ascii=False)


class FrontPlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["概要A：雨城追案。","概要B：废土迷局。"]}')
        if "【plan_outline_expand_batch】" in prompt:
            return _Resp(_front_fake_expand_batch(prompt))
        if "【plan_outline_skeleton】" in prompt:
            m = re.search(r"目标章节数[：:]\s*(\d+)", prompt)
            n = int(m.group(1)) if m else 12
            return _Resp(_front_fake_skeleton_volumes(n))
        if "【plan_outline_extend_window】" in prompt:
            return _Resp(_front_fake_extend_window(prompt))
        if "【plan_outline_single】" in prompt and '"volumes"' in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章 雨夜","points":["案发","主角入局"]},{"title":"第二章 追踪","points":["线索扩展"]}]}]}'
            )
        if "根据用户反馈和重写后章节，更新大纲要点" in prompt:
            return _Resp(
                '{"current_chapter_points":["结尾改成悬疑停顿"],'
                '"next_chapters_updates":[{"chapter_index":1,"points":["主角继续追踪"]}]}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp(
                '{"nodes":[{"id":"hero","name":"主角"}],'
                '"edges":[{"from_id":"hero","to_id":"case","relation":"调查"}]}'
            )
        return _Resp("本章摘要：案情推进。")


class FrontWriterLLM:
    async def ainvoke(self, prompt: str):
        if "根据用户反馈重写" in prompt:
            return _Resp("# 第一章 雨夜\n\n重写后结尾更悬疑。")
        if "润色" in prompt:
            if "悬疑" in prompt:
                return _Resp("# 第一章 雨夜\n\n润色后仍保留悬疑收束。")
            return _Resp("# 第一章 雨夜\n\n润色后的正文。")
        return _Resp("# 第一章 雨夜\n\n初稿正文。")


def test_frontend_assets_and_page():
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=FrontPlannerLLM(),
        writer_llm=FrontWriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "项目列表" in html
    assert "新建小说" in html
    assert "项目详情" in html
    assert "章节正文（Markdown 渲染）" in html
    assert 'id="btn-generate-outline-range"' in html

    r = client.get("/assets/app.js")
    assert r.status_code == 200
    js = r.text
    assert "renderMarkdown" in js
    assert "btn-next-chapter" in html
    assert "btn-rewrite" in html
    assert "![(" not in js  # 确认不是硬编码结果，而是解析逻辑
    assert "replace(/!\\[([^\\]]*)\\]\\(([^)]+)\\)/g" in js

    r = client.get("/assets/styles.css")
    assert r.status_code == 200
    assert ".markdown img" in r.text

    shutil.rmtree(root, ignore_errors=True)


def test_frontend_backend_flow_via_api():
    """
    以 API 模拟前端点击流程，确保前端依赖接口均可用。
    """
    from server import create_app

    root = _tmp_root()
    app = create_app(
        planner_llm=FrontPlannerLLM(),
        writer_llm=FrontWriterLLM(),
        projects_root=root / "projects",
        vector_root=root / "vector",
        checkpoint_root=root / "states",
    )
    client = TestClient(app)

    r = client.post("/projects", json={"instruction": "都市悬疑", "total_chapters": 2})
    assert r.status_code == 200
    pid = r.json()["project_id"]

    r = client.post(f"/projects/{pid}/plot-ideas", json={"instruction": "都市悬疑"})
    assert r.status_code == 200
    ideas = r.json()["plot_ideas"]
    assert ideas

    r = client.post(f"/projects/{pid}/outline", json={"selected_plot_summary": ideas[0]})
    assert r.status_code == 200
    assert r.json()["outline_structure"]["volumes"]

    r = client.post(f"/projects/{pid}/outline/window", json={})
    assert r.status_code == 200
    assert isinstance(r.json().get("outline_extended_indices"), list)

    r = client.post(f"/projects/{pid}/chapters/next", json={})
    assert r.status_code == 200
    assert r.json()["chapter_index"] == 0

    r = client.post(f"/projects/{pid}/chapters/next", json={})
    assert r.status_code == 200
    assert r.json()["chapter_index"] == 1

    r = client.get(f"/projects/{pid}/chapters/0")
    assert r.status_code == 200
    assert r.json()["content"].startswith("# ")

    r = client.post(
        f"/projects/{pid}/chapters/0/rewrite",
        json={"user_feedback": "把结尾改得更悬疑", "update_outline": True},
    )
    assert r.status_code == 400

    r = client.post(
        f"/projects/{pid}/chapters/1/rewrite",
        json={"user_feedback": "把结尾改得更悬疑", "update_outline": True},
    )
    assert r.status_code == 200
    assert "悬疑" in r.json()["chapter"]

    r = client.delete(f"/projects/{pid}/chapters/0/tail")
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1

    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_frontend_assets_and_page()
    test_frontend_backend_flow_via_api()
    print("Phase 6 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
