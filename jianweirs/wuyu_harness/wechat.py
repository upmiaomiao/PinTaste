"""Bounded MCP Streamable HTTP access to public WeChat articles.

One upstream search at a time; no retry during provider cooldown. Article text
is captured immediately while references are alive, then remains untrusted data.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import quote, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .policy import _source_url, _text


def tool_body(reply) -> dict:
    body = reply.structuredContent
    if not isinstance(body, dict):
        for item in reply.content:
            if getattr(item, "type", "") == "text":
                try:
                    body = json.loads(item.text)
                except (ValueError, TypeError):
                    continue
                if isinstance(body, dict):
                    break
    if not isinstance(body, dict):
        raise ValueError("invalid_mcp_tool_result")
    return body


def wechat_queries(task) -> list[str]:
    text = task.objective
    # Preserve the technical subject instead of searching only its broad sector.
    focus = re.search(r"膜生物反应器|\bMBR\b|危险废物贮存|危废贮存|固废资源化|工业固废资源化|垃圾焚烧飞灰|废旧电池回收", text, re.I)
    if focus:
        keyword = focus.group(0)
        return ["膜生物反应器" if keyword.upper() == "MBR" else keyword]
    topics = re.findall(r"污水处理|固废资源化|固废处理|固体废物|危险废物|危废|建筑垃圾|铝灰|再生资源|遥感|塑料污染|碳排放|垃圾焚烧|废旧电池|土壤污染", text)
    if topics:
        return [" ".join(dict.fromkeys(topics))[:100]]
    cleaned = re.sub(r"(?:请|帮我|查找|搜索|查询|检索|微信公众号|公众号|微信文章|联网|相关资料)", " ", text)
    parts = [part.strip() for part in re.split(r"[，,。！？?；;\n]", cleaned) if len(part.strip()) >= 3]
    return [re.sub(r"\s+", " ", parts[0] if parts else text)[:100]]


class WeChatClient:
    def __init__(self, url: str = "", *, enabled: bool = False, api_key: str = "",
                 timeout: int = 180, max_results: int = 5, max_pages: int = 3, read_articles: int = 2,
                 transport=None):
        parsed = urlsplit(url)
        if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or "\\" in url):
            raise ValueError("微信公众号MCP地址无效")
        if not 5 <= timeout <= 180 or not 1 <= max_results <= 50 or not 1 <= max_pages <= 3 or not 0 <= read_articles <= 5:
            raise ValueError("微信公众号搜索预算无效")
        self.url, self.enabled, self._api_key = url, enabled, api_key
        self.timeout, self.max_results, self.max_pages, self.read_articles = timeout, max_results, max_pages, read_articles
        self.transport = transport
        self.lock = asyncio.Lock()
        self.cooldown_until = 0.0

    def _cooldown(self, body):
        error = body.get("error")
        error = error if isinstance(error, dict) else {}
        delay = body.get("retry_after_s") or error.get("retry_after_s") or 0
        try:
            delay = min(max(float(delay), 0), 3600)
        except (TypeError, ValueError):
            delay = 0
        if delay:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
        return delay

    async def _call(self, session, name, arguments, timings):
        tick = time.perf_counter()
        reply = await session.call_tool(name, arguments)
        timings.append({"tool": name, "seconds": round(time.perf_counter()-tick, 3)})
        body = tool_body(reply)
        self._cooldown(body)
        if reply.isError or body.get("error"):
            raise ValueError("wechat_upstream_error")
        return body

    async def _open(self, action):
        headers = {"Authorization": "Bearer " + self._api_key} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False,
                                     headers=headers, transport=self.transport) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                    tick = time.perf_counter()
                    await session.initialize()
                    listed = await session.list_tools()
                    return await action(session, {tool.name for tool in listed.tools}, round(time.perf_counter()-tick, 3))

    async def status(self):
        if not self.enabled or not self.url:
            return {"provider": "wechat", "status": "disabled" if not self.enabled else "not_configured"}
        async def action(session, names, elapsed):
            required = {"wechat_search", "wechat_get_article"}
            if not required <= names:
                return {"provider": "wechat", "status": "unsupported_tools"}
            state = await self._call(session, "wechat_search_status", {}, []) if "wechat_search_status" in names else {}
            return {"provider": "wechat", "status": "cooldown" if self._cooldown(state) else "ready",
                    "handshake_seconds": elapsed, "tools": sorted(names),
                    "retry_after_s": state.get("retry_after_s", 0), "queued_requests": state.get("queued_requests", 0)}
        try:
            async with asyncio.timeout(min(self.timeout, 20)):
                return await self._open(action)
        except Exception:
            return {"provider": "wechat", "status": "unavailable"}

    async def _read_article(self, session, row, timings):
        offset, parts, seen = 0, [], set()
        next_offset = None
        for _ in range(2):
            if offset in seen:
                break
            seen.add(offset)
            body = await self._call(session, "wechat_get_article",
                {"reference_id": row["reference_id"], "offset": offset, "max_chars": 12000}, timings)
            text = body.get("text") or body.get("content") or body.get("markdown") or ""
            if not isinstance(text, str):
                raise ValueError("invalid_article_text")
            parts.append(text[:12000])
            row["article_text"] = "\n".join(parts)
            row["content_chars"] = len(row["article_text"])
            row["content_status"] = "partial"
            if offset == 0:
                for key in ("title", "account", "published_at"):
                    value = _text(body.get(key), 500)
                    if value:
                        previous = row.get(key)
                        if previous and previous != value:
                            row["indexed_" + key] = previous
                            if key == "published_at" and previous[:10] != value[:10]:
                                row["metadata_conflicts"] = ["索引时间与正文时间不同，当前显示正文时间"]
                        row[key] = value
                original = _source_url(body.get("original_url"))
                if original:
                    row["url"], row["original_url"] = original, original
            next_offset = body.get("next_offset")
            if not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset <= offset:
                next_offset = None
                break
            offset = next_offset
        row["article_text"] = "\n".join(parts)
        row["content_chars"] = len(row["article_text"])
        row["content_status"] = "partial" if next_offset is not None else "read" if row["article_text"].strip() else "empty"
        row["next_offset"] = next_offset
        row["content_read_at"] = datetime.now(timezone.utc).isoformat()
        row["full_text_verified"] = False

    async def search(self, task):
        tick = time.perf_counter()
        queries = wechat_queries(task)
        result = {"provider": "wechat", "source_type": "wechat", "status": "disabled" if not self.enabled else "not_configured",
                  "queries": [{"id": "WX01", "q": queries[0], "category": "wechat", "label": "微信公众号资料"}],
                  "sources": [], "errors": [], "timings": [], "exhaustive": False, "legal_review_completed": False}
        if not self.enabled or not self.url:
            return result
        async def action(session, names, elapsed):
            result["handshake_seconds"] = elapsed
            if not {"wechat_search", "wechat_get_article"} <= names:
                raise ValueError("wechat_tools_missing")
            if "wechat_search_status" in names:
                state = await self._call(session, "wechat_search_status", {}, result["timings"])
                if self._cooldown(state):
                    result.update(status="cooldown", retry_after_s=state.get("retry_after_s"))
                    return result
            time_range = "two_years"
            if task.date_start and task.date_start < (datetime.now(timezone.utc).date() - timedelta(days=730)):
                time_range = "any"
            parameters = {"max_results": self.max_results, "max_pages": self.max_pages, "time_range": time_range, "sort": "recent"}
            if len(queries) > 1 and "wechat_search_many" in names:
                body = await self._call(session, "wechat_search_many", {"queries": queries, **parameters}, result["timings"])
            else:
                body = await self._call(session, "wechat_search", {"query": queries[0], **parameters}, result["timings"])
            rows = body.get("items")
            if not isinstance(rows, list):
                raise ValueError("invalid_wechat_results")
            seen = set()
            for item in rows[:self.max_results * len(queries)]:
                if not isinstance(item, dict):
                    continue
                reference = _text(item.get("reference_id"), 256)
                if not reference or reference in seen:
                    continue
                seen.add(reference)
                original = _source_url(item.get("original_url"))
                url = original or _source_url(item.get("url"))
                if not url:
                    parsed = urlsplit(self.url)
                    url = parsed.scheme + "://" + parsed.netloc + "/articles/" + quote(reference, safe="")
                result["sources"].append({"title": _text(item.get("title"), 500), "url": url,
                    "original_url": original, "account": _text(item.get("account"), 200),
                    "published_at": _text(item.get("published_at"), 100) or None,
                    "snippet": _text(item.get("snippet"), 3000), "reference_id": reference,
                    "reference_expires_at": _text(item.get("reference_expires_at"), 100) or None,
                    "provider": "wechat", "source_type": "wechat", "query_ids": ["WX01"],
                    "authority": "unverified_account", "authority_verified": False, "legal_status": "unverified",
                    "full_text_verified": False, "content_status": "snippet_only", "content_trust": "untrusted_external_data"})
            result.update(status="completed" if result["sources"] else "no_results",
                          has_more=bool(body.get("has_more")), truncated=bool(body.get("truncated")), pages_fetched=body.get("pages_fetched"))
            for row in result["sources"][:self.read_articles]:
                if self.cooldown_until > time.monotonic():
                    break
                try:
                    await self._read_article(session, row, result["timings"])
                except Exception:
                    row["content_status"] = "partial" if row.get("article_text") else "failed"
                    result["errors"].append({"code": "wechat_article_unavailable", "reference_id": row["reference_id"]})
            if result["errors"]:
                result["status"] = "partial"
            return result
        async with self.lock:
            if self.cooldown_until > time.monotonic():
                result.update(status="cooldown", retry_after_s=round(self.cooldown_until-time.monotonic(), 1))
                return result
            try:
                async with asyncio.timeout(self.timeout + 5):
                    await self._open(action)
            except Exception:
                result["status"] = "partial" if result["sources"] else "failed"
                result["errors"].append({"code": "wechat_mcp_unavailable"})
                if self.cooldown_until > time.monotonic():
                    result.update(status="cooldown", retry_after_s=round(self.cooldown_until-time.monotonic(), 1))
        result["latency_seconds"] = round(time.perf_counter()-tick, 3)
        result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        return result
