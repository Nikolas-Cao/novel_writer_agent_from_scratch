from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from graph.llm import create_planner_llm
import graph.nodes.plan_outline as po
from graph.utils import extract_json_object, invoke_and_parse_with_retry
from state import NovelProjectState

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str, str], Awaitable[None]]]
OUTLINE_REPAIR_BACK_CHAPTERS = max(0, int(os.environ.get("OUTLINE_REPAIR_BACK_CHAPTERS", "3")))


def _format_recent_fact_pack(recent_fact_pack: Optional[Dict[str, Any]]) -> str:
    pack = recent_fact_pack or {}
    summaries = str(pack.get("recent_summaries") or "（无）")
    outline_pts = str(pack.get("recent_outline_points") or "（无）")
    snapshot = str(pack.get("character_snapshot") or "（无）")
    constraints = str(pack.get("story_constraints") or "（无）")
    return (
        "【recent_fact_pack】\n"
        f"recent_summaries:\n{summaries}\n\n"
        f"recent_outline_points:\n{outline_pts}\n\n"
        f"character_snapshot:\n{snapshot}\n\n"
        f"story_constraints:\n{constraints}"
    )


def _window_skeleton_rows(
    outline_structure: Dict[str, Any],
    start_index: int,
    end_index: int,
) -> str:
    refs = po._flatten_chapter_refs(outline_structure)
    rows: List[str] = []
    for g in range(start_index, end_index + 1):
        title = f"第{g + 1}章"
        desc = ""
        if g < len(refs):
            ch = refs[g][2]
            title = str(ch.get("title") or title).strip()
            desc = str(ch.get("description") or ch.get("beat") or "").strip()
        rows.append(f"- {g}|{title}|{desc or '（无）'}")
    return "\n".join(rows) if rows else "（无）"


def _extend_window_prompt(
    selected_plot_summary: str,
    total_chapters: int,
    start_index: int,
    end_index: int,
    repair_start: int,
    recent_fact_pack: Dict[str, Any],
    window_skeleton_rows: str,
    lo: int,
    hi: int,
    kb_suffix: str = "",
) -> str:
    repair_rule = (
        f"可选回修区间：[{repair_start}, {start_index - 1}]，仅允许回修 points 与线索字段，不允许改 title。"
        if repair_start <= start_index - 1
        else "本次无需回修前序章节。"
    )
    return (
        "你是一名长篇小说策划。【plan_outline_extend_window】请在既有剧情基础上，生成指定章节区间的大纲。\n"
        "输出要求：\n"
        f"1) 必须覆盖 global_index={start_index}..{end_index} 的所有章节；每章 points {lo}~{hi} 条；\n"
        "2) 每条 point 控制在 30~60 字，避免过度铺陈；\n"
        "3) 每章 points 总字数建议不超过 220 字；\n"
        "4) 每条 point 只表达「1 个核心动作 + 1 个情绪/信息点」，避免并列堆砌多个事件；\n"
        "5) 每章输出字段：global_index/title/description/beat/points/depends_on/carry_forward/new_threads/resolved_threads；\n"
        "6) 请优先参考本轮提供的章节骨架（title+description）；可微调措辞但不要偏离章节意图；\n"
        "7) depends_on 必须全部小于该章 global_index；\n"
        f"8) {repair_rule}\n"
        "9) 仅输出 JSON，不要解释。\n\n"
        'JSON 格式：{"chapters":[{"global_index":20,"title":"...","description":"约20字章节简述","beat":"...","points":["..."],'
        '"depends_on":[19],"carry_forward":["..."],"new_threads":[],"resolved_threads":[]}],'
        '"repairs":[{"global_index":19,"points":["..."],"carry_forward":["..."],"new_threads":[],"resolved_threads":["..."]}]}\n\n'
        f"全书目标章节数：{total_chapters}\n"
        f"本次新增区间：{start_index}..{end_index}\n"
        f"剧情概要：{selected_plot_summary}\n"
        f"本轮章节骨架（global_index|title|description）：\n{window_skeleton_rows}\n\n"
        f"{_format_recent_fact_pack(recent_fact_pack)}"
        f"{kb_suffix}"
    )


