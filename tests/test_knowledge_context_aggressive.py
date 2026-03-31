import asyncio

from graph.knowledge_context import build_kb_context_for_outline, build_kb_context_for_writing


class _FakeRetriever:
    def __init__(self) -> None:
        self.hybrid_called = False

    def assets_layer_text(self, kb_ids):
        return f"assets-for:{','.join(kb_ids)}"

    def hybrid_retrieve(self, kb_ids, query, doc_id=None):
        self.hybrid_called = True
        return [{"text": "should-not-be-used"}]


def test_aggressive_outline_context_only_assets():
    retriever = _FakeRetriever()
    out = asyncio.run(
        build_kb_context_for_outline(
            kb_ids=["kb-a"],
            plot_summary="剧情概要",
            retriever=retriever,
        )
    )
    assert "知识库分层摘要" in out
    assert "原文证据摘录" not in out
    assert retriever.hybrid_called is False


def test_aggressive_writing_context_only_assets():
    retriever = _FakeRetriever()
    out = asyncio.run(
        build_kb_context_for_writing(
            kb_ids=["kb-a"],
            title="第1章",
            points=["要点1"],
            retriever=retriever,
        )
    )
    assert out["kb_assets_text"].startswith("assets-for:")
    assert out["kb_evidence_text"] == ""
    assert out["kb_evidence"] == []
    assert out["kb_confidence"] == 1.0
    assert retriever.hybrid_called is False
