"""
阶段 7 验收测试：章节插图识别/生成/插入与端到端流程。
运行：py tests/test_phase7.py  或  py -m pytest tests/test_phase7.py -v
"""
import asyncio
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"phase7_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class IllustPlannerLLM:
    async def ainvoke(self, prompt: str):
        if "plot_ideas" in prompt:
            return _Resp('{"plot_ideas":["概要A：雨城追案。"]}')
        if "【plan_outline_single】" in prompt and '"volumes"' in prompt:
            return _Resp(
                '{"volumes":[{"volume_title":"第一卷","chapters":[{"title":"第一章 雨夜","points":["案发","追查"]}]}]}'
            )
        if "最适合插入一张插图" in prompt or "illustration_point" in prompt:
            return _Resp(
                '{"illustration_point":{"position_describe":"主角看到案发现场后",'
                '"anchor_text":"她站在雨中的巷口","alt":"雨夜巷口"},'
                '"illustration_prompt":"rainy night alley, detective silhouette, cinematic lighting, no text"}'
            )
        if "抽取人物节点与关系边" in prompt:
            return _Resp(
                '{"nodes":[{"id":"hero","name":"主角"}],'
                '"edges":[{"from_id":"hero","to_id":"case","relation":"调查"}]}'
            )
        return _Resp("本章摘要：主角抵达案发现场并展开追查。")


class IllustWriterLLM:
    async def ainvoke(self, prompt: str):
        if "润色" in prompt:
            return _Resp("# 第一章 雨夜\n\n她站在雨中的巷口，霓虹在积水中扭曲。")
        return _Resp("# 第一章 雨夜\n\n她站在雨中的巷口，霓虹在积水中扭曲。")


def test_identify_illustration_points():
    from graph.nodes.identify_illustration_points import identify_illustration_points_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root / "projects")
    project_id = "p7-identify"
    store.save(project_id, 0, "# 第一章\n\n她站在雨中的巷口，霓虹在积水中扭曲。")
    state = {
        "project_id": project_id,
        "current_chapter_index": 0,
        "enable_chapter_illustrations": True,
    }
    out = asyncio.run(
        identify_illustration_points_node(
            state,
            llm=IllustPlannerLLM(),
            chapter_store=store,
        )
    )
    assert out.get("illustration_prompt")
    assert out.get("illustration_point", {}).get("anchor_text")
    points = out["illustration_points"]
    assert points and isinstance(points, list)
    assert "image_query" in points[0]

    shutil.rmtree(root, ignore_errors=True)


def _fake_openai_generate(**kwargs):
    root = Path(kwargs["project_root"])
    project_id = kwargs["project_id"]
    chapter_index = kwargs["chapter_index"]
    rel = f"{project_id}/chapters/{int(chapter_index):03d}/images/000_01_test.png"
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"\x89PNG\r\n\x1a\n")
    return rel, "image/png", 42, 7


def test_fetch_or_generate_images():
    from graph.nodes.fetch_or_generate_images import fetch_or_generate_images_node

    root = _tmp_root()
    state = {
        "project_id": "p7-fetch",
        "current_chapter_index": 0,
        "enable_chapter_illustrations": True,
        "illustration_prompt": "test prompt",
        "illustration_point": {
            "position_describe": "段落后",
            "anchor_text": "她站在雨中的巷口",
            "alt": "雨夜巷口",
        },
        "illustration_points": [
            {
                "position_describe": "段落后",
                "anchor_text": "她站在雨中的巷口",
                "image_query": "雨夜巷口 侦探 氛围",
                "alt": "雨夜巷口",
            }
        ],
    }
    with patch(
        "graph.nodes.fetch_or_generate_images.generate_openai_chapter_image",
        side_effect=_fake_openai_generate,
    ):
        out = asyncio.run(fetch_or_generate_images_node(state, project_root=root / "projects"))
    assets = out["illustration_assets"]
    assert assets and assets[0]["image_path"].endswith(".png")
    assert assets[0]["source"] == "openai"
    img_path = root / "projects" / assets[0]["image_path"]
    assert img_path.exists()
    from config import IMAGE_GEN_MODEL

    tu = out.get("token_usage") or {}
    bucket = tu.get(str(IMAGE_GEN_MODEL), {})
    assert bucket.get("input_tokens") == 42
    assert bucket.get("output_tokens") == 7

    shutil.rmtree(root, ignore_errors=True)


