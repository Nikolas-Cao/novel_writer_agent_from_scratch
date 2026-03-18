"""
阶段 2 验收测试：核心工作流（剧情概要 -> 大纲 -> 写章 -> 润色）。
运行：py tests/test_phase2.py  或  py -m pytest tests/test_phase2.py -v
"""
import asyncio
import shutil
import sys
import uuid
from pathlib import Path


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"phase2_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class FakePlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp(
                '{"plot_ideas":["候选剧情A：在边陲城市成长的少女卷入王权阴谋。","候选剧情B：失忆机械师在废土寻找记忆与真相。"]}'
            )
        return _Resp(
            '{"volumes":[{"volume_title":"第一卷 迷雾初启","chapters":[{"title":"第一章 雨夜来信","points":["主角收到神秘来信","踏入旧城区调查","发现组织追踪线索"]}]}]}'
        )


class FakeWriterLLM:
    async def ainvoke(self, prompt: str):
        if "润色" in prompt:
            return _Resp("# 第一章 雨夜来信\n\n夜雨敲打窗沿，她在昏黄灯下拆开来信，字迹像一道冷光。")
        return _Resp("# 第一章 雨夜来信\n\n她在雨夜收到一封没有署名的信，命运从此偏转。")


def test_generate_plot_ideas():
    from graph.nodes.generate_plot_ideas import generate_plot_ideas_node

    state = {"instruction": "赛博朋克悬疑冒险"}
    out = asyncio.run(generate_plot_ideas_node(state, llm=FakePlannerLLM()))
    assert isinstance(out["plot_ideas"], list)
    assert len(out["plot_ideas"]) >= 1


def test_plan_outline():
    from graph.nodes.plan_outline import plan_outline_node
    from state import outline_structure_to_string

    state = {"selected_plot_summary": "候选剧情A", "total_chapters": 1}
    out = asyncio.run(plan_outline_node(state, llm=FakePlannerLLM()))
    structure = out["outline_structure"]
    assert len(structure["volumes"]) >= 1
    assert len(structure["volumes"][0]["chapters"]) >= 1
    assert len(structure["volumes"][0]["chapters"][0]["points"]) >= 1
    assert outline_structure_to_string(structure)


def test_write_and_refine_chapter():
    from graph.nodes.refine_chapter import refine_chapter_node
    from graph.nodes.write_chapter import write_chapter_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root)
    state = {
        "project_id": "p-stage2",
        "current_chapter_index": 0,
        "chapter_word_target": 800,
        "outline_structure": {
            "volumes": [
                {
                    "volume_title": "第一卷",
                    "chapters": [{"title": "第一章 雨夜来信", "points": ["收到信件", "开始调查"]}],
                }
            ]
        },
        "chapters": [],
    }

    out1 = asyncio.run(write_chapter_node(state, llm=FakeWriterLLM(), chapter_store=store))
    chapter_path = root / "p-stage2" / "chapters" / "000.md"
    assert chapter_path.exists()
    text1 = chapter_path.read_text(encoding="utf-8")
    assert "#" in text1

    state2 = {**state, **out1}
    out2 = asyncio.run(refine_chapter_node(state2, llm=FakeWriterLLM(), chapter_store=store))
    text2 = chapter_path.read_text(encoding="utf-8")
    assert text2 != text1
    assert out2["current_chapter_final"].startswith("# ")

    shutil.rmtree(root, ignore_errors=True)


def test_workflow_runs_and_checkpoint_available():
    from graph.workflow import build_stage2_workflow
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root)
    app, checkpointer = build_stage2_workflow(
        planner_llm=FakePlannerLLM(),
        writer_llm=FakeWriterLLM(),
        chapter_store=store,
        use_local_checkpointer=True,
    )
    init_state = {
        "instruction": "赛博朋克悬疑冒险",
        "selected_plot_summary": "候选剧情A",
        "project_id": "p-workflow",
        "current_chapter_index": 0,
        "chapter_word_target": 900,
        "total_chapters": 1,
        "chapters": [],
    }
    cfg = {"configurable": {"thread_id": "phase2-thread"}}
    out = app.invoke(init_state, config=cfg)
    assert "outline_structure" in out
    assert "chapters" in out and len(out["chapters"]) == 1
    assert (root / "p-workflow" / "chapters" / "000.md").exists()

    if checkpointer is not None:
        cp_data = checkpointer.get_tuple(cfg)
        assert cp_data is not None

    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_generate_plot_ideas()
    test_plan_outline()
    test_write_and_refine_chapter()
    test_workflow_runs_and_checkpoint_available()
    print("Phase 2 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
