"""Administrator source visibility/order policy, distinct from live capability evidence."""
from __future__ import annotations

from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field, field_validator


GROUP_LABELS = {"optical": "高分光学底图", "multispectral": "多光谱", "hyperspectral": "高光谱", "sar": "SAR"}


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    disabled_sources: list[str] = Field(default_factory=list, max_length=64)
    source_order: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("disabled_sources", "source_order")
    @classmethod
    def identifiers(cls, values):
        import re
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item) for item in values):
            raise ValueError("图源编号需来自系统目录，不支持自定义接口地址")
        if len(values) != len(set(values)):
            raise ValueError("图源编号不得重复")
        return values


def _source_id(row: dict) -> str | None:
    value = row.get("source", row.get("id"))
    return value if isinstance(value, str) else None


def validate_source_policy(value: dict | SourcePolicy, catalog: dict | None = None) -> dict:
    """Use catalogue validation when saving a policy from the administrator UI."""
    policy = value if isinstance(value, SourcePolicy) else SourcePolicy.model_validate(value)
    if catalog is not None:
        known = {_source_id(row) for group in catalog.get("groups", {}).values()
                 for row in group.get("sources", []) if isinstance(row, dict)}
        if set(policy.disabled_sources + policy.source_order) - known:
            raise ValueError("图源配置包含当前服务目录中不存在的编号")
    return policy.model_dump()


def source_choices(catalog: dict, policy: dict | SourcePolicy | None = None) -> list[dict]:
    """Only real published sources are selectable; enabling never grants upstream rights."""
    cfg = SourcePolicy.model_validate(policy or {}) if not isinstance(policy, SourcePolicy) else policy
    positions = {source: index for index, source in enumerate(cfg.source_order)}
    disabled = set(cfg.disabled_sources)
    choices = []
    for group, body in catalog.get("groups", {}).items():
        if group not in GROUP_LABELS or not isinstance(body, dict):
            continue
        for row in body.get("sources", []):
            if not isinstance(row, dict) or not (source := _source_id(row)):
                continue
            unavailable = row.get("configured") is False or row.get("query_enabled") is False or ("configured_nodes" in row and not row["configured_nodes"])
            reason = "administrator_disabled" if source in disabled else "upstream_not_configured" if unavailable else None
            choices.append({"source": source, "group": group, "group_label": GROUP_LABELS[group],
                "label": row.get("name") if isinstance(row.get("name"), str) else source,
                "enabled": source not in disabled, "available": not unavailable,
                "selectable": reason is None, "reason": reason,
                "provider_is_satellite": False if group == "optical" else None,
                "configured_nodes": list(row.get("configured_nodes", [])),
                "acquisition_time_known": bool(row.get("acquisition_time")),
                "supports_date": row.get("supports_date") is True,
                "supports_year": row.get("supports_year") is True,
                "product_years": list(row.get("product_years", [])),
                "edition_is_capture_date": row.get("edition_is_capture_date", False)})
    return sorted(choices, key=lambda choice: positions.get(choice["source"], len(positions)))


def apply_source_policy(catalog: dict, policy: dict | SourcePolicy | None = None) -> dict:
    """Filter auto-planning catalogues, preserving independent upstream status metadata."""
    cfg = SourcePolicy.model_validate(policy or {}) if not isinstance(policy, SourcePolicy) else policy
    result = deepcopy(catalog)
    disabled, order = set(cfg.disabled_sources), {source: i for i, source in enumerate(cfg.source_order)}
    for body in result.get("groups", {}).values():
        if not isinstance(body, dict):
            continue
        body["sources"] = sorted([row for row in body.get("sources", [])
            if isinstance(row, dict) and _source_id(row) not in disabled],
            key=lambda row: order.get(_source_id(row), len(order)))
    result["administrator_source_policy"] = cfg.model_dump()
    return result


def assert_sources_selectable(sources: list[str], catalog: dict, policy: dict | SourcePolicy | None = None) -> None:
    """Check explicit user/model choices at the execution boundary as well as the UI."""
    allowed = {row["source"] for row in source_choices(catalog, policy) if row["selectable"]}
    if not isinstance(sources, list) or any(not isinstance(s, str) or s not in allowed for s in sources):
        raise ValueError("所选图源未启用或当前服务未配置，请从可用选项选择")
