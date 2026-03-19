"""
阶段 2 节点：根据 selected_plot_summary 生成结构化大纲。
若传入 rag_indexer 且 state 有 project_id，会将每章大纲片段写入 RAG，供 write_chapter 检索。
"""
from typing import Any, Dict, Optional

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from rag import LocalRagIndexer
from state import NovelProjectState, outline_structure_to_string


async def plan_outline_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    selected_plot_summary = state.get("selected_plot_summary", "").strip()
    total_chapters = int(state.get("total_chapters", 0) or 0)
    if total_chapters <= 0:
        total_chapters = 12

    prompt = (
        "你是一名长篇小说策划，请根据给定剧情概要生成全书结构化大纲。\n"
        "输出要求：\n"
        "1) 至少一卷，章节总数尽量接近目标章节数；\n"
        "2) 每章含 title 与 points（3~5条）；\n"
        "3) 仅输出 JSON，不要解释。\n"
        "JSON 格式：\n"
        '{"volumes":[{"volume_title":"卷名","chapters":[{"title":"章名","points":["要点1","要点2"]}]}]}\n\n'
        f"目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}"
    )
    obj = await invoke_and_parse_with_retry(
        planner, prompt, extract_json_object, max_retries=3
    )
    outline_structure = {"volumes": obj.get("volumes", [])}

    # 若有 project_id 与 rag_indexer，将每章大纲片段写入 RAG，供 write_chapter 的 retriever 使用
    project_id = (state.get("project_id") or "").strip()
    if project_id and rag_indexer is not None and outline_structure.get("volumes"):
        _index_outline_chunks(rag_indexer, project_id, outline_structure)

    return {
        "outline_structure": outline_structure,
        "outline": outline_structure_to_string(outline_structure),
    }


def _index_outline_chunks(
    indexer: LocalRagIndexer,
    project_id: str,
    outline_structure: Dict[str, Any],
) -> None:
    """将 outline_structure 中每一章的大纲片段写入 RAG（与 write_chapter 的 _get_chapter_outline 序号一致）。"""
    global_idx = 0
    for vol_idx, volume in enumerate(outline_structure.get("volumes", [])):
        if not isinstance(volume, dict):
            continue
        for ch in volume.get("chapters", []):
            if not isinstance(ch, dict):
                continue
            title = ch.get("title") or f"第{global_idx + 1}章"
            points = ch.get("points") if isinstance(ch.get("points"), list) else []
            chunk_text = title + "\n" + "\n".join(f"- {p}" for p in points)
            indexer.add_outline_chunk(project_id, chunk_text, vol_idx, global_idx)
            global_idx += 1


def _run_test_with_user_input():
    """
    针对 plan_outline_node 的测试：total_chapters 与 selected_plot_summary 从用户输入获取，
    可选传入 project_id 与 rag_indexer，校验返回结构并验证大纲写入 RAG 后可被检索。
    直接运行本文件时执行：python -m graph.nodes.plan_outline
    """
    import asyncio
    import sys
    import uuid
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from rag import LocalRagRetriever

    selected_plot_summary = input("请输入剧情概要（selected_plot_summary）: ").strip()
    if not selected_plot_summary:
        selected_plot_summary = "一名少年在异世界觉醒能力，从弱小逐步成长并改变世界。"
        print("未输入，使用默认剧情概要。")

    raw_chapters = input("请输入目标章节数（total_chapters，直接回车默认 12）: ").strip()
    total_chapters = int(raw_chapters) if raw_chapters.isdigit() and int(raw_chapters) > 0 else 12
    print(f"使用目标章节数: {total_chapters}")

    test_rag = input("是否测试 RAG 写入与检索（y/回车=是，n=否）: ").strip().lower() != "n"
    project_id = f"test_plan_outline_{uuid.uuid4().hex[:8]}"
    state = {
        "selected_plot_summary": selected_plot_summary,
        "total_chapters": total_chapters,
    }
    if test_rag:
        state["project_id"] = project_id

    async def _run():
        indexer = LocalRagIndexer() if test_rag else None
        out = await plan_outline_node(state, rag_indexer=indexer)
        assert "outline_structure" in out
        assert "outline" in out
        assert "volumes" in out["outline_structure"]
        assert isinstance(out["outline_structure"]["volumes"], list)
        assert len(out["outline_structure"]["volumes"]) > 0

        if test_rag and indexer is not None:
            retriever = LocalRagRetriever()
            ctx = retriever.retrieve_for_chapter(project_id, 0, k_chapters=0, k_outline=1)
            outline_chunk = (ctx.get("outline_chunk") or "").strip()
            assert outline_chunk, "RAG 中应能检索到第 0 章的大纲片段"
            print("通过：RAG 检索到第 0 章 outline_chunk 长度 =", len(outline_chunk))

        return out

    result = asyncio.run(_run())
    print("通过：卷数 =", len(result["outline_structure"]["volumes"]))
    print("outline 预览（前 500 字）:")
    print(result["outline"][:500] + ("..." if len(result["outline"]) > 500 else ""))
    return result


if __name__ == "__main__":
    _run_test_with_user_input()
