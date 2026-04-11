"""
知识库 API 轻量回归：不跑 LLM、不跑完整摄取流水线。
"""
import json
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _tmp_dirs():
    base = _root / "tests_tmp" / f"kb_api_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    return {
        "projects": base / "projects",
        "vector": base / "vector",
        "states": base / "states",
    }


def _sample_assets_payload():
    return {
        "global_summary": "这是人工修订后的全书概要。",
        "characters": [
            {
                "name": "陈某",
                "aliases": ["阿陈"],
                "role": "主角",
                "relations": "与李某是搭档",
            }
        ],
        "timeline": [{"order": 1, "event": "第一幕事件", "actors": "陈某, 李某"}],
        "world_rules": [{"rule": "夜间禁火", "note": "违反会触发巡逻"}],
        "core_facts": [{"fact": "主角害怕高处", "importance": "high"}],
        "leaf_summaries": [{"id": "leaf-1", "char_approx_end": 8407, "summary": "叶子摘要"}],
        "section_summaries": [{"id": "section-1", "summary": "段落摘要"}],
    }


def test_knowledge_bases_crud_and_project_binding():
    from server import create_app

    d = _tmp_dirs()
    app = create_app(
        planner_llm=None,
        writer_llm=None,
        projects_root=d["projects"],
        vector_root=d["vector"],
        checkpoint_root=d["states"],
    )
    client = TestClient(app)

    r = client.post("/knowledge-bases", json={"name": "原著卷一"})
    assert r.status_code == 200
    kb_id = r.json()["kb_id"]
    assert kb_id

    r = client.get("/knowledge-bases")
    assert r.status_code == 200
    ids = [b["kb_id"] for b in r.json().get("knowledge_bases", [])]
    assert kb_id in ids

    r = client.get(f"/knowledge-bases/{kb_id}")
    assert r.status_code == 200
    assert r.json().get("kb_id") == kb_id

    r = client.post("/projects", json={"instruction": "同人测试", "selected_kb_ids": [kb_id]})
    assert r.status_code == 200
    pid = r.json()["project_id"]

    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body.get("selected_kb_ids") == [kb_id]
    assert body.get("kb_enabled") is True

    r = client.patch(f"/projects/{pid}/knowledge-bases", json={"selected_kb_ids": []})
    assert r.status_code == 200
    assert r.json().get("selected_kb_ids") == []
    assert r.json().get("kb_enabled") is False


def test_patch_knowledge_bases_rejected_after_outline():
    from server import create_app
    from memory.local_checkpointer import LocalFileCheckpointer

    d = _tmp_dirs()
    app = create_app(
        planner_llm=None,
        writer_llm=None,
        projects_root=d["projects"],
        vector_root=d["vector"],
        checkpoint_root=d["states"],
    )
    client = TestClient(app)

    r = client.post("/projects", json={"instruction": "x"})
    assert r.status_code == 200
    pid = r.json()["project_id"]

    cp = LocalFileCheckpointer(root=d["states"])
    st = cp.load_state(pid) or {}
    st["outline_structure"] = {
        "volumes": [{"volume_title": "V1", "chapters": [{"title": "C1", "points": ["p"]}]}]
    }
    cp.save_state(pid, st)

    r = client.patch(f"/projects/{pid}/knowledge-bases", json={"selected_kb_ids": ["kb-x"]})
    assert r.status_code == 400
    assert "大纲" in (r.json().get("detail") or "")


def test_save_knowledge_assets_summary_and_retriever_precedence():
    from knowledge_base.store import KnowledgeBaseStore
    from rag.global_kb_retriever import GlobalKbRetriever
    from server import create_app

    d = _tmp_dirs()
    app = create_app(
        planner_llm=None,
        writer_llm=None,
        projects_root=d["projects"],
        vector_root=d["vector"],
        checkpoint_root=d["states"],
    )
    client = TestClient(app)

    r = client.post("/knowledge-bases", json={"name": "手工摘要测试"})
    assert r.status_code == 200
    kb_id = r.json()["kb_id"]

    kb_dir = d["vector"] / "global_kb" / kb_id / "assets"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "layers_d-auto.json").write_text(
        json.dumps(
            {
                "global_summary": "自动摘要",
                "characters": [{"name": "自动角色", "aliases": [], "role": "自动", "relations": ""}],
                "timeline": [],
                "world_rules": [],
                "core_facts": [],
                "leaf_summaries": [],
                "section_summaries": [],
                "status": "ready",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = _sample_assets_payload()
    r = client.put(f"/knowledge-bases/{kb_id}/assets/summary", json={"assets": payload})
    assert r.status_code == 200
    assert r.json().get("saved") is True

    r = client.get(f"/knowledge-bases/{kb_id}/assets/summary")
    assert r.status_code == 200
    assets = r.json()["assets"]
    assert assets["global_summary"] == payload["global_summary"]
    assert assets["characters"][0]["name"] == "陈某"
    assert assets["leaf_summaries"][0]["char_approx_end"] == 8407
    assert assets.get("by_doc") == {}

    retriever = GlobalKbRetriever(d["vector"], KnowledgeBaseStore(d["vector"]))
    text = retriever.assets_layer_text([kb_id])
    assert "这是人工修订后的全书概要。" in text
    assert "自动摘要" not in text


def test_save_knowledge_assets_summary_rejects_invalid_schema():
    from server import create_app

    d = _tmp_dirs()
    app = create_app(
        planner_llm=None,
        writer_llm=None,
        projects_root=d["projects"],
        vector_root=d["vector"],
        checkpoint_root=d["states"],
    )
    client = TestClient(app)
    r = client.post("/knowledge-bases", json={"name": "校验失败测试"})
    kb_id = r.json()["kb_id"]

    bad_payload = _sample_assets_payload()
    bad_payload["unknown_field"] = "not-allowed"
    bad_payload["characters"] = [{"name": "缺失字段"}]

    r = client.put(f"/knowledge-bases/{kb_id}/assets/summary", json={"assets": bad_payload})
    assert r.status_code == 400
    detail = r.json().get("detail") or ""
    assert ("未定义字段" in detail) or ("缺少字段" in detail)
