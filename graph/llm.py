"""
LLM 工厂：统一创建 planner / writer 模型实例。
"""
from langchain_openai import ChatOpenAI

from config import (
    PLANNER_API_KEY,
    PLANNER_BASE_URL,
    PLANNER_MODEL,
    WRITER_API_KEY,
    WRITER_BASE_URL,
    WRITER_MODEL,
)


def create_planner_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=PLANNER_MODEL,
        api_key=PLANNER_API_KEY,
        base_url=PLANNER_BASE_URL,
        streaming=False,
    )


def create_writer_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=WRITER_MODEL,
        api_key=WRITER_API_KEY,
        base_url=WRITER_BASE_URL,
        streaming=False,
    )
