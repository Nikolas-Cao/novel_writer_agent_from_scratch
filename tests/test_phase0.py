"""
阶段 0 验收测试：config、state、outline_structure_to_string。
运行：py tests/test_phase0.py  或  py -m pytest tests/test_phase0.py -v
"""
import sys
from pathlib import Path

# 保证从项目根可导入 config、state
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def test_config_exists_and_exports():
    """config.py 存在且导出所需变量"""
    from config import (
        PROJECTS_ROOT,
        CHECKPOINT_DIR,
        VECTOR_STORE_DIR,
        CHAPTER_WORD_TARGET,
        DEFAULT_TOTAL_CHAPTERS,
        PLOT_IDEAS_COUNT,
        RAG_PREVIOUS_CHAPTERS,
        PLANNER_MODEL,
        PLANNER_BASE_URL,
        PLANNER_API_KEY,
        WRITER_MODEL,
        WRITER_BASE_URL,
        WRITER_API_KEY,
    )
    assert PROJECTS_ROOT is not None
    assert CHECKPOINT_DIR is not None
    assert VECTOR_STORE_DIR is not None
    assert isinstance(CHAPTER_WORD_TARGET, int)
    assert isinstance(PLOT_IDEAS_COUNT, int)
    assert isinstance(RAG_PREVIOUS_CHAPTERS, int)
    assert PLANNER_MODEL is not None
    assert WRITER_MODEL is not None


def test_state_import_and_types():
    """state.py 可被正常 import；定义 ChapterMeta、OutlineStructure、CharacterGraph、NovelProjectState"""
    from state import (
        ChapterMeta,
        OutlineStructure,
        OutlineVolume,
        OutlineChapterItem,
        CharacterGraph,
        CharacterNode,
        CharacterEdge,
        NovelProjectState,
    )
    # 类型存在即可，构造合法结构
    meta: ChapterMeta = {"title": "ch1", "index": 0, "path_or_content_ref": "chapters/000.md", "word_count": 100}
    assert meta["index"] == 0
    vol: OutlineStructure = {"volumes": [{"volume_title": "Vol1", "chapters": [{"title": "Ch1", "points": ["p1"]}]}]}
    assert len(vol["volumes"]) == 1
    graph: CharacterGraph = {"nodes": [], "edges": []}
    assert graph["nodes"] == []


def test_outline_structure_to_string():
    """outline_structure_to_string 能接收合法 OutlineStructure 并返回可读字符串（含卷、章、要点）"""
    from state import outline_structure_to_string, OutlineStructure

    structure: OutlineStructure = {
        "volumes": [
            {
                "volume_title": "Volume One",
                "chapters": [
                    {"title": "Chapter 1", "points": ["Point A", "Point B"], "conflict": "Main conflict"},
                ],
            },
        ]
    }
    s = outline_structure_to_string(structure)
    assert "Volume One" in s
    assert "Chapter 1" in s
    assert "Point A" in s
    assert "Point B" in s
    assert "Main conflict" in s or "conflict" in s.lower()


def test_outline_structure_to_string_empty():
    """空或缺少 volumes 时返回空字符串"""
    from state import outline_structure_to_string

    assert outline_structure_to_string({}) == ""
    assert outline_structure_to_string({"volumes": []}) == ""


def test_import_from_project_root():
    """在项目根执行 from config import PROJECTS_ROOT; from state import NovelProjectState 无报错"""
    from config import PROJECTS_ROOT
    from state import NovelProjectState

    assert PROJECTS_ROOT is not None
    # NovelProjectState 为 TypedDict，用作类型注解或 dict 形状约束
    state: NovelProjectState = {"instruction": "test", "project_id": "p1"}
    assert state["project_id"] == "p1"


def run_all():
    """无 pytest 时直接运行所有验收项"""
    test_config_exists_and_exports()
    test_state_import_and_types()
    test_outline_structure_to_string()
    test_outline_structure_to_string_empty()
    test_import_from_project_root()
    print("Phase 0 acceptance: all passed.")


if __name__ == "__main__":
    run_all()
