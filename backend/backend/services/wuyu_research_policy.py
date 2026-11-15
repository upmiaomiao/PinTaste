"""Administrator-owned bounds, separate from model capabilities and user page quota."""

DEFAULT_RESEARCH_POLICY = {
    'web_enabled': True,
    'web_search_limit': 2, 'web_fetch_limit': 3,
    'web_daily_search_limit': 200, 'web_daily_fetch_limit': 300,
    'deep_search_limit': 6, 'deep_fetch_limit': 8,
    'reading_workers': 8, 'reading_max_calls': 128,
    'reading_max_points': 1500, 'reading_input_tokens': 800000,
}
_BOUNDS = {
    'web_search_limit': (0, 10), 'web_fetch_limit': (0, 20),
    'web_daily_search_limit': (0, 1000), 'web_daily_fetch_limit': (0, 2000),
    'deep_search_limit': (0, 6), 'deep_fetch_limit': (0, 8),
    'reading_workers': (1, 16), 'reading_max_calls': (1, 512),
    'reading_max_points': (1, 100000), 'reading_input_tokens': (2048, 800000),
}


def validate_research_policy(changes, base=None):
    if not isinstance(changes, dict) or set(changes) - DEFAULT_RESEARCH_POLICY.keys():
        raise ValueError('包含不支持的材料阅读或联网配置。')
    result = dict(DEFAULT_RESEARCH_POLICY)
    if base is not None:
        result.update(validate_research_policy(base))
    for key, value in changes.items():
        if key == 'web_enabled':
            if type(value) is not bool:
                raise ValueError('默认联网设置必须为布尔值。')
        else:
            low, high = _BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError('材料阅读或联网预算超出管理员可配置范围。')
        result[key] = value
    return result
