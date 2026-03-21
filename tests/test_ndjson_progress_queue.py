"""
回归：ndjson_with_progress 在大量 token 级 progress 下不得死锁。

有界 Queue + 慢消费端会与 emit 形成环形等待；无界队列由内存兜底（单次请求可接受）。
"""
import asyncio
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from server import ndjson_with_progress


def test_ndjson_many_progress_events_complete():
    n = 300

    async def run(emit):
        for i in range(n):
            await emit("refine_chapter_stream", str(i))
        return {"ok": True}

    async def _consume():
        chunks: list[bytes] = []
        async for b in ndjson_with_progress(run):
            chunks.append(b)
        return chunks

    chunks = asyncio.run(_consume())
    text = b"".join(chunks).decode("utf-8")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert len(lines) == n + 1
    assert all(json.loads(ln)["type"] == "progress" for ln in lines[:-1])
    last = json.loads(lines[-1])
    assert last["type"] == "result"
    assert last["body"] == {"ok": True}


def test_ndjson_worker_exception_yields_error_line():
    async def run(_emit):
        raise ValueError("parse failed")

    async def _consume():
        chunks: list[bytes] = []
        async for b in ndjson_with_progress(run):
            chunks.append(b)
        return chunks

    chunks = asyncio.run(_consume())
    text = b"".join(chunks).decode("utf-8").strip()
    o = json.loads(text)
    assert o["type"] == "error"
    assert "parse failed" in o["detail"]
