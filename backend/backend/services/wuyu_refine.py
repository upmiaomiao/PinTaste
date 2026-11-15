"""The secondary 无隅/Qwen wording pass is stopped by current product policy.

Legacy environment switches and credentials deliberately cannot re-enable it.
DeepSeek receives complete extracted evidence in the single planner request;
this compatibility entry point never imports an SDK or changes planner text.
"""
from .wuyu_errors import check_cancel


def refiner_settings(config):
    return None


def refine_pages(config, pages, brief="", cancel_check=None):
    check_cancel(cancel_check)
    return {"status": "disabled", "pages_changed": 0}
