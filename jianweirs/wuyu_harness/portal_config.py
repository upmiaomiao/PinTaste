"""Encrypted administrator configuration. Secrets are write-only in the HTTP API."""
from __future__ import annotations

from dataclasses import asdict
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException

from .config import (DEEPSEEK_OFFICIAL_BASE_URL, DEEPSEEK_OFFICIAL_MODEL,
                     DEEPSEEK_OFFICIAL_MAX_TOKENS, DEEPSEEK_OFFICIAL_TIMEOUT,
                     ModelConfig, Settings, is_official_deepseek, model_display_name)

# 多用户容量与复核超时的默认值（管理员可在"任务策略"里调整）。
TASK_POLICY_CAPACITY_DEFAULTS = {
    "qa_concurrency": 8,          # 追问/资料检索同时处理数（只调模型）
    "max_pending_tasks": 400,     # 全系统排队+运行的任务上限
    "max_job_groups": 200,        # 同时挂起的任务协程上限
    "max_user_interactive": 3,    # 单用户同时进行的普通任务数
    "max_user_batch": 50,         # 单用户同时进行的批量/区域任务数
    "optical_review_timeout": 90, # 同图复核超时（秒）
    "cross_review_timeout": 45,   # 跨图复核超时（秒）
    "recognition_concurrency": 3, # 同一任务内多张影像同时判读的数量
}

ROLES = ("main", "selector", "recognizer", "reasoner", "science_analyst")
# 临时例外：无隅慎思接口暂无 HTTPS，运维方已接受密钥明文传输的风险（2026-10-30）。
# 只匹配这一个完整地址；接口支持 HTTPS 后删除。
PLAINTEXT_HTTP_EXCEPTIONS = frozenset({"http://103.139.212.177:8443/v1"})


def _url(value: str):
    if not value:
        return
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise HTTPException(422,"服务地址格式无效") from None
    if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422,"服务地址需为HTTP(S)，不得嵌入密码或查询密钥")
    if value.rstrip("/") in PLAINTEXT_HTTP_EXCEPTIONS:
        return
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_private
        except ValueError:
            local = parsed.hostname == "localhost"
        if not local:
            raise HTTPException(422,"公网服务地址需要HTTPS")


