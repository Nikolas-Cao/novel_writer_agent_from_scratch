"""
阶段 2 验收测试：核心工作流（剧情概要 -> 大纲 -> 写章 -> 润色）。
运行：py tests/test_phase2.py  或  py -m pytest tests/test_phase2.py -v
"""
import asyncio
import json
import re
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
        if "【plan_outline_extend_window】" in prompt:
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
                        "beat": f"beat{g}",
                        "points": [f"p1-{g}", f"p2-{g}", f"p3-{g}"],
                        "depends_on": [g - 1] if g > 0 else [],
                        "carry_forward": [f"cf-{g}"],
                        "new_threads": [f"new-{g}"],
                        "resolved_threads": [],
                    }
                )
            repairs = []
            if s > 0:
                repairs.append({"global_index": s - 1, "points": [f"repair-{s - 1}"]})
            return _Resp(json.dumps({"chapters": chapters, "repairs": repairs}, ensure_ascii=False))
        if "【plan_outline_expand_batch】" in prompt:
            m = re.search(r"本批 global_index 列表：([\d,]+)", prompt)
            raw = (m.group(1) if m else "").strip()
            indices = [int(x) for x in raw.split(",") if x.strip().isdigit()]
            chapters = [{"global_index": g, "points": [f"a-{g}", f"b-{g}", f"c-{g}"]} for g in indices]
            return _Resp(json.dumps({"chapters": chapters}, ensure_ascii=False))
        if "【plan_outline_skeleton_lite】" in prompt or "【plan_outline_skeleton】" in prompt:
            m = re.search(r"本批区间[：:]\s*(\d+)\.\.(\d+)", prompt)
            s = int(m.group(1)) if m else 0
            e = int(m.group(2)) if m else s
            chapters = [
                {
                    "global_index": i,
                    "title": f"第{i + 1}章",
                    "description": f"第{i + 1}章约20字简述",
                }
                for i in range(s, e + 1)
            ]
            return _Resp(json.dumps({"chapters": chapters}, ensure_ascii=False))
        if "【plan_outline_single】" in prompt:
            m = re.search(r"目标章节数[：:]\s*(\d+)", prompt)
            n = int(m.group(1)) if m else 1
            chs = [
                {"title": f"第{i + 1}章 雨夜来信", "points": [f"主线推进{i}", f"冲突升级{i}", f"悬念埋设{i}"]}
                for i in range(n)
            ]
            return _Resp(json.dumps({"volumes": [{"volume_title": "第一卷 迷雾初启", "chapters": chs}]}, ensure_ascii=False))
        return _Resp("{}")


class CaptureOutlineModePlannerLLM(FakePlannerLLM):
    def __init__(self) -> None:
        self.saw_single = False
        self.saw_skeleton_lite = False

    async def ainvoke(self, prompt: str):
        if "【plan_outline_single】" in prompt:
            self.saw_single = True
        if "【plan_outline_skeleton_lite】" in prompt or "【plan_outline_skeleton】" in prompt:
            self.saw_skeleton_lite = True
        return await super().ainvoke(prompt)


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


def test_plan_outline_multi_phase_batches_and_rag():
    from graph.nodes import plan_outline as po
    from graph.nodes.plan_outline import plan_outline_node
    from state import outline_structure_to_string

    n = po.PLAN_OUTLINE_SINGLE_CALL_MAX + 5
    state = {"selected_plot_summary": "长篇回归", "total_chapters": n, "project_id": "p-mphase"}

    class _Idx:
        def __init__(self) -> None:
            self.outline_chunks = 0

        def add_outline_chunk(self, *_a, **_kw) -> None:
            self.outline_chunks += 1

    idx = _Idx()
    out = asyncio.run(plan_outline_node(state, llm=FakePlannerLLM(), rag_indexer=idx))
    structure = out["outline_structure"]
    total = sum(len(vol.get("chapters") or []) for vol in structure["volumes"])
    assert total == n
    generated_until = int(out.get("outline_generated_until", -1))
    assert generated_until >= 9
    refs = []
    for vol in structure["volumes"]:
        refs.extend(vol.get("chapters") or [])
    for i in range(generated_until + 1):
        assert isinstance(refs[i].get("points"), list) and len(refs[i]["points"]) >= 1
    assert str(refs[0].get("description") or "").strip()
    assert idx.outline_chunks == n
    assert outline_structure_to_string(structure)


def test_plan_outline_skeleton_batch_alignment_202():
    from graph.nodes import plan_outline as po
    from graph.nodes.plan_outline import plan_outline_node

    n = 202
    state = {"selected_plot_summary": "分批对齐测试", "total_chapters": n}
    out = asyncio.run(plan_outline_node(state, llm=FakePlannerLLM()))
    structure = out["outline_structure"]
    refs = []
    for vol in structure.get("volumes") or []:
        refs.extend(vol.get("chapters") or [])
    assert len(refs) == n
    if po.DEBUG:
        assert all(isinstance(ch.get("points"), list) and len(ch["points"]) >= 1 for ch in refs)
    else:
        assert str(refs[0].get("description") or "").strip()
        assert str(refs[-1].get("description") or "").strip()


