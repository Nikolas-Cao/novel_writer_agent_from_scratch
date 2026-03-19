"""
阶段 1 验收测试：storage、memory/checkpoint、RAG 本地向量库。
运行：py tests/test_phase1.py  或  py -m pytest tests/test_phase1.py -v
"""
import shutil
import sys
import uuid
from pathlib import Path


# 保证从项目根可导入模块
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"phase1_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_chapter_store_save_load():
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root)
    project_id = "p1"
    content = "# Chapter 1\n\nhello"

    ref = store.save(project_id, 0, content)
    expected = root / project_id / "chapters" / "000.md"
    assert expected.exists()
    assert ref == f"{project_id}/chapters/000.md"
    assert store.load(project_id, 0) == content
    assert store.load_by_ref(ref) == content

    shutil.rmtree(root, ignore_errors=True)


def test_character_graph_store_save_load_merge():
    from storage import CharacterGraphStore

    root = _tmp_root()
    store = CharacterGraphStore(root=root)
    project_id = "p2"
    graph = {
        "nodes": [{"id": "a", "name": "Alice"}],
        "edges": [{"from_id": "a", "to_id": "b", "relation": "knows"}],
    }
    store.save(project_id, graph)

    path = root / project_id / "character_graph.json"
    assert path.exists()
    loaded = store.load(project_id)
    assert loaded["nodes"][0]["id"] == "a"

    merged = store.merge(
        project_id,
        new_nodes=[{"id": "a", "description": "updated"}, {"id": "b", "name": "Bob"}],
        new_edges=[{"from_id": "a", "to_id": "b", "relation": "knows", "note": "old friends"}],
    )
    assert len(merged["nodes"]) == 2
    assert len(merged["edges"]) == 1
    assert any(node.get("id") == "a" and node.get("description") == "updated" for node in merged["nodes"])

    shutil.rmtree(root, ignore_errors=True)


def test_local_file_checkpointer():
    from memory import LocalFileCheckpointer

    root = _tmp_root()
    cp = LocalFileCheckpointer(root=root)
    thread_id = "thread-x"
    state = {"project_id": "p3", "current_chapter_index": 2}
    path = cp.save_state(thread_id, state)
    assert path.exists()
    loaded = cp.load_state(thread_id)
    assert loaded == state

    shutil.rmtree(root, ignore_errors=True)


def test_rag_indexer_and_retriever():
    from rag import LocalRagIndexer, LocalRagRetriever

    root = _tmp_root()
    project_id = "p4"
    indexer = LocalRagIndexer(root=root)
    retriever = LocalRagRetriever(root=root)

    indexer.add_chapter_summary(project_id, 0, "第0章摘要")
    indexer.add_chapter_summary(project_id, 1, "第1章摘要")
    indexer.add_chapter_summary(project_id, 2, "第2章摘要")
    indexer.add_outline_chunk(project_id, "第3章大纲片段（旧）", volume_idx=0, chapter_idx=3)
    # 模拟大纲重排后同一章出现新 chunk：检索应优先返回最新版本
    indexer.add_outline_chunk(project_id, "第3章大纲片段（新）", volume_idx=1, chapter_idx=3)

    res = retriever.retrieve_for_chapter(project_id, current_chapter_index=3, k_chapters=2, k_outline=1)
    assert "summaries" in res
    assert "outline_chunk" in res
    assert [item["chapter_index"] for item in res["summaries"]] == [1, 2]
    assert "第1章摘要" in res["summaries"][0]["text"]
    assert "第2章摘要" in res["summaries"][1]["text"]
    assert "第3章大纲片段（新）" in res["outline_chunk"]
    assert "第3章大纲片段（旧）" not in res["outline_chunk"]

    # Windows 下 Chroma 可能短时占用文件句柄，delete_project 只要求不抛错。
    indexer.delete_project(project_id)

    shutil.rmtree(root, ignore_errors=True)


def test_try_create_sqlite_checkpointer_optional():
    """
    环境里若未安装 sqlite checkpointer 扩展，允许返回 None；
    若安装了则应返回对象（阶段 2 会在 workflow 编译时接入验证）。
    """
    from memory import try_create_sqlite_checkpointer

    cp = try_create_sqlite_checkpointer()
    if cp is not None:
        # 仅验证对象被成功创建且看起来是可用 checkpointer
        assert hasattr(cp, "get") or hasattr(cp, "put") or hasattr(cp, "aput")


def run_all():
    test_chapter_store_save_load()
    test_character_graph_store_save_load_merge()
    test_local_file_checkpointer()
    test_rag_indexer_and_retriever()
    test_try_create_sqlite_checkpointer_optional()
    print("Phase 1 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
