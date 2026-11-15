"""图源的共同事实：只在这里定义一次，其余模块读取。

之前同样的集合与年份范围分别写在 optical_time、temporal_optical、unified_imagery、vicinity 里。
这里不改变任何取值，只是把它们收到一处，避免日后只改一处而漏掉其余。
"""
from __future__ import annotations

# 年度/固定版次产品：图源名 -> 对应年份
ANNUAL_SOURCE_YEARS = {"jilin2024": 2024, "siwei2025": 2025}

# 世纪空间年度影像覆盖的年份（每年一版）
CENTURY_YEARS = tuple(range(2021, 2026))
CENTURY_FIRST_YEAR, CENTURY_LAST_YEAR = CENTURY_YEARS[0], CENTURY_YEARS[-1]

# 带"历史时相"含义的图源：世纪空间、谷歌历史、吉林一号 2024、四维 2025
HISTORICAL_OPTICAL = frozenset({"century", "history246", *ANNUAL_SOURCE_YEARS})

# 只能按指定年份/日期取、不能当作"当前影像"的图源
EXCLUSIVE_SOURCES = frozenset({"gaofen2026", "century", "jilin2024", "siwei2025", "history246"})

# 图源声称的最高缩放级别必须是 1–22 的整数；其余一律视为未声明
MIN_ZOOM, MAX_ZOOM = 1, 22


def valid_zoom(value):
    return value if type(value) is int and MIN_ZOOM <= value <= MAX_ZOOM else None


def century_year_supported(year) -> bool:
    return isinstance(year, int) and CENTURY_FIRST_YEAR <= year <= CENTURY_LAST_YEAR