def _merge_window_into_outline(
    outline_structure: Dict[str, Any],
    payload: Dict[str, List[Dict[str, Any]]],
    start_index: int,
    end_index: int,
    allowed_repair_indices: Set[int],
) -> Set[int]:
    # 合并策略：
    # 1) 窗口范围内章节优先“就地覆盖”（已存在）；
    # 2) 对超出现有长度的章节按顺序 append；
    # 3) repairs 只对允许回修的索引生效（由上层边界计算决定）。
    refs = po._flatten_chapter_refs(outline_structure)
    affected: Set[int] = set()

    for g in range(start_index, end_index + 1):
        if g < len(refs):
            src = (payload.get("chapters") or [])[g - start_index]
            dst = refs[g][2]
            dst["title"] = str(src.get("title") or dst.get("title") or f"第{g + 1}章")
            dst["description"] = str(src.get("description") or dst.get("description") or "").strip()
            dst["beat"] = str(src.get("beat") or "")
            dst["points"] = list(src.get("points") or [])
            dst["depends_on"] = list(src.get("depends_on") or [])
            dst["carry_forward"] = list(src.get("carry_forward") or [])
            dst["new_threads"] = list(src.get("new_threads") or [])
            dst["resolved_threads"] = list(src.get("resolved_threads") or [])
            affected.add(g)
            continue
        src = (payload.get("chapters") or [])[g - start_index]
        po._append_chapter_to_tail(
            outline_structure,
            {
                "title": str(src.get("title") or f"第{g + 1}章"),
                "description": str(src.get("description") or "").strip(),
                "beat": str(src.get("beat") or ""),
                "points": list(src.get("points") or []),
                "depends_on": list(src.get("depends_on") or []),
                "carry_forward": list(src.get("carry_forward") or []),
                "new_threads": list(src.get("new_threads") or []),
                "resolved_threads": list(src.get("resolved_threads") or []),
            },
        )
        affected.add(g)

    refs = po._flatten_chapter_refs(outline_structure)
    for rp in payload.get("repairs") or []:
        idx = int(rp.get("global_index", -1))
        if idx < 0 or idx >= len(refs) or idx not in allowed_repair_indices:
            continue
        target = refs[idx][2]
        target["points"] = list(rp.get("points") or [])
        target["carry_forward"] = list(rp.get("carry_forward") or [])
        target["new_threads"] = list(rp.get("new_threads") or [])
        target["resolved_threads"] = list(rp.get("resolved_threads") or [])
        affected.add(idx)
    return affected