class PortalConfig:
    def __init__(self, db, settings: Settings):
        self.db, self.output_dir = db, settings.output_dir
        self.search_defaults = {
            "unified_data": {"enabled": False, "base_url": "", "api_key": "", "max_concurrency": 2, "timeout_seconds": 120},
            "appearance": {"default_font_size": 16},
            "geography": {"enabled": False, "api_key": "", "timeout_seconds": 8},
            "sources": {"disabled_sources": [], "source_order": []},
            "web_search": {"provider": "deepseek", "base_url": settings.web_search_base_url, "api_key": settings.web_search_key},
            "wechat": {"enabled": settings.wechat_enabled, "mcp_url": settings.wechat_mcp_url, "api_key": settings.wechat_api_key,
                       "timeout": settings.wechat_timeout, "max_results": settings.wechat_max_results,
                       "max_pages": settings.wechat_max_pages, "read_articles": settings.wechat_read_articles},
        }
        self.model_defaults = {role:asdict(settings.models.get(role, ModelConfig(role,
            "gemini-3.8-flash" if role == "science_analyst" else DEEPSEEK_OFFICIAL_MODEL if role in ("main","selector") else "wuyu-jianwei-rs" if role == "recognizer" else "wuyu-shensi-base",
            DEEPSEEK_OFFICIAL_BASE_URL if role in ("main","selector") else "",
            max_tokens=DEEPSEEK_OFFICIAL_MAX_TOKENS if role in ("main","selector") else 4096,
            timeout=DEEPSEEK_OFFICIAL_TIMEOUT if role in ("main","selector") else 120,
            input_modalities=("text","image") if role in ("main","recognizer","science_analyst") else ("text",)))) for role in ROLES}
        keyfile = db.folder / ".config.key"
        if not keyfile.exists():
            with db.connect() as conn:
                if conn.execute("SELECT 1 FROM config").fetchone():
                    raise RuntimeError("门户配置密钥文件缺失，请恢复本机备份")
            descriptor = os.open(keyfile,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(descriptor,"wb") as stream:
                stream.write(Fernet.generate_key())
        keyfile.chmod(0o600)
        self.cipher = Fernet(keyfile.read_bytes())
        with db.connect() as conn:
            existing = conn.execute("SELECT ciphertext FROM config WHERE id=1").fetchone()
        if not existing:
            config = {"models":self.model_defaults,
                      "metaso":{"base_url":settings.metaso_base_url,"api_key":settings.metaso_key},
                      "data":{"base_url":os.getenv("WUYU_DATA_BASE_URL","http://127.0.0.1:8765"),"api_key":os.getenv("WUYU_DATA_API_KEY",""),"mode":os.getenv("WUYU_DATA_MODE","auto")},
                      "task_policy":{"search_policy":True,"consult_shensi":False,"max_policy_queries":settings.max_policy_queries,"max_assets":settings.max_assets,"concurrency":settings.concurrency,"rs_output_format":settings.rs_output_format,**TASK_POLICY_CAPACITY_DEFAULTS}}
            config["task_policy"].update(selector_mode="auto",adaptive_review=True)
            config.update(self.search_defaults)
            self.save(config)
        else:
            config = self.read()
            raw = json.loads(self.cipher.decrypt(existing[0]))
            if any(role not in raw["models"] for role in ROLES) or any(key not in raw for key in self.search_defaults):
                self.save(config)

    def read(self):
        with self.db.connect() as conn:
            value = conn.execute("SELECT ciphertext FROM config WHERE id=1").fetchone()[0]
        result = json.loads(self.cipher.decrypt(value))
        for role in ROLES:
            result["models"].setdefault(role, dict(self.model_defaults[role]))
        result["task_policy"].setdefault("selector_mode","auto")
        # 多用户容量默认值：旧配置没有这些键时自动补上，不改变管理员已设的值。
        for key, default in TASK_POLICY_CAPACITY_DEFAULTS.items():
            result["task_policy"].setdefault(key, default)
        result["task_policy"].setdefault("adaptive_review",True)
        for group, defaults in self.search_defaults.items():
            result.setdefault(group, dict(defaults))
            for key, value in defaults.items():
                result[group].setdefault(key, value)
        return result

    def save(self, config):
        encoded = self.cipher.encrypt(json.dumps(config,ensure_ascii=False).encode())
        with self.db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO config VALUES(1,?)",(encoded,))

    def public(self):
        config = self.read()
        for model in config["models"].values():
            model["display_name"] = model_display_name(model["model"], model["base_url"])
        for section in [*config["models"].values(), config["metaso"], config["data"], config["web_search"], config["wechat"], config["geography"], config["unified_data"]]:
            section["api_key_configured"] = bool(section.pop("api_key", ""))
        return config

    def migrate_official_deepseek(self, api_key: str, *, roles=("main", "selector")):
        """Explicit operator migration; never run automatically at startup.

        Supply an official DeepSeek key separately: a previous provider's key
        must never be silently forwarded to api.deepseek.com. Only the named
        base roles are changed; specialist endpoints and other policies stay.
        Restart the portal after an offline migration to apply runtime settings.
        """
        if not isinstance(api_key, str) or not api_key.strip():
            raise HTTPException(422, "迁移需要显式提供DeepSeek官方API密钥")
        if not isinstance(roles, (tuple, list)) or not roles or any(role not in ("main", "selector") for role in roles):
            raise HTTPException(422, "迁移仅支持主模型与选星模型角色")
        return self.update({"models": {role: {
            "model": DEEPSEEK_OFFICIAL_MODEL, "base_url": DEEPSEEK_OFFICIAL_BASE_URL,
            "api_key": api_key.strip(), "enable_thinking": None,
            "max_tokens": DEEPSEEK_OFFICIAL_MAX_TOKENS, "timeout": DEEPSEEK_OFFICIAL_TIMEOUT,
            "input_modalities": ["text", "image"] if role == "main" else ["text"],
        } for role in dict.fromkeys(roles)}})

    def update(self, patch):
        if not isinstance(patch,dict) or not set(patch) <= {"models","metaso","data","task_policy","web_search","wechat","geography","sources","unified_data","appearance"}:
            raise HTTPException(422,"配置分组无效")
        config = self.read()
        if 'appearance' in patch:
            value = patch['appearance']
            if not isinstance(value, dict) or set(value) != {'default_font_size'} or type(value['default_font_size']) is not int or not 12 <= value['default_font_size'] <= 24:
                raise HTTPException(422, '默认字号需为12至24')
            config['appearance'].update(value)
        if 'unified_data' in patch:
            value = patch['unified_data']
            if not isinstance(value, dict) or set(value) - {'enabled','base_url','api_key','max_concurrency','timeout_seconds'}:
                raise HTTPException(422, '新影像服务字段无效')
            for key, item in value.items():
                if key == 'enabled' and type(item) is not bool:
                    raise HTTPException(422, 'enabled需为布尔值')
                if key == 'max_concurrency' and (type(item) is not int or not 1 <= item <= 2):
                    raise HTTPException(422, '新影像服务最多2个并发')
                if key == 'timeout_seconds' and (type(item) is not int or not 30 <= item <= 120):
                    raise HTTPException(422, '取图超时需为30至120秒')
                if key in ('base_url','api_key') and (not isinstance(item,str) or len(item)>8192 or any(ord(c)<32 for c in item)):
                    raise HTTPException(422, '新影像服务配置无效')
                if key == 'base_url':
                    _url(item)
                    if urlsplit(item).path not in ('','/'):
                        raise HTTPException(422, '新影像服务需为来源地址')
                config['unified_data'][key] = item
        if 'sources' in patch:
            from .schemas import ResearchSource
            from typing import get_args
            values = patch['sources']
            if not isinstance(values, dict) or set(values) - {'disabled_sources', 'source_order'}:
                raise HTTPException(422, '图源配置字段无效')
            for k, v in values.items():
                if not isinstance(v, list) or len(v) > 40 or any(not isinstance(s, str) or s not in get_args(ResearchSource) for s in v):
                    raise HTTPException(422, '图源列表包含未知来源')
                config['sources'][k] = list(dict.fromkeys(v))
        sections = []
        if "models" in patch:
            if not isinstance(patch["models"],dict) or not set(patch["models"]) <= set(ROLES):
                raise HTTPException(422,"模型角色无效")
            for role, values in patch["models"].items():
                sections.append((config["models"][role],values,{"model","base_url","api_key","timeout","max_tokens","trust_env","input_modalities","enable_thinking"}))
        for key, allowed in (("metaso",{"base_url","api_key"}), ("data",{"base_url","api_key","mode"}),
                             ("geography",{"enabled","api_key","timeout_seconds"}),
                             ("web_search",{"base_url","api_key"}),
                             ("wechat",{"enabled","mcp_url","api_key","timeout","max_results","max_pages","read_articles"}),
                             ("task_policy",{"search_policy","consult_shensi","max_policy_queries","max_assets","concurrency","rs_output_format","selector_mode","adaptive_review","qa_concurrency","max_pending_tasks","max_job_groups","max_user_interactive","max_user_batch","optical_review_timeout","cross_review_timeout","recognition_concurrency"})):
            if key in patch:
                sections.append((config[key],patch[key],allowed))
        for current, values, allowed in sections:
            if not isinstance(values,dict) or not set(values) <= allowed:
                raise HTTPException(422,"配置字段无效")
            effective = {**current, **values}
            official = is_official_deepseek(effective.get("model"), effective.get("base_url"))
            for key,value in values.items():
                if key in ("model","base_url","api_key","mcp_url"):
                    if not isinstance(value,str) or len(value)>8192 or any(ord(char)<32 for char in value):
                        raise HTTPException(422,"配置文本无效")
                    if key == "base_url":
                        _url(value)
                    if key == "mcp_url" and value:
                        parsed = urlsplit(value)
                        try:
                            port = parsed.port
                        except ValueError:
                            raise HTTPException(422,"MCP端口无效") from None
                        if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or "\\" in value or port == 0:
                            raise HTTPException(422,"MCP地址需为完整HTTP(S)地址，不得嵌入凭据")
                elif key in ("timeout","timeout_seconds","max_tokens","max_policy_queries","max_assets","concurrency","max_results","max_pages","read_articles","qa_concurrency","max_pending_tasks","max_job_groups","max_user_interactive","max_user_batch","optical_review_timeout","cross_review_timeout","recognition_concurrency"):
                    bounds = {"timeout_seconds":(2,20),"timeout":(5,DEEPSEEK_OFFICIAL_TIMEOUT if official else 180 if "mcp_url" in current else 600),"max_tokens":(1,DEEPSEEK_OFFICIAL_MAX_TOKENS) if official else (128,32768),"max_policy_queries":(1,8),"max_assets":(1,8),"concurrency":(1,8),"qa_concurrency":(1,32),"max_pending_tasks":(10,2000),"max_job_groups":(4,1000),"max_user_interactive":(1,10),"max_user_batch":(1,200),"optical_review_timeout":(10,300),"cross_review_timeout":(10,300),"recognition_concurrency":(1,8),"max_results":(1,50),"max_pages":(1,3),"read_articles":(0,5)}[key]
                    if type(value) is not int or not bounds[0]<=value<=bounds[1]:
                        raise HTTPException(422,"配置数值超出范围")
                elif key in ("trust_env","search_policy","consult_shensi","adaptive_review","enabled"):
                    if type(value) is not bool:
                        raise HTTPException(422,"配置开关必须为布尔值")
                elif key == "enable_thinking":
                    if value is not None and type(value) is not bool:
                        raise HTTPException(422,"思考模式必须为布尔值或null")
                elif key == "input_modalities":
                    if not isinstance(value,list) or not value or any(not isinstance(item,str) for item in value) or not set(value)<={"text","image"}:
                        raise HTTPException(422,"输入模态只支持text/image")
                elif key == "mode" and value not in ("auto","worker","gateway"):
                    raise HTTPException(422,"数据服务模式无效")
                elif key == "rs_output_format" and value not in ("json","xml"):
                    raise HTTPException(422,"专业输出格式无效")
                elif key == "selector_mode" and value not in ("auto","model"):
                    raise HTTPException(422,"选星策略必须为auto或model")
                current[key] = value
            if "model" in current:
                for key, bounds in (("max_tokens", (1,DEEPSEEK_OFFICIAL_MAX_TOKENS) if official else (128,32768)),
                                    ("timeout", (5,DEEPSEEK_OFFICIAL_TIMEOUT if official else 600))):
                    value=current.get(key)
                    if not isinstance(value,(int,float)) or isinstance(value,bool) or not bounds[0]<=value<=bounds[1]:
                        raise HTTPException(422,"当前服务的输出或超时预算超出范围，请同步调整")
        for key in ("data","metaso"):
            address = config[key]["base_url"]
            parsed = urlsplit(address)
            if not address or parsed.path not in ("","/") or (key=="metaso" and parsed.scheme!="https"):
                raise HTTPException(422,"数据与搜索接口需填写来源地址，不含接口路径；秘塔需要HTTPS")
            config[key]["base_url"] = address.rstrip("/")
        address = config["web_search"]["base_url"]
        parsed = urlsplit(address)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/", "/v1", "/v1/"):
            raise HTTPException(422,"DeepSeek搜索地址需为HTTPS来源或以/v1结尾")
        config["web_search"]["base_url"] = address.rstrip("/")
        if config["wechat"]["enabled"] and not config["wechat"]["mcp_url"]:
            raise HTTPException(422,"启用微信公众号需要填写MCP地址")
        self.save(config)
        return self.public()

    def settings(self):
        config = self.read()
        models = {role:ModelConfig(**{**config["models"][role],"role":role,
                    "input_modalities":tuple(config["models"][role]["input_modalities"])}) for role in ROLES}
        policy = config["task_policy"]
        return Settings(output_dir=self.output_dir,models=models,metaso_key=config["metaso"]["api_key"],
                        metaso_base_url=config["metaso"]["base_url"],max_assets=policy["max_assets"],
                        max_policy_queries=policy["max_policy_queries"],concurrency=policy["concurrency"],qa_concurrency=policy["qa_concurrency"],recognition_concurrency=policy["recognition_concurrency"],
                        optical_review_timeout=policy["optical_review_timeout"],cross_review_timeout=policy["cross_review_timeout"],
                        rs_output_format=policy["rs_output_format"],
                        web_search_base_url=config["web_search"]["base_url"],web_search_key=config["web_search"]["api_key"],
                        wechat_mcp_url=config["wechat"]["mcp_url"],wechat_api_key=config["wechat"]["api_key"],
                        wechat_enabled=config["wechat"]["enabled"],wechat_timeout=config["wechat"]["timeout"],
                        wechat_max_results=config["wechat"]["max_results"],wechat_max_pages=config["wechat"]["max_pages"],
                        wechat_read_articles=config["wechat"]["read_articles"],
                        amap_key=config["geography"]["api_key"] if config["geography"]["enabled"] else "",
                        amap_timeout=config["geography"]["timeout_seconds"])
