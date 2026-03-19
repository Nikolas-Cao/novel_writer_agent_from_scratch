"""
阶段 2 节点：根据 instruction 生成剧情概要候选。
"""
from typing import Any, Dict, List, Optional

from config import PLOT_IDEAS_COUNT
from graph.llm import create_planner_llm
from graph.utils import (
    extract_json_object,
    get_message_text,
    invoke_and_parse_with_retry,
    normalize_plot_ideas,
)
from state import NovelProjectState


async def generate_plot_ideas_node(
    state: NovelProjectState,
    llm: Optional[Any] = None,
) -> Dict[str, List[str]]:
    planner = llm or create_planner_llm()
    instruction = state.get("instruction", "").strip()
    if not instruction:
        return {"plot_ideas": []}

    prompt = (
        "你是一名资深小说策划。请根据用户创作意图生成多个剧情概要候选。\n"
        f"要求生成 {PLOT_IDEAS_COUNT} 条，每条 100-200 字，方向明显不同。\n"
        "严格输出 JSON 对象，格式如下：\n"
        '{"plot_ideas":["概要1","概要2"]}\n\n'
        f"用户创作意图：{instruction}"
    )
    ideas: List[str] = []
    text = ""
    try:
        obj = await invoke_and_parse_with_retry(
            planner, prompt, extract_json_object, max_retries=3
        )
        ideas = normalize_plot_ideas(obj)
    except Exception:
        resp = await planner.ainvoke(prompt)
        text = get_message_text(resp)
        ideas = normalize_plot_ideas(text)

    if not ideas and text.strip():
        ideas = [text.strip()]

    return {"plot_ideas": ideas}


def _run_test_with_user_instruction():
    """
    针对 generate_plot_ideas_node 的测试：instruction 从用户输入获取，
    调用节点后校验返回结构（含 plot_ideas 列表）。
    直接运行本文件时执行：python -m graph.nodes.generate_plot_ideas
    """
    import asyncio
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    instruction = input("请输入创作意图（instruction）: ").strip()
    if not instruction:
        print("未输入 instruction，将测试空 instruction 返回空列表。")
    state = {"instruction": instruction}

    async def _run():
        out = await generate_plot_ideas_node(state)
        assert "plot_ideas" in out
        assert isinstance(out["plot_ideas"], list)
        if instruction:
            assert len(out["plot_ideas"]) > 0, "有 instruction 时期望至少一条剧情概要"
        else:
            assert out["plot_ideas"] == []
        return out

    result = asyncio.run(_run())
    print("通过：plot_ideas 数量 =", len(result["plot_ideas"]))
    for i, idea in enumerate(result["plot_ideas"], 1):
        print(f"  {i}. {idea}")
    return result


if __name__ == "__main__":
    _run_test_with_user_instruction()
