"""
LLM 工厂：统一创建 planner / writer 模型实例；TokenTrackingLLM 用于累计 token 用量。
"""
import logging
import os
from typing import Any, AsyncIterator, Dict, MutableMapping, Tuple

from langchain_openai import ChatOpenAI

from config import (
    PLANNER_API_KEY,
    PLANNER_BASE_URL,
    PLANNER_MODEL,
    WRITER_API_KEY,
    WRITER_BASE_URL,
    WRITER_MODEL,
)

logger = logging.getLogger(__name__)


def _extract_token_usage_from_message(resp: Any) -> Tuple[int, int]:
    """从 LangChain AIMessage 等响应中提取 (input_tokens, output_tokens)。"""
    inp = out = 0
    um = getattr(resp, "usage_metadata", None)
    if isinstance(um, dict):
        inp = int(um.get("input_tokens") or um.get("prompt_tokens") or 0)
        out = int(um.get("output_tokens") or um.get("completion_tokens") or 0)
    if inp or out:
        return inp, out
    rm = getattr(resp, "response_metadata", None)
    if isinstance(rm, dict):
        tu = rm.get("token_usage")
        if isinstance(tu, dict):
            inp = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
            out = int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
    return inp, out


def accumulate_usage_into_state(
    state: MutableMapping[str, Any],
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """
    将 token 用量合并到 state['token_usage'][model]（与 TokenTrackingLLM 结构一致）。
    用于非 Chat 类 API（如 OpenAI Images）手写累计。
    """
    if not (model or "").strip():
        return
    tu = state.setdefault("token_usage", {})
    bucket = tu.setdefault(str(model), {"input_tokens": 0, "output_tokens": 0})
    bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + max(0, int(input_tokens))
    bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + max(0, int(output_tokens))


class TokenTrackingLLM:
    """
    包装 ChatOpenAI：拦截 invoke/ainvoke，把用量累计到 usage_by_model[model_name]。
    其它属性透传给内层，便于 node 里照常使用。
    """

    def __init__(self, inner: ChatOpenAI, usage_by_model: Dict[str, Dict[str, int]]):
        self._inner = inner
        self._usage_by_model = usage_by_model
        self._model_name = (
            getattr(inner, "model_name", None)
            or getattr(inner, "model", None)
            or "unknown"
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _accumulate(self, resp: Any) -> None:
        inp, out = _extract_token_usage_from_message(resp)
        if inp == 0 and out == 0:
            return
        model = str(self._model_name)
        bucket = self._usage_by_model.setdefault(
            model, {"input_tokens": 0, "output_tokens": 0}
        )
        bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + inp
        bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + out

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        resp = self._inner.invoke(*args, **kwargs)
        self._accumulate(resp)
        return resp

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        resp = await self._inner.ainvoke(*args, **kwargs)
        self._accumulate(resp)
        return resp

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """
        兼容 LangChain 的 streaming 调用。

        注意：不同供应商对 streaming chunk 的 usage 语义可能不同：

        1) 累计语义（running total）：每个 chunk 携带“到目前为止”的累计 token。
           此时直接逐 chunk 累加会导致重复计费。

        2) 增量语义（per-chunk delta）：每个 chunk 携带“本 chunk 新增”的 token。
           此时需要对每个 chunk 的 usage 做求和。

        本实现做启发式判断：在流结束后，根据 chunk 间 token 序列的形态推断语义，
        然后用对应方式得到“本次调用的真实 token 用量”，并只写入一次。
        """

        class _FieldUsageStats:
            """统计一个字段（input 或 output）在流式 chunk 中的 usage 形态。"""

            __slots__ = (
                "sum",
                "last",
                "count",
                "non_decreasing_transitions",
                "negative_transitions",
                "repeat_transitions",
                "prev",
            )

            def __init__(self) -> None:
                self.sum: int = 0
                self.last: int = 0
                self.count: int = 0
                self.non_decreasing_transitions: int = 0
                self.negative_transitions: int = 0
                self.repeat_transitions: int = 0
                self.prev: Any = None

            def add(self, v: int) -> None:
                if v <= 0:
                    return
                if self.prev is None:
                    self.prev = v
                    self.sum = int(v)
                    self.last = int(v)
                    self.count = 1
                    return

                diff = int(v) - int(self.prev)
                self.sum += int(v)
                self.last = int(v)
                self.count += 1

                if diff >= 0:
                    self.non_decreasing_transitions += 1
                else:
                    self.negative_transitions += 1
                if int(v) == int(self.prev):
                    self.repeat_transitions += 1

                self.prev = v

            def resolve_total(self) -> int:
                """
                启发式推断：是累计语义还是增量语义。

                返回“本次调用对该字段的 token 用量”：
                - 累计语义：取最后一次值（等价于取 max/last）
                - 增量语义：对所有 chunk 的值求和
                """
                if self.count == 0:
                    return 0
                if self.count == 1:
                    return int(self.last)

                transitions = max(self.count - 1, 1)
                non_decreasing_ratio = self.non_decreasing_transitions / transitions
                neg_ratio = self.negative_transitions / transitions
                repeat_ratio = self.repeat_transitions / transitions

                # 明显出现“下降”的场景更像 per-chunk delta（增量）而不是 running total（累计）。
                if neg_ratio >= 0.2:
                    return int(self.sum)

                # 只要整体呈现“非递减 + 存在重复/或累计值增长太夸张”，就更像 running total。
                # repeat_ratio 用于捕捉“多个 chunk 报同一个累计值”的情况。
                sum_to_last = self.sum / max(self.last, 1)
                if non_decreasing_ratio >= 0.85 and (repeat_ratio >= 0.15 or sum_to_last >= 10):
                    return int(self.last)
                return int(self.sum)

        inp_stats = _FieldUsageStats()
        out_stats = _FieldUsageStats()
        saw_usage = False

        debug_usage = str(os.getenv("DEBUG_LLM_USAGE", "")).strip().lower() in {"1", "true", "yes", "y", "on"}
        async for chunk in self._inner.astream(*args, **kwargs):
            inp, out = _extract_token_usage_from_message(chunk)
            if inp or out:
                saw_usage = True
                inp_stats.add(inp)
                out_stats.add(out)
            yield chunk

        if saw_usage and (inp_stats.count or out_stats.count):
            model = str(self._model_name)
            resolved_inp = int(inp_stats.resolve_total())
            resolved_out = int(out_stats.resolve_total())
            bucket = self._usage_by_model.setdefault(
                model, {"input_tokens": 0, "output_tokens": 0}
            )
            bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + resolved_inp
            bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + resolved_out

            if debug_usage:
                logger.debug(
                    "usage resolve model=%s input_count=%s output_count=%s input_sum=%s input_last=%s output_sum=%s output_last=%s resolved_inp=%s resolved_out=%s",
                    model,
                    inp_stats.count,
                    out_stats.count,
                    inp_stats.sum,
                    inp_stats.last,
                    out_stats.sum,
                    out_stats.last,
                    resolved_inp,
                    resolved_out,
                )


def create_planner_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=PLANNER_MODEL,
        api_key=PLANNER_API_KEY,
        base_url=PLANNER_BASE_URL,
        streaming=False,
    )


def create_writer_llm(streaming: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        model=WRITER_MODEL,
        api_key=WRITER_API_KEY,
        base_url=WRITER_BASE_URL,
        streaming=streaming,
    )
