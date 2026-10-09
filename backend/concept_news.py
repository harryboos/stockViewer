"""Retrieve real-world catalysts before asking GLM to interpret their impact."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from . import ai, database
from .concept_data import ConceptResearchClient, clean_text, research_terms
from .research_prompt import publication_time

SEARCH_TIMEOUT_SECONDS = 10
NEWS_BUDGET_SECONDS = 25


async def discover_world_news(boards: list[dict], key: str) -> tuple[dict, dict]:
    """Two bounded broad searches before ranking; literal topic matches are clues only."""
    if not key:
        return {}, {"status": "unavailable", "queries": 0, "matchedCount": 0,
                    "note": "未配置检索密钥，本次未使用事件发现通道"}
    queries = ("产业政策 产品获批 新订单", "技术突破 供需 价格变化")
    results = await asyncio.gather(*(search_world_news(query, "discovery", key, forecast=True)
                                     for query in queries), return_exceptions=True)
    successful = [rows for rows in results if isinstance(rows, list)]
    articles = {row["url"]: row for rows in successful for row in rows}
    mapped = {}
    for board in boards:
        # Use the actual index name, not an LLM-created industry-to-index mapping.
        term = str(board.get("name", "")).removesuffix("概念").strip()
        if len(term) < 2 or (term.isascii() and len(term) < 3):
            continue
        matches = []
        for row in articles.values():
            text = (row["title"] + " " + row["excerpt"]).casefold()
            # ASCII abbreviations must be tokens, while adjacent Chinese text
            # remains valid ("国内CRO订单"). A substring in Microsoft/micro is not CRO.
            matched = (re.search(r"(?<![a-z0-9_])" + re.escape(term.casefold()) + r"(?![a-z0-9_])", text)
                       if term.isascii() else term.casefold() in text)
            if not matched:
                continue
            matches.append({**row, "id": f"{board['code']}:web:{hashlib.sha256(row['url'].encode()).hexdigest()[:16]}",
                            "discoveryMatch": term})
        if matches:
            mapped[board["code"]] = sorted(matches, key=lambda row: row["publishedAt"], reverse=True)[:2]
    # Public search coverage is limited; a zero match is not proof of no catalyst.
    return mapped, {"status": "available" if len(successful) == len(queries) else "partial" if successful else "unavailable",
                    "queries": len(queries), "articleCount": len(articles), "matchedCount": len(mapped),
                    "note": "有限公开检索，按真实概念名称匹配；命中不等于新事件或利好，未命中不等于没有催化"}


def search_query(name: str, *, forecast: bool = False) -> str:
    terms, topics = research_terms(name)
    # The provider caps query length at 70 characters; recency is a separate filter.
    focus = "未来两周 业绩 订单 政策 供需 风险" if forecast else "近期走强 产业新闻"
    return f"{' '.join(terms[:3])} {focus} {topics}"[:70]


def normalize_search_results(payload: dict, code: str) -> list[dict]:
    now = datetime.now(database.CHINA_TZ)
    sources = {}
    for row in payload.get("search_result") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("link") or "").strip()
        try:
            address = urlsplit(url)
            published, precision = publication_time(row.get("publish_date"))
        except ValueError:
            continue
        if (address.scheme not in ("http", "https") or not address.hostname or address.username
                or published is None or not now - timedelta(days=30) <= published <= now):
            continue
        title, content = clean_text(row.get("title"), 200), clean_text(row.get("content"), 1600)
        if not title or not content:
            continue
        sources[url] = {"id": f"{code}:web:{hashlib.sha256(url.encode()).hexdigest()[:16]}", "kind": "news",
                        "title": title, "excerpt": content, "url": url,
                        "source": clean_text(row.get("media"), 80) or address.hostname,
                        "publishedAt": published.isoformat(timespec="seconds"), "publishedAtPrecision": precision}
    # Keep the search engine's relevance order rather than preferring daily price recaps.
    return list(sources.values())[:6]


async def search_world_news(name: str, code: str, key: str, *, forecast: bool = False) -> list[dict]:
    # Official Web Search API; the reasoning model remains glm-5.3.
    async with httpx.AsyncClient(timeout=httpx.Timeout(SEARCH_TIMEOUT_SECONDS, connect=4)) as client:
        response = await client.post(
            f"{ai.base_url_for('glm')}/web_search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"search_engine": "search_std", "search_query": search_query(name, forecast=forecast), "search_intent": False, "count": 6,
                  "search_recency_filter": "oneMonth", "content_size": "high"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid search response")
        return normalize_search_results(payload, code)


async def enrich_world_news(evidence: dict, key: str, *, forecast: bool = False) -> dict:
    semaphore = asyncio.Semaphore(4)
    client = ConceptResearchClient()

    async def enrich(candidate: dict) -> None:
        sources = []
        async with semaphore:
            try:
                options = {"forecast": True} if forecast else {}
                sources = await search_world_news(candidate["name"], candidate["code"], key, **options)
            except (httpx.HTTPError, ValueError, TypeError):
                candidate["warnings"].append("现实事件联网检索暂不可用，已尝试财经资讯备用来源")
            if not sources:
                try:
                    sources = await asyncio.wait_for(
                        asyncio.to_thread(client.news, candidate["name"], candidate["code"]), timeout=10,
                    )
                except (TimeoutError, RuntimeError, ValueError, KeyError, TypeError):
                    pass
        existing = {item.get("url") for item in candidate["evidence"] if item.get("kind") == "news"}
        candidate["evidence"].extend(item for item in sources if item.get("url") not in existing)
        if not sources:
            candidate["warnings"].append("未取得近30天可核对的现实事件来源，催化因素仅能列为待验证机制")

    tasks = [asyncio.create_task(enrich(candidate)) for candidate in evidence["candidates"]]
    if not tasks:
        return evidence
    try:
        _, pending = await asyncio.wait(tasks, timeout=NEWS_BUDGET_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for candidate in evidence["candidates"]:
            if not any(source["kind"] == "news" for source in candidate["evidence"]):
                note = "现实事件来源不足；模型必须明确标注待验证，不能断言事件已发生或导致上涨"
                candidate["warnings"].append(note)
        return evidence
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
