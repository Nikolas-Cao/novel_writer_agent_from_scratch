"""
阶段 4 节点：根据用户反馈重写当前章，并写回 ChapterStore。
"""
from typing import Any, Dict, Optional

from config import CHAPTER_WORD_TARGET
from graph.llm import create_writer_llm
from graph.utils import get_message_text, sanitize_chapter_markdown
from state import NovelProjectState
from storage import ChapterStore


async def rewrite_with_feedback_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
) -> Dict[str, Any]:
    writer = llm or create_writer_llm()
    store = chapter_store or ChapterStore()

    project_id = state.get("project_id", "").strip()
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    feedback = state.get("user_feedback", "").strip()
    if not project_id:
        raise ValueError("project_id is required.")
    if not feedback:
        return {}

    chapter_text = state.get("current_chapter_final") or state.get("current_chapter_draft") or ""
    if not chapter_text:
        chapter_text = store.load(project_id, current_idx)

    word_target = int(state.get("chapter_word_target", CHAPTER_WORD_TARGET) or CHAPTER_WORD_TARGET)

    prompt = (
        "请根据用户反馈重写这一章。\n"
        "要求：\n"
        "1) 保持 Markdown 格式；\n"
        "2) 保持章节主线不偏题；\n"
        "3) 重点满足反馈要求；\n"
        "4) 字数接近目标字数；\n"
        "5) 仅输出重写后的章节正文，禁止输出“核心亮点/说明/总结/点评”；\n"
        "6) 禁止输出 Markdown 代码围栏（不要出现 ```markdown 或 ```）。\n\n"
        f"目标字数：约{word_target}字\n\n"
        f"用户反馈：{feedback}\n\n"
        f"当前章节：\n{chapter_text}"
    )
    resp = await writer.ainvoke(prompt)
    rewritten = sanitize_chapter_markdown(get_message_text(resp))
    if not rewritten:
        rewritten = chapter_text

    ref = store.save(project_id, current_idx, rewritten)

    chapters = list(state.get("chapters", []))
    for item in chapters:
        if int(item.get("index", -1)) == current_idx:
            item["path_or_content_ref"] = ref
            item["word_count"] = len(rewritten.replace("\n", ""))
            item["summary"] = rewritten[:120]

    return {
        "current_chapter_final": rewritten,
        "last_rewrite_draft": rewritten,
        "chapters": chapters,
    }


def _run_test_first_three_chapters():
    """
    通过 generate_plot_ideas、plan_outline、write_chapter、refine_chapter、post_chapter 生成 state，
    再对 rewrite_with_feedback_node 只测前三章；user_feedback 由测试自动生成并注入，无需用户输入。
    每章流程：write -> refine -> 注入反馈并 rewrite -> post_chapter（摘要入 RAG + 人物图谱）。
    直接运行本文件时执行：python -m graph.nodes.rewrite_feedback
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
    from graph.nodes.post_chapter import post_chapter_node
    from graph.nodes.refine_chapter import refine_chapter_node
    from graph.nodes.write_chapter import write_chapter_node
    from rag import LocalRagIndexer
    from storage import CharacterGraphStore

    # 前三章各自注入的反馈（自动生成，无需用户输入）
    FEEDBACK_BY_CHAPTER = [
        "请加强本章开头的氛围描写，让读者更快进入情境。",
        "请让本章对话更简洁有力，减少冗余叙述。",
        "请在本章末尾增加一处伏笔，暗示下一卷的冲突。",
    ]

    async def _run():
        # 1) 生成剧情概要（使用默认创作意图）
        instruction = "一名少年在异世界觉醒能力，从弱小逐步成长并改变世界。"
        state = {"instruction": instruction}
        ideas_out = await generate_plot_ideas_node(state)
        plot_ideas = ideas_out.get("plot_ideas") or []
        if not plot_ideas:
            raise RuntimeError("generate_plot_ideas 未返回任何剧情概要")
        selected_plot_summary = plot_ideas[0]
        print("已选第一条剧情概要，长度:", len(selected_plot_summary))

        # 2) 生成大纲并写入 RAG
        project_id = "test_rewrite_3ch_" + uuid.uuid4().hex[:8]
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

        # 3) 前三章：每章 write -> refine -> 注入反馈并 rewrite -> post_chapter
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
            # 注入自动生成的反馈并重写
            current["user_feedback"] = FEEDBACK_BY_CHAPTER[idx]
            rewrite_out = await rewrite_with_feedback_node(current)
            if rewrite_out:
                current["current_chapter_final"] = rewrite_out.get("current_chapter_final", "")
                current["last_rewrite_draft"] = rewrite_out.get("last_rewrite_draft", "")
                current["chapters"] = rewrite_out.get("chapters", current["chapters"])
            # 后处理：摘要写入 RAG + 人物图谱（与正式流程一致）
            post_out = await post_chapter_node(
                current,
                rag_indexer=indexer,
                graph_store=graph_store,
            )
            current["last_chapter_summary"] = post_out.get("last_chapter_summary", "")
            current["character_graph"] = post_out.get("character_graph", {})
            meta = next((c for c in current["chapters"] if c.get("index") == idx), None)
            title = meta.get("title", f"第{idx + 1}章") if meta else f"第{idx + 1}章"
            wc = meta.get("word_count", 0) if meta else 0
            summary_len = len(current.get("last_chapter_summary", ""))
            nodes_count = len(current.get("character_graph", {}).get("nodes", []))
            print(f"  第{idx + 1}章 write+refine+rewrite+post 完成: {title}, 字数约 {wc}, 摘要 {summary_len}, 人物数 {nodes_count}")
            fb = FEEDBACK_BY_CHAPTER[idx]
            print(f"    注入反馈: {fb[:50]}{'...' if len(fb) > 50 else ''}")

        assert len(current["chapters"]) == 3
        print("通过：前三章均已 write、refine、rewrite_with_feedback、post_chapter，chapters 数量 =", len(current["chapters"]))
        return current

    return asyncio.run(_run())


if __name__ == "__main__":
    _run_test_first_three_chapters()
