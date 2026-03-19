"""
阶段 4 验收测试：rewrite_with_feedback、update_outline_from_feedback、workflow 条件边。
运行：py tests/test_phase4.py  或  py -m pytest tests/test_phase4.py -v
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
    base = _root / "tests_tmp" / f"phase4_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class Stage4WriterLLM:
    async def ainvoke(self, prompt: str):
        if "根据用户反馈重写" in prompt:
            return _Resp("# 第一章\n\n重写后结尾更悬疑，黑影在门外停住。")
        if "润色" in prompt:
            return _Resp("# 第一章\n\n润色后的章节内容。")
        return _Resp("# 第一章\n\n初稿内容。")


class Stage4PlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["概要A：雨城悬案。"]}')
        if '"volumes"' in prompt and "剧情概要" in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章","points":["旧案重启","发现线索"]},{"title":"第二章","points":["追查升级"]}]}]}'
            )
        if "根据用户反馈和重写后章节，更新大纲要点" in prompt:
            return _Resp(
                '{"current_chapter_points":["结尾改为悬疑停顿","增加黑影伏笔"],'
                '"next_chapters_updates":[{"chapter_index":1,"points":["围绕黑影展开调查","主角遭遇反追踪"]}]}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp(
                '{"nodes":[{"id":"hero","name":"主角"}],'
                '"edges":[{"from_id":"hero","to_id":"shadow","relation":"追查"}]}'
            )
        return _Resp("本章摘要：主角重写后在结尾留下悬疑伏笔。")


def test_rewrite_with_feedback_updates_chapter_file():
    from graph.nodes.rewrite_feedback import rewrite_with_feedback_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root / "projects")
    project_id = "p4-rewrite"
    store.save(project_id, 0, "# 第一章\n\n原始结尾平淡。")

    state = {
        "project_id": project_id,
        "current_chapter_index": 0,
        "user_feedback": "把结尾改得更悬疑",
        "current_chapter_final": "# 第一章\n\n原始结尾平淡。",
        "chapters": [{"index": 0, "title": "第一章", "path_or_content_ref": f"{project_id}/chapters/000.md"}],
    }
    out = asyncio.run(rewrite_with_feedback_node(state, llm=Stage4WriterLLM(), chapter_store=store))
    content = store.load(project_id, 0)

    assert "悬疑" in content
    assert out["last_rewrite_draft"]
    assert out["current_chapter_final"].startswith("# ")

    shutil.rmtree(root, ignore_errors=True)


def test_update_outline_from_feedback_updates_points():
    from graph.nodes.update_outline import update_outline_from_feedback_node

    state = {
        "user_feedback": "把结尾改得更悬疑",
        "last_rewrite_draft": "# 第一章\n\n重写后结尾更悬疑。",
        "current_chapter_index": 0,
        "outline_structure": {
            "volumes": [
                {
                    "volume_title": "第一卷",
                    "chapters": [
                        {"title": "第一章", "points": ["旧案重启"]},
                        {"title": "第二章", "points": ["追查升级"]},
                    ],
                }
            ]
        },
    }
    out = asyncio.run(update_outline_from_feedback_node(state, llm=Stage4PlannerLLM()))
    new_outline = out["outline_structure"]
    ch0_points = new_outline["volumes"][0]["chapters"][0]["points"]
    ch1_points = new_outline["volumes"][0]["chapters"][1]["points"]
    assert "悬疑" in "".join(ch0_points)
    assert "黑影" in "".join(ch1_points)


def test_stage4_workflow_conditional_edges():
    from graph.workflow import build_stage4_workflow
    from rag import LocalRagIndexer, LocalRagRetriever
    from storage import ChapterStore, CharacterGraphStore

    root = _tmp_root()
    chapter_store = ChapterStore(root=root / "projects")
    graph_store = CharacterGraphStore(root=root / "projects")
    indexer = LocalRagIndexer(root=root / "vector")
    retriever = LocalRagRetriever(root=root / "vector")

    app, _ = build_stage4_workflow(
        planner_llm=Stage4PlannerLLM(),
        writer_llm=Stage4WriterLLM(),
        chapter_store=chapter_store,
        rag_retriever=retriever,
        rag_indexer=indexer,
        graph_store=graph_store,
        use_local_checkpointer=False,
    )

    init_state = {
        "instruction": "都市悬疑",
        "selected_plot_summary": "概要A：雨城悬案。",
        "project_id": "p4-flow",
        "current_chapter_index": 0,
        "chapter_word_target": 700,
        "chapters": [],
        "user_feedback": "把结尾改得更悬疑",
        "update_outline_on_feedback": True,
    }
    out = app.invoke(init_state)

    assert out.get("last_rewrite_draft")
    assert out.get("outline_structure")
    assert out.get("last_chapter_summary")

    ch0_points = out["outline_structure"]["volumes"][0]["chapters"][0]["points"]
    assert "悬疑" in "".join(ch0_points)

    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_rewrite_with_feedback_updates_chapter_file()
    test_update_outline_from_feedback_updates_points()
    test_stage4_workflow_conditional_edges()
    print("Phase 4 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