def test_plan_outline_debug_mode_forces_single_call(monkeypatch):
    from graph.nodes import plan_outline as po
    from graph.nodes.plan_outline import plan_outline_node

    monkeypatch.setattr(po, "DEBUG", True)
    n = po.PLAN_OUTLINE_SINGLE_CALL_MAX + 5
    state = {"selected_plot_summary": "debug 模式大纲", "total_chapters": n}
    llm = CaptureOutlineModePlannerLLM()
    out = asyncio.run(plan_outline_node(state, llm=llm))
    refs = []
    for vol in (out.get("outline_structure") or {}).get("volumes") or []:
        refs.extend(vol.get("chapters") or [])
    assert len(refs) == n
    assert llm.saw_single is True
    assert llm.saw_skeleton_lite is False


def test_skeleton_lite_parse_payload_accepts_short_and_long_keys():
    from graph.nodes.outline_skeleton_lite import _parse_batch_payload

    short_data = {
        "chapters": [
            {"g": 0, "t": "第1章", "d": "第1章约20字简述"},
            {"g": 1, "t": "第2章", "d": "第2章约20字简述"},
        ]
    }
    long_data = {
        "chapters": [
            {"global_index": 0, "title": "第1章", "description": "第1章约20字简述"},
            {"global_index": 1, "title": "第2章", "description": "第2章约20字简述"},
        ]
    }

    out_short = _parse_batch_payload(short_data, 0, 1)
    out_long = _parse_batch_payload(long_data, 0, 1)
    assert out_short == out_long
    assert out_short[0]["title"] == "第1章"
    assert out_short[1]["description"] == "第2章约20字简述"


def test_plan_outline_extend_with_repair_and_continuity_fields():
    from graph.nodes.plan_outline import plan_outline_extend_node, plan_outline_node

    state = {"selected_plot_summary": "窗口测试", "total_chapters": 8, "project_id": "p-extend"}
    base = asyncio.run(plan_outline_node(state, llm=FakePlannerLLM(), target_chapters=4))
    merged_state = {**state, **base}
    out = asyncio.run(
        plan_outline_extend_node(
            merged_state,
            start_chapter=4,
            extend_count=3,
            llm=FakePlannerLLM(),
            recent_fact_pack={
                "recent_summaries": "x",
                "recent_outline_points": "y",
                "character_snapshot": "z",
                "story_constraints": "k",
            },
        )
    )
    structure = out["outline_structure"]
    total = sum(len(v.get("chapters") or []) for v in structure["volumes"])
    assert total >= 7
    refs = []
    for vol in structure["volumes"]:
        for ch in vol.get("chapters") or []:
            refs.append(ch)
    assert refs[3]["points"][0].startswith("repair-")
    assert refs[4].get("depends_on") == [3]
    assert refs[4].get("carry_forward")
    assert int(out.get("outline_generated_until", -1)) >= 6


def test_plan_outline_extend_skip_repair_for_written_chapters():
    from graph.nodes.plan_outline import plan_outline_extend_node, plan_outline_node

    state = {"selected_plot_summary": "窗口测试", "total_chapters": 12, "project_id": "p-no-repair"}
    base = asyncio.run(plan_outline_node(state, llm=FakePlannerLLM(), target_chapters=6))
    merged_state = {
        **state,
        **base,
        "last_written_chapter_index": 5,
    }
    out = asyncio.run(
        plan_outline_extend_node(
            merged_state,
            start_chapter=6,
            extend_count=3,
            llm=FakePlannerLLM(),
            recent_fact_pack={"recent_summaries": "x"},
        )
    )
    refs = []
    for vol in out["outline_structure"]["volumes"]:
        refs.extend(vol.get("chapters") or [])
    # g=5 已写正文，repair 应被禁用，不应出现 repair-5
    assert not str((refs[5].get("points") or [""])[0]).startswith("repair-")


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


def test_write_and_refine_prompt_contains_style_constraint():
    from graph.nodes.refine_chapter import refine_chapter_node
    from graph.nodes.write_chapter import write_chapter_node
    from storage import ChapterStore

    class CaptureWriterLLM:
        def __init__(self) -> None:
            self.prompts = []

        async def ainvoke(self, prompt: str):
            self.prompts.append(prompt)
            if "润色" in prompt:
                return _Resp("# 第一章\n\n润色版本。")
            return _Resp("# 第一章\n\n初稿版本。")

    root = _tmp_root()
    store = ChapterStore(root=root)
    llm = CaptureWriterLLM()
    state = {
        "project_id": "p-style",
        "current_chapter_index": 0,
        "chapter_word_target": 800,
        "style_constraint": "冷峻克制，短句，减少抒情。",
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
    out1 = asyncio.run(write_chapter_node(state, llm=llm, chapter_store=store))
    out2 = asyncio.run(refine_chapter_node({**state, **out1}, llm=llm, chapter_store=store))
    assert out2["current_chapter_final"].startswith("# ")
    assert any("【文风约束】" in p and "冷峻克制" in p for p in llm.prompts)
    assert any("请严格遵守上述文风约束" in p for p in llm.prompts)

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
    test_plan_outline_multi_phase_batches_and_rag()
    test_plan_outline_extend_with_repair_and_continuity_fields()
    test_plan_outline_extend_skip_repair_for_written_chapters()
    test_write_and_refine_chapter()
    test_write_and_refine_prompt_contains_style_constraint()
    test_workflow_runs_and_checkpoint_available()
    print("Phase 2 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
