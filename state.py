"""
状态与数据结构：NovelProjectState、OutlineStructure、ChapterMeta、CharacterGraph 等。
与 PLAN.md 中状态设计一致，供 LangGraph 工作流使用。
"""
from typing import TypedDict, List, Optional, Any, Dict


# ----- 大纲结构 -----

class OutlineChapterItem(TypedDict, total=False):
    """单章：标题、要点、可选冲突/伏笔"""
    title: str
    points: List[str]          # 3～5 句本章要点
    conflict: Optional[str]    # 关键冲突/伏笔
    beat: Optional[str]        # 本章节拍一句话
    depends_on: List[int]      # 承接的前文章节索引
    carry_forward: List[str]   # 延续线索
    new_threads: List[str]     # 新引入线索
    resolved_threads: List[str]  # 本章收束线索


class OutlineVolume(TypedDict):
    """卷：卷标题 + 章列表"""
    volume_title: str
    chapters: List[OutlineChapterItem]


class OutlineStructure(TypedDict):
    """结构化大纲：卷 → 章 → 要点。可序列化为字符串供 LLM 使用。"""
    volumes: List[OutlineVolume]


def outline_structure_to_string(structure: OutlineStructure) -> str:
    """
    将 OutlineStructure 转为可读字符串，含卷、章、要点，供 LLM 使用。
    """
    if not structure or "volumes" not in structure:
        return ""
    lines: List[str] = []
    for vol in structure["volumes"]:
        title = vol.get("volume_title") or "未命名卷"
        lines.append(f"## {title}")
        for i, ch in enumerate(vol.get("chapters") or [], 1):
            ch_title = ch.get("title") or f"第{i}章"
            lines.append(f"### {ch_title}")
            for pt in ch.get("points") or []:
                lines.append(f"- {pt}")
            if ch.get("conflict"):
                lines.append(f"  （冲突/伏笔：{ch['conflict']}）")
        lines.append("")
    return "\n".join(lines).strip()


# ----- 章节元数据 -----

class ChapterMeta(TypedDict, total=False):
    """章节元数据；正文存 chapter_store（.md 文件）"""
    chapter_id: str
    title: str
    summary: str
    path_or_content_ref: str   # 章节 .md 文件相对路径或 DB key
    word_count: int
    index: int
    images_refs: Optional[List[str]]  # 本节内插图相对路径或 URL 列表（可选）


# ----- 人物图谱 -----

class CharacterNode(TypedDict, total=False):
    """人物节点"""
    id: str
    name: str
    aliases: Optional[List[str]]
    description: Optional[str]
    first_chapter: Optional[int]


class CharacterEdge(TypedDict, total=False):
    """人物关系边"""
    from_id: str
    to_id: str
    relation: str
    note: Optional[str]
    first_chapter: Optional[int]  # 关系首次出现的章节索引，供写章时滑动窗口过滤


class CharacterGraph(TypedDict):
    """人物图谱：节点与边，存于 project_id/character_graph.json"""
    nodes: List[CharacterNode]
    edges: List[CharacterEdge]


# ----- LangGraph 状态 -----

class NovelProjectState(TypedDict, total=False):
    """长篇创作 Agent 的 LangGraph 状态"""
    instruction: str
    plot_ideas: List[str]
    selected_plot_summary: str
    outline: str
    outline_structure: Optional[OutlineStructure]
    chapters: List[ChapterMeta]
    current_chapter_index: int
    current_chapter_draft: str
    current_chapter_final: str
    character_graph: Optional[CharacterGraph]
    user_feedback: str
    last_rewrite_draft: str
    total_chapters: int
    outline_generated_until: int  # 已生成到的全局章索引（含）
    outline_window_size: int      # 扩窗章节数
    outline_initial_chapters: int  # 初始大纲章节数
    outline_mode: str              # short|long
    outline_seed_done: bool        # 首窗是否完成
    outline_extended_indices: List[int]  # 本轮扩窗影响的章节索引
    last_written_chapter_index: int  # 已写正文的最大章节索引（用于禁回修）
    chapter_word_target: int
    project_id: str
    chapter_output_format: str   # 如 "markdown"
    enable_chapter_illustrations: bool
    retrieved_summaries: List[dict]
    retrieved_outline_chunk: str
    character_context_summary: str
    last_chapter_summary: str
    update_outline_on_feedback: bool
    # 插图（单图优先）：新字段 + 兼容旧版 illustration_points 列表
    illustration_point: Optional[Dict[str, Any]]  # { position_describe, anchor_text, alt }
    illustration_prompt: Optional[str]  # 文生图详细描述
    illustration_asset: Optional[Dict[str, Any]]  # { image_path, source, alt, anchor_text, ... }
    illustration_points: List[dict]
    illustration_assets: List[dict]
    token_usage: Optional[Dict[str, Dict[str, int]]]  # model -> { input_tokens, output_tokens }
    created_at: Optional[int]  # Unix 秒，创建时间
    # 全局同人知识库（可选）
    selected_kb_ids: List[str]
    kb_enabled: bool
    canon_overrides: List[Dict[str, Any]]  # 二创对原著的覆盖说明
    consistency_report: Optional[List[Dict[str, Any]]]
    kb_assets_text: str
    kb_evidence_text: str
    kb_confidence: Optional[float]
