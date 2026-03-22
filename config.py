"""
项目配置：从环境变量读取模型与 API，定义章字数、RAG 参数及本地存储路径。
"""
import os
from pathlib import Path

# 加载 .env（若存在）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----- 模型与 API（环境变量） -----
_default_model = os.getenv("MODEL") or "deepseek-r1:8b"
_default_base_url = os.getenv("BASE_URL") or "http://localhost:11434/v1"
_default_api_key = os.getenv("API_KEY") or "ollama"

# PLANNER：大纲/概要等规划用
PLANNER_MODEL = os.getenv("PLANNER_MODEL") or _default_model
PLANNER_BASE_URL = os.getenv("PLANNER_BASE_URL") or _default_base_url
PLANNER_API_KEY = os.getenv("PLANNER_API_KEY") or _default_api_key

# WRITER：写章/润色等正文用
WRITER_MODEL = os.getenv("WRITER_MODEL") or _default_model
WRITER_BASE_URL = os.getenv("WRITER_BASE_URL") or _default_base_url
WRITER_API_KEY = os.getenv("WRITER_API_KEY") or _default_api_key

# 章节插图：OpenAI Images API（失败时跳过插图，不回退占位图）
# 未设置 IMAGE_GEN_API_KEY 时回退到 OPENAI_API_KEY / PLANNER_API_KEY
IMAGE_GEN_API_KEY = os.getenv("IMAGE_GEN_API_KEY") or os.getenv("OPENAI_API_KEY") or PLANNER_API_KEY
_image_gen_base = os.getenv("IMAGE_GEN_BASE_URL", "").strip()
IMAGE_GEN_BASE_URL = _image_gen_base or None  # None 表示官方 api.openai.com
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "dall-e-3")
IMAGE_GEN_SIZE = os.getenv("IMAGE_GEN_SIZE", "1024x1024")  # 依模型支持而定
IMAGE_GEN_TIMEOUT_S = float(os.getenv("IMAGE_GEN_TIMEOUT_S", "120"))

# ----- 写作与 RAG 参数 -----
CHAPTER_WORD_TARGET = int(os.getenv("CHAPTER_WORD_TARGET", "3000"))
DEFAULT_TOTAL_CHAPTERS = int(os.getenv("DEFAULT_TOTAL_CHAPTERS", "100"))
PLOT_IDEAS_COUNT = int(os.getenv("PLOT_IDEAS_COUNT", "5"))
RAG_PREVIOUS_CHAPTERS = int(os.getenv("RAG_PREVIOUS_CHAPTERS", "5"))
# 人物图谱滑动窗口：写章时只使用最近 N 章内出现的关系
CHARACTER_GRAPH_RECENT_CHAPTERS = int(os.getenv("CHARACTER_GRAPH_RECENT_CHAPTERS", "5"))

# ----- 本地路径（落盘到项目内或可配置目录） -----
_project_root = Path(__file__).resolve().parent
PROJECTS_ROOT = Path(os.getenv("PROJECTS_ROOT", str(_project_root / "projects")))
CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", str(_project_root / "checkpoints")))
VECTOR_STORE_DIR = Path(os.getenv("VECTOR_STORE_DIR", str(_project_root / "vector_store")))

# 目录约定（文档用）：每个项目对应 PROJECTS_ROOT/{project_id}/，
# 其下可有 chapters/、images/、character_graph.json；
# 向量库使用 VECTOR_STORE_DIR/{project_id} 或统一库按 project_id 区分 collection。

# ----- 全局同人知识库（txt/md，大文件流式构建）-----
# 物理根目录：VECTOR_STORE_DIR/global_kb/（见 knowledge_base.store）
KB_CHUNK_TARGET_CHARS = int(os.getenv("KB_CHUNK_TARGET_CHARS", "900"))
KB_CHUNK_OVERLAP_CHARS = int(os.getenv("KB_CHUNK_OVERLAP_CHARS", "120"))
KB_INGEST_BATCH_CHUNKS = int(os.getenv("KB_INGEST_BATCH_CHUNKS", "400"))
KB_READ_BLOCK_BYTES = int(os.getenv("KB_READ_BLOCK_BYTES", "65536"))
KB_MAX_CHUNKS_PER_DOCUMENT = int(os.getenv("KB_MAX_CHUNKS_PER_DOCUMENT", "200000"))
KB_ASSET_LEAF_BATCH_CHARS = int(os.getenv("KB_ASSET_LEAF_BATCH_CHARS", "12000"))
KB_ASSET_MAX_LEAF_WINDOWS = int(os.getenv("KB_ASSET_MAX_LEAF_WINDOWS", "600"))
KB_RETRIEVE_CHROMA_K = int(os.getenv("KB_RETRIEVE_CHROMA_K", "12"))
KB_RETRIEVE_FTS_K = int(os.getenv("KB_RETRIEVE_FTS_K", "12"))
KB_RETRIEVE_FINAL_K = int(os.getenv("KB_RETRIEVE_FINAL_K", "8"))
KB_TOOL_LOOP_MAX_CALLS = int(os.getenv("KB_TOOL_LOOP_MAX_CALLS", "2"))
