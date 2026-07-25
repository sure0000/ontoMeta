"""字段级溯源的只读派生属性 Mixin。

把存为 JSON 字符串的 overridden_fields / conflict_json 解析为结构化视图，
供 Pydantic ``from_attributes`` 自动映射到读模型（pinned_fields / conflicts /
has_conflict），无需在每个序列化点重复解析。
"""

from __future__ import annotations

import json


class ProvenanceMixin:
    # 由具体模型以 Column 提供：overridden_fields, conflict_json, origin, upstream_removed
    overridden_fields: str | None
    conflict_json: str | None

    @property
    def pinned_fields(self) -> list[str]:
        if not self.overridden_fields:
            return []
        try:
            data = json.loads(self.overridden_fields)
            return data if isinstance(data, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    @property
    def conflicts(self) -> dict:
        if not self.conflict_json:
            return {}
        try:
            data = json.loads(self.conflict_json)
            return data if isinstance(data, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)
