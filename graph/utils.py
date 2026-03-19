"""
图节点通用工具：JSON 提取、文本清洗与 LLM 输出解析重试。
"""
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def invoke_and_parse_with_retry(
    llm: Any,
    prompt: str,
    parse_fn: Callable[[str], T],
    get_text_fn: Optional[Callable[[Any], str]] = None,
    max_retries: int = 3,
) -> T:
    """
    调用 LLM 并解析输出；解析失败时重试，最多 max_retries 次。
    - llm: 有 ainvoke(prompt) 的模型实例
    - prompt: 输入提示
    - parse_fn: 接收模型输出文本，返回解析结果；解析失败时应抛出异常
    - get_text_fn: 从 ainvoke 返回的响应中提取文本，默认 get_message_text
    """
    get_text = get_text_fn or get_message_text
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = await llm.ainvoke(prompt)
            text = get_text(resp)
            return parse_fn(text)
        except Exception as e:
            last_error = e
            logger.warning(
                "invoke_and_parse_with_retry: attempt %s/%s failed: %s",
                attempt + 1,
                max_retries,
                e,
            )
            if attempt == max_retries - 1:
                raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("invoke_and_parse_with_retry: unexpected")


def get_message_text(resp: Any) -> str:
    """兼容 LangChain 响应对象与测试假对象。"""
    if isinstance(resp, str):
        return resp
    if hasattr(resp, "content"):
        return str(resp.content)
    return str(resp)


def _extract_balanced_brace_block(text: str, start: int) -> str:
    """从 start 位置起提取配对的 {...} 子串。"""
    if start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    i = start
    in_string = False
    escape = False
    quote = None
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\" and in_string:
            escape = True
            i += 1
            continue
        if in_string:
            if c == quote:
                in_string = False
            i += 1
            continue
        if c in ('"', "'"):
            in_string = True
            quote = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return ""


def _repair_json(s: str) -> str:
    """尝试修复常见 LLM JSON 错误：尾部逗号等。"""
    # 移除数组/对象内末尾的逗号（, ] 或 , }）
    s = re.sub(r",\s*\]", "]", s)
    s = re.sub(r",\s*}", "}", s)
    return s


def extract_json_object(text: str) -> Dict[str, Any]:
    """从文本中提取第一个 JSON 对象；对 LLM 常见非法输出做容错。"""
    text = text.strip()
    # 若有 markdown 代码块，只在块内从第一个 { 开始做平衡括号提取
    if "```" in text:
        parts = re.split(r"```(?:json)?\s*", text, maxsplit=1)
        if len(parts) > 1:
            block = parts[1].split("```")[0].strip()
            start = block.find("{")
            if start != -1:
                raw = _extract_balanced_brace_block(block, start)
                if raw:
                    for attempt in (raw, _repair_json(raw)):
                        try:
                            data = json.loads(attempt)
                            if isinstance(data, dict):
                                return data
                        except json.JSONDecodeError:
                            continue
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output.")
    raw = _extract_balanced_brace_block(text, start)
    if not raw:
        raise ValueError("No complete JSON object found in model output.")
    for attempt in (raw, _repair_json(raw)):
        try:
            data = json.loads(attempt)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise ValueError("Model output is not valid JSON object.")


def normalize_plot_ideas(raw: Any) -> List[str]:
    """
    将模型输出规范化为 List[str]。
    支持:
    - {"plot_ideas":[...]}
    - {"ideas":[...]}
    - 纯文本按行拆分
    """
    if isinstance(raw, dict):
        ideas = raw.get("plot_ideas") or raw.get("ideas") or []
        return [str(i).strip() for i in ideas if str(i).strip()]

    text = str(raw).strip()
    lines = [line.strip("- ").strip() for line in text.splitlines() if line.strip()]
    return [line for line in lines if line]


def sanitize_chapter_markdown(raw_text: Any) -> str:
    """
    清理模型可能输出的章节无关内容，仅保留章节正文。
    - 去掉 markdown 代码围栏
    - 去掉末尾“核心亮点/说明/总结/点评”等附加段落
    """
    text = str(raw_text or "").strip()
    if not text:
        return ""

    fenced_match = re.fullmatch(r"```(?:markdown|md)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fenced_match:
        text = fenced_match.group(1).strip()

    # 去掉尾部常见“附加说明”段落（保留正文）
    extra_section = re.search(
        r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:核心亮点|亮点|写作思路|创作说明|改写说明|章节总结|本章总结|总结|点评|注释|附注|后记)\s*[：:]",
        text,
    )
    if extra_section:
        text = text[: extra_section.start()].rstrip()

    # 二次清理残留围栏
    text = re.sub(r"(?im)^\s*```(?:markdown|md)?\s*$", "", text)
    text = re.sub(r"(?im)^\s*```\s*$", "", text)
    return text.strip()
