import asyncio
import json
from pathlib import Path

from graph.llm import TokenTrackingLLM
from storage.llm_invoke_store import LLMInvokeStore


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Chunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _InnerLLM:
    async def ainvoke(self, prompt: str):
        return _Msg("非流式输出")

    async def astream(self, prompt: str):
        for part in ["流式", "输", "出"]:
            yield _Chunk(part)


def _record_files(root: Path, project_id: str):
    return sorted((root / project_id / "llm_invoke_results").glob("*.json"))


def test_debug_false_should_not_persist(tmp_path: Path):
    store = LLMInvokeStore(root=tmp_path)
    usage = {}
    llm = TokenTrackingLLM(
        _InnerLLM(),
        usage,
        debug_enabled=False,
        invoke_store=store,
        debug_context={"project_id": "p-debug-off"},
    )

    async def write_chapter_node():
        await llm.ainvoke("prompt")

    asyncio.run(write_chapter_node())
    assert _record_files(tmp_path, "p-debug-off") == []


def test_debug_true_non_stream_persist_full_record(tmp_path: Path):
    store = LLMInvokeStore(root=tmp_path)
    usage = {}
    llm = TokenTrackingLLM(
        _InnerLLM(),
        usage,
        debug_enabled=True,
        invoke_store=store,
        debug_context={"project_id": "p-debug-on"},
    )

    async def write_chapter_node():
        await llm.ainvoke("完整提示词")

    asyncio.run(write_chapter_node())
    files = _record_files(tmp_path, "p-debug-on")
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["status"] == "success"
    assert data["is_stream"] is False
    assert data["prompt"] == "完整提示词"
    assert data["response_text"] == "非流式输出"
    assert data["purpose"] == "write_chapter"
    assert data["purpose_group"] == "chapter_generation"
    assert isinstance(data["duration_ms"], int)
    assert "_success_write_chapter_" in files[0].name


def test_debug_true_stream_should_aggregate_once(tmp_path: Path):
    store = LLMInvokeStore(root=tmp_path)
    usage = {}
    llm = TokenTrackingLLM(
        _InnerLLM(),
        usage,
        debug_enabled=True,
        invoke_store=store,
        debug_context={"project_id": "p-stream"},
    )

    async def refine_chapter_node():
        chunks = []
        async for chunk in llm.astream("流式提示词"):
            chunks.append(chunk.content)
        return "".join(chunks)

    text = asyncio.run(refine_chapter_node())
    assert text == "流式输出"
    files = _record_files(tmp_path, "p-stream")
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["is_stream"] is True
    assert data["response_text"] == "流式输出"
    assert data["status"] == "success"
    assert data["purpose"] == "refine_chapter"
    assert "_success_refine_chapter_" in files[0].name
