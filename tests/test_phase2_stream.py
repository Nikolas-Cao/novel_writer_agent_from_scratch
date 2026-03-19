"""
阶段 2/4 单测：验证 refine/rewrite 的 LLM streaming 分支工作正常。
运行：py -m pytest tests/test_phase2_stream.py -v
"""

import asyncio
import shutil
import sys
import uuid
from pathlib import Path


_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_root() -> Path:
    base = _root / "tests_tmp" / f"phase2_stream_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return base


class _RespChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeWriterLLMStream:
    def __init__(self, full_text: str, chunk_size: int = 8) -> None:
        self._full_text = full_text
        self._chunk_size = chunk_size

    async def astream(self, _prompt: str):
        # 断点式吐出内容，模拟 token 增量。
        i = 0
        while i < len(self._full_text):
            part = self._full_text[i : i + self._chunk_size]
            i += self._chunk_size
            yield _RespChunk(part)


def test_refine_chapter_stream_emits_and_saves():
    from graph.nodes.refine_chapter import refine_chapter_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root)
    project_id = "p-stream-refine"
    idx = 0

    expected = "# 第一章\n\n这里是流式输出内容。后续继续。"
    emitted: list[tuple[str, str]] = []

    async def emit(stage: str, message: str) -> None:
        emitted.append((stage, message))

    state = {
        "project_id": project_id,
        "current_chapter_index": idx,
        "current_chapter_draft": "# 第一章\n\ndraft",
        "chapters": [{"index": idx, "title": "第一章"}],
    }

    out = asyncio.run(
        refine_chapter_node(
            state,
            llm=FakeWriterLLMStream(expected, chunk_size=6),
            chapter_store=store,
            stream_llm_output=True,
            emit_token_progress=emit,
        )
    )

    saved = store.load(project_id, idx)
    assert saved == expected
    assert out["current_chapter_final"] == expected

    assert len(emitted) >= 2
    assert all(stage == "refine_chapter_stream" for stage, _ in emitted)
    joined = "".join(msg for _, msg in emitted)
    assert joined == expected

    shutil.rmtree(root, ignore_errors=True)


def test_rewrite_feedback_stream_emits_and_saves():
    from graph.nodes.rewrite_feedback import rewrite_with_feedback_node
    from storage import ChapterStore

    root = _tmp_root()
    store = ChapterStore(root=root)
    project_id = "p-stream-rewrite"
    idx = 1

    expected = "# 第一章\n\n重写后结尾更悬疑。黑影停在门外。"
    emitted: list[tuple[str, str]] = []

    async def emit(stage: str, message: str) -> None:
        emitted.append((stage, message))

    state = {
        "project_id": project_id,
        "current_chapter_index": idx,
        "user_feedback": "让结尾更悬疑",
        "current_chapter_final": "# 第一章\n\n旧文本",
        "chapters": [{"index": idx, "title": "第一章"}],
        "chapter_word_target": 300,
    }

    out = asyncio.run(
        rewrite_with_feedback_node(
            state,
            llm=FakeWriterLLMStream(expected, chunk_size=7),
            chapter_store=store,
            stream_llm_output=True,
            emit_token_progress=emit,
        )
    )

    saved = store.load(project_id, idx)
    assert saved == expected
    assert out["current_chapter_final"] == expected

    assert len(emitted) >= 2
    assert all(stage == "rewrite_feedback_stream" for stage, _ in emitted)
    joined = "".join(msg for _, msg in emitted)
    assert joined == expected

    shutil.rmtree(root, ignore_errors=True)

