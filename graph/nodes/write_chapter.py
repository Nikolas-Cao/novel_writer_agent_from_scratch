"""
阶段 3 节点：按大纲写当前章（Markdown），并写入 ChapterStore。
接入 RAG 检索结果与人物图谱摘要。
"""
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from config import (
    CHAPTER_WORD_TARGET,
    CHARACTER_GRAPH_RECENT_CHAPTERS,
    RAG_PREVIOUS_CHAPTERS,
    WRITE_CHAPTER_PREV_TAIL_CHARS,
)
from graph.chapter_prompt_defaults import DEFAULT_CHAPTER_ENDING_RULES
from graph.knowledge_context import build_kb_context_for_writing, format_canon_overrides
from graph.llm import create_writer_llm
from graph.utils import get_message_text, sanitize_chapter_markdown
from rag import LocalRagRetriever
from rag.global_kb_retriever import GlobalKbRetriever
from state import ChapterMeta, NovelProjectState
from storage import ChapterStore, CharacterGraphStore


def _get_chapter_outline(
    outline_structure: Dict[str, Any],
    chapter_index: int,
) -> Tuple[str, list]:
    if not outline_structure or not isinstance(outline_structure.get("volumes"), list):
        if chapter_index == 0:
            return f"第{chapter_index + 1}章", []
        raise IndexError(f"Chapter index out of range: {chapter_index}")
    idx = 0
    for volume in outline_structure["volumes"]:
        if not isinstance(volume, dict):
            continue
        for ch in volume.get("chapters", []):
            if not isinstance(ch, dict):
                idx += 1
                continue
            if idx == chapter_index:
                title = ch.get("title") or f"第{chapter_index + 1}章"
                points = ch.get("points") if isinstance(ch.get("points"), list) else []
                return title, points
            idx += 1
    raise IndexError(f"Chapter index out of range: {chapter_index}")


def _previous_chapter_tail_for_prompt(
    store: ChapterStore,
    project_id: str,
    current_chapter_index: int,
    max_chars: int,
) -> str:
    # 思路：
    # 1) RAG 摘要会丢失上一章结尾的细粒度动作/对话，与「章末约束」叠加时，下一章更难自然衔接；
    # 2) 从已落盘的上一章 Markdown 取末尾若干字，仅作衔接提示，不替代 RAG 的远距前文信息。
    # 边界：首章无前文；文件缺失或 max_chars<=0 时返回空串；超长时截断并加「略」提示以免模型误判为全文。
    if current_chapter_index <= 0 or max_chars <= 0:
        return ""
    prev_idx = current_chapter_index - 1
    try:
        full = store.load(project_id, prev_idx)
    except FileNotFoundError:
        logger.info(
            "[write_chapter] prev_chapter_tail_skip project=%s reason=no_file chapter_index=%s",
            project_id,
            prev_idx,
        )
        return ""
    text = full.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return "……（上文略）\n" + text[-max_chars:]
    return text


