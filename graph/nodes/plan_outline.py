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
                "beat": "待模型补全结构时自动添加的占位节拍。",
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


def _skeleton_prompt(selected_plot_summary: str, total_chapters: int) -> str:
    return (
        "你是一名长篇小说策划。【plan_outline_skeleton】请根据剧情概要生成全书结构骨架（不写每章详细要点）。\n"
        "输出要求：\n"
        "1) 至少一卷；章节总数必须等于目标章节数；\n"
        "2) 每章仅含 title 与 beat（一句剧情走向/节拍，15~40 字）；不要写 points 或写空数组 []；\n"
        "3) 仅输出 JSON，不要解释。\n"
        "JSON 格式：\n"
        '{"volumes":[{"volume_title":"卷名","chapters":[{"title":"章名","beat":"一句节拍"}]}]}\n\n'
        f"目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}"
    )


def _ensure_placeholder_points(ch: Dict[str, Any]) -> None:
    pts = ch.get("points")
    if not isinstance(pts, list) or len(pts) == 0:
        ch["points"] = ["（待扩写）"]


def _prepare_skeleton_structure(obj: Dict[str, Any], total_chapters: int) -> Dict[str, Any]:
    outline_structure: Dict[str, Any] = {"volumes": list(obj.get("volumes", []))}
    _normalize_skeleton_chapter_count(outline_structure, total_chapters)
    for _v, _c, ch in _flatten_chapter_refs(outline_structure):
        _ensure_placeholder_points(ch)
        ch.pop("one_liner", None)
    return outline_structure


def _batch_indices_line(indices: List[int]) -> str:
    return ",".join(str(i) for i in indices)


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
        '2) JSON 格式：{{"chapters":[{{"global_index":0,"points":["..."]}},...]}}；\n'
        "3) chapters 必须覆盖本批全部 global_index，顺序不限但不可遗漏。\n\n"
        f"全书目标章节数：{total_chapters}\n"
        f"剧情概要：{selected_plot_summary}\n"
        f"上一批衔接摘要：{carry}\n"
        "本批章节（global_index | 标题 | 节拍）：\n"
        f"{rows}\n"
        f"本批 global_index 列表：{_batch_indices_line(batch_global_indices)}"
        f"{kb_suffix}"
    )


def _parse_expand_batch(
    data: Dict[str, Any], expected_indices: List[int]
) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    for item in data.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        try:
            g = int(item.get("global_index", -1))
        except (TypeError, ValueError):
            continue
        pts = item.get("points")
        if isinstance(pts, list) and pts:
            out[g] = [str(p).strip() for p in pts if str(p).strip()]
    missing = [i for i in expected_indices if i not in out or not out[i]]
    if missing:
        raise ValueError(f"expand_batch missing chapters: {missing}")
    return out


def _carry_forward_from_points(points: List[str]) -> str:
    if not points:
        return ""
    tail = points[-3:]
    return "；".join(tail)[:800]


def _fallback_points_from_chapter(ch: Dict[str, Any], min_len: int) -> List[str]:
    beat = (ch.get("beat") or ch.get("one_liner") or "").strip()
    title = (ch.get("title") or "").strip()
    if beat:
        return [beat] if min_len <= 1 else [beat, f"围绕「{title or '本章'}」推进情节。"]
    return [f"围绕「{title or '本章'}」展开剧情。"]


async def _expand_batches(
    planner: Any,
    selected_plot_summary: str,
    total_chapters: int,
    outline_structure: Dict[str, Any],
    project_id: str,
    on_progress: ProgressCallback,
    lo: int,
    hi: int,
    kb_suffix: str = "",
) -> None:
    refs = _flatten_chapter_refs(outline_structure)
    batch_size = PLAN_OUTLINE_BATCH_SIZE
    prev_carry = ""

    for start in range(0, len(refs), batch_size):
        batch = refs[start : start + batch_size]
        indices = [start + k for k in range(len(batch))]
        chapter_rows = []
        for k, (_vi, _ci, ch) in enumerate(batch):
            g = indices[k]
            title = (ch.get("title") or f"第{g + 1}章").strip()
            beat = (ch.get("beat") or "").strip()
            chapter_rows.append(f"{g}|{title}|{beat}")
        prompt = _expand_batch_prompt(
            selected_plot_summary,
            total_chapters,
            indices,
            chapter_rows,
            prev_carry,
            lo,
            hi,
            kb_suffix,
        )
        logger.info(
            "[plan_outline] expand_batch project=%s range=%s..%s",
            project_id,
            indices[0],
            indices[-1],
        )
        if on_progress:
            await on_progress(
                "plan_outline",
                f"正在扩写大纲要点 {indices[-1] + 1}/{len(refs)} 章（本批 {len(indices)} 章）…",
            )
        try:
            data = await invoke_and_parse_with_retry(
                planner, prompt, extract_json_object, max_retries=3
            )
            by_idx = _parse_expand_batch(data, indices)
        except Exception as exc:
            logger.warning("[plan_outline] expand_batch failed, using fallback: %s", exc)
            by_idx = {}
            for g, (_vi, _ci, ch) in zip(indices, batch):
                by_idx[g] = _fallback_points_from_chapter(ch, lo)

        for g, (_vi, _ci, ch) in zip(indices, batch):
            pts = by_idx.get(g) or _fallback_points_from_chapter(ch, lo)
            ch["points"] = pts

        last_g = indices[-1]
        last_ch = batch[-1][2]
        prev_carry = _carry_forward_from_points(last_ch.get("points") or [])


