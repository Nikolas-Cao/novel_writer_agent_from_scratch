"""
人物图谱存储：
1) 兼容旧版：每项目一个 character_graph.json
2) 新版：每章一个快照 character_graph/{chapter_index:03d}.json
"""
import copy
import json
from pathlib import Path
from typing import List, Optional

from config import PROJECTS_ROOT
from state import CharacterEdge, CharacterGraph, CharacterNode


def _empty_graph() -> CharacterGraph:
    return {"nodes": [], "edges": []}


class CharacterGraphStore:
    """人物图谱本地文件存储。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _graph_path(self, project_id: str) -> Path:
        project_dir = self.root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "character_graph.json"

    def _graph_snapshots_dir(self, project_id: str) -> Path:
        project_dir = self.root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        snapshots_dir = project_dir / "character_graph"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        return snapshots_dir

    def _snapshot_path(self, project_id: str, chapter_index: int) -> Path:
        return self._graph_snapshots_dir(project_id) / f"{int(chapter_index):03d}.json"

    def load(self, project_id: str) -> CharacterGraph:
        path = self._graph_path(project_id)
        if not path.exists():
            return _empty_graph()
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "nodes": list(data.get("nodes", [])),
            "edges": list(data.get("edges", [])),
        }

    def save(self, project_id: str, graph: CharacterGraph) -> None:
        path = self._graph_path(project_id)
        payload = {
            "nodes": list(graph.get("nodes", [])),
            "edges": list(graph.get("edges", [])),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_snapshot(self, project_id: str, chapter_index: int, graph: CharacterGraph) -> None:
        path = self._snapshot_path(project_id, chapter_index)
        payload = {
            "nodes": list(graph.get("nodes", [])),
            "edges": list(graph.get("edges", [])),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 兼容旧逻辑：始终把最新结果回写到聚合文件。
        self.save(project_id, payload)

    def load_snapshot(self, project_id: str, chapter_index: int) -> CharacterGraph:
        path = self._snapshot_path(project_id, chapter_index)
        if not path.exists():
            raise FileNotFoundError(f"Character graph snapshot not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "nodes": list(data.get("nodes", [])),
            "edges": list(data.get("edges", [])),
        }

    def load_for_chapter(self, project_id: str, chapter_index: int) -> CharacterGraph:
        """
        读取 <= chapter_index 的最近一个快照。
        若不存在快照，回退到旧版聚合图谱文件。
        """
        target_idx = int(chapter_index)
        if target_idx >= 0:
            snapshots_dir = self._graph_snapshots_dir(project_id)
            for path in sorted(snapshots_dir.glob("*.json"), reverse=True):
                stem = path.stem
                if stem.isdigit() and int(stem) <= target_idx:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    return {
                        "nodes": list(data.get("nodes", [])),
                        "edges": list(data.get("edges", [])),
                    }
        return self.load(project_id)

    def delete_snapshots_from(self, project_id: str, start_index: int) -> None:
        snapshots_dir = self._graph_snapshots_dir(project_id)
        begin = int(start_index)
        for path in snapshots_dir.glob("*.json"):
            stem = path.stem
            if not stem.isdigit():
                continue
            if int(stem) >= begin and path.exists():
                path.unlink()

    def refresh_legacy_latest(self, project_id: str) -> None:
        """
        把 legacy character_graph.json 对齐为当前最后一个快照；
        若无快照则保留原有聚合文件（或空图）。
        """
        snapshots_dir = self._graph_snapshots_dir(project_id)
        snapshots = sorted(snapshots_dir.glob("*.json"))
        if not snapshots:
            return
        latest_path = snapshots[-1]
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        self.save(project_id, data)

    def merge(
        self,
        project_id: str,
        new_nodes: List[CharacterNode],
        new_edges: List[CharacterEdge],
        chapter_index: Optional[int] = None,
        base_graph: Optional[CharacterGraph] = None,
    ) -> CharacterGraph:
        graph = copy.deepcopy(base_graph) if base_graph is not None else self.load(project_id)

        nodes_by_id = {
            str(node.get("id")): dict(node)
            for node in graph.get("nodes", [])
            if node.get("id") is not None
        }
        for node in new_nodes:
            node_id = node.get("id")
            if not node_id:
                continue
            nodes_by_id[str(node_id)] = {**nodes_by_id.get(str(node_id), {}), **dict(node)}

        edge_key = lambda e: (
            str(e.get("from_id", "")),
            str(e.get("to_id", "")),
            str(e.get("relation", "")),
        )
        edges_by_key = {edge_key(edge): dict(edge) for edge in graph.get("edges", [])}
        for edge in new_edges:
            key = edge_key(edge)
            if not key[0] or not key[1]:
                continue
            edges_by_key[key] = {**edges_by_key.get(key, {}), **dict(edge)}

        merged: CharacterGraph = {
            "nodes": list(nodes_by_id.values()),
            "edges": list(edges_by_key.values()),
        }
        if chapter_index is None:
            self.save(project_id, merged)
        else:
            self.save_snapshot(project_id, int(chapter_index), merged)
        return merged