async def write_chapter_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    rag_retriever: Optional[LocalRagRetriever] = None,
    graph_store: Optional[CharacterGraphStore] = None,
    global_kb_retriever: Optional[GlobalKbRetriever] = None,
    planner_llm: Optional[Any] = None,
) -> Dict[str, Any]:
    writer = llm or create_writer_llm()
    store = chapter_store or ChapterStore()
    retriever = rag_retriever or LocalRagRetriever()
    cgraph_store = graph_store or CharacterGraphStore()

    project_id = state.get("project_id", "").strip()
    if not project_id:
        raise ValueError("project_id is required.")

    outline_structure = state.get("outline_structure") or {"volumes": []}
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    title, points = _get_chapter_outline(outline_structure, current_idx)
    word_target = int(state.get("chapter_word_target", CHAPTER_WORD_TARGET) or CHAPTER_WORD_TARGET)

    rag_ctx = retriever.retrieve_for_chapter(
        project_id=project_id,
        current_chapter_index=current_idx,
        k_chapters=RAG_PREVIOUS_CHAPTERS,
        k_outline=1,
    )
    summaries = rag_ctx.get("summaries", [])
    outline_chunk = rag_ctx.get("outline_chunk", "")

    # 角色上下文使用“上一章快照”，避免未来章节信息泄漏到当前章生成。
    # 只使用最近 N 章内出现的关系，再取前 8 节点、12 边。
    if current_idx <= 0:
        char_graph = {"nodes": [], "edges": []}
    else:
        char_graph = cgraph_store.load_for_chapter(project_id, current_idx - 1)
    all_edges = list(char_graph.get("edges", []))
    all_nodes = list(char_graph.get("nodes", []))
    window_start = max(0, current_idx - CHARACTER_GRAPH_RECENT_CHAPTERS)
    kept_edges = [
        e for e in all_edges
        if e.get("first_chapter") is not None and e["first_chapter"] >= window_start
    ]
    kept_node_ids = set()
    for e in kept_edges:
        if e.get("from_id"):
            kept_node_ids.add(str(e["from_id"]))
        if e.get("to_id"):
            kept_node_ids.add(str(e["to_id"]))
    kept_nodes = [n for n in all_nodes if n.get("id") is not None and str(n.get("id")) in kept_node_ids]
    char_nodes = kept_nodes[:8]
    char_edges = kept_edges[:12]
    character_summary = "（暂无人物图谱信息）"
    if char_nodes:
        # 构建 id -> name 的映射，方便通过 id 查名字
        id_to_name = {str(n.get("id")): str(n.get("name") or n.get("id")) for n in char_nodes}
        names = list(id_to_name.values())
        relations = [
            f"{id_to_name.get(str(e.get('from_id')), e.get('from_id'))}"
            f"->{id_to_name.get(str(e.get('to_id')), e.get('to_id'))}"
            f"({e.get('relation')})"
            for e in char_edges
            if e.get("from_id") and e.get("to_id")
        ]
        character_summary = (
            f"相关人物：{', '.join(names)}\n"
            f"人物关系：{'; '.join(relations) if relations else '（暂无关系）'}"
        )

    summary_block = "\n".join(
        [f"- 第{item.get('chapter_index')}章：{item.get('text')}" for item in summaries]
    ) if summaries else "（暂无可检索前文摘要）"

    points_text = "\n".join([f"- {p}" for p in points]) if points else "- （暂无要点）"

    prev_tail = _previous_chapter_tail_for_prompt(
        store,
        project_id,
        current_idx,
        WRITE_CHAPTER_PREV_TAIL_CHARS,
    )
    prev_tail_block = ""
    if prev_tail:
        pnum = current_idx  # 上一章为第 current_idx 章（0-based 序号为 current_idx-1，人类可读章号 = current_idx）
        prev_tail_block = (
            f"【上一章正文末尾（承接上文）】\n"
            f"以下为第{pnum}章（序号 {current_idx - 1}）已落盘正文之末尾节选，用于时空、动作与语气衔接。\n"
            "请在本章开头自然承接下列片段所悬置的场景或动作：可顺接、可转场、可略作时间跳跃，但勿整段复述；"
            "若与本章要点在情节推进上冲突，以本章要点为准。\n\n"
            f"{prev_tail}\n\n"
        )

    kb_assets_text = ""
    kb_evidence_text = ""
    kb_confidence: Optional[float] = None
    consistency_report: List[Dict[str, Any]] = []
    if (
        state.get("kb_enabled")
        and state.get("selected_kb_ids")
        and global_kb_retriever is not None
    ):
        kb_ctx = await build_kb_context_for_writing(
            kb_ids=list(state.get("selected_kb_ids") or []),
            title=title,
            points=list(points or []),
            retriever=global_kb_retriever,
            planner=planner_llm,
            doc_id=None,
        )
        kb_assets_text = kb_ctx.get("kb_assets_text") or ""
        kb_evidence_text = kb_ctx.get("kb_evidence_text") or ""
        kb_confidence = float(kb_ctx.get("kb_confidence") or 0.0)
        if kb_confidence < 0.55:
            consistency_report.append(
                {
                    "type": "kb_retrieval_low_confidence",
                    "detail": "知识库检索证据偏少，请人工核对关键设定",
                }
            )

    overrides_block = format_canon_overrides(state.get("canon_overrides"))
    kb_block = ""
    if kb_assets_text.strip():
        kb_block = (
            f"\n\n{overrides_block}\n\n"
            "【优先级】二创设定与上文已写情节 > 参考知识库（原著）。若冲突，遵循二创与本章要点。\n"
            f"【知识库分层摘要】\n{kb_assets_text[:8000] or '（无）'}"
        )
    elif overrides_block.strip():
        kb_block = f"\n\n{overrides_block}"

    style_constraint = str(state.get("style_constraint") or "").strip()
    style_constraint_block = ""
    if style_constraint:
        style_constraint_block = (
            "【文风约束】\n"
            f"{style_constraint}\n"
            "请严格遵守上述文风约束，同时不得与既有剧情事实冲突。\n\n"
        )

    prompt = (
        "请根据以下章节信息撰写小说正文。\n"
        "要求：\n"
        "1) 输出 Markdown 格式；\n"
        "2) 包含章节标题与正文段落；\n"
        "3) 字数接近目标字数；\n"
        "4) 本阶段只写当前章，不总结后续剧情；\n"
        "5) 仅输出小说章节正文本身，禁止输出“核心亮点/写作思路/总结/点评/说明”；\n"
        "6) 禁止输出 Markdown 代码围栏（不要出现 ```markdown 或 ```）。\n\n"
        f"{DEFAULT_CHAPTER_ENDING_RULES}\n\n"
        f"{style_constraint_block}"
        f"章节标题：{title}\n"
        f"目标字数：约{word_target}字\n"
        f"章节要点：\n{points_text}\n\n"
        f"{prev_tail_block}"
        f"前文摘要（RAG）：\n{summary_block}\n\n"
        f"当前章大纲补充（RAG）：\n{outline_chunk or '（无）'}\n\n"
        f"相关人物与关系摘要：\n{character_summary}"
        f"{kb_block}"
    )
    t0 = time.monotonic()
    logger.info(
        "[write_chapter] llm_invoke_begin project=%s chapter_index=%s title=%s",
        project_id,
        current_idx,
        title[:40] if title else "",
    )
    resp = await writer.ainvoke(prompt)
    draft = sanitize_chapter_markdown(get_message_text(resp))
    logger.info(
        "[write_chapter] llm_invoke_done project=%s chapter_index=%s chars=%s elapsed_s=%.2f",
        project_id,
        current_idx,
        len(draft),
        time.monotonic() - t0,
    )

    ref = store.save(project_id, current_idx, draft)
    word_count = len(draft.replace("\n", ""))
    chapter_meta: ChapterMeta = {
        "chapter_id": str(uuid.uuid4()),
        "title": title,
        "summary": "",
        "path_or_content_ref": ref,
        "word_count": word_count,
        "index": current_idx,
    }
    chapters = list(state.get("chapters", []))
    chapters = [c for c in chapters if int(c.get("index", -1)) != current_idx]
    chapters.append(chapter_meta)
    chapters.sort(key=lambda x: int(x.get("index", 0)))

    return {
        "current_chapter_draft": draft,
        "chapters": chapters,
        "retrieved_summaries": summaries,
        "retrieved_outline_chunk": outline_chunk,
        "character_context_summary": character_summary,
        "kb_assets_text": kb_assets_text,
        "kb_evidence_text": kb_evidence_text,
        "kb_confidence": kb_confidence,
        "consistency_report": consistency_report,
    }


