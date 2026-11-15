"""Environment-only service configuration; credentials never enter public status."""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path

DEEPSEEK_OFFICIAL_MODEL = "deepseek-flash"
DEEPSEEK_OFFICIAL_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_OFFICIAL_LABEL = "DeepSeek V4.1 Flash 1017"
DEEPSEEK_OFFICIAL_MAX_TOKENS = 393216
DEEPSEEK_OFFICIAL_TIMEOUT = 3600
# Official model alias mapping verified 2026-10-17:
# https://api-docs.deepseek.com/quick_start/pricing/
# Output ceiling: https://api-docs.deepseek.com/api/create-chat-completion/


def is_official_deepseek(model: str, base_url: str) -> bool:
    if not isinstance(model, str) or not isinstance(base_url, str):
        return False
    try:
        return model == DEEPSEEK_OFFICIAL_MODEL and (urlsplit(base_url).hostname or "").lower() == "api.deepseek.com"
    except ValueError:
        return False


def default_model_limits(role: str, model: str, base_url: str) -> tuple[int, int]:
    if role in ("main", "selector") and is_official_deepseek(model, base_url):
        return DEEPSEEK_OFFICIAL_MAX_TOKENS, DEEPSEEK_OFFICIAL_TIMEOUT
    return (8192 if role in ("main", "selector") else 4096), 120


def model_display_name(model: str, base_url: str) -> str:
    if is_official_deepseek(model, base_url):
        return DEEPSEEK_OFFICIAL_LABEL
    return model


