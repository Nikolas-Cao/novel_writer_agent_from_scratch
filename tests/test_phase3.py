"""
阶段 3 验收测试：RAG 与人物图谱接入写章 + post_chapter_update。
运行：py tests/test_phase3.py  或  py -m pytest tests/test_phase3.py -v
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
    base = _root / "tests_tmp" / f"phase3_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class CaptureWriterLLM:
    def __init__(self) -> None:
        self.last_prompt = ""

    async def ainvoke(self, prompt: str):
        self.last_prompt = prompt
        if "润色" in prompt:
            return _Resp("# 第二章\n\n润色后的正文。")
        return _Resp("# 第二章\n\n初稿正文。")


class Stage3PlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["概要A：主角在雨城追查失踪案。"]}')
        if "【plan_outline_single】" in prompt and '"volumes"' in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章","points":["线索出现"]},{"title":"第二章","points":["追查升级"]}]}]}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp(
                '{"nodes":[{"id":"lin","name":"林岚","description":"调查记者"}],'
                '"edges":[{"from_id":"lin","to_id":"old","relation":"追查","note":"围绕旧案"}]}'
            )
        return _Resp("这一章中主角追查线索，冲突升级，并锁定下一步行动。")


def test_write_chapter_with_rag_and_character_context():
    from graph.nodes.write_chapter import write_chapter_node
    from rag import LocalRagIndexer, LocalRagRetriever
    from storage import ChapterStore, CharacterGraphStore

    root = _tmp_root()
    project_id = "p3-write"
    chapter_store = ChapterStore(root=root / "projects")
    graph_store = CharacterGraphStore(root=root / "projects")
    indexer = LocalRagIndexer(root=root / "vector")
    retriever = LocalRagRetriever(root=root / "vector")
    writer = CaptureWriterLLM()

    indexer.add_chapter_summary(project_id, 0, "第0章摘要：主角发现神秘符号。")
    indexer.add_outline_chunk(project_id, "第1章大纲补充：追查线索来源。", volume_idx=0, chapter_idx=1)
    graph_store.save(
        project_id,
        {
            "nodes": [
                {"id": "lin", "name": "林岚", "first_chapter": 0},
                {"id": "old", "name": "老周", "first_chapter": 0},
            ],
            "edges": [
                {"from_id": "lin", "to_id": "old", "relation": "同盟", "first_chapter": 0}
            ],
        },
    )
    state = {
        "project_id": project_id,
        "current_chapter_index": 1,
        "chapter_word_target": 800,
        "outline_structure": {
            "volumes": [
                {
                    "volume_title": "第一卷",
                    "chapters": [{"title": "第一章", "points": ["发现符号"]}, {"title": "第二章", "points": ["追查来源"]}],
                }
            ]
        },
        "chapters": [],
    }
    out = asyncio.run(
        write_chapter_node(
            state,
            llm=writer,
            chapter_store=chapter_store,
            rag_retriever=retriever,
            graph_store=graph_store,
        )
    )

    assert "前文摘要（RAG）" in writer.last_prompt
    assert "第0章摘要：主角发现神秘符号。" in writer.last_prompt
    assert "相关人物与关系摘要" in writer.last_prompt
    assert "林岚" in writer.last_prompt
    assert out["retrieved_summaries"]
    assert out["character_context_summary"]

    shutil.rmtree(root, ignore_errors=True)


def test_post_chapter_updates_rag_and_character_graph():
    from graph.nodes.post_chapter import post_chapter_node
    from rag import LocalRagIndexer, LocalRagRetriever
    from storage import ChapterStore, CharacterGraphStore

    root = _tmp_root()
    project_id = "p3-post"
    chapter_store = ChapterStore(root=root / "projects")
    graph_store = CharacterGraphStore(root=root / "projects")
    indexer = LocalRagIndexer(root=root / "vector")
    retriever = LocalRagRetriever(root=root / "vector")
    planner = Stage3PlannerLLM()

    out = asyncio.run(
        post_chapter_node(
            {
                "project_id": project_id,
                "current_chapter_index": 1,
                "current_chapter_final": "# 第二章\n\n林岚继续追查旧案。",
            },
            llm=planner,
            chapter_store=chapter_store,
            rag_indexer=indexer,
            graph_store=graph_store,
        )
    )

    assert "last_chapter_summary" in out
    rag_for_next = retriever.retrieve_for_chapter(project_id, current_chapter_index=2, k_chapters=2, k_outline=0)
    assert rag_for_next["summaries"]
    merged = graph_store.load(project_id)
    assert any(node.get("name") == "林岚" for node in merged["nodes"])
    assert any(edge.get("relation") == "追查" for edge in merged["edges"])

    shutil.rmtree(root, ignore_errors=True)


def test_stage3_workflow_runs():
    from graph.workflow import build_stage3_workflow
    from rag import LocalRagIndexer, LocalRagRetriever
    from storage import ChapterStore, CharacterGraphStore

    root = _tmp_root()
    chapter_store = ChapterStore(root=root / "projects")
    graph_store = CharacterGraphStore(root=root / "projects")
    indexer = LocalRagIndexer(root=root / "vector")
    retriever = LocalRagRetriever(root=root / "vector")
    planner = Stage3PlannerLLM()
    writer = CaptureWriterLLM()

    app, _ = build_stage3_workflow(
        planner_llm=planner,
        writer_llm=writer,
        chapter_store=chapter_store,
        rag_retriever=retriever,
        rag_indexer=indexer,
        graph_store=graph_store,
        use_local_checkpointer=False,
    )

    init_state = {
        "instruction": "都市悬疑",
        "selected_plot_summary": "概要A：主角在雨城追查失踪案。",
        "project_id": "p3-flow",
        "current_chapter_index": 1,
        "chapter_word_target": 800,
        "chapters": [],
    }
    out = app.invoke(init_state)
    assert "last_chapter_summary" in out
    assert "character_graph" in out

    rag_for_next = retriever.retrieve_for_chapter("p3-flow", current_chapter_index=2, k_chapters=3, k_outline=0)
    assert rag_for_next["summaries"]

    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_write_chapter_with_rag_and_character_context()
    test_post_chapter_updates_rag_and_character_graph()
    test_stage3_workflow_runs()
    print("Phase 3 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
