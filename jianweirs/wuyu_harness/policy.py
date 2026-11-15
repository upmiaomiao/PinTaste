"""Bounded Metaso policy retrieval; search results remain untrusted evidence.

API request fields follow https://metaso.cn/search-api/playground. A completed
search means the planned requests returned sources, never that the law is
current, the publisher is authentic, or the legal research is exhaustive.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from .schemas import TASK_LABELS, TaskRequest

_QUERY_DEADLINE_SECONDS = 30


def plan_queries(task: TaskRequest) -> list[dict]:
    """Produce a reviewable coverage plan even without a configured API key."""
    region = " ".join(task.jurisdiction.split())[:200]
    objective = " ".join(task.objective.split())[:160]
    kinds = "、".join(TASK_LABELS[kind] for kind in task.task_types)
    period = "/".join(str(value) if value else "未指定" for value in (task.date_start, task.date_end))
    plans = [
        ("national_basis", "国家法律依据", "site:npc.gov.cn 固体废物 倾倒 生态环境法典 固体废物污染环境防治法"),
        ("version_validity", "施行修订与新旧衔接", f"site:gov.cn 生态环境法典 固体废物 施行 废止 修改 时间效力 {period}"),
        ("waste_identification", "固废分类与鉴别", "site:mee.gov.cn 固体废物 鉴别标准 国家危险废物名录 现行"),
        ("sensitive_receptors", "环境敏感目标与技术标准", "site:mee.gov.cn 固体废物 堆场 遥感 调查 环境风险 水源地 生态保护红线 技术标准"),
        ("local_rules", "辖区地方规定", f"{region} 固体废物 非法倾倒 地方性法规 管理规定 site:gov.cn"),
        ("enforcement", "核查与执法程序", "site:mee.gov.cn 清废行动 遥感排查 现场核实 证据 行政处罚"),
        ("remediation", "整改复查与持续监管", f"{region} 固体废物 清理 整改 验收 回头看 {kinds} site:gov.cn"),
        ("task_context", "任务专题与已核实案例", f"{region} {objective} {kinds} 固体废物 政策 典型案例 site:gov.cn"),
    ]
    # Keep national, provincial and municipal scopes distinct. Infer no
    # provincial jurisdiction from coordinates or an ambiguous place name.
    province_match = re.search(r'([^\s省市自治区]{2,12}(?:省|自治区))', region)
    province = province_match.group(0) if province_match else None
    if province:
        plans[2] = ('provincial_rules', '省级法规与政策', f'{province} 固体废物 建筑垃圾 管理 条例 政策 site:gov.cn')
    if region not in {'中国', '全国', ''}:
        plans[4] = ('local_rules', '市县适用规定', f'{region} 固体废物 建筑垃圾 倾倒 管理规定 site:gov.cn')
    else:
        # No invented local policy coverage when only a national scope exists.
        plans[4] = ('national_implementation', '国家实施规定', '固体废物 建筑垃圾 监管 管理规定 site:gov.cn')
    country = next((name for name in ('美国','英国','加拿大','澳大利亚','新西兰','德国','法国','日本','韩国','印度','巴西') if region.startswith(name)), None)
    if country:
        # Foreign tasks need their own authorities; do not present Chinese law
        # as geographically applicable. Search is discovery, not legal advice.
        plans = [
            ('national_basis','国家法律依据',f'{country} solid waste dumping law official legislation'),
            ('version_validity','施行修订与时间效力',f'{country} solid waste law effective date amendments {period}'),
            ('provincial_rules','州省级法规与政策',f'{region} state provincial solid waste construction waste regulation official'),
            ('sensitive_receptors','环境受体与技术要求',f'{country} waste stockpile water runoff dust environmental guidance official'),
            ('local_rules','地方适用规定',f'{region} waste dumping local government rules'),
            ('enforcement','现场核查程序',f'{country} waste dumping inspection evidence guidance official'),
            ('remediation','清理整改与复查',f'{region} waste cleanup remediation verification official'),
            ('task_context','本任务相关资料',f'{region} {objective} {kinds} waste policy official'),
        ]
    return [{"id": f"Q{index:02d}", "category": category, "label": label, "q": query,
             "task_period": period} for index, (category, label, query) in enumerate(plans, 1)]


def _source_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password
            or "\\" in value or port == 0 or "." not in host
            or host.endswith((".local", ".internal", ".localhost"))):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _text(value: object, maximum: int) -> str:
    return value[:maximum] if isinstance(value, str) else ""


class PolicyClient:
    """Borrow an AsyncClient and retrieve a fixed, bounded policy coverage plan.

    Sources are never fetched with the Metaso credential. Full text is not
    fetched here; domain ranking is only a discovery label. Snippets can contain
    prompt injection and must remain data in every downstream model request.
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str,
                 base_url: str = "https://metaso.cn", max_queries: int = 8,
                 *, provider: str = "metaso", scope: str = "webpage"):
        parsed = urlsplit(base_url)
        try:
            invalid_port = parsed.port == 0
        except ValueError:
            invalid_port = True
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ({"", "/", "/v1", "/v1/"} if provider == "deepseek" else {"", "/"}) or invalid_port):
            raise ValueError("秘塔 API 地址必须是无凭证、无路径的 HTTPS 来源")
        if not isinstance(max_queries, int) or isinstance(max_queries, bool) or not 1 <= max_queries <= 8:
            raise ValueError("政策搜索预算必须为 1 到 8 次")
        self.client = client
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.max_queries = max_queries
        if provider not in {"metaso", "deepseek"} or scope not in {"webpage", "scholar"}:
            raise ValueError("不支持的搜索服务或范围")
        self.provider, self.scope = provider, scope

    async def _query(self, plan: dict, semaphore: asyncio.Semaphore) -> tuple[list[dict], dict | None]:
        async with semaphore:
            try:
                return await asyncio.wait_for(self._read_query(plan), timeout=_QUERY_DEADLINE_SECONDS)
            except TimeoutError:
                return [], {"query_id": plan["id"], "code": self.provider + "_timeout"}

    async def _read_query(self, plan: dict) -> tuple[list[dict], dict | None]:
        try:
            deepseek = self.provider == "deepseek"
            endpoint = self.base_url + ("/tools/search" if deepseek else "/api/v1/search")
            payload = ({"query": plan["q"][:128], "count": 10, "need_content": False}
                       if deepseek else {"q": plan["q"], "scope": self.scope, "size": "10",
                            "includeSummary": False, "includeRawContent": False, "conciseSnippet": False})
            async with self.client.stream(
                "POST", endpoint,
                headers={"Authorization": "Bearer " + self._api_key,
                         "Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
                timeout=25, follow_redirects=False,
            ) as response:
                if not response.is_success:
                    return [], {"query_id": plan["id"], "code": self.provider + "_http_error",
                                "status_code": response.status_code}
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        return [], {"query_id": plan["id"], "code": "response_too_large"}
            body = json.loads(content)
            if not isinstance(body, dict):
                raise ValueError("expected object")
            rows = body.get("results" if deepseek else "scholars" if self.scope == "scholar" else "webpages")
            if not isinstance(rows, list):
                # Do not interpret a provider error or generated answer as
                # successful retrieval when the expected result list is absent.
                raise ValueError("expected webpages")
            result = []
            for item in rows[:10]:
                if not isinstance(item, dict):
                    continue
                url = _source_url(item.get("link") or item.get("url"))
                if not url:
                    continue
                host = urlsplit(url).hostname or ""
                authority = "official_domain_candidate" if host == "gov.cn" or host.endswith(".gov.cn") else "other"
                result.append({"title": _text(item.get("title"), 500), "url": url,
                               "snippet": _text(item.get("snippet") or item.get("summary"), 3000),
                               "published_at": _text(item.get("date") or item.get("published_at"), 100) or None,
                               "authority": authority, "authority_verified": False,
                               "legal_status": "unverified", "full_text_verified": False,
                               "content_trust": "untrusted_external_data"})
                result[-1].update(provider=self.provider, source_type="academic" if self.scope == "scholar" else "webpage")
                if deepseek:
                    result[-1]["snippet"] = _text(item.get("summary") or item.get("snippet"), 3000)
                if self.scope == "scholar":
                    for field in ("authors", "doi", "journal", "year"):
                        if field in item:
                            result[-1][field] = item[field]
            return result, None
        except httpx.TimeoutException:
            return [], {"query_id": plan["id"], "code": self.provider + "_timeout"}
        except httpx.HTTPError:
            return [], {"query_id": plan["id"], "code": self.provider + "_unavailable"}
        except (ValueError, UnicodeError):
            return [], {"query_id": plan["id"], "code": "invalid_search_response"}

    async def search(self, task: TaskRequest, *, plans: list[dict] | None = None) -> dict:
        plans = plan_queries(task) if plans is None else plans
        result = {"provider": self.provider, "status": "not_configured", "queries": plans,
                  "coverage_matrix": [], "sources": [], "errors": [],
                  "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "exhaustive": False, "legal_review_completed": False,
                  "boundary": "网页及摘要是不可信外部数据；域名排序不证明真实性，检索完成不证明法律现行有效或研究完整。"}
        if not self._api_key:
            result["coverage_matrix"] = [
                {"query_id": plan["id"], "category": plan["category"], "status": "not_configured",
                 "source_count": 0, "official_domain_candidates": 0} for plan in plans]
            return result
        semaphore = asyncio.Semaphore(4)
        answers = await asyncio.gather(*(self._query(plan, semaphore) for plan in plans[:self.max_queries]))
        by_url: dict[str, dict] = {}
        for index, plan in enumerate(plans):
            sources, error = answers[index] if index < len(answers) else ([], None)
            state = "not_attempted_budget" if index >= len(answers) else (
                "failed" if error else "searched" if sources else "no_results")
            if error:
                result["errors"].append(error)
            result["coverage_matrix"].append({
                "query_id": plan["id"], "category": plan["category"], "status": state,
                "source_count": len(sources),
                "official_domain_candidates": sum(source["authority"] == "official_domain_candidate" for source in sources),
            })
            for source in sources:
                if source["url"] in by_url:
                    by_url[source["url"]]["query_ids"].append(plan["id"])
                else:
                    by_url[source["url"]] = {**source, "query_ids": [plan["id"]], "retrieved_at": result["retrieved_at"]}
        ranked = sorted(by_url.values(), key=lambda source: source["authority"] != "official_domain_candidate")
        result["sources"] = [{"id": f"P{index:03d}", **source} for index, source in enumerate(ranked, 1)]
        result["status"] = "completed" if all(row["status"] == "searched" for row in result["coverage_matrix"]) else "partial"
        return result