async def outline_extend_window_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    on_progress: ProgressCallback = None,
    kb_context: Optional[str] = None,
    recent_fact_pack: Optional[Dict[str, Any]] = None,
    start_chapter: Optional[int] = None,
    extend_count: Optional[int] = None,
    repair_back: int = OUTLINE_REPAIR_BACK_CHAPTERS,
) -> Dict[str, Any]:
    t0 = time.monotonic()
    planner = llm or create_planner_llm()
    selected_plot_summary = str(state.get("selected_plot_summary") or "").strip()
    total_chapters = int(state.get("total_chapters", 0) or 0) or 12
    project_id = (state.get("project_id") or "").strip() or "(no_project)"
    outline_structure: Dict[str, Any] = {
        "volumes": list((state.get("outline_structure") or {"volumes": []}).get("volumes") or [])
    }
    if not outline_structure.get("volumes"):
        outline_structure = {"volumes": [{"volume_title": "第一卷", "chapters": []}]}

    # 窗口起点默认从“当前已生成边界 + 1”开始，形成滚动扩窗行为；
    # 也允许调用方显式传入 start_chapter 做定点重算。
    window_size = int(state.get("outline_window_size", 0) or 0) or 10
    start = int(start_chapter) if start_chapter is not None else int(state.get("outline_generated_until", -1)) + 1
    start = max(0, start)
    if start >= total_chapters:
        return {
            "outline_structure": outline_structure,
            "outline_generated_until": po._count_chapters(outline_structure) - 1,
            "outline_extended_indices": [],
            "outline_seed_done": bool(state.get("outline_seed_done")),
        }
    count = int(extend_count) if extend_count is not None else window_size
    end = min(total_chapters - 1, start + max(1, count) - 1)

    refs = po._flatten_chapter_refs(outline_structure)
    # 首窗判定：仅用于 recent_fact_pack 默认值和日志语义，不改变主流程结构。
    is_seed = start == 0 and not bool(state.get("outline_seed_done"))
    raw_repair_start = max(0, start - max(0, int(repair_back)))
    last_written = int(state.get("last_written_chapter_index", -1) or -1)
    # 策略1：已写正文不允许回修
    # repair_cap 及之前视为“已写正文保护区”，本轮 repairs 会被忽略。
    repair_cap = max(-1, min(start - 1, last_written))
    # 实际可回修索引 = 理论回修窗口 ∩ 未写正文区间 ∩ 当前已存在章节索引。
    allowed_repair_indices = set(i for i in range(raw_repair_start, start) if i > repair_cap and i < len(refs))
    # 传给 prompt 的 repair_start 若无有效回修索引，则置为 start（等价“本轮不回修”）。
    repair_start = raw_repair_start if allowed_repair_indices else start

    lo, hi = po._points_range(end - start + 1)
    kb_suffix = ""
    if (kb_context or "").strip():
        kb_suffix = (
            "\n\n【参考知识库（原著/设定；若与概要冲突，以概要与本作二创为准）】\n"
            + (kb_context or "").strip()[:12000]
        )
    if on_progress:
        await on_progress("outline_extend_window", f"正在扩写窗口大纲 {start + 1}~{end + 1} 章…")

    # recent_fact_pack 由上游显式提供更佳；这里仅给出最小可运行兜底，
    # 避免调用方未传时 prompt 缺少关键信息而报错。
    if not recent_fact_pack:
        recent_fact_pack = {
            "recent_summaries": "（首窗可为空）" if is_seed else "（无）",
            "recent_outline_points": po._recent_outline_points_text(outline_structure, max(-1, start - 1), 5),
            "character_snapshot": "（无）",
            "story_constraints": "（无）",
        }

    window_skeleton_rows = _window_skeleton_rows(
        outline_structure=outline_structure,
        start_index=start,
        end_index=end,
    )
    prompt = _extend_window_prompt(
        selected_plot_summary=selected_plot_summary,
        total_chapters=total_chapters,
        start_index=start,
        end_index=end,
        repair_start=repair_start,
        recent_fact_pack=recent_fact_pack or {},
        window_skeleton_rows=window_skeleton_rows,
        lo=lo,
        hi=hi,
        kb_suffix=kb_suffix,
    )
    parse_fn = lambda text: po._validate_extend_payload(extract_json_object(text), start, end, repair_start)
    payload = await invoke_and_parse_with_retry(planner, prompt, parse_fn, max_retries=3)

    # 注意：合并后返回 affected 用于上游做增量回写（如事件日志/RAG 等）。
    affected = _merge_window_into_outline(
        outline_structure=outline_structure,
        payload=payload,
        start_index=start,
        end_index=end,
        allowed_repair_indices=allowed_repair_indices,
    )
    logger.info(
        "[outline_extend_window] done project=%s range=%s..%s is_seed=%s allowed_repairs=%s elapsed_s=%.3f affected=%s",
        project_id,
        start,
        end,
        is_seed,
        sorted(allowed_repair_indices),
        time.monotonic() - t0,
        len(affected),
    )
    return {
        "outline_structure": outline_structure,
        "outline_generated_until": max(int(state.get("outline_generated_until", -1) or -1), end),
        "outline_extended_indices": sorted(int(i) for i in affected),
        "outline_seed_done": True,
    }

