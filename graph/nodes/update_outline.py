"""
阶段 4 节点：根据反馈与重写结果更新大纲（当前章 + 可选后续章节）。
"""
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from rag import LocalRagIndexer
from state import NovelProjectState, outline_structure_to_string


def _chapter_pos(outline_structure: Dict[str, Any], chapter_index: int) -> Optional[Tuple[int, int]]:
    idx = 0
    for v_i, volume in enumerate(outline_structure.get("volumes", [])):
        for c_i, _ in enumerate(volume.get("chapters", [])):
            if idx == chapter_index:
                return v_i, c_i
            idx += 1
    return None


def _chapter_by_global_index(outline_structure: Dict[str, Any], chapter_index: int) -> Optional[Dict[str, Any]]:
    pos = _chapter_pos(outline_structure, chapter_index)
    if pos is None:
        return None
    v_i, c_i = pos
    return outline_structure["volumes"][v_i]["chapters"][c_i]


async def update_outline_from_feedback_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()

    feedback = state.get("user_feedback", "").strip()
    rewritten = state.get("last_rewrite_draft", "").strip()
    outline_structure = deepcopy(state.get("outline_structure") or {"volumes": []})
    current_idx = int(state.get("current_chapter_index", 0) or 0)
    if not feedback or not rewritten:
        return {}

    prompt = (
        "根据用户反馈和重写后章节，更新大纲要点。\n"
        "只输出 JSON，格式：\n"
        '{"current_chapter_points":["..."],'
        '"next_chapters_updates":[{"chapter_index":2,"points":["..."]}]}\n'
        "其中 next_chapters_updates 最多给出后续 1~2 章。\n\n"
        f"当前章序号：{current_idx}\n"
        f"用户反馈：{feedback}\n"
        f"重写后章节：\n{rewritten}"
    )
    data = await invoke_and_parse_with_retry(
        planner, prompt, extract_json_object, max_retries=3
    )

    current_points: List[str] = list(data.get("current_chapter_points", []))
    next_updates: List[Dict[str, Any]] = list(data.get("next_chapters_updates", []))
    affected_chapters: Set[int] = {current_idx}

    current_chapter = _chapter_by_global_index(outline_structure, current_idx)
    if current_chapter is not None and current_points:
        current_chapter["points"] = current_points

    for upd in next_updates:
        idx = upd.get("chapter_index")
        pts = upd.get("points")
        if idx is None or not isinstance(pts, list):
            continue
        idx_int = int(idx)
        chapter_obj = _chapter_by_global_index(outline_structure, idx_int)
        if chapter_obj is not None:
            chapter_obj["points"] = list(pts)
            affected_chapters.add(idx_int)

    project_id = (state.get("project_id") or "").strip()
    if project_id and rag_indexer is not None and affected_chapters:
        rag_indexer.upsert_outline_chunks_for_chapters(
            project_id=project_id,
            outline_structure=outline_structure,
            chapter_indices=affected_chapters,
        )

    return {"outline_structure": outline_structure}


def _run_test_first_three_chapters():
    """
    通过已实现节点生成 update_outline 所需 state，仅测试前三章。
    要求：
    1) 打印更新前后大纲；
    2) 只调试前三章；
    3) 自动注入会推动大纲变化的 user_feedback。
    直接运行本文件时执行：python -m graph.nodes.update_outline
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
    from graph.nodes.rewrite_feedback import rewrite_with_feedback_node
    from graph.nodes.write_chapter import write_chapter_node
    from rag import LocalRagIndexer
    from storage import CharacterGraphStore

    # 反馈刻意包含“改设定 + 调整后续章方向”，以推动 outline 发生变化
    feedback_by_chapter = [
        "第一章改为主角在学院觉醒能力而非边境村，并新增导师“沈砚”登场；"
        "第二章请提前安排学院考核冲突，减少铺垫。",
        "第二章把反派从“神秘黑衣人”改为“同窗林彻”，并在第三章加入擂台对决的关键伏笔。",
        "第三章结尾新增“遗迹地图碎片”线索，后续章节主线转向寻找遗迹并引出古代势力。",
    ]

    async def _run():
        # 1) 先用已有节点生成剧情概要和大纲
        instruction = "一名少年在异世界觉醒能力，从弱小逐步成长并改变世界。"
        ideas_out = await generate_plot_ideas_node({"instruction": instruction})
        plot_ideas = ideas_out.get("plot_ideas") or []
        if not plot_ideas:
            raise RuntimeError("generate_plot_ideas 未返回任何剧情概要")

        selected_plot_summary = plot_ideas[0]
        project_id = "test_update_outline_3ch_" + uuid.uuid4().hex[:8]
        indexer = LocalRagIndexer()
        graph_store = CharacterGraphStore()

        outline_out = await plan_outline_node(
            {
                "selected_plot_summary": selected_plot_summary,
                "total_chapters": 12,
                "project_id": project_id,
            },
            rag_indexer=indexer,
        )
        outline_structure = outline_out.get("outline_structure") or {"volumes": []}
        if not outline_structure.get("volumes"):
            raise RuntimeError("plan_outline 未返回有效大纲")

        current: Dict[str, Any] = {
            "project_id": project_id,
            "outline_structure": outline_structure,
            "chapters": [],
            "chapter_word_target": CHAPTER_WORD_TARGET,
        }

        print("\n===== 初始大纲（更新前） =====")
        print(outline_structure_to_string(current["outline_structure"]))

        changed_count = 0

        # 2) 仅调试前三章：write -> refine -> rewrite(注入反馈) -> update_outline
        for idx in range(3):
            current["current_chapter_index"] = idx

            write_out = await write_chapter_node(current, graph_store=graph_store)
            current["chapters"] = write_out.get("chapters", [])
            current["current_chapter_draft"] = write_out.get("current_chapter_draft", "")

            refine_out = await refine_chapter_node(current)
            current["chapters"] = refine_out.get("chapters", current["chapters"])
            current["current_chapter_final"] = refine_out.get("current_chapter_final", "")

            feedback = feedback_by_chapter[idx]
            current["user_feedback"] = feedback
            rewrite_out = await rewrite_with_feedback_node(current)
            if rewrite_out:
                current["current_chapter_final"] = rewrite_out.get("current_chapter_final", "")
                current["last_rewrite_draft"] = rewrite_out.get("last_rewrite_draft", "")
                current["chapters"] = rewrite_out.get("chapters", current["chapters"])

            before_outline = outline_structure_to_string(current["outline_structure"])
            update_out = await update_outline_from_feedback_node(current)
            if update_out.get("outline_structure"):
                current["outline_structure"] = update_out["outline_structure"]
            after_outline = outline_structure_to_string(current["outline_structure"])

            if after_outline != before_outline:
                changed_count += 1

            print(f"\n===== 第{idx + 1}章注入反馈 =====")
            print(feedback)
            print(f"\n===== 第{idx + 1}章更新前大纲 =====")
            print(before_outline)
            print(f"\n===== 第{idx + 1}章更新后大纲 =====")
            print(after_outline)

        assert len(current["chapters"]) == 3, "应仅完成前三章调试"
        assert changed_count > 0, "注入 feedback 后，大纲应至少发生一次变化"
        print("\n通过：仅测试前三章，且已打印每章更新前后大纲。")
        print("通过：反馈已注入，检测到大纲发生变化次数 =", changed_count)
        return current

    return asyncio.run(_run())


if __name__ == "__main__":
    _run_test_first_three_chapters()
