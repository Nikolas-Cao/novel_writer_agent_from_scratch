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
