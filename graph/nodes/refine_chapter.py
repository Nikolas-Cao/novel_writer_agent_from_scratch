"""
阶段 2 节点：润色当前章并写回 ChapterStore（保持 Markdown）。
"""
from typing import Any, Dict, Optional

from graph.llm import create_writer_llm
from graph.utils import get_message_text, sanitize_chapter_markdown
from state import NovelProjectState
from storage import ChapterStore


async def refine_chapter_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    writer = llm or create_writer_llm()
    store = chapter_store or ChapterStore()

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not project_id:
        raise ValueError("project_id is required.")

    draft = state.get("current_chapter_draft", "").strip()
    if not draft:
        try:
            draft = store.load(project_id, current_idx)
        except FileNotFoundError:
            draft = ""
    if not draft:
        raise ValueError("No chapter draft found for refinement.")

    prompt = (
        "你是小说编辑，请润色以下章节。\n"
        "要求：\n"
        "1) 保持 Markdown 格式；\n"
        "2) 修复语病并增强文学性；\n"
        "3) 不改变核心剧情与章节结构；\n"
        "4) 仅输出润色后的章节正文，禁止输出“核心亮点/说明/总结/点评”；\n"
        "5) 禁止输出 Markdown 代码围栏（不要出现 ```markdown 或 ```）。\n\n"
        f"{draft}"
    )
    resp = await writer.ainvoke(prompt)
    final_text = sanitize_chapter_markdown(get_message_text(resp))
    if not final_text:
        final_text = draft

    ref = store.save(project_id, current_idx, final_text)

    chapters = list(state.get("chapters", []))
    for item in chapters:
        if int(item.get("index", -1)) == current_idx:
            item["path_or_content_ref"] = ref
            item["word_count"] = len(final_text.replace("\n", ""))
            item["summary"] = final_text[:120]

    return {
        "current_chapter_final": final_text,
        "chapters": chapters,
    }


def _run_test_first_three_chapters():
    """
    使用已实现的 node（generate_plot_ideas、plan_outline、write_chapter）生成大纲与初稿，
    再对 refine_chapter_node 只测前三章（每章：先 write 再 refine）。
    直接运行本文件时执行：python -m graph.nodes.refine_chapter
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
    from graph.nodes.write_chapter import write_chapter_node
    from rag import LocalRagIndexer

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

        # 2) 生成大纲（plan_outline 会顺带把每章大纲写入 RAG）
        project_id = "test_refine_3ch_" + uuid.uuid4().hex[:8]
        indexer = LocalRagIndexer()
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

        # 3) 前三章：每章先 write 再 refine
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
            out = await write_chapter_node(current)
            current["chapters"] = out.get("chapters", [])
            current["current_chapter_draft"] = out.get("current_chapter_draft", "")
            # 润色
            refine_out = await refine_chapter_node(current)
            current["chapters"] = refine_out.get("chapters", current["chapters"])
            current["current_chapter_final"] = refine_out.get("current_chapter_final", "")
            # 测试中未跑 post_chapter，用润色后正文前 500 字写入 RAG，供下一章检索
            summary = (current.get("current_chapter_final") or "")[:500]
            if summary:
                indexer.add_chapter_summary(project_id, idx, summary)
            meta = next((c for c in current["chapters"] if c.get("index") == idx), None)
            title = meta.get("title", f"第{idx + 1}章") if meta else f"第{idx + 1}章"
            wc = meta.get("word_count", 0) if meta else 0
            print(f"  第{idx + 1}章 write+refine 完成: {title}, 字数约 {wc}")

        assert len(current["chapters"]) == 3
        print("通过：前三章均已 write 并 refine，chapters 数量 =", len(current["chapters"]))
        return current

    return asyncio.run(_run())


if __name__ == "__main__":
    _run_test_first_three_chapters()
