import os
import time
import asyncio
from dotenv import load_dotenv
from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI

class StroyState(TypedDict) :
    instruction: str        # 原始需求
    outline: str            # 大纲
    draft: str              # 初稿
    final_stroy: str        # 最终成品

load_dotenv()
default_model = os.getenv("MODEL") or "deepseek-r1:8b"
default_base_url = os.getenv("BASE_URL") or "http://localhost:11434/v1"
default_api_key = os.getenv("API_KEY") or"ollama"

print(f"default model : {default_model}")

planner_model = os.getenv("PLANNER_MODEL") or default_model
planner_base_url = os.getenv("PLANNER_BASE_URL") or default_base_url
planner_key = os.getenv("PLANNER_API_KEY") or default_api_key
planner_llm = ChatOpenAI(model=planner_model, api_key=planner_key, base_url=planner_base_url, streaming=True)

writer_model = os.getenv("WRITER_MODEL") or default_model
writer_base_url = os.getenv("WRITER_BASE_URL") or default_base_url
writer_key = os.getenv("WRITER_API_KEY") or default_api_key
writer_llm = ChatOpenAI(model=writer_model, api_key=writer_key, base_url=writer_base_url, streaming=True)

async def plan_node(state: StroyState) :
    print("=== 大纲生成中 ===")
    full_content = ""
    prompt = f"根据主题『{state['instruction']}』，创作一个1000字短篇小说的详细大纲，包含起承转合和核心冲突。"
    async for chunk in planner_llm.astream(prompt) :
        content = chunk.content
        print(content, end="", flush=True)
        full_content += content
    return {"outline" : full_content}

async def write_node(state: StroyState) :
    print("\n\n=== 初稿生成中 ===")
    full_content = ""
    prompt = f"根据以下大纲，撰写一部1000字左右的小说正文。要求描写细腻，多用感官描写，严格限制字数最多不能超过1500字：\n\n{state['outline']}"
    async for chunk in writer_llm.astream(prompt):
        content = chunk.content
        print(content, end="", flush=True)
        full_content += content
    
    return {"draft" : full_content}

async def refine_node(state: StroyState) :
    print("\n\n=== 终稿生成中 ===")
    full_content = ""
    prompt = f"你是金牌编辑，请对以下小说进行润色，修复语病，增强文学性，严格保持在1000字左右 ：\n\n{state['draft']}"
    async for trunk in writer_llm.astream(prompt) :
        content = trunk.content
        print(content, end="", flush=True)
        full_content += content
    return {"final_stroy" : full_content}

workflow = StateGraph(StroyState)
workflow.add_node("planner", plan_node)
workflow.add_node("writer", write_node)
workflow.add_node("refiner", refine_node)

workflow.set_entry_point("planner")
workflow.add_edge("planner", "writer")
workflow.add_edge("writer", "refiner")
workflow.add_edge("refiner", END)

app = workflow.compile()
instruction = input("请输入你的想法：")

async def main():
    input = {"instruction": instruction}  #"一个在废弃空间站独自醒来的仿生人发现自己拥有了做梦的能力"
    state = None
    async for event in app.astream(input, stream_mode="values"):
        state = event

    final_stroy = state["final_stroy"]
    with open(f"novel_{time.time()}.txt", "w", encoding="utf-8") as f:
        f.write(final_stroy)

if __name__ == "__main__":
    asyncio.run(main())