def _run_test_first_three_chapters():
    """
    基于现有 plan_outline：用 generate_plot_ideas + plan_outline（含 project_id 与 rag_indexer）
    生成大纲并写入 RAG，再对 write_chapter_node 只测前三章。
    直接运行本文件时执行：python -m graph.nodes.write_chapter
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
        print("已选第一条剧情概要：", selected_plot_summary, "\n\n")

        # 2) 生成大纲（plan_outline 会顺带把每章大纲写入 RAG，无需在此处再索引）
        project_id = "test_write_3ch_" + uuid.uuid4().hex[:8]
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

        # 3) 依次写第 0、1、2 章
        base_state = {
            "project_id": project_id,
            "outline_structure": outline_structure,
            "chapters": [],
            "chapter_word_target": CHAPTER_WORD_TARGET,
        }
        current = dict(base_state)
        for idx in range(3):
            current["current_chapter_index"] = idx
            out = await write_chapter_node(current)
            current["chapters"] = out.get("chapters", [])
            current["current_chapter_draft"] = out.get("current_chapter_draft", "")
            # 测试中未跑 post_chapter，此处用正文前 500 字模拟「章节摘要」写入 RAG，供下一章检索前文
            # 正式流程中由 post_chapter_node 用 LLM 生成摘要并调用 add_chapter_summary，不在此 node 内调用
            summary = (out.get("current_chapter_draft") or "")[:500]
            if summary:
                indexer.add_chapter_summary(project_id, idx, summary)
            meta = next((c for c in current["chapters"] if c.get("index") == idx), None)
            title = meta.get("title", f"第{idx + 1}章") if meta else f"第{idx + 1}章"
            print(f"  第{idx + 1}章 完成: {title}, 字数约 {meta.get('word_count', 0) if meta else 0}")

        assert len(current["chapters"]) == 3
        print("通过：前三章均已写入，chapters 数量 =", len(current["chapters"]))
        return current

    return asyncio.run(_run())


if __name__ == "__main__":
    _run_test_first_three_chapters()
