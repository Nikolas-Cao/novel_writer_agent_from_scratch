"""
阶段 2/3 工作流构建：
- stage2: generate_plot_ideas -> plan_outline -> write_chapter -> refine_chapter
- stage3: generate_plot_ideas -> plan_outline -> write_chapter -> refine_chapter -> post_chapter
"""
import asyncio
from typing import Any, Optional, Tuple

from langgraph.graph import END, StateGraph

from graph.nodes import (
    fetch_or_generate_images_node,
    generate_plot_ideas_node,
    identify_illustration_points_node,
    insert_illustrations_into_chapter_node,
    plan_outline_node,
    post_chapter_node,
    refine_chapter_node,
    rewrite_with_feedback_node,
    update_outline_from_feedback_node,
    write_chapter_node,
)
from memory import try_create_sqlite_checkpointer
from rag import LocalRagIndexer, LocalRagRetriever
from state import NovelProjectState
from storage import ChapterStore, CharacterGraphStore


def build_stage2_workflow(
    planner_llm: Optional[Any] = None,
    writer_llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    use_local_checkpointer: bool = True,
) -> Tuple[Any, Optional[Any]]:
    """
    返回 (compiled_app, checkpointer)。
    - 若 use_local_checkpointer=True，将尝试创建 SqliteSaver；
    - 创建失败时不抛错，返回 checkpointer=None。
    """
    store = chapter_store or ChapterStore()
    graph = StateGraph(NovelProjectState)

    def n_generate_plot_ideas(state: NovelProjectState):
        return asyncio.run(generate_plot_ideas_node(state, llm=planner_llm))

    def n_plan_outline(state: NovelProjectState):
        return asyncio.run(plan_outline_node(state, llm=planner_llm))

    def n_write_chapter(state: NovelProjectState):
        return asyncio.run(write_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_refine_chapter(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    graph.add_node("generate_plot_ideas", n_generate_plot_ideas)
    graph.add_node("plan_outline", n_plan_outline)
    graph.add_node("write_chapter", n_write_chapter)
    graph.add_node("refine_chapter", n_refine_chapter)

    graph.set_entry_point("generate_plot_ideas")
    graph.add_edge("generate_plot_ideas", "plan_outline")
    graph.add_edge("plan_outline", "write_chapter")
    graph.add_edge("write_chapter", "refine_chapter")
    graph.add_edge("refine_chapter", END)

    checkpointer = try_create_sqlite_checkpointer() if use_local_checkpointer else None
    app = graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
    return app, checkpointer


def _illustration_edges(graph: StateGraph) -> None:
    """identify -> fetch -> insert -> post（调用方需已注册四节点）。"""
    graph.add_edge("identify_illustration_points", "fetch_or_generate_images")
    graph.add_edge("fetch_or_generate_images", "insert_illustrations_into_chapter")
    graph.add_edge("insert_illustrations_into_chapter", "post_chapter_update")


def build_stage7_workflow(
    planner_llm: Optional[Any] = None,
    writer_llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    rag_retriever: Optional[LocalRagRetriever] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    graph_store: Optional[CharacterGraphStore] = None,
    use_local_checkpointer: bool = True,
) -> Tuple[Any, Optional[Any]]:
    """
    阶段7工作流（插图可选）：
    生成/重写章节后，若启用插图则执行
    identify_illustration_points -> fetch_or_generate_images -> insert_illustrations_into_chapter
    然后进入 post_chapter_update。
    """
    store = chapter_store or ChapterStore()
    retriever = rag_retriever or LocalRagRetriever()
    indexer = rag_indexer or LocalRagIndexer()
    cgraph_store = graph_store or CharacterGraphStore()
    graph = StateGraph(NovelProjectState)

    def n_generate_plot_ideas(state: NovelProjectState):
        return asyncio.run(generate_plot_ideas_node(state, llm=planner_llm))

    def n_plan_outline(state: NovelProjectState):
        return asyncio.run(
            plan_outline_node(state, llm=planner_llm, rag_indexer=indexer)
        )

    def n_write_chapter(state: NovelProjectState):
        return asyncio.run(
            write_chapter_node(
                state,
                llm=writer_llm,
                chapter_store=store,
                rag_retriever=retriever,
                graph_store=cgraph_store,
            )
        )

    def n_refine_chapter(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_refine_after_rewrite(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_rewrite_feedback(state: NovelProjectState):
        return asyncio.run(rewrite_with_feedback_node(state, llm=writer_llm, chapter_store=store))

    def n_update_outline(state: NovelProjectState):
        return asyncio.run(
            update_outline_from_feedback_node(
                state,
                llm=planner_llm,
                rag_indexer=indexer,
            )
        )

    def n_identify_illustration_points(state: NovelProjectState):
        return asyncio.run(
            identify_illustration_points_node(
                state,
                llm=planner_llm,
                chapter_store=store,
            )
        )

    def n_fetch_or_generate_images(state: NovelProjectState):
        return asyncio.run(fetch_or_generate_images_node(state, project_root=store.root))

    def n_insert_illustrations(state: NovelProjectState):
        return asyncio.run(insert_illustrations_into_chapter_node(state, chapter_store=store))

    def n_post_chapter(state: NovelProjectState):
        return asyncio.run(
            post_chapter_node(
                state,
                llm=planner_llm,
                chapter_store=store,
                rag_indexer=indexer,
                graph_store=cgraph_store,
            )
        )

    def after_refine(state: NovelProjectState) -> str:
        feedback = (state.get("user_feedback") or "").strip()
        if feedback:
            return "rewrite_with_feedback"
        if state.get("enable_chapter_illustrations"):
            return "identify_illustration_points"
        return "post_chapter_update"

    def after_rewrite(state: NovelProjectState) -> str:
        if state.get("update_outline_on_feedback"):
            return "update_outline_from_feedback"
        if state.get("enable_chapter_illustrations"):
            return "identify_illustration_points"
        return "post_chapter_update"

    def after_update_outline(state: NovelProjectState) -> str:
        return (
            "identify_illustration_points"
            if state.get("enable_chapter_illustrations")
            else "post_chapter_update"
        )

    graph.add_node("generate_plot_ideas", n_generate_plot_ideas)
    graph.add_node("plan_outline", n_plan_outline)
    graph.add_node("write_chapter", n_write_chapter)
    graph.add_node("refine_chapter", n_refine_chapter)
    graph.add_node("refine_after_rewrite", n_refine_after_rewrite)
    graph.add_node("rewrite_with_feedback", n_rewrite_feedback)
    graph.add_node("update_outline_from_feedback", n_update_outline)
    graph.add_node("identify_illustration_points", n_identify_illustration_points)
    graph.add_node("fetch_or_generate_images", n_fetch_or_generate_images)
    graph.add_node("insert_illustrations_into_chapter", n_insert_illustrations)
    graph.add_node("post_chapter_update", n_post_chapter)

    graph.set_entry_point("generate_plot_ideas")
    graph.add_edge("generate_plot_ideas", "plan_outline")
    graph.add_edge("plan_outline", "write_chapter")
    graph.add_edge("write_chapter", "refine_chapter")

    graph.add_conditional_edges(
        "refine_chapter",
        after_refine,
        {
            "rewrite_with_feedback": "rewrite_with_feedback",
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    graph.add_edge("rewrite_with_feedback", "refine_after_rewrite")
    graph.add_conditional_edges(
        "refine_after_rewrite",
        after_rewrite,
        {
            "update_outline_from_feedback": "update_outline_from_feedback",
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    graph.add_conditional_edges(
        "update_outline_from_feedback",
        after_update_outline,
        {
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )

    _illustration_edges(graph)
    graph.add_edge("post_chapter_update", END)

    checkpointer = try_create_sqlite_checkpointer() if use_local_checkpointer else None
    app = graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
    return app, checkpointer


def build_stage3_workflow(
    planner_llm: Optional[Any] = None,
    writer_llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    rag_retriever: Optional[LocalRagRetriever] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    graph_store: Optional[CharacterGraphStore] = None,
    use_local_checkpointer: bool = True,
) -> Tuple[Any, Optional[Any]]:
    """阶段3工作流：在 refine 后增加 post_chapter_update。"""
    store = chapter_store or ChapterStore()
    retriever = rag_retriever or LocalRagRetriever()
    indexer = rag_indexer or LocalRagIndexer()
    cgraph_store = graph_store or CharacterGraphStore()
    graph = StateGraph(NovelProjectState)

    def n_generate_plot_ideas(state: NovelProjectState):
        return asyncio.run(generate_plot_ideas_node(state, llm=planner_llm))

    def n_plan_outline(state: NovelProjectState):
        return asyncio.run(
            plan_outline_node(state, llm=planner_llm, rag_indexer=indexer)
        )

    def n_write_chapter(state: NovelProjectState):
        return asyncio.run(
            write_chapter_node(
                state,
                llm=writer_llm,
                chapter_store=store,
                rag_retriever=retriever,
                graph_store=cgraph_store,
            )
        )

    def n_refine_chapter(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_identify_illustration_points(state: NovelProjectState):
        return asyncio.run(
            identify_illustration_points_node(
                state,
                llm=planner_llm,
                chapter_store=store,
            )
        )

    def n_fetch_or_generate_images(state: NovelProjectState):
        return asyncio.run(fetch_or_generate_images_node(state, project_root=store.root))

    def n_insert_illustrations(state: NovelProjectState):
        return asyncio.run(insert_illustrations_into_chapter_node(state, chapter_store=store))

    def n_post_chapter(state: NovelProjectState):
        return asyncio.run(
            post_chapter_node(
                state,
                llm=planner_llm,
                chapter_store=store,
                rag_indexer=indexer,
                graph_store=cgraph_store,
            )
        )

    def route_after_refine_s3(state: NovelProjectState) -> str:
        return (
            "identify_illustration_points"
            if state.get("enable_chapter_illustrations")
            else "post_chapter_update"
        )

    graph.add_node("generate_plot_ideas", n_generate_plot_ideas)
    graph.add_node("plan_outline", n_plan_outline)
    graph.add_node("write_chapter", n_write_chapter)
    graph.add_node("refine_chapter", n_refine_chapter)
    graph.add_node("identify_illustration_points", n_identify_illustration_points)
    graph.add_node("fetch_or_generate_images", n_fetch_or_generate_images)
    graph.add_node("insert_illustrations_into_chapter", n_insert_illustrations)
    graph.add_node("post_chapter_update", n_post_chapter)

    graph.set_entry_point("generate_plot_ideas")
    graph.add_edge("generate_plot_ideas", "plan_outline")
    graph.add_edge("plan_outline", "write_chapter")
    graph.add_edge("write_chapter", "refine_chapter")
    graph.add_conditional_edges(
        "refine_chapter",
        route_after_refine_s3,
        {
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    _illustration_edges(graph)
    graph.add_edge("post_chapter_update", END)

    checkpointer = try_create_sqlite_checkpointer() if use_local_checkpointer else None
    app = graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
    return app, checkpointer


def build_stage4_workflow(
    planner_llm: Optional[Any] = None,
    writer_llm: Optional[Any] = None,
    chapter_store: Optional[ChapterStore] = None,
    rag_retriever: Optional[LocalRagRetriever] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    graph_store: Optional[CharacterGraphStore] = None,
    use_local_checkpointer: bool = True,
) -> Tuple[Any, Optional[Any]]:
    """
    阶段4工作流（反馈与重写）：
    generate_plot_ideas -> plan_outline -> write_chapter -> refine_chapter
    refine 后按条件进入 rewrite -> (可选 update_outline) -> post_chapter_update
    """
    store = chapter_store or ChapterStore()
    retriever = rag_retriever or LocalRagRetriever()
    indexer = rag_indexer or LocalRagIndexer()
    cgraph_store = graph_store or CharacterGraphStore()
    graph = StateGraph(NovelProjectState)

    def n_generate_plot_ideas(state: NovelProjectState):
        return asyncio.run(generate_plot_ideas_node(state, llm=planner_llm))

    def n_plan_outline(state: NovelProjectState):
        return asyncio.run(
            plan_outline_node(state, llm=planner_llm, rag_indexer=indexer)
        )

    def n_write_chapter(state: NovelProjectState):
        return asyncio.run(
            write_chapter_node(
                state,
                llm=writer_llm,
                chapter_store=store,
                rag_retriever=retriever,
                graph_store=cgraph_store,
            )
        )

    def n_refine_chapter(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_refine_after_rewrite(state: NovelProjectState):
        return asyncio.run(refine_chapter_node(state, llm=writer_llm, chapter_store=store))

    def n_rewrite_feedback(state: NovelProjectState):
        return asyncio.run(rewrite_with_feedback_node(state, llm=writer_llm, chapter_store=store))

    def n_update_outline(state: NovelProjectState):
        return asyncio.run(
            update_outline_from_feedback_node(
                state,
                llm=planner_llm,
                rag_indexer=indexer,
            )
        )

    def n_identify_illustration_points(state: NovelProjectState):
        return asyncio.run(
            identify_illustration_points_node(
                state,
                llm=planner_llm,
                chapter_store=store,
            )
        )

    def n_fetch_or_generate_images(state: NovelProjectState):
        return asyncio.run(fetch_or_generate_images_node(state, project_root=store.root))

    def n_insert_illustrations(state: NovelProjectState):
        return asyncio.run(insert_illustrations_into_chapter_node(state, chapter_store=store))

    def n_post_chapter(state: NovelProjectState):
        return asyncio.run(
            post_chapter_node(
                state,
                llm=planner_llm,
                chapter_store=store,
                rag_indexer=indexer,
                graph_store=cgraph_store,
            )
        )

    def after_refine(state: NovelProjectState) -> str:
        feedback = (state.get("user_feedback") or "").strip()
        if feedback:
            return "rewrite_with_feedback"
        if state.get("enable_chapter_illustrations"):
            return "identify_illustration_points"
        return "post_chapter_update"

    def after_rewrite(state: NovelProjectState) -> str:
        if state.get("update_outline_on_feedback"):
            return "update_outline_from_feedback"
        if state.get("enable_chapter_illustrations"):
            return "identify_illustration_points"
        return "post_chapter_update"

    def after_update_outline(state: NovelProjectState) -> str:
        return (
            "identify_illustration_points"
            if state.get("enable_chapter_illustrations")
            else "post_chapter_update"
        )

    graph.add_node("generate_plot_ideas", n_generate_plot_ideas)
    graph.add_node("plan_outline", n_plan_outline)
    graph.add_node("write_chapter", n_write_chapter)
    graph.add_node("refine_chapter", n_refine_chapter)
    graph.add_node("refine_after_rewrite", n_refine_after_rewrite)
    graph.add_node("rewrite_with_feedback", n_rewrite_feedback)
    graph.add_node("update_outline_from_feedback", n_update_outline)
    graph.add_node("identify_illustration_points", n_identify_illustration_points)
    graph.add_node("fetch_or_generate_images", n_fetch_or_generate_images)
    graph.add_node("insert_illustrations_into_chapter", n_insert_illustrations)
    graph.add_node("post_chapter_update", n_post_chapter)

    graph.set_entry_point("generate_plot_ideas")
    graph.add_edge("generate_plot_ideas", "plan_outline")
    graph.add_edge("plan_outline", "write_chapter")
    graph.add_edge("write_chapter", "refine_chapter")
    graph.add_conditional_edges(
        "refine_chapter",
        after_refine,
        {
            "rewrite_with_feedback": "rewrite_with_feedback",
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    graph.add_edge("rewrite_with_feedback", "refine_after_rewrite")
    graph.add_conditional_edges(
        "refine_after_rewrite",
        after_rewrite,
        {
            "update_outline_from_feedback": "update_outline_from_feedback",
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    graph.add_conditional_edges(
        "update_outline_from_feedback",
        after_update_outline,
        {
            "identify_illustration_points": "identify_illustration_points",
            "post_chapter_update": "post_chapter_update",
        },
    )
    _illustration_edges(graph)
    graph.add_edge("post_chapter_update", END)

    checkpointer = try_create_sqlite_checkpointer() if use_local_checkpointer else None
    app = graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
    return app, checkpointer
