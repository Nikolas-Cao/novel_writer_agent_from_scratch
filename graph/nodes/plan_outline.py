"""
阶段 2 节点：根据 selected_plot_summary 生成结构化大纲。
若传入 rag_indexer 且 state 有 project_id，会将每章大纲片段写入 RAG，供 write_chapter 检索。

章节数较多时采用「骨架 + 分批扩写 points」多轮 LLM，降低单次输出长度与 JSON 失败率；
章节数 ≤ PLAN_OUTLINE_SINGLE_CALL_MAX 时仍用单次调用。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from config import DEBUG
logger = logging.getLogger(__name__)

from graph.llm import create_planner_llm
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from rag import LocalRagIndexer
from state import NovelProjectState, outline_structure_to_string

# 超过该章节数则走骨架 + 分批扩写（避免单次 JSON 过大）
PLAN_OUTLINE_SINGLE_CALL_MAX = int(os.environ.get("PLAN_OUTLINE_SINGLE_CALL_MAX", "16"))
# 每批扩写的章节数
PLAN_OUTLINE_BATCH_SIZE = max(1, int(os.environ.get("PLAN_OUTLINE_BATCH_SIZE", "10")))
# 超过该章节数则每章要点条数降为 2~3
PLAN_OUTLINE_LARGE_BOOK_CHAPTERS = int(os.environ.get("PLAN_OUTLINE_LARGE_BOOK_CHAPTERS", "40"))
OUTLINE_REPAIR_BACK_CHAPTERS = max(0, int(os.environ.get("OUTLINE_REPAIR_BACK_CHAPTERS", "3")))

ProgressCallback = Optional[Callable[[str, str], Awaitable[None]]]


def _points_range(total_chapters: int) -> Tuple[int, int]:
    if total_chapters >= PLAN_OUTLINE_LARGE_BOOK_CHAPTERS:
        return 2, 3
    return 3, 5


def _count_chapters(outline_structure: Dict[str, Any]) -> int:
    n = 0
    for volume in outline_structure.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        n += len(volume.get("chapters") or [])
    return n


def _flatten_chapter_refs(outline_structure: Dict[str, Any]) -> List[Tuple[int, int, Dict[str, Any]]]:
    """(volume_idx, chapter_idx_in_volume, chapter_dict) 按阅读顺序。"""
    refs: List[Tuple[int, int, Dict[str, Any]]] = []
    for vol_idx, volume in enumerate(outline_structure.get("volumes") or []):
        if not isinstance(volume, dict):
            continue
        for ch_idx, ch in enumerate(volume.get("chapters") or []):
            if isinstance(ch, dict):
                refs.append((vol_idx, ch_idx, ch))
    return refs


def _normalize_skeleton_chapter_count(
    outline_structure: Dict[str, Any], total_chapters: int
) -> None:
    """将骨架章节数对齐到 total_chapters（多删少补）。"""
    refs = _flatten_chapter_refs(outline_structure)
    current = len(refs)
    if current == total_chapters:
        return
    logger.warning(
        "[plan_outline] skeleton chapter count mismatch: got %s want %s, normalizing",
        current,
        total_chapters,
    )
    if current > total_chapters:
        to_drop = current - total_chapters
        # 从最后一卷最后一章向前删
        for _ in range(to_drop):
            vols = outline_structure.get("volumes") or []
            if not vols:
                break
            last_vol = vols[-1]
            if not isinstance(last_vol, dict):
                vols.pop()
                continue
            chs = last_vol.get("chapters") or []
            if chs:
                chs.pop()
                last_vol["chapters"] = chs
            else:
                vols.pop()
        return
    # current < total_chapters：在最后一卷末尾补章
    need = total_chapters - current
    vols = outline_structure.get("volumes") or []
    if not vols:
        outline_structure["volumes"] = [{"volume_title": "第一卷", "chapters": []}]
        vols = outline_structure["volumes"]
    last_vol = vols[-1]
    if not isinstance(last_vol, dict):
        last_vol = {"volume_title": "补充卷", "chapters": []}
        vols[-1] = last_vol
    chs = last_vol.setdefault("chapters", [])
    start_idx = current
    for j in range(need):
        chs.append(
            {
                "title": f"第{start_idx + j + 1}章",
                "description": "（待补充章节简述）",
                "points": ["（待扩写）"],
            }
        )


def _single_call_prompt(selected_plot_summary: str, total_chapters: int) -> str:
    lo, hi = _points_range(total_chapters)
    return (
        "你是一名长篇小说策划。【plan_outline_single】请根据给定剧情概要生成全书结构化大纲。\n"
        "输出要求：\n"
        "1) 至少一卷，章节总数尽量接近目标章节数；\n"
        f"2) 每章含 title 与 points（{lo}~{hi}条）；\n"
        "3) 仅输出 JSON，不要解释。\n"
        "JSON 格式：\n"
        '{"volumes":[{"volume_title":"卷名","chapters":[{"title":"章名","points":["要点1","要点2"]}]}]}\n\n'
        f"目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}"
    )


def _batch_indices_line(indices: List[int]) -> str:
    return ",".join(str(i) for i in indices)


def _recent_outline_points_text(outline_structure: Dict[str, Any], end_index_inclusive: int, k: int = 5) -> str:
    refs = _flatten_chapter_refs(outline_structure)
    if not refs:
        return "（无）"
    s = max(0, int(end_index_inclusive) - int(k) + 1)
    e = min(int(end_index_inclusive), len(refs) - 1)
    rows: List[str] = []
    for g in range(s, e + 1):
        ch = refs[g][2]
        title = str(ch.get("title") or f"第{g + 1}章")
        points = ch.get("points") if isinstance(ch.get("points"), list) else []
        pt_text = "；".join(str(p) for p in points[:3]) if points else "（无）"
        rows.append(f"- {g}: {title} | {pt_text}")
    return "\n".join(rows) if rows else "（无）"


def _expand_batch_prompt(
    selected_plot_summary: str,
    total_chapters: int,
    batch_global_indices: List[int],
    chapter_rows: List[str],
    prev_carry: str,
    lo: int,
    hi: int,
    kb_suffix: str = "",
) -> str:
    rows = "\n".join(chapter_rows)
    carry = prev_carry.strip() if prev_carry.strip() else "（无，本批为开篇）"
    return (
        "你是一名长篇小说策划。【plan_outline_expand_batch】请为下列章节扩写详细要点（承接上一批结尾，勿重复已交代信息）。\n"
        "输出要求：\n"
        f"1) 仅输出 JSON；每章 points 含 {lo}~{hi} 条，具体、可指导写作；\n"
        "2) 每条 point 控制在 30~60 字，避免过度铺陈；\n"
        "3) 每章 points 总字数建议不超过 220 字；\n"
        "4) 每条 point 只表达「1 个核心动作 + 1 个情绪/信息点」，避免并列堆砌多个事件；\n"
        '5) JSON 格式：{{"chapters":[{{"global_index":0,"points":["..."]}},...]}}；\n'
        "6) chapters 必须覆盖本批全部 global_index，顺序不限但不可遗漏。\n\n"
        f"全书目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}\n"
        f"上一批衔接摘要：{carry}\n"
        "本批章节（global_index | 标题 | 节拍）：\n"
        f"{rows}\n"
        f"本批 global_index 列表：{_batch_indices_line(batch_global_indices)}"
        f"{kb_suffix}"
    )


def _normalize_threads(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _validate_extend_payload(
    data: Dict[str, Any],
    start_index: int,
    end_index: int,
    repair_start: int,
) -> Dict[str, List[Dict[str, Any]]]:
    chapters_raw = data.get("chapters") or []
    if not isinstance(chapters_raw, list):
        raise ValueError("extend_window: chapters must be list")
    by_idx: Dict[int, Dict[str, Any]] = {}
    for item in chapters_raw:
        if not isinstance(item, dict):
            continue
        g = int(item.get("global_index", -1))
        if g < start_index or g > end_index:
            continue
        points = item.get("points")
        if not isinstance(points, list) or not points:
            raise ValueError(f"extend_window: chapter {g} points empty")
        depends_on = [int(i) for i in (item.get("depends_on") or []) if str(i).strip()]
        if any(i >= g for i in depends_on):
            raise ValueError(f"extend_window: chapter {g} has future depends_on")
        by_idx[g] = {
            "global_index": g,
            "title": str(item.get("title") or f"第{g + 1}章").strip(),
            "description": str(item.get("description") or "").strip(),
            "beat": str(item.get("beat") or "").strip(),
            "points": [str(p).strip() for p in points if str(p).strip()],
            "depends_on": depends_on,
            "carry_forward": _normalize_threads(item.get("carry_forward")),
            "new_threads": _normalize_threads(item.get("new_threads")),
            "resolved_threads": _normalize_threads(item.get("resolved_threads")),
        }
    missing = [i for i in range(start_index, end_index + 1) if i not in by_idx]
    if missing:
        raise ValueError(f"extend_window: missing chapters {missing}")

    repairs_norm: List[Dict[str, Any]] = []
    repairs_raw = data.get("repairs") or []
    if isinstance(repairs_raw, list):
        for item in repairs_raw:
            if not isinstance(item, dict):
                continue
            g = int(item.get("global_index", -1))
            if g < repair_start or g >= start_index:
                continue
            points = item.get("points")
            if not isinstance(points, list) or not points:
                continue
            repairs_norm.append(
                {
                    "global_index": g,
                    "points": [str(p).strip() for p in points if str(p).strip()],
                    "carry_forward": _normalize_threads(item.get("carry_forward")),
                    "new_threads": _normalize_threads(item.get("new_threads")),
                    "resolved_threads": _normalize_threads(item.get("resolved_threads")),
                }
            )
    return {
        "chapters": [by_idx[i] for i in range(start_index, end_index + 1)],
        "repairs": sorted(repairs_norm, key=lambda x: int(x["global_index"])),
    }


def _append_chapter_to_tail(outline_structure: Dict[str, Any], chapter: Dict[str, Any]) -> Tuple[int, int]:
    vols = outline_structure.setdefault("volumes", [])
    if not vols or not isinstance(vols[-1], dict):
        vols.append({"volume_title": "续写卷", "chapters": []})
    last = vols[-1]
    chs = last.setdefault("chapters", [])
    chs.append(chapter)
    return len(vols) - 1, len(chs) - 1


async def _extract_canon_overrides(
    planner: Any,
    selected_plot_summary: str,
    outline_text: str,
    kb_context: str,
) -> List[Dict[str, Any]]:
    if not kb_context.strip():
        return []
    t0 = time.monotonic()
    prompt = (
        "你是同人创作策划。根据「剧情概要」「全书大纲」与「参考知识库」，列出明确的二创与原著可能冲突点及本作采用设定。\n"
        "仅输出 JSON 对象：{\"canon_overrides\":[{\"subject\":\"\",\"original_fact\":\"\",\"fanfic_fact\":\"\",\"effective_from_chapter\":0}]}\n"
        "若无明确冲突，canon_overrides 为空数组。\n\n"
        f"剧情概要：{selected_plot_summary[:4000]}\n\n"
        f"大纲：{outline_text[:12000]}\n\n"
        f"参考知识：{kb_context[:8000]}"
    )
    try:
        t_llm = time.monotonic()
        obj = await invoke_and_parse_with_retry(
            planner,
            prompt,
            extract_json_object,
            max_retries=2,
        )
        llm_elapsed = time.monotonic() - t_llm
        raw = obj.get("canon_overrides")
        if isinstance(raw, list):
            ret = [x for x in raw if isinstance(x, dict)]
            logger.info(
                "[plan_outline] canon_overrides_done count=%s llm_s=%.3f total_s=%.3f",
                len(ret),
                llm_elapsed,
                time.monotonic() - t0,
            )
            return ret
        if isinstance(obj, list):
            ret = [x for x in obj if isinstance(x, dict)]
            logger.info(
                "[plan_outline] canon_overrides_done count=%s llm_s=%.3f total_s=%.3f",
                len(ret),
                llm_elapsed,
                time.monotonic() - t0,
            )
            return ret
    except Exception as exc:
        logger.warning("[plan_outline] canon_overrides extract failed: %s", exc)
    logger.info(
        "[plan_outline] canon_overrides_done count=0 total_s=%.3f",
        time.monotonic() - t0,
    )
    return []


async def plan_outline_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    on_progress: ProgressCallback = None,
    kb_context: Optional[str] = None,
    target_chapters: Optional[int] = None,
) -> Dict[str, Any]:
    from graph.nodes.outline_extend_window import outline_extend_window_node
    from graph.nodes.outline_finalize import outline_finalize_node
    from graph.nodes.outline_short import outline_short_node
    from graph.nodes.outline_skeleton_lite import outline_skeleton_lite_node

    total_chapters = int(state.get("total_chapters", 0) or 0) or 12
    run_target = int(target_chapters if target_chapters is not None else total_chapters)
    run_target = max(1, min(run_target, total_chapters))
    run_state = {**state, "total_chapters": run_target}

    # 调试模式(debug_mode)优先走一次性大纲，避免骨架+扩窗的多阶段调用干扰问题定位。
    if DEBUG or run_target <= PLAN_OUTLINE_SINGLE_CALL_MAX:
        s1 = await outline_short_node(run_state, llm=llm, kb_context=kb_context, target_chapters=run_target)
        merged = {**run_state, **s1}
    else:
        s1 = await outline_skeleton_lite_node(run_state, llm=llm, kb_context=kb_context)
        merged1 = {**run_state, **s1, "outline_window_size": int(run_state.get("outline_window_size", 10) or 10)}
        s2 = await outline_extend_window_node(
            merged1,
            llm=llm,
            on_progress=on_progress,
            kb_context=kb_context,
            start_chapter=0,
            extend_count=int(merged1.get("outline_window_size", 10) or 10),
        )
        merged = {**merged1, **s2}
    out = await outline_finalize_node(merged, llm=llm, rag_indexer=rag_indexer, kb_context=kb_context)
    return out


async def plan_outline_extend_node(
    state: NovelProjectState,
    start_chapter: int,
    extend_count: int,
    llm: Optional[Any] = None,
    on_progress: ProgressCallback = None,
    kb_context: Optional[str] = None,
    recent_fact_pack: Optional[Dict[str, Any]] = None,
    repair_back: int = OUTLINE_REPAIR_BACK_CHAPTERS,
) -> Dict[str, Any]:
    from graph.nodes.outline_extend_window import outline_extend_window_node
    from graph.nodes.outline_finalize import outline_finalize_node

    ext = await outline_extend_window_node(
        state,
        llm=llm,
        on_progress=on_progress,
        kb_context=kb_context,
        recent_fact_pack=recent_fact_pack,
        start_chapter=start_chapter,
        extend_count=extend_count,
        repair_back=repair_back,
    )
    out = await outline_finalize_node({**state, **ext}, llm=llm, rag_indexer=None, kb_context=kb_context)
    return {**out, "outline_extended_indices": ext.get("outline_extended_indices", [])}


def _index_outline_chunks(
    indexer: LocalRagIndexer,
    project_id: str,
    outline_structure: Dict[str, Any],
) -> None:
    """将 outline_structure 中每一章的大纲片段写入 RAG（与 write_chapter 的 _get_chapter_outline 序号一致）。"""
    t0 = time.monotonic()
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
    logger.info(
        "[plan_outline] index_outline_chunks_done project=%s chunks=%s elapsed_s=%.3f",
        project_id,
        global_idx,
        time.monotonic() - t0,
    )


def _run_test_with_user_input():
    """
    针对 plan_outline_node 的测试：total_chapters 与 selected_plot_summary 从用户输入获取，
    可选传入 project_id 与 rag_indexer，校验返回结构并验证大纲写入 RAG 后可被检索。
    直接运行本文件时执行：python -m graph.nodes.plan_outline
    """
    import asyncio
    import logging
    import sys
    import uuid
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )

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

    async def _progress_cb(stage: str, message: str) -> None:
        print(f"[progress][{stage}] {message}")

    async def _run():
        run_t0 = time.monotonic()
        indexer = LocalRagIndexer() if test_rag else None
        t_node = time.monotonic()
        out = await plan_outline_node(state, rag_indexer=indexer, on_progress=_progress_cb)
        print(f"[timing] plan_outline_node: {time.monotonic() - t_node:.3f}s")
        assert "outline_structure" in out
        assert "outline" in out
        assert "volumes" in out["outline_structure"]
        assert isinstance(out["outline_structure"]["volumes"], list)
        assert len(out["outline_structure"]["volumes"]) > 0

        if test_rag and indexer is not None:
            retriever = LocalRagRetriever()
            t_retrieve = time.monotonic()
            ctx = retriever.retrieve_for_chapter(project_id, 0, k_chapters=0, k_outline=1)
            print(f"[timing] rag_retrieve_chapter0: {time.monotonic() - t_retrieve:.3f}s")
            outline_chunk = (ctx.get("outline_chunk") or "").strip()
            assert outline_chunk, "RAG 中应能检索到第 0 章的大纲片段"
            print("通过：RAG 检索到第 0 章 outline_chunk 长度 =", len(outline_chunk))

        print(f"[timing] _run_total: {time.monotonic() - run_t0:.3f}s")
        return out

    t_all = time.monotonic()
    result = asyncio.run(_run())
    print(f"[timing] run_test_with_user_input_total: {time.monotonic() - t_all:.3f}s")
    print("通过：卷数 =", len(result["outline_structure"]["volumes"]))
    print("outline 预览（前 500 字）:")
    print(result["outline"][:500] + ("..." if len(result["outline"]) > 500 else ""))
    return result


def _run_extend_test_with_user_input():
    """
    针对 plan_outline_extend_node 的交互测试：
    先生成初始大纲，再从指定章节开始扩窗续写，并打印关键阶段耗时。
    直接运行本文件时可选择执行：python -m graph.nodes.plan_outline
    """
    import asyncio
    import logging
    import sys
    import uuid
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )

    selected_plot_summary = input("请输入剧情概要（selected_plot_summary）: ").strip()
    if not selected_plot_summary:
        selected_plot_summary = "一名少年在异世界觉醒能力，从弱小逐步成长并改变世界。"
        print("未输入，使用默认剧情概要。")

    raw_chapters = input("请输入全书总章节数（直接回车默认 24）: ").strip()
    total_chapters = int(raw_chapters) if raw_chapters.isdigit() and int(raw_chapters) > 0 else 24
    print(f"使用全书总章节数: {total_chapters}")

    raw_seed = input("请输入先生成的前置章节数（直接回车默认 8）: ").strip()
    seed_target = int(raw_seed) if raw_seed.isdigit() and int(raw_seed) > 0 else 8
    seed_target = max(1, min(seed_target, total_chapters))

    raw_start = input("请输入扩窗起始章节索引（0-based，默认=前置章节数）: ").strip()
    start_idx = int(raw_start) if raw_start.isdigit() else seed_target
    start_idx = max(0, min(start_idx, total_chapters - 1))

    raw_extend = input("请输入扩窗章节数（默认 6）: ").strip()
    extend_count = int(raw_extend) if raw_extend.isdigit() and int(raw_extend) > 0 else 6
    print(
        f"扩窗参数：start_index={start_idx}, extend_count={extend_count}, seed_target={seed_target}"
    )

    state: Dict[str, Any] = {
        "project_id": f"test_plan_outline_extend_{uuid.uuid4().hex[:8]}",
        "selected_plot_summary": selected_plot_summary,
        "total_chapters": total_chapters,
    }

    async def _progress_cb(stage: str, message: str) -> None:
        print(f"[progress][{stage}] {message}")

    async def _run():
        run_t0 = time.monotonic()
        t_seed = time.monotonic()
        seed_out = await plan_outline_node(
            state,
            target_chapters=seed_target,
            on_progress=_progress_cb,
        )
        print(f"[timing] seed_plan_outline_node: {time.monotonic() - t_seed:.3f}s")
        assert seed_out.get("outline_structure", {}).get("volumes"), "初始大纲应包含 volumes"

        ext_state = {
            **state,
            "outline_structure": seed_out["outline_structure"],
            "outline": seed_out["outline"],
            "outline_generated_until": seed_out["outline_generated_until"],
        }
        t_extend = time.monotonic()
        ext_out = await plan_outline_extend_node(
            ext_state,
            start_chapter=start_idx,
            extend_count=extend_count,
            on_progress=_progress_cb,
            recent_fact_pack={
                "recent_summaries": "上一窗口：主角确认敌方内鬼并拿到关键线索。",
                "recent_outline_points": _recent_outline_points_text(
                    seed_out["outline_structure"],
                    int(seed_out.get("outline_generated_until", -1)),
                    k=5,
                ),
                "character_snapshot": "主角：受伤但意志坚定；搭档：疑似隐瞒信息。",
                "story_constraints": "保持悬疑张力，避免当场揭露最终反派。",
            },
        )
        print(f"[timing] plan_outline_extend_node: {time.monotonic() - t_extend:.3f}s")
        assert "outline_structure" in ext_out
        assert "outline_extended_indices" in ext_out
        assert isinstance(ext_out["outline_extended_indices"], list)

        print(f"[timing] _run_extend_total: {time.monotonic() - run_t0:.3f}s")
        return seed_out, ext_out

    t_all = time.monotonic()
    seed_result, extend_result = asyncio.run(_run())
    print(f"[timing] run_extend_test_with_user_input_total: {time.monotonic() - t_all:.3f}s")
    print("通过：初始大纲章节上限 =", seed_result.get("outline_generated_until", -1) + 1)
    print("通过：本次扩写影响章节索引 =", extend_result.get("outline_extended_indices", []))
    print("扩写后 outline 预览（前 500 字）:")
    text = str(extend_result.get("outline") or "")
    print(text[:500] + ("..." if len(text) > 500 else ""))
    return extend_result


if __name__ == "__main__":
    mode = input(
        "选择测试模式：1=plan_outline_node，2=plan_outline_extend_node（默认 1）: "
    ).strip()
    if mode == "2":
        _run_extend_test_with_user_input()
    else:
        _run_test_with_user_input()
