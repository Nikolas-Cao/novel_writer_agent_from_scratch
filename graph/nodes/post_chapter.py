"""
阶段 3 节点：章节后处理。
1) 生成章节摘要并写入本地 RAG 索引
2) 抽取人物与关系并合并到人物图谱
"""
from typing import Any, Dict, List, Optional

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, get_message_text, invoke_and_parse_with_retry
from rag import LocalRagIndexer
from state import CharacterEdge, CharacterNode, NovelProjectState
from storage import ChapterStore, CharacterGraphStore


def _fallback_extract_characters(text: str) -> List[CharacterNode]:
    # 轻量兜底：按常见中文姓名长度切分并去重（仅用于模型输出异常时）
    tokens = [t.strip("，。！？；：、“”‘’（）()[] ") for t in text.split() if t.strip()]
    out: List[CharacterNode] = []
    seen = set()
    for tok in tokens:
        if 2 <= len(tok) <= 4 and tok not in seen:
            seen.add(tok)
            out.append({"id": tok, "name": tok})
        if len(out) >= 8:
            break
    return out


async def post_chapter_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    graph_store: Optional[CharacterGraphStore] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    store = chapter_store or ChapterStore()
    indexer = rag_indexer or LocalRagIndexer()
    cgraph_store = graph_store or CharacterGraphStore()

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not project_id:
        raise ValueError("project_id is required.")

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    summary_prompt = (
        "请将下面章节内容总结为 200-500 字摘要，包含关键事件与人物变化，输出纯文本。\n\n"
        f"{chapter_text}"
    )
    summary_resp = await planner.ainvoke(summary_prompt)
    chapter_summary = get_message_text(summary_resp).strip()
    if not chapter_summary:
        chapter_summary = chapter_text[:300]
    indexer.add_chapter_summary(project_id, current_idx, chapter_summary)

    extract_prompt = (
        "从以下章节中抽取人物节点与关系边，并仅输出 JSON：\n"
        '{"nodes":[{"id":"id","name":"姓名","description":"可选"}],'
        '"edges":[{"from_id":"id1","to_id":"id2","relation":"关系","note":"可选"}]}\n\n'
        f"{chapter_text}"
    )
    new_nodes: List[CharacterNode] = []
    new_edges: List[CharacterEdge] = []
    try:
        obj = await invoke_and_parse_with_retry(
            planner, extract_prompt, extract_json_object, max_retries=3
        )
        new_nodes = list(obj.get("nodes", []))
        new_edges = list(obj.get("edges", []))
    except Exception:
        new_nodes = _fallback_extract_characters(chapter_text)
        new_edges = []

    # 为本章抽取的边打上 first_chapter，供写章时滑动窗口过滤
    for e in new_edges:
        e["first_chapter"] = current_idx

    base_graph = cgraph_store.load_for_chapter(project_id, current_idx - 1)
    merged_graph = cgraph_store.merge(
        project_id,
        new_nodes,
        new_edges,
        chapter_index=current_idx,
        base_graph=base_graph,
    )
    return {
        "last_chapter_summary": chapter_summary,
        "character_graph": merged_graph,
    }


def _run_test_first_three_chapters():
    """
    通过 generate_plot_ideas、plan_outline、write_chapter、refine_chapter 生成 state，
    再对 post_chapter_node 只测前三章（每章：write -> refine -> post_chapter）。
    直接运行本文件时执行：python -m graph.nodes.post_chapter
    """
    import asyncio
    import sys
    import uuid
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from config import CHAPTER_WORD_TARGET
    from graph.nodes.generate_plot_ideas import generate_plot_ideas_node
    from graph.nodes.plan_outline import plan_outline_node
    from graph.nodes.refine_chapter import refine_chapter_node
    from graph.nodes.write_chapter import write_chapter_node
    from rag import LocalRagIndexer
    from storage import CharacterGraphStore

    instruction = input("请输入创作意图（instruction，直接回车用默认）: ").strip()
    if not instruction:
        instruction = "一名少年在异世界觉醒能力，从弱小逐步成长并改变世界。"
        print("使用默认创作意图。")

    async def _run():
        # 1) 生成剧情概要
        state = {"instruction": instruction}
        ideas_out = await generate_plot_ideas_node(state)
        plot_ideas = ideas_out.get("plot_ideas") or []
        if not plot_ideas:
            raise RuntimeError("generate_plot_ideas 未返回任何剧情概要")
        selected_plot_summary = plot_ideas[0]
        print("已选第一条剧情概要，长度:", len(selected_plot_summary))

        # 2) 生成大纲并写入 RAG
        project_id = "test_post_3ch_" + uuid.uuid4().hex[:8]
        indexer = LocalRagIndexer()
        graph_store = CharacterGraphStore()
        state = {
            "selected_plot_summary": selected_plot_summary,
            "total_chapters": 12,
            "project_id": project_id,
        }
        outline_out = await plan_outline_node(state, rag_indexer=indexer)
        outline_structure = outline_out.get("outline_structure") or {"volumes": []}
        if not outline_structure.get("volumes"):
            raise RuntimeError("plan_outline 未返回有效大纲")
        print("已生成大纲并写入 RAG，project_id:", project_id)

        # 3) 前三章：每章 write -> refine -> post_chapter
        base_state = {
            "project_id": project_id,
            "outline_structure": outline_structure,
            "chapters": [],
            "chapter_word_target": CHAPTER_WORD_TARGET,
        }
        current = dict(base_state)
        for idx in range(3):
            current["current_chapter_index"] = idx
            # 写章
            out = await write_chapter_node(current, graph_store=graph_store)
            current["chapters"] = out.get("chapters", [])
            current["current_chapter_draft"] = out.get("current_chapter_draft", "")
            # 润色
            refine_out = await refine_chapter_node(current)
            current["chapters"] = refine_out.get("chapters", current["chapters"])
            current["current_chapter_final"] = refine_out.get("current_chapter_final", "")
            # 后处理：摘要写入 RAG + 人物图谱合并
            post_out = await post_chapter_node(
                current,
                rag_indexer=indexer,
                graph_store=graph_store,
            )
            current["last_chapter_summary"] = post_out.get("last_chapter_summary", "")
            current["character_graph"] = post_out.get("character_graph", {})
            meta = next((c for c in current["chapters"] if c.get("index") == idx), None)
            title = meta.get("title", f"第{idx + 1}章") if meta else f"第{idx + 1}章"
            summary_len = len(current.get("last_chapter_summary", ""))
            nodes_count = len(current.get("character_graph", {}).get("nodes", []))
            print(f"  第{idx + 1}章 write+refine+post 完成: {title}, 摘要长度 {summary_len}, 人物数 {nodes_count}")

        assert len(current["chapters"]) == 3
        print("通过：前三章均已 write、refine、post_chapter，chapters 数量 =", len(current["chapters"]))

        # 打印人物关系
        graph = current.get("character_graph", {})
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        id_to_name = {str(n.get("id")): str(n.get("name") or n.get("id")) for n in nodes}
        print("\n人物关系：")
        if not edges:
            print("  （暂无）")
        for e in edges:
            from_id = e.get("from_id", "")
            to_id = e.get("to_id", "")
            rel = e.get("relation", "")
            note = e.get("note", "")
            from_name = id_to_name.get(str(from_id), from_id)
            to_name = id_to_name.get(str(to_id), to_id)
            line = f"  {from_name} -> {to_name}（{rel})"
            if note:
                line += f" [{note}]"
            print(line)

        return current

    return asyncio.run(_run())


if __name__ == "__main__":
    _run_test_first_three_chapters()
