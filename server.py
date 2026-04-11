"""
阶段 5：FastAPI 后端服务
提供项目、剧情概要、大纲、章节续写、反馈重写等接口。
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APITimeoutError as OpenAIAPITimeoutError
from pydantic import BaseModel, Field

from config import (
    CHAPTER_WORD_TARGET,
    CHECKPOINT_DIR,
    DEFAULT_TOTAL_CHAPTERS,
    OUTLINE_HEARTBEAT_INTERVAL_S,
    OUTLINE_JOB_STALE_AFTER_S,
    PROJECTS_ROOT,
    VECTOR_STORE_DIR,
)
from logging_setup import configure_app_logging

configure_app_logging()

from graph.llm import TokenTrackingLLM, create_planner_llm, create_writer_llm
from graph.nodes.generate_plot_ideas import generate_plot_ideas_node
from graph.nodes.fetch_or_generate_images import fetch_or_generate_images_node
from graph.nodes.identify_illustration_points import identify_illustration_points_node
from graph.nodes.insert_illustrations_into_chapter import insert_illustrations_into_chapter_node
from graph.nodes.outline_extend_window import outline_extend_window_node
from graph.nodes.outline_finalize import outline_finalize_node
from graph.nodes.outline_short import outline_short_node
from graph.nodes.outline_skeleton_lite import outline_skeleton_lite_node
from graph.nodes.plan_outline import PLAN_OUTLINE_SINGLE_CALL_MAX, plan_outline_extend_node
from graph.nodes.post_chapter import post_chapter_node
from graph.nodes.refine_chapter import refine_chapter_node
from graph.nodes.rewrite_feedback import rewrite_with_feedback_node
from graph.nodes.update_outline import update_outline_from_feedback_node
from graph.knowledge_context import build_kb_context_for_outline
from graph.nodes.write_chapter import write_chapter_node
from knowledge_base.assets_builder import build_assets_task
from knowledge_base.assets_schema import validate_assets_payload
from knowledge_base.ingest import IngestCancelled, run_document_ingest
from knowledge_base.store import KnowledgeBaseStore
from memory import LocalFileCheckpointer
from rag import LocalRagIndexer, LocalRagRetriever
from rag.global_kb_chroma import GlobalKbChroma
from rag.global_kb_retriever import GlobalKbRetriever
from storage import ChapterHeadOverlayStore, ChapterStore, CharacterGraphStore, EventLogStore

logger = logging.getLogger(__name__)

NDJSON_MEDIA = "application/x-ndjson"
ProgressFn = Callable[[str, str], Awaitable[None]]

OUTLINE_INITIAL_CHAPTERS = max(1, int(os.environ.get("OUTLINE_INITIAL_CHAPTERS", "20")))
OUTLINE_WINDOW_SIZE = max(1, int(os.environ.get("OUTLINE_WINDOW_SIZE", "10")))
OUTLINE_TRIGGER_MARGIN = max(0, int(os.environ.get("OUTLINE_TRIGGER_MARGIN", "0")))


async def _noop_progress(_stage: str, _msg: str = "") -> None:
    return None


async def ndjson_with_progress(run: Callable[[ProgressFn], Awaitable[Any]]) -> AsyncIterator[bytes]:
    """执行 run(emit)，将进度与最终结果打成 NDJSON 行（供 ?stream=1）。

    进度队列必须无界：润色/重写等场景会按 token 高频 emit；若使用有界 Queue，
    而下游因 TCP 反压在 yield 上阻塞，worker 会在 put 上永久等待，形成死锁，
    前端表现为连接挂起、既收不到 result 也收不到 error。

    思路：
    1) emit 仅入队，不触网；真正发往浏览器的是本生成器里的 yield。
    2) 客户端断开时异常通常出在 yield，而不是 run(emit) 内部；故不因进度行写出失败而中断 worker。
    3) finally 里用 asyncio.shield 等待 worker，降低断连取消当前 Task 时未等完业务协程的概率。
    """

    q: asyncio.Queue = asyncio.Queue()

    async def emit(stage: str, message: str = "") -> None:
        # put 在无界队列上几乎不会失败；若未来改队列实现，避免异常冒泡打断 run(emit)。
        try:
            await q.put(("p", {"type": "progress", "stage": stage, "message": message}))
        except Exception as exc:
            logger.warning("ndjson emit queue put failed: %s", exc)

    async def worker() -> None:
        try:
            body = await run(emit)
            await q.put(("ok", body))
        except HTTPException as exc:
            detail = exc.detail
            msg = detail if isinstance(detail, str) else str(detail)
            await q.put(("http_err", (exc.status_code, msg)))
        except Exception as exc:
            await q.put(("err", str(exc)))

    task = asyncio.create_task(worker())
    progress_yield_fail_logged = False
    try:
        while True:
            kind, payload = await q.get()
            if kind == "p":
                chunk = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                try:
                    yield chunk
                except Exception as exc:
                    # 常见：对端关闭连接后 ASGI send 失败；业务 worker 应继续跑完并落盘。
                    if not progress_yield_fail_logged:
                        logger.debug("ndjson progress yield skipped (client likely gone): %s", exc)
                        progress_yield_fail_logged = True
                    continue
            elif kind == "ok":
                chunk = (json.dumps({"type": "result", "body": payload}, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                try:
                    yield chunk
                except Exception as exc:
                    logger.debug("ndjson result yield skipped (client likely gone): %s", exc)
                break
            elif kind == "http_err":
                status_code, msg = payload
                chunk = (
                    json.dumps({"type": "error", "detail": msg, "status": status_code}, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                try:
                    yield chunk
                except Exception as exc:
                    logger.debug("ndjson error-line yield skipped (client likely gone): %s", exc)
                break
            elif kind == "err":
                chunk = (json.dumps({"type": "error", "detail": payload}, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                try:
                    yield chunk
                except Exception as exc:
                    logger.debug("ndjson error-line yield skipped (client likely gone): %s", exc)
                break
    finally:
        await asyncio.shield(task)


class CreateProjectRequest(BaseModel):
    project_id: Optional[str] = None
    instruction: Optional[str] = ""
    style_constraint: Optional[str] = None
    total_chapters: Optional[int] = None
    chapter_word_target: Optional[int] = None
    enable_chapter_illustrations: Optional[bool] = None
    selected_kb_ids: Optional[List[str]] = None


class PatchProjectRequest(BaseModel):
    nickname: Optional[str] = None
    style_constraint: Optional[str] = None


class PatchProjectKnowledgeRequest(BaseModel):
    selected_kb_ids: List[str] = Field(default_factory=list)


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(..., min_length=1)


class SaveKnowledgeAssetsRequest(BaseModel):
    assets: Dict[str, Any]


class PlotIdeasRequest(BaseModel):
    instruction: str = Field(..., min_length=1)


class OutlineRequest(BaseModel):
    selected_plot_summary: str = Field(..., min_length=1)
    total_chapters: Optional[int] = None
    force_restart: bool = False


class OutlineWindowRequest(BaseModel):
    start_chapter: Optional[int] = Field(None, ge=1)
    end_chapter: Optional[int] = Field(None, ge=1)


class NextChapterRequest(BaseModel):
    chapter_word_target: Optional[int] = None
    enable_chapter_illustrations: Optional[bool] = None
    style_constraint: Optional[str] = None


class RewriteRequest(BaseModel):
    user_feedback: str = Field(..., min_length=1)
    update_outline: bool = False
    enable_chapter_illustrations: Optional[bool] = None
    style_constraint: Optional[str] = None


class RegenerateChapterRequest(BaseModel):
    chapter_word_target: Optional[int] = None
    enable_chapter_illustrations: Optional[bool] = None
    style_constraint: Optional[str] = None


def _default_state(project_id: str) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "nickname": None,
        "instruction": "",
        "style_constraint": "",
        "plot_ideas": [],
        "selected_plot_summary": "",
        "outline": "",
        "outline_structure": {"volumes": []},
        "chapters": [],
        "current_chapter_index": 0,
        "current_chapter_draft": "",
        "current_chapter_final": "",
        "character_graph": {"nodes": [], "edges": []},
        "user_feedback": "",
        "last_rewrite_draft": "",
        "total_chapters": DEFAULT_TOTAL_CHAPTERS,
        "outline_generated_until": -1,
        "outline_window_size": OUTLINE_WINDOW_SIZE,
        "outline_initial_chapters": OUTLINE_INITIAL_CHAPTERS,
        "chapter_word_target": CHAPTER_WORD_TARGET,
        "chapter_output_format": "markdown",
        "enable_chapter_illustrations": False,
        "update_outline_on_feedback": False,
        # created_at 由持久化 state 文件提供；当 state 文件不存在时，
        # cleanup_empty_projects() 应回退到 projects_dir/{project_id} 的目录 mtime。
        # 因此这里用 0 表示“未持久化创建时间”。
        "created_at": 0,
        "token_usage": {},
        "selected_kb_ids": [],
        "kb_enabled": False,
        "canon_overrides": [],
        "consistency_report": None,
        "kb_assets_text": "",
        "kb_evidence_text": "",
        "kb_confidence": None,
        "outline_checkpoint": {
            "phase": None,
            "input_fingerprint": "",
            "updated_at": 0,
        },
        "outline_job": {
            "status": "idle",
            "job_id": "",
            "started_at": 0,
            "last_heartbeat_at": 0,
        },
    }


def create_app(
    planner_llm: Optional[Any] = None,
    writer_llm: Optional[Any] = None,
    projects_root: Optional[Path] = None,
    vector_root: Optional[Path] = None,
    checkpoint_root: Optional[Path] = None,
) -> FastAPI:
    app = FastAPI(title="Novel Writer Agent API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5500", "http://localhost:5500", "http://127.0.0.1:8000", "http://localhost:8000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    frontend_dir = Path(__file__).resolve().parent / "frontend"

    projects_dir = Path(projects_root or PROJECTS_ROOT)
    vector_dir = Path(vector_root or VECTOR_STORE_DIR)
    states_dir = Path(checkpoint_root or (Path(CHECKPOINT_DIR) / "api_state"))
    projects_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)
    states_dir.mkdir(parents=True, exist_ok=True)

    app.mount("/project-data", StaticFiles(directory=str(projects_dir)), name="project_data")

    if frontend_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(frontend_dir)), name="assets")

        @app.get("/")
        async def frontend_index():
            return FileResponse(str(frontend_dir / "index.html"))

        @app.get("/app")
        async def frontend_app():
            return FileResponse(str(frontend_dir / "index.html"))

        # index.html 使用 ./app.js、./styles.css，从 / 打开时需提供这些路径
        @app.get("/app.js")
        async def serve_app_js():
            return FileResponse(str(frontend_dir / "app.js"), media_type="application/javascript")

        @app.get("/styles.css")
        async def serve_styles():
            return FileResponse(str(frontend_dir / "styles.css"), media_type="text/css")

    planner = planner_llm or create_planner_llm()
    writer = writer_llm or create_writer_llm()
    # 仅用于 refine/rewrite 阶段 token 级流式；write/outline 等保持原有 `ainvoke()` 行为。
    writer_streaming = writer_llm or create_writer_llm(streaming=True)

    chapter_store = ChapterStore(root=projects_dir)
    head_overlay_store = ChapterHeadOverlayStore(root=projects_dir)
    graph_store = CharacterGraphStore(root=projects_dir)
    event_store = EventLogStore(root=projects_dir)
    rag_indexer = LocalRagIndexer(root=vector_dir)
    rag_retriever = LocalRagRetriever(root=vector_dir)
    checkpointer = LocalFileCheckpointer(root=states_dir)

    kb_store = KnowledgeBaseStore(vector_dir)
    global_kb_retriever = GlobalKbRetriever(vector_dir, kb_store)
    kb_chroma = GlobalKbChroma(vector_dir)
    _kb_cancel_events: Dict[str, asyncio.Event] = {}
    _kb_background_tasks: Dict[str, asyncio.Task] = {}
    _outline_project_locks: Dict[str, asyncio.Lock] = {}

    def _kb_cancel_key(kb_id: str, job_id: str) -> str:
        return f"{kb_id}:{job_id}"

    def _truncate_event_text(text: Any, max_len: int = 300) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        compact = " ".join(raw.split())
        if len(compact) <= max_len:
            return compact
        return compact[: max_len - 3] + "..."

    def emit_project_event(
        project_id: str,
        *,
        event_name: str,
        event_content: str,
        chapter_index: Optional[int] = None,
        status: str = "success",
    ) -> None:
        event_store.append_event(
            project_id,
            {
                "event_id": f"evt-{uuid.uuid4().hex[:16]}",
                "ts": int(time.time()),
                "project_id": project_id,
                "chapter_index": int(chapter_index) if chapter_index is not None else None,
                "event_name": event_name,
                "event_content": _truncate_event_text(event_content),
                "status": status,
            },
        )

    def bootstrap_project_events_if_empty(project_id: str) -> None:
        path = event_store.path_for(project_id)
        if path.exists() and path.stat().st_size > 0:
            return
        state = load_state(project_id)
        base_ts = int(state.get("created_at") or time.time())

        def _append_bootstrap_event(
            *,
            event_name: str,
            event_content: str,
            chapter_index: Optional[int] = None,
            ts_offset: int = 0,
        ) -> None:
            event_store.append_event(
                project_id,
                {
                    "event_id": f"evt-{uuid.uuid4().hex[:16]}",
                    "ts": int(base_ts + ts_offset),
                    "project_id": project_id,
                    "chapter_index": int(chapter_index) if chapter_index is not None else None,
                    "event_name": event_name,
                    "event_content": _truncate_event_text(event_content),
                    "status": "success",
                    "bootstrapped": True,
                },
            )

        instruction = _truncate_event_text(state.get("instruction", ""))
        if instruction:
            _append_bootstrap_event(
                event_name="generate_plot_ideas",
                event_content=f"根据{instruction}生成概要",
                ts_offset=1,
            )

        summary = _truncate_event_text(state.get("selected_plot_summary", ""))
        if summary:
            _append_bootstrap_event(
                event_name="generate_outline",
                event_content=f"根据{summary}生成大纲",
                ts_offset=2,
            )

        chapters = sorted(
            [c for c in (state.get("chapters") or []) if isinstance(c, dict)],
            key=lambda x: int(x.get("index", -1)),
        )
        for idx, chapter in enumerate(chapters, start=3):
            chapter_index = int(chapter.get("index", -1))
            if chapter_index < 0:
                continue
            _append_bootstrap_event(
                event_name="write_next_chapter",
                event_content=f"续写第{chapter_index + 1}章",
                chapter_index=chapter_index,
                ts_offset=idx,
            )

    def _project_has_outline(state: Dict[str, Any]) -> bool:
        outline_structure = state.get("outline_structure") or {}
        if (outline_structure.get("volumes") or []):
            return True
        return bool((state.get("outline") or "").strip())

    async def _kb_document_pipeline(kb_id: str, doc_id: str, job_id: str, raw_path: Path) -> None:
        key = _kb_cancel_key(kb_id, job_id)
        ev = _kb_cancel_events.setdefault(key, asyncio.Event())
        ev.clear()
        try:
            kb_store.save_job(
                kb_id,
                {
                    "job_id": job_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "status": "indexing",
                    "byte_offset": 0,
                    "processed_chunks": 0,
                    "next_chunk_seq": 0,
                    "error_message": None,
                    "cancel_requested": False,
                },
            )
            kb_store.upsert_document_record(
                kb_id,
                {
                    **(kb_store.get_document(kb_id, doc_id) or {"doc_id": doc_id}),
                    "doc_id": doc_id,
                    "status": "indexing",
                    "job_id": job_id,
                },
            )
            fts_db = kb_store.kb_dir(kb_id) / "search.sqlite"
            stats = await run_document_ingest(
                kb_id=kb_id,
                doc_id=doc_id,
                raw_path=raw_path,
                store=kb_store,
                chroma=kb_chroma,
                fts_db=fts_db,
                job_id=job_id,
                cancel_check=lambda: ev.is_set(),
            )
            n_chunks = int(stats.get("chunks") or 0)
            kb_store.save_job(
                kb_id,
                {
                    "job_id": job_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "status": "summarizing_assets",
                    "processed_chunks": n_chunks,
                    "error_message": None,
                    "cancel_requested": False,
                },
            )
            assets = await build_assets_task(
                raw_path,
                planner,
                cancel_check=lambda: ev.is_set(),
                kb_id=kb_id,
                doc_id=doc_id,
                job_id=job_id,
            )
            kb_store.save_assets_doc(kb_id, doc_id, assets)
            kb_store.upsert_document_record(
                kb_id,
                {
                    **(kb_store.get_document(kb_id, doc_id) or {"doc_id": doc_id}),
                    "doc_id": doc_id,
                    "status": "ready",
                    "job_id": job_id,
                    "chunks": n_chunks,
                },
            )
            kb_store.save_job(
                kb_id,
                {
                    "job_id": job_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "status": "ready",
                    "processed_chunks": n_chunks,
                    "error_message": None,
                    "cancel_requested": False,
                },
            )
            kb_store.update_base_meta(kb_id, status="ready")
        except IngestCancelled:
            kb_store.save_job(
                kb_id,
                {
                    "job_id": job_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "status": "cancelled",
                    "error_message": "cancelled",
                    "cancel_requested": True,
                },
            )
            kb_store.upsert_document_record(
                kb_id,
                {
                    **(kb_store.get_document(kb_id, doc_id) or {"doc_id": doc_id}),
                    "doc_id": doc_id,
                    "status": "cancelled",
                    "job_id": job_id,
                },
            )
        except Exception as exc:
            logger.exception("kb pipeline failed kb=%s doc=%s", kb_id, doc_id)
            kb_store.save_job(
                kb_id,
                {
                    "job_id": job_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "status": "failed",
                    "error_message": str(exc),
                },
            )
            kb_store.upsert_document_record(
                kb_id,
                {
                    **(kb_store.get_document(kb_id, doc_id) or {"doc_id": doc_id}),
                    "doc_id": doc_id,
                    "status": "failed",
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
            kb_store.update_base_meta(kb_id, status="failed")
        finally:
            _kb_background_tasks.pop(key, None)
            _kb_cancel_events.pop(key, None)

    def ensure_project(project_id: str) -> None:
        (projects_dir / project_id).mkdir(parents=True, exist_ok=True)

    def project_exists(project_id: str) -> bool:
        return (projects_dir / project_id).exists() or (states_dir / f"{project_id}.json").exists()

    def _outline_last_index(outline_structure: Dict[str, Any]) -> int:
        vols = (outline_structure or {}).get("volumes") or []
        total = 0
        for vol in vols:
            if isinstance(vol, dict):
                total += len(vol.get("chapters") or [])
        return total - 1

    _HEAD_OVERLAY_PENDING_KEY = "_chapter_head_overlay_pending"

    def _safe_int(value: Any, default: int = -1) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _canon_override_key(item: Dict[str, Any]) -> str:
        subj = str(item.get("subject") or "").strip()
        eff = _safe_int(item.get("effective_from_chapter"), default=-1)
        return f"{subj}::{eff}"

    def _merge_canon_overrides(base: List[Dict[str, Any]], patch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = [dict(x) for x in (base or []) if isinstance(x, dict)]
        by_key = {_canon_override_key(x): i for i, x in enumerate(merged)}
        for item in patch or []:
            if not isinstance(item, dict):
                continue
            key = _canon_override_key(item)
            if key in by_key:
                merged[by_key[key]] = {**merged[by_key[key]], **dict(item)}
            else:
                by_key[key] = len(merged)
                merged.append(dict(item))
        return merged

    def _apply_head_overlay_view(state: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
        # 思路：
        # 1) 主 state 只保留“已提交”事实，overlay 只描述最新章可被重写推翻的派生分析；
        # 2) 对外读取（GET）时合并视图，保证前端可见最新章摘要与覆盖，而不污染主持久化。
        # 边界：overlay 章号与当前最新章不一致时视为过期，不做合并。
        if not isinstance(overlay, dict):
            return state
        out = dict(state)
        overlay_idx = _safe_int(overlay.get("chapter_index"), default=-1)
        latest_idx = latest_chapter_index(out)
        if latest_idx is None or overlay_idx != latest_idx:
            return out
        overlay_summary = str(overlay.get("last_chapter_summary") or "").strip()
        if overlay_summary:
            chapters = [dict(x) for x in (out.get("chapters") or [])]
            for item in chapters:
                if _safe_int(item.get("index"), default=-1) == overlay_idx:
                    item["summary"] = overlay_summary
                    break
            out["chapters"] = chapters
            out["last_chapter_summary"] = overlay_summary
        delta = [dict(x) for x in (overlay.get("canon_overrides_delta") or []) if isinstance(x, dict)]
        if delta:
            out["canon_overrides"] = _merge_canon_overrides(
                list(out.get("canon_overrides") or []),
                delta,
            )
        if isinstance(overlay.get("character_graph"), dict):
            out["character_graph"] = dict(overlay.get("character_graph") or {})
        return out

    def _commit_head_overlay_into_state(project_id: str, state: Dict[str, Any]) -> None:
        overlay = head_overlay_store.load(project_id)
        if not overlay:
            return
        merged = _apply_head_overlay_view(state, overlay)
        state.update(merged)
        head_overlay_store.clear(project_id)

    def _clear_head_overlay(project_id: str) -> None:
        head_overlay_store.clear(project_id)

    def _stage_head_overlay_from_post_result(state: Dict[str, Any], post_out: Dict[str, Any]) -> None:
        current_idx = _safe_int(state.get("current_chapter_index"), default=0)
        all_canon = list(post_out.get("canon_overrides") or [])
        delta = [
            dict(item)
            for item in all_canon
            if isinstance(item, dict)
            and _safe_int(item.get("effective_from_chapter"), default=-1) == current_idx
        ]
        payload = {
            "chapter_index": current_idx,
            "last_chapter_summary": str(post_out.get("last_chapter_summary") or "").strip(),
            "canon_overrides_delta": delta,
            "character_graph": dict(post_out.get("character_graph") or {}),
        }
        chapters = [dict(x) for x in (state.get("chapters") or [])]
        for item in chapters:
            if _safe_int(item.get("index"), default=-1) == current_idx:
                item["summary"] = ""
                break
        state["chapters"] = chapters
        state[_HEAD_OVERLAY_PENDING_KEY] = payload

    def load_state(project_id: str, *, include_head_overlay: bool = False) -> Dict[str, Any]:
        data = checkpointer.load_state(project_id) or _default_state(project_id)
        has_generated_until = "outline_generated_until" in data
        if "project_id" not in data:
            data["project_id"] = project_id
        if "token_usage" not in data:
            data["token_usage"] = {}
        defaults = _default_state(project_id)
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        checkpoint = data.get("outline_checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = {}
            data["outline_checkpoint"] = checkpoint
        checkpoint.setdefault("phase", None)
        checkpoint.setdefault("input_fingerprint", "")
        checkpoint.setdefault("updated_at", 0)
        job = data.get("outline_job")
        if not isinstance(job, dict):
            job = {}
            data["outline_job"] = job
        job.setdefault("status", "idle")
        job.setdefault("job_id", "")
        job.setdefault("started_at", 0)
        job.setdefault("last_heartbeat_at", 0)
        if not has_generated_until:
            data["outline_generated_until"] = _outline_last_index(data.get("outline_structure") or {"volumes": []})
        if include_head_overlay:
            overlay = head_overlay_store.load(project_id)
            if overlay:
                data = _apply_head_overlay_view(data, overlay)
        return data

    def save_state(project_id: str, state: Dict[str, Any]) -> None:
        overlay_payload = state.pop(_HEAD_OVERLAY_PENDING_KEY, None)
        checkpointer.save_state(project_id, state)
        if isinstance(overlay_payload, dict):
            head_overlay_store.save(project_id, overlay_payload)

    def _outline_lock(project_id: str) -> asyncio.Lock:
        lock = _outline_project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _outline_project_locks[project_id] = lock
        return lock

    def _outline_checkpoint(state: Dict[str, Any]) -> Dict[str, Any]:
        cp = state.get("outline_checkpoint")
        if not isinstance(cp, dict):
            cp = {"phase": None, "input_fingerprint": "", "updated_at": 0}
            state["outline_checkpoint"] = cp
        return cp

    def _outline_job(state: Dict[str, Any]) -> Dict[str, Any]:
        job = state.get("outline_job")
        if not isinstance(job, dict):
            job = {"status": "idle", "job_id": "", "started_at": 0, "last_heartbeat_at": 0}
            state["outline_job"] = job
        return job

    def _set_outline_checkpoint(state: Dict[str, Any], *, phase: Optional[str], fingerprint: str) -> None:
        cp = _outline_checkpoint(state)
        cp["phase"] = phase
        cp["input_fingerprint"] = fingerprint
        cp["updated_at"] = int(time.time())

    def _set_outline_job(
        state: Dict[str, Any],
        *,
        status: str,
        job_id: Optional[str] = None,
        started_at: Optional[int] = None,
        heartbeat_at: Optional[int] = None,
    ) -> None:
        job = _outline_job(state)
        job["status"] = status
        if job_id is not None:
            job["job_id"] = job_id
        if started_at is not None:
            job["started_at"] = int(started_at)
        if heartbeat_at is not None:
            job["last_heartbeat_at"] = int(heartbeat_at)

    def _outline_fingerprint(
        *,
        selected_plot_summary: str,
        total_chapters: int,
        kb_enabled: bool,
        selected_kb_ids: List[str],
    ) -> str:
        payload = {
            "selected_plot_summary": str(selected_plot_summary or "").strip(),
            "total_chapters": int(total_chapters),
            "kb_enabled": bool(kb_enabled),
            "selected_kb_ids": sorted(str(x) for x in (selected_kb_ids or [])),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _is_outline_job_stale(state: Dict[str, Any], now_ts: int) -> bool:
        job = _outline_job(state)
        if str(job.get("status") or "") != "running":
            return False
        try:
            last_hb = int(job.get("last_heartbeat_at") or 0)
        except (TypeError, ValueError):
            last_hb = 0
        if last_hb <= 0:
            return True
        return (int(now_ts) - int(last_hb)) >= int(OUTLINE_JOB_STALE_AFTER_S)

    def list_project_ids() -> List[str]:
        ids = {p.name for p in projects_dir.iterdir() if p.is_dir()}
        ids.update({p.stem for p in states_dir.glob("*.json")})
        return sorted(ids)

    def list_projects_with_meta() -> List[Dict[str, Any]]:
        """返回所有项目及 created_at，按创建时间升序（越早越靠前，越晚越靠后）。"""
        results: List[Dict[str, Any]] = []
        for pid in list_project_ids():
            st = checkpointer.load_state(pid) or {}
            created_at = _project_created_ts(pid, st)
            nickname = st.get("nickname")
            if nickname is not None:
                nickname = str(nickname).strip() or None
            results.append({"project_id": pid, "created_at": created_at, "nickname": nickname})
        results.sort(key=lambda x: int(x["created_at"]))
        return results

    def _tracked(state: Dict[str, Any]):
        """返回绑定到 state['token_usage'] 的 (planner, writer) 包装器。"""
        tu = state.setdefault("token_usage", {})
        project_id = str(state.get("project_id") or "").strip()
        return (
            TokenTrackingLLM(planner, tu, debug_context={"project_id": project_id}),
            TokenTrackingLLM(writer, tu, debug_context={"project_id": project_id}),
        )

    EMPTY_PROJECT_GRACE_SECONDS = 10 * 60

    def _project_has_chapter_files(project_id: str) -> bool:
        chapters_dir = projects_dir / project_id / "chapters"
        return chapters_dir.exists() and any(chapters_dir.glob("*.md"))

    def _project_created_ts(project_id: str, state: Dict[str, Any]) -> int:
        ts = state.get("created_at")
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts)

        state_file = states_dir / f"{project_id}.json"
        if state_file.exists():
            return int(state_file.stat().st_mtime)

        project_dir = projects_dir / project_id
        if project_dir.exists():
            return int(project_dir.stat().st_mtime)

        return int(time.time())

    def _is_empty_project(project_id: str) -> bool:
        state = load_state(project_id)
        outline_structure = state.get("outline_structure") or {}
        has_outline = bool((outline_structure.get("volumes") or [])) or bool((state.get("outline") or "").strip())
        has_chapters = bool(state.get("chapters")) or _project_has_chapter_files(project_id)
        has_activity = bool((state.get("instruction") or "").strip()) or bool(state.get("plot_ideas")) or bool(
            (state.get("selected_plot_summary") or "").strip()
        )
        if has_outline or has_chapters:
            return False
        if has_activity:
            return False

        created_ts = _project_created_ts(project_id, state)
        age_seconds = int(time.time()) - int(created_ts)
        return age_seconds >= EMPTY_PROJECT_GRACE_SECONDS

    def _delete_project_data(project_id: str) -> List[str]:
        deleted_paths: List[str] = []
        state_file = states_dir / f"{project_id}.json"
        project_dir = projects_dir / project_id
        project_vector_dir = vector_dir / project_id

        if state_file.exists():
            state_file.unlink()
            deleted_paths.append(str(state_file))
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
            deleted_paths.append(str(project_dir))
        if project_vector_dir.exists():
            shutil.rmtree(project_vector_dir, ignore_errors=True)
            deleted_paths.append(str(project_vector_dir))
        return deleted_paths

    def cleanup_empty_projects() -> None:
        for project_id in list_project_ids():
            if _is_empty_project(project_id):
                _delete_project_data(project_id)

    def chapter_meta_of(state: Dict[str, Any], chapter_index: int) -> Optional[Dict[str, Any]]:
        for item in state.get("chapters", []):
            if int(item.get("index", -1)) == int(chapter_index):
                return item
        return None

    def outline_chapter_count(state: Dict[str, Any]) -> int:
        vols = (state.get("outline_structure") or {}).get("volumes") or []
        total = 0
        for vol in vols:
            if isinstance(vol, dict):
                total += len(vol.get("chapters") or [])
        return total

    def outline_generated_until(state: Dict[str, Any]) -> int:
        if "outline_generated_until" in state:
            try:
                return int(state.get("outline_generated_until", -1))
            except (TypeError, ValueError):
                pass
        return outline_chapter_count(state) - 1

    def _character_snapshot_text(project_id: str, chapter_index: int) -> str:
        try:
            snap = graph_store.load_for_chapter(project_id, chapter_index)
        except FileNotFoundError:
            snap = {"nodes": [], "edges": []}
        nodes = list(snap.get("nodes", []))[:8]
        edges = list(snap.get("edges", []))[:12]
        if not nodes:
            return "（暂无）"
        id_to_name = {str(n.get("id")): str(n.get("name") or n.get("id")) for n in nodes if n.get("id")}
        names = [id_to_name[k] for k in list(id_to_name.keys())[:8]]
        rels: List[str] = []
        for e in edges:
            fr = id_to_name.get(str(e.get("from_id")), str(e.get("from_id") or ""))
            to = id_to_name.get(str(e.get("to_id")), str(e.get("to_id") or ""))
            rel = str(e.get("relation") or "")
            if fr and to:
                rels.append(f"{fr}->{to}({rel})")
        return f"人物：{', '.join(names)}\n关系：{'; '.join(rels) if rels else '（暂无）'}"

    def _recent_fact_pack(state: Dict[str, Any], start_idx: int) -> Dict[str, Any]:
        pid = str(state.get("project_id") or "")
        rag_ctx = rag_retriever.retrieve_for_chapter(
            project_id=pid,
            current_chapter_index=max(0, int(start_idx)),
            k_chapters=5,
            k_outline=0,
        )
        summaries = rag_ctx.get("summaries") or []
        recent_summaries = "\n".join(
            f"- 第{int(item.get('chapter_index', -1)) + 1}章：{item.get('text')}" for item in summaries
        ) if summaries else "（无）"

        vols = (state.get("outline_structure") or {}).get("volumes") or []
        flat: List[Dict[str, Any]] = []
        for vol in vols:
            if not isinstance(vol, dict):
                continue
            for ch in vol.get("chapters") or []:
                if isinstance(ch, dict):
                    flat.append(ch)
        begin = max(0, int(start_idx) - 5)
        end = min(len(flat), int(start_idx))
        rows: List[str] = []
        for g in range(begin, end):
            ch = flat[g]
            pts = ch.get("points") if isinstance(ch.get("points"), list) else []
            rows.append(f"- {g}: {ch.get('title') or f'第{g + 1}章'} | {'；'.join(str(p) for p in pts[:3]) if pts else '（无）'}")
        recent_outline_points = "\n".join(rows) if rows else "（无）"

        # 约束块只保留“创作意图 + 二创覆盖”：
        # - 剧情概要(selected_plot_summary)已经在 outline_extend_window 的主 prompt 中单独注入；
        # - 若这里再次注入，会造成同一语义重复，增加 token 且可能放大模型对重复文本的偏置。
        constraints = (
            f"创作意图：{state.get('instruction') or '（无）'}\n"
            f"二创覆盖：{json.dumps(state.get('canon_overrides') or [], ensure_ascii=False)[:3000]}"
        )
        return {
            "recent_summaries": recent_summaries,
            "recent_outline_points": recent_outline_points,
            "character_snapshot": _character_snapshot_text(pid, int(start_idx) - 1),
            "story_constraints": constraints,
        }

    async def ensure_outline_window(
        state: Dict[str, Any],
        *,
        emit: ProgressFn,
        tp: Any,
    ) -> None:
        total = int(state.get("total_chapters") or 0)
        if total <= 0:
            return
        current_idx = int(state.get("current_chapter_index") or 0)
        generated_until = outline_generated_until(state)
        need_extend = (current_idx + OUTLINE_TRIGGER_MARGIN) > generated_until
        if not need_extend:
            return
        if generated_until >= total - 1:
            return

        start_idx = generated_until + 1
        extend_count = int(state.get("outline_window_size") or OUTLINE_WINDOW_SIZE)
        end_idx = min(total - 1, start_idx + max(1, extend_count) - 1)
        await emit("plan_outline_extend", f"正在补齐后续大纲（第 {start_idx + 1}~{end_idx + 1} 章）…")
        kb_context = ""
        if state.get("kb_enabled") and state.get("selected_kb_ids"):
            kb_context = await build_kb_context_for_outline(
                kb_ids=list(state.get("selected_kb_ids") or []),
                plot_summary=str(state.get("selected_plot_summary") or ""),
                retriever=global_kb_retriever,
            )
        pack = _recent_fact_pack(state, start_idx)
        out = await plan_outline_extend_node(
            state,
            start_chapter=start_idx,
            extend_count=extend_count,
            llm=tp,
            on_progress=emit,
            kb_context=kb_context or None,
            recent_fact_pack=pack,
        )
        new_indices = set(int(i) for i in (out.get("outline_extended_indices") or []))
        state.update(out)
        if new_indices:
            rag_indexer.upsert_outline_chunks_range(
                project_id=str(state.get("project_id") or ""),
                outline_structure=state.get("outline_structure") or {"volumes": []},
                start_index=min(new_indices),
                end_index=max(new_indices),
            )
        await emit("persist_outline_extend", "正在保存扩窗后的大纲…")
        save_state(str(state.get("project_id") or ""), state)

    def latest_chapter_index(state: Dict[str, Any]) -> Optional[int]:
        chapters = state.get("chapters", [])
        if not chapters:
            return None
        return max(int(item.get("index", -1)) for item in chapters)

    def ensure_latest_chapter_only(state: Dict[str, Any], index: int) -> None:
        latest_idx = latest_chapter_index(state)
        if latest_idx is None:
            raise HTTPException(status_code=400, detail="no chapters generated")
        if int(index) != int(latest_idx):
            raise HTTPException(
                status_code=400,
                detail=f"only latest chapter can be rewritten/regenerated (latest={latest_idx})",
            )

    def raise_llm_http_error(exc: Exception, *, scene: str) -> None:
        """把 LLM 常见异常转换为前端可展示的 HTTP 错误。"""
        if isinstance(exc, (OpenAIAPIConnectionError, httpx.ConnectError)):
            msg = f"LLM 连接失败（{scene}）：{exc}"
            logger.error(msg)
            raise HTTPException(status_code=502, detail=msg)
        if isinstance(exc, (OpenAIAPITimeoutError, httpx.TimeoutException, TimeoutError)):
            msg = f"LLM 响应超时（{scene}）：{exc}"
            logger.error(msg)
            raise HTTPException(status_code=504, detail=msg)

    async def run_illustration_pipeline_after_refine(
        state: Dict[str, Any],
        *,
        scene: str,
        planner_llm: Any,
        emit: ProgressFn,
    ) -> None:
        """在 refine 之后：识别插图点 → OpenAI 生图（失败则跳过）→ 插入正文。"""
        if not state.get("enable_chapter_illustrations", False):
            return
        pid = state.get("project_id", "")
        await emit("illustration_points", "正在识别插图位置并生成画面描述（LLM）…")
        logger.info("[%s] step=identify_illustration_points project=%s", scene, pid)
        out = await identify_illustration_points_node(
            state, llm=planner_llm, chapter_store=chapter_store
        )
        state.update(out)
        await emit("illustration_fetch", "正在使用 OpenAI 生成插图…")
        logger.info("[%s] step=fetch_or_generate_images project=%s", scene, pid)
        out = await fetch_or_generate_images_node(state, project_root=projects_dir)
        state.update(out)
        await emit("illustration_insert", "正在将插图插入正文…")
        logger.info("[%s] step=insert_illustrations project=%s", scene, pid)
        out = await insert_illustrations_into_chapter_node(state, chapter_store=chapter_store)
        state.update(out)

    async def generate_chapter_for_current_index(
        state: Dict[str, Any],
        *,
        scene: str,
        tp: Any = None,
        tw: Any = None,
        tw_stream: Any = None,
        emit_progress: Optional[ProgressFn] = None,
        stream_llm_output: bool = False,
    ) -> None:
        _emit = emit_progress or _noop_progress
        _p, _w = (tp, tw) if tp is not None and tw is not None else (planner, writer)
        _tw_refine = tw_stream or _w
        pid = state.get("project_id", "")
        ch_idx = state.get("current_chapter_index")
        logger.info("[%s] pipeline_start project=%s chapter_index=%s", scene, pid, ch_idx)
        try:
            await _emit("write_chapter", "正在撰写本章初稿（LLM，可能较慢）…")
            logger.info("[%s] step=write_chapter project=%s chapter_index=%s", scene, pid, ch_idx)
            out = await write_chapter_node(
                state,
                llm=_w,
                chapter_store=chapter_store,
                rag_retriever=rag_retriever,
                graph_store=graph_store,
                global_kb_retriever=global_kb_retriever,
                planner_llm=_p,
            )
            state.update(out)
            await _emit("refine_chapter", "正在润色本章（LLM）…")
            logger.info("[%s] step=refine_chapter project=%s chapter_index=%s", scene, pid, ch_idx)
            out = await refine_chapter_node(
                state,
                llm=_tw_refine,
                chapter_store=chapter_store,
                stream_llm_output=stream_llm_output,
                emit_token_progress=_emit if stream_llm_output else None,
            )
            state.update(out)
            await run_illustration_pipeline_after_refine(
                state, scene=scene, planner_llm=_p, emit=_emit
            )
            await _emit("post_chapter", "正在生成摘要并更新人物图谱（LLM）…")
            logger.info("[%s] step=post_chapter project=%s chapter_index=%s", scene, pid, ch_idx)
            out = await post_chapter_node(
                state,
                llm=_p,
                chapter_store=chapter_store,
                rag_indexer=rag_indexer,
                graph_store=graph_store,
            )
            _stage_head_overlay_from_post_result(state, out)
            logger.info("[%s] pipeline_done project=%s chapter_index=%s", scene, pid, ch_idx)
        except Exception as exc:
            raise_llm_http_error(exc, scene=scene)
            raise

    @app.post("/projects")
    async def create_project(req: CreateProjectRequest):
        project_id = req.project_id or f"p-{uuid.uuid4().hex[:12]}"
        ensure_project(project_id)
        state = _default_state(project_id)
        if req.instruction:
            state["instruction"] = req.instruction
        if req.style_constraint is not None:
            state["style_constraint"] = str(req.style_constraint).strip()
        if req.total_chapters is not None:
            state["total_chapters"] = int(req.total_chapters)
        if req.chapter_word_target is not None:
            state["chapter_word_target"] = int(req.chapter_word_target)
        if req.enable_chapter_illustrations is not None:
            state["enable_chapter_illustrations"] = bool(req.enable_chapter_illustrations)
        if req.selected_kb_ids is not None:
            state["selected_kb_ids"] = list(req.selected_kb_ids)
            state["kb_enabled"] = bool(state["selected_kb_ids"])
        save_state(project_id, state)
        return {"project_id": project_id}

    @app.get("/projects")
    async def list_projects():
        cleanup_empty_projects()
        return {"projects": list_projects_with_meta()}

    @app.get("/projects/{project_id}")
    async def get_project(project_id: str):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id, include_head_overlay=True)
        return {
            "project_id": project_id,
            "nickname": state.get("nickname"),
            "instruction": state.get("instruction", ""),
            "plot_ideas": state.get("plot_ideas") or [],
            "style_constraint": state.get("style_constraint", ""),
            "selected_plot_summary": state.get("selected_plot_summary", ""),
            "outline_structure": state.get("outline_structure", {"volumes": []}),
            "chapters": state.get("chapters", []),
            "current_chapter_index": state.get("current_chapter_index", 0),
            "total_chapters": state.get("total_chapters"),
            "outline_generated_until": outline_generated_until(state),
            "outline_window_size": state.get("outline_window_size"),
            "outline_initial_chapters": state.get("outline_initial_chapters"),
            "chapter_word_target": state.get("chapter_word_target"),
            "enable_chapter_illustrations": state.get("enable_chapter_illustrations", False),
            "created_at": _project_created_ts(project_id, state),
            "token_usage": state.get("token_usage") or {},
            "selected_kb_ids": state.get("selected_kb_ids") or [],
            "kb_enabled": bool(state.get("kb_enabled")),
            "canon_overrides": state.get("canon_overrides") or [],
            "outline_checkpoint": state.get("outline_checkpoint") or {"phase": None, "input_fingerprint": "", "updated_at": 0},
            "outline_job": state.get("outline_job") or {
                "status": "idle",
                "job_id": "",
                "started_at": 0,
                "last_heartbeat_at": 0,
            },
        }

    @app.get("/projects/{project_id}/events")
    async def list_project_events(project_id: str, chapter_index: Optional[int] = None, limit: int = Query(200)):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        bootstrap_project_events_if_empty(project_id)
        events = event_store.list_events(project_id, chapter_index=chapter_index, limit=int(limit))
        return {
            "project_id": project_id,
            "events": events,
        }

    @app.patch("/projects/{project_id}")
    async def patch_project(project_id: str, req: PatchProjectRequest):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id)
        nickname = req.nickname
        if nickname is not None:
            normalized = str(nickname).strip()
            state["nickname"] = normalized or None
        if req.style_constraint is not None:
            state["style_constraint"] = str(req.style_constraint).strip()
        save_state(project_id, state)
        return {
            "project_id": project_id,
            "nickname": state.get("nickname"),
            "style_constraint": state.get("style_constraint", ""),
        }

    @app.delete("/projects/{project_id}")
    async def delete_project(project_id: str):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        deleted_paths = _delete_project_data(project_id)
        return {
            "project_id": project_id,
            "deleted": True,
            "deleted_paths": deleted_paths,
        }

    @app.post("/projects/{project_id}/plot-ideas")
    async def generate_plot_ideas(project_id: str, req: PlotIdeasRequest, stream: bool = Query(False)):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        async def _run_plot_ideas(emit: ProgressFn) -> Dict[str, Any]:
            state = load_state(project_id)
            state["instruction"] = req.instruction
            instruction_brief = _truncate_event_text(req.instruction)
            emit_project_event(
                project_id,
                event_name="generate_plot_ideas",
                event_content=f"根据{instruction_brief}生成概要",
                status="start",
            )
            tp, tw = _tracked(state)
            logger.info("[生成剧情概要] start project=%s", project_id)
            await emit("plot_ideas", "正在生成剧情概要候选（LLM）…")
            try:
                kb_context = ""
                if state.get("kb_enabled") and state.get("selected_kb_ids"):
                    kb_context = await build_kb_context_for_outline(
                        kb_ids=list(state.get("selected_kb_ids") or []),
                        plot_summary=str(req.instruction or ""),
                        retriever=global_kb_retriever,
                    )
                out = await generate_plot_ideas_node(state, llm=tp, kb_context=kb_context or None)
            except Exception as exc:
                emit_project_event(
                    project_id,
                    event_name="generate_plot_ideas",
                    event_content=f"根据{instruction_brief}生成概要失败：{_truncate_event_text(str(exc))}",
                    status="error",
                )
                raise_llm_http_error(exc, scene="生成剧情概要")
                raise
            state.update(out)
            save_state(project_id, state)
            emit_project_event(
                project_id,
                event_name="generate_plot_ideas",
                event_content=f"根据{instruction_brief}生成概要",
                status="success",
            )
            logger.info("[生成剧情概要] done project=%s ideas=%s", project_id, len(state.get("plot_ideas", [])))
            return {"plot_ideas": state.get("plot_ideas", [])}

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_plot_ideas), media_type=NDJSON_MEDIA)
        return await _run_plot_ideas(_noop_progress)

    @app.post("/projects/{project_id}/outline")
    async def generate_outline(project_id: str, req: OutlineRequest, stream: bool = Query(False)):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        lock = _outline_lock(project_id)

        async def _save_state_with_lock(state: Dict[str, Any]) -> None:
            async with lock:
                save_state(project_id, state)

        async def _prepare_outline_run() -> Dict[str, Any]:
            async with lock:
                state = load_state(project_id)
                state["selected_plot_summary"] = req.selected_plot_summary
                if req.total_chapters is not None:
                    state["total_chapters"] = int(req.total_chapters)
                if "outline_initial_chapters" not in state:
                    state["outline_initial_chapters"] = OUTLINE_INITIAL_CHAPTERS
                if "outline_window_size" not in state:
                    state["outline_window_size"] = OUTLINE_WINDOW_SIZE
                total = int(state.get("total_chapters") or DEFAULT_TOTAL_CHAPTERS)
                fp = _outline_fingerprint(
                    selected_plot_summary=req.selected_plot_summary,
                    total_chapters=total,
                    kb_enabled=bool(state.get("kb_enabled")),
                    selected_kb_ids=list(state.get("selected_kb_ids") or []),
                )
                cp = _outline_checkpoint(state)
                prev_fp = str(cp.get("input_fingerprint") or "")
                if req.force_restart:
                    _set_outline_checkpoint(state, phase=None, fingerprint=fp)
                elif prev_fp and prev_fp != fp:
                    _set_outline_checkpoint(state, phase=None, fingerprint=fp)
                elif not prev_fp:
                    _set_outline_checkpoint(state, phase=cp.get("phase"), fingerprint=fp)

                now_ts = int(time.time())
                job = _outline_job(state)
                if str(job.get("status") or "") == "running":
                    if not _is_outline_job_stale(state, now_ts):
                        raise HTTPException(status_code=409, detail="大纲生成进行中，请稍后再试")
                    _set_outline_job(state, status="idle", heartbeat_at=now_ts)
                new_job_id = f"oj-{uuid.uuid4().hex[:16]}"
                _set_outline_job(
                    state,
                    status="running",
                    job_id=new_job_id,
                    started_at=now_ts,
                    heartbeat_at=now_ts,
                )
                save_state(project_id, state)
                return state

        async def _run_outline(emit: ProgressFn) -> Dict[str, Any]:
            state = await _prepare_outline_run()
            summary_brief = _truncate_event_text(req.selected_plot_summary)
            emit_project_event(
                project_id,
                event_name="generate_outline",
                event_content=f"根据{summary_brief}生成大纲",
                status="start",
            )
            tp, _tw = _tracked(state)
            logger.info("[生成大纲] start project=%s total_chapters=%s", project_id, state.get("total_chapters"))
            total = int(state.get("total_chapters") or DEFAULT_TOTAL_CHAPTERS)
            cp = _outline_checkpoint(state)
            input_fp = str(cp.get("input_fingerprint") or "")
            phase = str(cp.get("phase") or "")

            hb_stop = asyncio.Event()

            async def _heartbeat_loop() -> None:
                while not hb_stop.is_set():
                    try:
                        await asyncio.wait_for(hb_stop.wait(), timeout=float(OUTLINE_HEARTBEAT_INTERVAL_S))
                        break
                    except asyncio.TimeoutError:
                        pass
                    _set_outline_job(state, status="running", heartbeat_at=int(time.time()))
                    try:
                        await _save_state_with_lock(state)
                    except Exception as exc:
                        logger.warning("[生成大纲] heartbeat save failed project=%s err=%s", project_id, exc)

            hb_task = asyncio.create_task(_heartbeat_loop())
            try:
                kb_context = ""
                if state.get("kb_enabled") and state.get("selected_kb_ids"):
                    kb_context = await build_kb_context_for_outline(
                        kb_ids=list(state.get("selected_kb_ids") or []),
                        plot_summary=req.selected_plot_summary,
                        retriever=global_kb_retriever,
                    )

                if total <= PLAN_OUTLINE_SINGLE_CALL_MAX:
                    if phase != "skeleton_done":
                        await emit("plan_outline_short", f"正在生成全书结构化大纲（共 {total} 章）…")
                        short_out = await outline_short_node(
                            state,
                            llm=tp,
                            kb_context=kb_context or None,
                            target_chapters=total,
                        )
                        state.update(short_out)
                        _set_outline_checkpoint(state, phase="skeleton_done", fingerprint=input_fp)
                        await emit("persist_checkpoint", "正在保存阶段进度（outline_short）…")
                        await _save_state_with_lock(state)
                        phase = "skeleton_done"
                else:
                    if phase not in {"skeleton_done", "initial_extend_done"}:
                        await emit("plan_outline_skeleton", f"正在生成全书大纲骨架（共 {total} 章）…")
                        sk_out = await outline_skeleton_lite_node(
                            state,
                            llm=tp,
                            kb_context=kb_context or None,
                        )
                        state.update(sk_out)
                        _set_outline_checkpoint(state, phase="skeleton_done", fingerprint=input_fp)
                        await emit("persist_checkpoint", "正在保存阶段进度（outline_skeleton）…")
                        await _save_state_with_lock(state)
                        phase = "skeleton_done"

                    if phase != "initial_extend_done":
                        extend_count = int(state.get("outline_window_size") or OUTLINE_WINDOW_SIZE)
                        await emit("plan_outline_extend", f"正在扩写首个窗口（前 {extend_count} 章）…")
                        ext_out = await outline_extend_window_node(
                            state,
                            llm=tp,
                            on_progress=emit,
                            kb_context=kb_context or None,
                            start_chapter=0,
                            extend_count=extend_count,
                        )
                        state.update(ext_out)
                        _set_outline_checkpoint(state, phase="initial_extend_done", fingerprint=input_fp)
                        await emit("persist_checkpoint", "正在保存阶段进度（outline_extend）…")
                        await _save_state_with_lock(state)

                await emit("outline_finalize", "正在收敛最终大纲并写入索引…")
                fin_out = await outline_finalize_node(
                    state,
                    llm=tp,
                    rag_indexer=rag_indexer,
                    kb_context=kb_context or None,
                )
                state.update(fin_out)
                if "outline_generated_until" not in fin_out:
                    state["outline_generated_until"] = outline_generated_until(state)
                _set_outline_checkpoint(state, phase=None, fingerprint=input_fp)
                _set_outline_job(state, status="idle", heartbeat_at=int(time.time()))
                await emit("persist", "正在保存大纲并写入 RAG 索引…")
                await _save_state_with_lock(state)
            except Exception as exc:
                emit_project_event(
                    project_id,
                    event_name="generate_outline",
                    event_content=f"根据{summary_brief}生成大纲失败：{_truncate_event_text(str(exc))}",
                    status="error",
                )
                _set_outline_job(state, status="idle", heartbeat_at=int(time.time()))
                try:
                    await _save_state_with_lock(state)
                except Exception:
                    pass
                raise_llm_http_error(exc, scene="生成大纲")
                raise
            finally:
                hb_stop.set()
                await hb_task

            emit_project_event(
                project_id,
                event_name="generate_outline",
                event_content=f"根据{summary_brief}生成大纲",
                status="success",
            )
            vols = (state.get("outline_structure") or {}).get("volumes") or []
            logger.info("[生成大纲] done project=%s volumes=%s", project_id, len(vols))
            return {
                "outline_structure": state.get("outline_structure", {"volumes": []}),
                "outline": state.get("outline", ""),
                "canon_overrides": state.get("canon_overrides") or [],
            }

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_outline), media_type=NDJSON_MEDIA)
        return await _run_outline(_noop_progress)

    @app.post("/projects/{project_id}/outline/window")
    async def generate_outline_window(
        project_id: str,
        req: Optional[OutlineWindowRequest] = None,
        stream: bool = Query(False),
    ):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        async def _run_outline_window(emit: ProgressFn) -> Dict[str, Any]:
            state = load_state(project_id)
            outline = state.get("outline_structure", {"volumes": []})
            if not outline.get("volumes"):
                raise HTTPException(status_code=400, detail="outline not generated")
            total = int(state.get("total_chapters") or DEFAULT_TOTAL_CHAPTERS)
            generated_until = outline_generated_until(state)
            i = int(req.start_chapter) if req and req.start_chapter is not None else (generated_until + 2)
            if req and req.end_chapter is not None:
                j = int(req.end_chapter)
            else:
                window = int(state.get("outline_window_size") or OUTLINE_WINDOW_SIZE)
                j = min(total, i + max(1, window) - 1)
            if generated_until >= total - 1 or i > total:
                return {
                    "outline_structure": state.get("outline_structure", {"volumes": []}),
                    "outline": state.get("outline", ""),
                    "outline_generated_until": generated_until,
                    "outline_extended_indices": [],
                }
            if i > j:
                raise HTTPException(status_code=400, detail="start_chapter must be <= end_chapter")
            if j > total:
                raise HTTPException(status_code=400, detail=f"end_chapter exceeds total_chapters={total}")
            start_idx = i - 1
            extend_count = j - i + 1
            state["last_written_chapter_index"] = int(latest_chapter_index(state) or -1)
            emit_project_event(
                project_id,
                event_name="generate_outline_window",
                event_content=f"生成第{i}~{j}章窗口大纲",
                status="start",
            )

            tp, _tw = _tracked(state)
            await emit("outline_window", f"正在生成第 {i}~{j} 章大纲…")
            try:
                kb_context = ""
                if state.get("kb_enabled") and state.get("selected_kb_ids"):
                    kb_context = await build_kb_context_for_outline(
                        kb_ids=list(state.get("selected_kb_ids") or []),
                        plot_summary=str(state.get("selected_plot_summary") or ""),
                        retriever=global_kb_retriever,
                    )
                pack = _recent_fact_pack(state, start_idx)
                ext_out = await outline_extend_window_node(
                    state,
                    llm=tp,
                    on_progress=emit,
                    kb_context=kb_context or None,
                    recent_fact_pack=pack,
                    start_chapter=start_idx,
                    extend_count=extend_count,
                )
                state.update(ext_out)
                fin_out = await outline_finalize_node(
                    state,
                    llm=tp,
                    rag_indexer=rag_indexer,
                    kb_context=kb_context or None,
                )
                state.update(fin_out)
                await emit("persist", "正在保存更新后的大纲…")
                save_state(project_id, state)
            except Exception as exc:
                emit_project_event(
                    project_id,
                    event_name="generate_outline_window",
                    event_content=f"生成第{i}~{j}章窗口大纲失败：{_truncate_event_text(str(exc))}",
                    status="error",
                )
                raise
            emit_project_event(
                project_id,
                event_name="generate_outline_window",
                event_content=f"生成第{i}~{j}章窗口大纲",
                status="success",
            )
            return {
                "outline_structure": state.get("outline_structure", {"volumes": []}),
                "outline": state.get("outline", ""),
                "outline_generated_until": outline_generated_until(state),
                "outline_extended_indices": state.get("outline_extended_indices") or [],
            }

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_outline_window), media_type=NDJSON_MEDIA)
        return await _run_outline_window(_noop_progress)

    @app.get("/projects/{project_id}/chapters")
    async def list_chapters(project_id: str):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id, include_head_overlay=True)
        return {"chapters": state.get("chapters", [])}

    @app.get("/projects/{project_id}/chapters/{index}")
    async def get_chapter(project_id: str, index: int):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id, include_head_overlay=True)
        meta = chapter_meta_of(state, index)
        try:
            if meta and meta.get("path_or_content_ref"):
                content = chapter_store.load_by_ref(meta["path_or_content_ref"])
            else:
                content = chapter_store.load(project_id, index)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="chapter not found")
        return {"index": index, "content": content, "meta": meta}

    @app.get("/projects/{project_id}/character-graph")
    async def get_character_graph(project_id: str, chapter_index: Optional[int] = None):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id)
        if chapter_index is None:
            latest_idx = latest_chapter_index(state)
            graph = graph_store.load_for_chapter(project_id, latest_idx if latest_idx is not None else -1)
            return {"chapter_index": latest_idx, "character_graph": graph}
        try:
            graph = graph_store.load_for_chapter(project_id, int(chapter_index))
        except FileNotFoundError:
            graph = {"nodes": [], "edges": []}
        return {"chapter_index": int(chapter_index), "character_graph": graph}

    @app.post("/projects/{project_id}/chapters/next")
    async def write_next_chapter(project_id: str, req: NextChapterRequest, stream: bool = Query(False)):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        async def _run_next(emit: ProgressFn) -> Dict[str, Any]:
            state = load_state(project_id)
            _commit_head_overlay_into_state(project_id, state)
            outline = state.get("outline_structure", {"volumes": []})
            if not outline.get("volumes"):
                raise HTTPException(status_code=400, detail="outline not generated")

            chapters = state.get("chapters", [])
            next_idx = max([int(c.get("index", -1)) for c in chapters], default=-1) + 1
            total_chapters = int(state.get("total_chapters") or DEFAULT_TOTAL_CHAPTERS)
            if next_idx >= total_chapters:
                raise HTTPException(status_code=400, detail=f"all chapters completed (total={total_chapters})")
            chapter_no = next_idx + 1
            state["current_chapter_index"] = next_idx
            if req.chapter_word_target is not None:
                state["chapter_word_target"] = int(req.chapter_word_target)
            if req.enable_chapter_illustrations is not None:
                state["enable_chapter_illustrations"] = bool(req.enable_chapter_illustrations)
            if req.style_constraint is not None:
                state["style_constraint"] = str(req.style_constraint).strip()

            tp, tw = _tracked(state)
            tw_stream = None
            if stream:
                tu = state.setdefault("token_usage", {})
                tw_stream = TokenTrackingLLM(
                    writer_streaming,
                    tu,
                    debug_context={"project_id": str(state.get("project_id") or "").strip()},
                )
            logger.info("[续写下一章] start project=%s next_index=%s", project_id, next_idx)
            emit_project_event(
                project_id,
                event_name="write_next_chapter",
                event_content=f"续写第{chapter_no}章",
                chapter_index=next_idx,
                status="start",
            )
            await emit("chapter_pipeline", f"开始续写第 {next_idx + 1} 章…")
            try:
                await ensure_outline_window(state, emit=emit, tp=tp)
                await generate_chapter_for_current_index(
                    state,
                    scene="续写下一章",
                    tp=tp,
                    tw=tw,
                    tw_stream=tw_stream,
                    emit_progress=emit,
                    stream_llm_output=bool(stream),
                )
            except Exception as exc:
                emit_project_event(
                    project_id,
                    event_name="write_next_chapter",
                    event_content=f"续写第{chapter_no}章失败：{_truncate_event_text(str(exc))}",
                    chapter_index=next_idx,
                    status="error",
                )
                raise
            save_state(project_id, state)
            emit_project_event(
                project_id,
                event_name="write_next_chapter",
                event_content=f"续写第{chapter_no}章",
                chapter_index=next_idx,
                status="success",
            )

            meta = chapter_meta_of(state, next_idx)
            content = chapter_store.load(project_id, next_idx)
            logger.info("[续写下一章] done project=%s chapter_index=%s", project_id, next_idx)
            return {"chapter_index": next_idx, "chapter": content, "meta": meta}

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_next), media_type=NDJSON_MEDIA)
        return await _run_next(_noop_progress)

    @app.post("/projects/{project_id}/chapters/{index}/regenerate")
    async def regenerate_chapter(
        project_id: str, index: int, req: RegenerateChapterRequest, stream: bool = Query(False)
    ):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        async def _run_regen(emit: ProgressFn) -> Dict[str, Any]:
            state = load_state(project_id)
            _clear_head_overlay(project_id)
            outline = state.get("outline_structure", {"volumes": []})
            if not outline.get("volumes"):
                raise HTTPException(status_code=400, detail="outline not generated")
            ensure_latest_chapter_only(state, index)

            try:
                chapter_store.load(project_id, int(index))
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="chapter not found")

            state["current_chapter_index"] = int(index)
            if req.chapter_word_target is not None:
                state["chapter_word_target"] = int(req.chapter_word_target)
            if req.enable_chapter_illustrations is not None:
                state["enable_chapter_illustrations"] = bool(req.enable_chapter_illustrations)
            if req.style_constraint is not None:
                state["style_constraint"] = str(req.style_constraint).strip()

            tp, tw = _tracked(state)
            tw_stream = None
            if stream:
                tu = state.setdefault("token_usage", {})
                tw_stream = TokenTrackingLLM(
                    writer_streaming,
                    tu,
                    debug_context={"project_id": str(state.get("project_id") or "").strip()},
                )
            logger.info("[重新生成本章] start project=%s chapter_index=%s", project_id, index)
            await emit("chapter_pipeline", f"开始重新生成第 {int(index) + 1} 章…")
            await generate_chapter_for_current_index(
                state,
                scene="重新生成本章",
                tp=tp,
                tw=tw,
                tw_stream=tw_stream,
                emit_progress=emit,
                stream_llm_output=bool(stream),
            )
            save_state(project_id, state)

            meta = chapter_meta_of(state, int(index))
            content = chapter_store.load(project_id, int(index))
            logger.info("[重新生成本章] done project=%s chapter_index=%s", project_id, index)
            return {"chapter_index": int(index), "chapter": content, "meta": meta}

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_regen), media_type=NDJSON_MEDIA)
        return await _run_regen(_noop_progress)

    @app.delete("/projects/{project_id}/chapters/{index}/tail")
    async def rollback_chapters_tail(project_id: str, index: int):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id)
        chapters = list(state.get("chapters", []))
        if not chapters:
            raise HTTPException(status_code=400, detail="no chapters generated")

        keep_to = int(index)
        latest_idx = latest_chapter_index(state)
        if keep_to < 0:
            raise HTTPException(status_code=400, detail="index must be >= 0")
        if latest_idx is None or keep_to > latest_idx:
            raise HTTPException(status_code=400, detail="index out of range")

        to_delete = [item for item in chapters if int(item.get("index", -1)) > keep_to]
        if not to_delete:
            return {
                "kept_until": keep_to,
                "deleted_count": 0,
                "chapters": chapters,
                "current_chapter_index": state.get("current_chapter_index", 0),
            }

        for item in to_delete:
            chapter_idx = int(item.get("index", -1))
            ref = item.get("path_or_content_ref")
            try:
                if ref:
                    chapter_path = projects_dir / ref
                else:
                    chapter_path = chapter_store.path_for(project_id, chapter_idx)
                if chapter_path.exists():
                    chapter_path.unlink()
            except OSError:
                logger.warning("Failed to delete chapter file: project=%s index=%s", project_id, chapter_idx)

        state["chapters"] = [item for item in chapters if int(item.get("index", -1)) <= keep_to]
        state["current_chapter_index"] = keep_to
        if int(state.get("current_chapter_index", 0)) > keep_to:
            state["current_chapter_index"] = keep_to
        if int(state.get("current_chapter_index", 0)) < 0:
            state["current_chapter_index"] = 0

        rag_indexer.delete_chapter_summaries_from(project_id, keep_to + 1)
        graph_store.delete_snapshots_from(project_id, keep_to + 1)
        graph_store.refresh_legacy_latest(project_id)
        _clear_head_overlay(project_id)
        state["character_graph"] = graph_store.load_for_chapter(project_id, keep_to)
        save_state(project_id, state)
        return {
            "kept_until": keep_to,
            "deleted_count": len(to_delete),
            "chapters": state.get("chapters", []),
            "current_chapter_index": state.get("current_chapter_index", keep_to),
        }

    @app.post("/projects/{project_id}/chapters/{index}/rewrite")
    async def rewrite_chapter(project_id: str, index: int, req: RewriteRequest, stream: bool = Query(False)):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")

        async def _run_rewrite(emit: ProgressFn) -> Dict[str, Any]:
            state = load_state(project_id)
            _clear_head_overlay(project_id)
            ensure_latest_chapter_only(state, index)
            try:
                chapter_text = chapter_store.load(project_id, index)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="chapter not found")

            state["current_chapter_index"] = int(index)
            state["current_chapter_final"] = chapter_text
            state["user_feedback"] = req.user_feedback
            feedback_brief = _truncate_event_text(req.user_feedback)
            chapter_no = int(index) + 1
            emit_project_event(
                project_id,
                event_name="rewrite_chapter_with_feedback",
                event_content=f"用户反馈{feedback_brief}，并重新生成第{chapter_no}章",
                chapter_index=int(index),
                status="start",
            )
            state["update_outline_on_feedback"] = bool(req.update_outline)
            if req.enable_chapter_illustrations is not None:
                state["enable_chapter_illustrations"] = bool(req.enable_chapter_illustrations)
            if req.style_constraint is not None:
                state["style_constraint"] = str(req.style_constraint).strip()

            tp, tw = _tracked(state)
            tw_stream = None
            if stream:
                tu = state.setdefault("token_usage", {})
                tw_stream = TokenTrackingLLM(
                    writer_streaming,
                    tu,
                    debug_context={"project_id": str(state.get("project_id") or "").strip()},
                )
            logger.info(
                "[反馈重写] start project=%s chapter_index=%s update_outline=%s",
                project_id,
                index,
                req.update_outline,
            )
            try:
                await emit("rewrite", "正在根据反馈重写本章（LLM）…")
                # stream=1 时仅 refine_chapter 推送 token 流；重写阶段不流式，避免前端预览停在「重写稿」与最终「润色稿」不一致。
                out = await rewrite_with_feedback_node(
                    state,
                    llm=tw_stream or tw,
                    chapter_store=chapter_store,
                    stream_llm_output=False,
                    emit_token_progress=None,
                )
                state.update(out)
                if req.update_outline:
                    await emit("update_outline", "正在根据重写结果更新大纲要点（LLM）…")
                    out = await update_outline_from_feedback_node(
                        state,
                        llm=tp,
                        rag_indexer=rag_indexer,
                    )
                    state.update(out)

                await emit("refine_chapter", "正在润色重写后的本章（LLM）…")
                out = await refine_chapter_node(
                    state,
                    llm=tw_stream or tw,
                    chapter_store=chapter_store,
                    stream_llm_output=bool(stream),
                    emit_token_progress=emit if stream else None,
                )
                state.update(out)
                await run_illustration_pipeline_after_refine(
                    state, scene="反馈重写", planner_llm=tp, emit=emit
                )

                await emit("post_chapter", "正在刷新摘要与人物图谱（LLM）…")
                out = await post_chapter_node(
                    state,
                    llm=tp,
                    chapter_store=chapter_store,
                    rag_indexer=rag_indexer,
                    graph_store=graph_store,
                )
                _stage_head_overlay_from_post_result(state, out)
            except Exception as exc:
                emit_project_event(
                    project_id,
                    event_name="rewrite_chapter_with_feedback",
                    event_content=f"用户反馈{feedback_brief}，并重新生成第{chapter_no}章失败：{_truncate_event_text(str(exc))}",
                    chapter_index=int(index),
                    status="error",
                )
                raise_llm_http_error(exc, scene="反馈重写")
                raise
            save_state(project_id, state)
            emit_project_event(
                project_id,
                event_name="rewrite_chapter_with_feedback",
                event_content=f"用户反馈{feedback_brief}，并重新生成第{chapter_no}章",
                chapter_index=int(index),
                status="success",
            )

            new_text = chapter_store.load(project_id, index)
            logger.info("[反馈重写] done project=%s chapter_index=%s", project_id, index)
            return {
                "chapter_index": index,
                "chapter": new_text,
                "outline_structure": state.get("outline_structure", {"volumes": []}),
            }

        if stream:
            return StreamingResponse(ndjson_with_progress(_run_rewrite), media_type=NDJSON_MEDIA)
        return await _run_rewrite(_noop_progress)

    @app.post("/knowledge-bases")
    async def create_knowledge_base(req: CreateKnowledgeBaseRequest):
        rec = kb_store.create_base(req.name)
        return rec

    @app.get("/knowledge-bases")
    async def list_knowledge_bases():
        bases = kb_store.list_bases()
        enriched = []
        for b in bases:
            kid = b.get("kb_id")
            if not kid:
                continue
            docs = kb_store.list_documents(str(kid))
            enriched.append({**b, "documents": len(docs)})
        return {"knowledge_bases": enriched}

    @app.get("/knowledge-bases/{kb_id}")
    async def get_knowledge_base(kb_id: str):
        base = kb_store.get_base(kb_id)
        if not base:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        docs = kb_store.list_documents(kb_id)
        return {**base, "documents": docs}

    @app.post("/knowledge-bases/{kb_id}/documents")
    async def upload_knowledge_document(kb_id: str, file: UploadFile = File(...)):
        if not kb_store.get_base(kb_id):
            raise HTTPException(status_code=404, detail="knowledge base not found")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".txt", ".md"):
            raise HTTPException(status_code=400, detail="仅支持上传 .txt 或 .md 文件")
        doc_id = f"d-{uuid.uuid4().hex[:12]}"
        job_id = f"j-{uuid.uuid4().hex[:10]}"
        raw_dir = kb_store.kb_dir(kb_id) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        dest = raw_dir / f"{doc_id}{suffix}"
        data = await file.read()
        dest.write_bytes(data)
        kb_store.upsert_document_record(
            kb_id,
            {
                "doc_id": doc_id,
                "filename": file.filename or dest.name,
                "status": "indexing",
                "job_id": job_id,
                "bytes": len(data),
            },
        )
        kb_store.update_base_meta(kb_id, status="indexing")
        key = _kb_cancel_key(kb_id, job_id)
        task = asyncio.create_task(_kb_document_pipeline(kb_id, doc_id, job_id, dest))
        _kb_background_tasks[key] = task
        return {"doc_id": doc_id, "job_id": job_id, "status": "started"}

    @app.get("/knowledge-bases/{kb_id}/documents")
    async def list_knowledge_documents(kb_id: str):
        if not kb_store.get_base(kb_id):
            raise HTTPException(status_code=404, detail="knowledge base not found")
        return {"documents": kb_store.list_documents(kb_id)}

    @app.get("/knowledge-bases/{kb_id}/jobs/{job_id}")
    async def get_knowledge_job(kb_id: str, job_id: str):
        job = kb_store.load_job(kb_id, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/knowledge-bases/{kb_id}/jobs/{job_id}/cancel")
    async def cancel_knowledge_job(kb_id: str, job_id: str):
        key = _kb_cancel_key(kb_id, job_id)
        ev = _kb_cancel_events.setdefault(key, asyncio.Event())
        ev.set()
        return {"ok": True, "job_id": job_id}

    @app.post("/knowledge-bases/{kb_id}/documents/{doc_id}/rebuild")
    async def rebuild_knowledge_document(kb_id: str, doc_id: str):
        if not kb_store.get_base(kb_id):
            raise HTTPException(status_code=404, detail="knowledge base not found")
        doc = kb_store.get_document(kb_id, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="document not found")
        raw_dir = kb_store.kb_dir(kb_id) / "raw"
        matches = list(raw_dir.glob(f"{doc_id}.*"))
        if not matches:
            raise HTTPException(status_code=404, detail="raw file not found")
        raw_path = matches[0]
        job_id = f"j-{uuid.uuid4().hex[:10]}"
        kb_store.upsert_document_record(
            kb_id,
            {**doc, "doc_id": doc_id, "status": "indexing", "job_id": job_id},
        )
        key = _kb_cancel_key(kb_id, job_id)
        task = asyncio.create_task(_kb_document_pipeline(kb_id, doc_id, job_id, raw_path))
        _kb_background_tasks[key] = task
        return {"doc_id": doc_id, "job_id": job_id, "status": "started"}

    @app.get("/knowledge-bases/{kb_id}/assets/summary")
    async def get_knowledge_assets_summary(kb_id: str, doc_id: Optional[str] = Query(None)):
        if not kb_store.get_base(kb_id):
            raise HTTPException(status_code=404, detail="knowledge base not found")
        if doc_id:
            data = kb_store.load_assets_doc(kb_id, doc_id)
            return {"kb_id": kb_id, "doc_id": doc_id, "assets": data}
        data = kb_store.load_assets(kb_id)
        return {"kb_id": kb_id, "doc_id": None, "assets": data}

    @app.put("/knowledge-bases/{kb_id}/assets/summary")
    async def save_knowledge_assets_summary(kb_id: str, req: SaveKnowledgeAssetsRequest):
        if not kb_store.get_base(kb_id):
            raise HTTPException(status_code=404, detail="knowledge base not found")
        try:
            assets = validate_assets_payload(req.assets)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        kb_store.save_user_edited_assets(kb_id, assets)
        return {"kb_id": kb_id, "saved": True, "assets": kb_store.load_assets(kb_id)}

    @app.patch("/projects/{project_id}/knowledge-bases")
    async def patch_project_knowledge_bases(project_id: str, req: PatchProjectKnowledgeRequest):
        if not project_exists(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        state = load_state(project_id)
        if _project_has_outline(state):
            raise HTTPException(
                status_code=400,
                detail="已生成大纲后不可修改知识库绑定，请新建项目",
            )
        state["selected_kb_ids"] = list(req.selected_kb_ids or [])
        state["kb_enabled"] = bool(state["selected_kb_ids"])
        save_state(project_id, state)
        return {
            "project_id": project_id,
            "selected_kb_ids": state["selected_kb_ids"],
            "kb_enabled": state["kb_enabled"],
        }

    return app


app = create_app()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 Novel Writer Agent 后端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--reload", action="store_true", help="启用自动重载（开发环境）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("未安装 uvicorn，请先执行：pip install uvicorn") from exc

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
