"""Normalize legacy and structured action suggestions for all report formats."""
from __future__ import annotations

from collections.abc import Mapping
import re


def _plain(value):
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<think(?:ing)?\b[^>]*>[\s\S]*?(?:</think(?:ing)?\s*>|$)", "", value, flags=re.I)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()
    if text.lower() in {"null", "none", "undefined", "[]", "{}"} or re.fullmatch(r"[\s\-—–•\d.、)）]+", text or " "):
        return ""
    return text[:2000]


def normalize_recommendations(value, *, limit=100):
    """Only an actual action can create a list item; reasons alone cannot."""
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values:
        if isinstance(item, Mapping):
            action = next((_plain(item.get(key)) for key in ("action", "recommendation", "suggestion", "text") if _plain(item.get(key))), "")
            reason = _plain(item.get("reason"))
            text = action + ("；" + reason if reason and reason != action else "") if action else ""
        else:
            text = _plain(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result