def test_insert_illustrations_fuzzy_anchor_not_appended_to_end():
    """锚点与正文仅有空白差异时仍应插在首段锚点之后，而非追加在全文末尾。"""
    from graph.nodes.insert_illustrations_into_chapter import insert_illustrations_into_chapter_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root / "projects")
    project_id = "p7-fuzzy"
    body = "# 第一章\n\n第一段正文在此。\n\n第二段继续展开情节。"
    store.save(project_id, 0, body)
    rel = f"{project_id}/chapters/000/images/demo.png"
    state = {
        "project_id": project_id,
        "current_chapter_index": 0,
        "enable_chapter_illustrations": True,
        "chapters": [{"index": 0, "title": "第一章", "path_or_content_ref": f"{project_id}/chapters/000.md"}],
        "illustration_assets": [
            {
                # 与正文相比多出空格，旧逻辑会整章追加到「第二段」之后
                "anchor_text": "第一段  正文  在此。",
                "image_path": rel,
                "alt": "test",
                "generation_prompt": "x",
            }
        ],
    }
    out = asyncio.run(insert_illustrations_into_chapter_node(state, chapter_store=store))
    text = out["current_chapter_final"]
    assert '<img class="chapter-illustration"' in text
    assert text.index("chapter-illustration") < text.index("第二段")

    shutil.rmtree(root, ignore_errors=True)


def test_insert_illustrations_into_chapter():
    from graph.nodes.insert_illustrations_into_chapter import insert_illustrations_into_chapter_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root / "projects")
    project_id = "p7-insert"
    store.save(project_id, 0, "# 第一章\n\n她站在雨中的巷口，霓虹在积水中扭曲。")
    rel = f"{project_id}/chapters/000/images/demo.png"
    state = {
        "project_id": project_id,
        "current_chapter_index": 0,
        "enable_chapter_illustrations": True,
        "chapters": [{"index": 0, "title": "第一章", "path_or_content_ref": f"{project_id}/chapters/000.md"}],
        "illustration_assets": [
            {
                "anchor_text": "她站在雨中的巷口",
                "image_path": rel,
                "alt": "雨夜巷口",
                "generation_prompt": "rainy alley test prompt",
            }
        ],
    }
    out = asyncio.run(insert_illustrations_into_chapter_node(state, chapter_store=store))
    text = store.load(project_id, 0)
    assert '<img class="chapter-illustration"' in text
    assert 'title="rainy alley test prompt"' in text
    assert rel in text
    assert out["current_chapter_final"] == text

    shutil.rmtree(root, ignore_errors=True)


def test_stage7_end_to_end():
    from graph.workflow import build_stage7_workflow
    from rag import LocalRagIndexer, LocalRagRetriever
    from storage import ChapterStore, CharacterGraphStore

    root = _tmp_root()
    chapter_store = ChapterStore(root=root / "projects")
    graph_store = CharacterGraphStore(root=root / "projects")
    indexer = LocalRagIndexer(root=root / "vector")
    retriever = LocalRagRetriever(root=root / "vector")

    app, _ = build_stage7_workflow(
        planner_llm=IllustPlannerLLM(),
        writer_llm=IllustWriterLLM(),
        chapter_store=chapter_store,
        rag_retriever=retriever,
        rag_indexer=indexer,
        graph_store=graph_store,
        use_local_checkpointer=False,
    )
    with patch(
        "graph.nodes.fetch_or_generate_images.generate_openai_chapter_image",
        side_effect=_fake_openai_generate,
    ):
        out = app.invoke(
            {
                "instruction": "都市悬疑",
                "selected_plot_summary": "概要A：雨城追案。",
                "project_id": "p7-flow",
                "current_chapter_index": 0,
                "chapter_word_target": 700,
                "chapters": [],
                "enable_chapter_illustrations": True,
            }
        )
    chapter_text = chapter_store.load("p7-flow", 0)
    assert "chapter-illustration" in chapter_text
    assert "title=" in chapter_text
    assert out.get("last_chapter_summary")

    shutil.rmtree(root, ignore_errors=True)


def run_all():
    test_identify_illustration_points()
    test_fetch_or_generate_images()
    test_insert_illustrations_fuzzy_anchor_not_appended_to_end()
    test_insert_illustrations_into_chapter()
    test_stage7_end_to_end()
    print("Phase 7 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