async def _extract_canon_overrides(
    planner: Any,
    selected_plot_summary: str,
    outline_text: str,
    kb_context: str,
) -> List[Dict[str, Any]]:
    if not kb_context.strip():
        return []
    prompt = (
        "你是同人创作策划。根据「剧情概要」「全书大纲」与「参考知识库」，列出明确的二创与原著可能冲突点及本作采用设定。\n"
        "仅输出 JSON 对象：{\"canon_overrides\":[{\"subject\":\"\",\"original_fact\":\"\",\"fanfic_fact\":\"\",\"effective_from_chapter\":0}]}\n"
        "若无明确冲突，canon_overrides 为空数组。\n\n"
        f"剧情概要：{selected_plot_summary[:4000]}\n\n"
        f"大纲：{outline_text[:12000]}\n\n"
        f"参考知识：{kb_context[:8000]}"
    )
    try:
        obj = await invoke_and_parse_with_retry(
            planner,
            prompt,
            extract_json_object,
            max_retries=2,
        )
        raw = obj.get("canon_overrides")
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
    except Exception as exc:
        logger.warning("[plan_outline] canon_overrides extract failed: %s", exc)
    return []


async def plan_outline_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
    rag_indexer: Optional[LocalRagIndexer] = None,
    on_progress: ProgressCallback = None,
    kb_context: Optional[str] = None,
) -> Dict[str, Any]:
    planner = llm or create_planner_llm()
    selected_plot_summary = state.get("selected_plot_summary", "").strip()
    total_chapters = int(state.get("total_chapters", 0) or 0)
    if total_chapters <= 0:
        total_chapters = 12

    project_id = (state.get("project_id") or "").strip() or "(no_project)"
    t0 = time.monotonic()
    lo, hi = _points_range(total_chapters)
    kb_suffix = ""
    if (kb_context or "").strip():
        kb_suffix = "\n\n【参考知识库（原著/设定；若与概要冲突，以概要与本作二创为准）】\n" + (kb_context or "").strip()[:16000]

    if total_chapters <= PLAN_OUTLINE_SINGLE_CALL_MAX:
        prompt = _single_call_prompt(selected_plot_summary, total_chapters) + kb_suffix
        logger.info(
            "[plan_outline] single_call_begin project=%s target_chapters=%s",
            project_id,
            total_chapters,
        )
        obj = await invoke_and_parse_with_retry(
            planner, prompt, extract_json_object, max_retries=3
        )
        outline_structure = {"volumes": obj.get("volumes", [])}
    else:
        logger.info(
            "[plan_outline] multi_phase_begin project=%s target_chapters=%s batch_size=%s",
            project_id,
            total_chapters,
            PLAN_OUTLINE_BATCH_SIZE,
        )
        if on_progress:
            await on_progress(
                "plan_outline",
                f"正在生成全书结构骨架（{total_chapters} 章，分阶段扩写）…",
            )
        skel = await invoke_and_parse_with_retry(
            planner,
            _skeleton_prompt(selected_plot_summary, total_chapters) + kb_suffix,
            extract_json_object,
            max_retries=3,
        )
        outline_structure = _prepare_skeleton_structure(skel, total_chapters)
        await _expand_batches(
            planner,
            selected_plot_summary,
            total_chapters,
            outline_structure,
            project_id,
            on_progress,
            lo,
            hi,
            kb_suffix,
        )
        for _v, _c, ch in _flatten_chapter_refs(outline_structure):
            pts = ch.get("points")
            if not isinstance(pts, list) or not pts or pts == ["（待扩写）"]:
                ch["points"] = _fallback_points_from_chapter(ch, lo)

    # 若有 project_id 与 rag_indexer，将每章大纲片段写入 RAG，供 write_chapter 的 retriever 使用
    if project_id and project_id != "(no_project)" and rag_indexer is not None and outline_structure.get("volumes"):
        logger.info("[plan_outline] rag_index_outline_chunks project=%s", project_id)
        _index_outline_chunks(rag_indexer, project_id, outline_structure)

    n_vol = len(outline_structure.get("volumes") or [])
    outline_str = outline_structure_to_string(outline_structure)
    canon_overrides: List[Dict[str, Any]] = list(state.get("canon_overrides") or [])
    if kb_suffix.strip():
        new_ov = await _extract_canon_overrides(
            planner,
            selected_plot_summary,
            outline_str,
            (kb_context or "").strip()[:12000],
        )
        seen = {str(o.get("subject")) for o in canon_overrides}
        for o in new_ov:
            subj = str(o.get("subject") or "")
            if subj and subj not in seen:
                seen.add(subj)
                canon_overrides.append(o)

    logger.info(
        "[plan_outline] done project=%s volumes=%s elapsed_s=%.2f",
        project_id,
        n_vol,
        time.monotonic() - t0,
    )
    return {
        "outline_structure": outline_structure,
        "outline": outline_str,
        "canon_overrides": canon_overrides,
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