@dataclass(frozen=True)
class ModelConfig:
    role: str
    model: str
    base_url: str
    api_key: str = field(repr=False, default="")
    timeout: float = 120
    max_tokens: int = 4096
    trust_env: bool = True
    input_modalities: tuple[str, ...] = ("text",)
    enable_thinking: bool | None = None

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    models: dict[str, ModelConfig]
    metaso_key: str = field(repr=False, default="")
    metaso_base_url: str = "https://metaso.cn"
    service_key: str = field(repr=False, default="")
    max_assets: int = 8
    max_upload_bytes: int = 20 * 1024 * 1024
    max_workbook_bytes: int = 64 * 1024 * 1024
    max_policy_queries: int = 8
    concurrency: int = 3  # 影像分析同时执行数；外部图源并发另有上限
    qa_concurrency: int = 8  # 追问/资料检索通道名额（只调模型，不取图）
    recognition_concurrency: int = 3  # 同一任务里多张影像同时判读的数量
    rs_output_format: str = "json"
    web_search_base_url: str = "https://www.dmxapi.cn/v1"
    web_search_key: str = field(repr=False, default="")
    wechat_mcp_url: str = ""
    wechat_api_key: str = field(repr=False, default="")
    wechat_enabled: bool = False
    wechat_timeout: int = 180
    wechat_max_results: int = 5
    wechat_max_pages: int = 3
    wechat_read_articles: int = 2
    # 高德 Web 服务（追问时查询所在行政区与周边设施）；为空表示未启用。
    amap_key: str = field(repr=False, default="")
    amap_timeout: int = 8
    # Upper bounds for review calls; previously hardcoded as 90s / 45s.
    optical_review_timeout: float = 90
    cross_review_timeout: float = 45

    def __post_init__(self) -> None:
        if self.rs_output_format not in ("json", "xml"):
            raise ValueError("WUYU_RS_OUTPUT_FORMAT 必须为 json 或 xml")

    @classmethod
    def from_env(cls) -> Settings:
        main_model = os.getenv("WUYU_MAIN_MODEL", DEEPSEEK_OFFICIAL_MODEL)
        main_base_url = os.getenv("WUYU_MAIN_BASE_URL", os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_OFFICIAL_BASE_URL))
        main_tokens, main_timeout = default_model_limits("main", main_model, main_base_url)
        main = ModelConfig(
            "main", main_model, main_base_url,
            os.getenv("WUYU_MAIN_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")),
            timeout=float(os.getenv("WUYU_MAIN_TIMEOUT", str(main_timeout))),
            max_tokens=int(os.getenv("WUYU_MAIN_MAX_TOKENS", str(main_tokens))),
            trust_env=os.getenv("WUYU_MAIN_TRUST_ENV", "1").lower() not in ("0", "false"),
            input_modalities=tuple(value.strip() for value in os.getenv("WUYU_MAIN_INPUT", "text,image").split(",") if value.strip()),
            enable_thinking=None if os.getenv("WUYU_MAIN_ENABLE_THINKING") is None else os.getenv("WUYU_MAIN_ENABLE_THINKING", "").lower() not in ("0", "false"),
        )
        models = {"main": main}
        for role, prefix, default_model in (
            ("selector", "WUYU_SELECTOR", main.model),
            ("recognizer", "WUYU_RS", "wuyu-jianwei-rs"),
            ("reasoner", "WUYU_SHENSI", "wuyu-shensi-base"),
        ):
            inherit = role == "selector"
            model = os.getenv(f"{prefix}_MODEL", default_model)
            base_url = os.getenv(f"{prefix}_BASE_URL", main.base_url if inherit else "")
            tokens, timeout = default_model_limits(role, model, base_url)
            models[role] = ModelConfig(
                role, model, base_url,
                os.getenv(f"{prefix}_API_KEY", main.api_key if inherit else ""),
                timeout=float(os.getenv(f"{prefix}_TIMEOUT", str(timeout))),
                max_tokens=int(os.getenv(f"{prefix}_MAX_TOKENS", str(tokens))),
                trust_env=os.getenv(f"{prefix}_TRUST_ENV", "1").lower() not in ("0", "false"),
                input_modalities=tuple(value.strip() for value in os.getenv(f"{prefix}_INPUT", "text,image" if role == "recognizer" else "text").split(",") if value.strip()),
                enable_thinking=None if os.getenv(f"{prefix}_ENABLE_THINKING") is None else os.getenv(f"{prefix}_ENABLE_THINKING", "").lower() not in ("0", "false"),
            )
        # Reuse only an explicitly configured DMX channel, never arbitrary main
        # provider credentials for a different vendor.
        dmx_main = (urlsplit(main.base_url).hostname or "").lower() in {"dmxapi.cn", "www.dmxapi.cn", "www.dmxapi.com", "dmxapi.com"}
        models["science_analyst"] = ModelConfig(
            "science_analyst", os.getenv("WUYU_SCIENCE_MODEL", "gemini-3.8-flash"),
            os.getenv("WUYU_SCIENCE_BASE_URL", os.getenv("DMX_BASE_URL", main.base_url if dmx_main else "")),
            os.getenv("WUYU_SCIENCE_API_KEY", os.getenv("DMX_API_KEY", main.api_key if dmx_main else "")),
            timeout=float(os.getenv("WUYU_SCIENCE_TIMEOUT", "180")),
            max_tokens=int(os.getenv("WUYU_SCIENCE_MAX_TOKENS", "4096")),
            trust_env=os.getenv("WUYU_SCIENCE_TRUST_ENV", "1").lower() not in ("0", "false"),
            input_modalities=tuple(value.strip() for value in os.getenv("WUYU_SCIENCE_INPUT", "text,image").split(",") if value.strip()),
        )
        return cls(
            Path(os.getenv("WUYU_OUTPUT_DIR", "outputs/wuyu")).resolve(), models,
            os.getenv("METASO_API_KEY", ""), os.getenv("METASO_BASE_URL", "https://metaso.cn"),
            os.getenv("WUYU_SERVICE_API_KEY", ""),
            max_policy_queries=int(os.getenv("WUYU_MAX_POLICY_QUERIES", "8")),
            rs_output_format=os.getenv("WUYU_RS_OUTPUT_FORMAT", "json"),
            web_search_base_url=os.getenv("WUYU_WEB_SEARCH_BASE_URL", os.getenv("DMX_BASE_URL", main.base_url if dmx_main else "https://www.dmxapi.cn/v1")),
            web_search_key=os.getenv("WUYU_WEB_SEARCH_API_KEY", os.getenv("DMX_API_KEY", main.api_key if dmx_main else "")),
            wechat_mcp_url=os.getenv("WUYU_WECHAT_MCP_URL", ""),
            wechat_api_key=os.getenv("WUYU_WECHAT_API_KEY", ""),
            wechat_enabled=os.getenv("WUYU_WECHAT_ENABLED", "0").lower() in ("1", "true"),
            wechat_timeout=int(os.getenv("WUYU_WECHAT_TIMEOUT", "180")),
            wechat_max_results=int(os.getenv("WUYU_WECHAT_MAX_RESULTS", "5")),
            wechat_max_pages=int(os.getenv("WUYU_WECHAT_MAX_PAGES", "3")),
            wechat_read_articles=int(os.getenv("WUYU_WECHAT_READ_ARTICLES", "2")),
            optical_review_timeout=float(os.getenv("WUYU_OPTICAL_REVIEW_TIMEOUT", "90")),
            cross_review_timeout=float(os.getenv("WUYU_CROSS_REVIEW_TIMEOUT", "45")),
        )

    def public_status(self) -> dict:
        return {
            "models": {role: {"model": model.model, "display_name": model_display_name(model.model, model.base_url), "configured": model.ready, "input": list(model.input_modalities)} for role, model in self.models.items()},
            "metaso_configured": bool(self.metaso_key),
            "search": {"web": {"provider": "deepseek", "configured": bool(self.web_search_key)},
                       "academic": {"provider": "metaso", "configured": bool(self.metaso_key)},
                       "wechat": {"enabled": self.wechat_enabled, "configured": bool(self.wechat_mcp_url)}},
            "small_model": {"enabled": False, "status": "not_integrated"},
            "status": "development_pending_validation",
            "rs_output_format": self.rs_output_format,
        }
