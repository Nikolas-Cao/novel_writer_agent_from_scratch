"""
知识库分层摘要 JSON 的白名单校验。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


TOP_LEVEL_FIELDS = {
    "global_summary",
    "characters",
    "timeline",
    "world_rules",
    "core_facts",
    "leaf_summaries",
    "section_summaries",
}

LIST_ITEM_SCHEMAS: Dict[str, Tuple[Tuple[str, type], ...]] = {
    "characters": (
        ("name", str),
        ("aliases", list),
        ("role", str),
        ("relations", str),
    ),
    "timeline": (
        ("order", (int, float)),
        ("event", str),
        ("actors", str),
    ),
    "world_rules": (
        ("rule", str),
        ("note", str),
    ),
    "core_facts": (
        ("fact", str),
        ("importance", str),
    ),
    "leaf_summaries": (
        ("id", str),
        ("summary", str),
    ),
    "section_summaries": (
        ("id", str),
        ("summary", str),
    ),
}


def _describe_keys(keys: Iterable[str]) -> str:
    return ", ".join(sorted(set(keys)))


def _assert_exact_keys(value: Dict[str, Any], required_keys: set[str], path: str) -> None:
    actual = set(value.keys())
    missing = sorted(required_keys - actual)
    extras = sorted(actual - required_keys)
    if missing:
        raise ValueError(f"{path} 缺少字段：{', '.join(missing)}")
    if extras:
        raise ValueError(f"{path} 包含未定义字段：{', '.join(extras)}")


def _require_type(value: Any, expected: Any, path: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{path} 类型错误，期望 {expected}，实际 {type(value).__name__}")


def validate_assets_payload(payload: Any) -> Dict[str, Any]:
    # 思路：
    # 1) 顶层与子项都采用“精确白名单”而不是“仅检查关键字段”，保证人工编辑内容可预测；
    # 2) 对每层同时检查“缺失字段”和“未定义字段”，满足前端提示“必须字段/非法字段”；
    # 3) 返回新对象而非原引用，避免后续写盘时被调用方意外原地修改。
    #
    # 输入前提：
    # payload 为用户提交的 JSON 反序列化对象，预期是 dict。
    #
    # 边界行为：
    # - 非 dict、字段缺失、字段多余、元素类型不符：抛 ValueError，由 API 转为 400。
    # - 数组允许为空；但数组元素一旦存在，必须严格符合 schema。
    if not isinstance(payload, dict):
        raise ValueError("assets 必须是 JSON 对象")

    _assert_exact_keys(payload, TOP_LEVEL_FIELDS, "assets")
    if not isinstance(payload.get("global_summary"), str):
        raise ValueError("assets.global_summary 必须是字符串")

    normalized: Dict[str, Any] = {"global_summary": payload["global_summary"]}
    for field_name, schema in LIST_ITEM_SCHEMAS.items():
        items = payload.get(field_name)
        if not isinstance(items, list):
            raise ValueError(f"assets.{field_name} 必须是数组")
        normalized_items: List[Dict[str, Any]] = []
        expected_keys = {k for k, _ in schema}
        for idx, item in enumerate(items):
            path = f"assets.{field_name}[{idx}]"
            if not isinstance(item, dict):
                raise ValueError(f"{path} 必须是对象")
            _assert_exact_keys(item, expected_keys, path)
            out_item: Dict[str, Any] = {}
            for key, expected_type in schema:
                value = item.get(key)
                _require_type(value, expected_type, f"{path}.{key}")
                if field_name == "characters" and key == "aliases":
                    if not all(isinstance(alias, str) for alias in value):
                        raise ValueError(f"{path}.aliases 必须是字符串数组")
                    out_item[key] = list(value)
                else:
                    out_item[key] = value
            normalized_items.append(out_item)
        normalized[field_name] = normalized_items
    return normalized

