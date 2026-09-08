"""Historical scholarly-work discovery and open-PDF retrieval."""

import asyncio
import os
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
USER_AGENT = "TalentMiner/1.1 (historical paper discovery)"
OPENALEX_MIN_REQUEST_INTERVAL = 0.4
OPENALEX_MAX_RETRIES = 5
SEARCH_CACHE_TTL_SECONDS = 600

_openalex_request_lock = asyncio.Lock()
_last_openalex_request_at = 0.0
_search_cache = {}


class OpenAlexQuotaExhausted(RuntimeError):
    """The anonymous daily credit pool will not recover with short retries."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__("OpenAlex 今日匿名额度已耗尽")


def _retry_after_seconds(value: str, attempt: int) -> float:
    """Use OpenAlex's delay when present, otherwise exponential backoff."""
    try:
        return max(0.5, min(float(value), 60.0))
    except (TypeError, ValueError):
        return min(2.0 ** (attempt + 1), 30.0)


async def _request_openalex_page(
    session: aiohttp.ClientSession, params: dict, api_key: str
) -> dict:
    """Fetch one page with process-wide pacing and bounded retry recovery."""
    global _last_openalex_request_at
    last_error = ""
    for attempt in range(OPENALEX_MAX_RETRIES):
        retry_delay = 0.0
        async with _openalex_request_lock:
            elapsed = time.monotonic() - _last_openalex_request_at
            if elapsed < OPENALEX_MIN_REQUEST_INTERVAL:
                await asyncio.sleep(OPENALEX_MIN_REQUEST_INTERVAL - elapsed)
            try:
                async with session.get(OPENALEX_WORKS_URL, params=params) as response:
                    _last_openalex_request_at = time.monotonic()
                    if response.status == 200:
                        return await response.json()
                    detail = (await response.text())[:300]
                    if response.status == 429:
                        raw_retry_after = response.headers.get("Retry-After")
                        try:
                            reset_seconds = float(raw_retry_after or 0)
                        except (TypeError, ValueError):
                            reset_seconds = 0
                        if reset_seconds > 300:
                            raise OpenAlexQuotaExhausted(reset_seconds)
                        retry_delay = _retry_after_seconds(
                            raw_retry_after, attempt
                        )
                        last_error = detail or "请求频率超限"
                    elif response.status in {500, 502, 503, 504}:
                        retry_delay = _retry_after_seconds(None, attempt)
                        last_error = f"服务暂时不可用（{response.status}）"
                    elif response.status in {401, 403} and not api_key:
                        raise RuntimeError(
                            "OpenAlex 当前要求 API Key，请设置 OPENALEX_API_KEY"
                        )
                    else:
                        raise RuntimeError(
                            f"OpenAlex 检索失败（{response.status}）：{detail}"
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                retry_delay = _retry_after_seconds(None, attempt)
                last_error = str(exc) or "网络连接暂时不可用"
        if attempt < OPENALEX_MAX_RETRIES - 1:
            await asyncio.sleep(retry_delay)
    raise RuntimeError(
        "OpenAlex 连续限流或暂时不可用，系统已自动重试5次。"
        f"请稍后再试；最后响应：{last_error}"
    )


def _crossref_date(item: dict) -> str:
    for key in ("published-online", "published-print", "published", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if not parts or not parts[0]:
            continue
        values = [int(value) for value in parts[0][:3]]
        while len(values) < 3:
            values.append(1)
        try:
            return date(values[0], values[1], values[2]).isoformat()
        except ValueError:
            continue
    return ""


def compact_crossref_work(item: dict) -> dict:
    """Convert Crossref metadata to the same preview schema as OpenAlex."""
    authorships = []
    for index, author in enumerate(item.get("author") or []):
        name = " ".join(filter(None, [author.get("given"), author.get("family")])).strip()
        if not name:
            name = (author.get("name") or "").strip()
        if not name:
            continue
        affiliations = [
            affiliation.get("name") or ""
            for affiliation in (author.get("affiliation") or [])
            if affiliation.get("name")
        ]
        authorships.append({
            "author_position": (
                "first" if index == 0 else
                "last" if index == len(item.get("author") or []) - 1 else "middle"
            ),
            "is_corresponding": False,
            "author": {
                "id": "",
                "display_name": name,
                "orcid": author.get("ORCID") or "",
            },
            "institutions": [
                {"id": "", "ror": "", "display_name": name,
                 "country_code": "", "type": ""}
                for name in affiliations
            ],
            "raw_affiliation_strings": affiliations,
        })
    links = item.get("link") or []
    pdf_url = next((
        link.get("URL") or "" for link in links
        if "pdf" in (link.get("content-type") or "").lower()
        and (link.get("URL") or "").startswith(("http://", "https://"))
    ), "")
    publication_date = _crossref_date(item)
    title = next(iter(item.get("title") or []), "")
    venue = next(iter(item.get("container-title") or []), "")
    doi = normalize_doi(item.get("DOI") or "")
    result = {
        "openalex_id": "",
        "doi": doi,
        "title": title,
        "publication_date": publication_date,
        "publication_year": int(publication_date[:4]) if publication_date else None,
        "type": item.get("type") or "",
        "cited_by_count": int(item.get("is-referenced-by-count") or 0),
        "is_retracted": False,
        "venue": venue,
        "landing_page_url": item.get("URL") or "",
        "pdf_url": pdf_url,
        "is_open_access": bool(pdf_url),
        "authorships": authorships,
        "topics": list(item.get("subject") or [])[:10],
        "metadata_source": "Crossref（OpenAlex 配额耗尽后的备用检索）",
    }
    result["identity"] = paper_identity(result)
    result["author_count"] = len(authorships)
    result["authors"] = [entry["author"]["display_name"] for entry in authorships]
    result["corresponding_authors"] = []
    return result


async def search_crossref_works(
    query: str, start: date, end: date, limit: int, open_access_only: bool
) -> dict:
    """Fallback discovery when OpenAlex's daily anonymous quota is exhausted."""
    params = {
        "query": query,
        "filter": (
            f"from-pub-date:{start.isoformat()},"
            f"until-pub-date:{end.isoformat()}"
        ),
        "rows": min(1000, max(100, limit * 3)),
        "offset": 0,
        "sort": "relevance",
        "order": "desc",
    }
    email = os.environ.get("CROSSREF_API_EMAIL", "").strip()
    if email:
        params["mailto"] = email
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(CROSSREF_WORKS_URL, params=params) as response:
            if response.status != 200:
                detail = (await response.text())[:300]
                raise RuntimeError(f"Crossref 备用检索失败（{response.status}）：{detail}")
            message = (await response.json()).get("message") or {}
    allowed_types = {"journal-article", "posted-content", "proceedings-article"}
    candidates = []
    for item in message.get("items") or []:
        if item.get("type") not in allowed_types:
            continue
        paper = compact_crossref_work(item)
        if not paper.get("identity") or not paper.get("title") or not paper.get("authorships"):
            continue
        if open_access_only and not paper.get("pdf_url"):
            continue
        candidates.append(paper)
    unique_candidates = deduplicate_papers(candidates)
    papers = unique_candidates[:limit]
    return {
        "estimated_total": int(message.get("total-results") or 0),
        "returned": len(papers),
        "papers": papers,
        "duplicates_removed": len(candidates) - len(unique_candidates),
        "metadata_source": "crossref",
        "notice": "OpenAlex 今日额度已耗尽，已自动使用 Crossref 备用检索。",
    }


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def paper_identity(work: dict) -> str:
    doi = normalize_doi(work.get("doi") or "")
    if doi:
        return f"doi:{doi}"
    openalex_id = (work.get("openalex_id") or work.get("id") or "").rstrip("/").split("/")[-1]
    return f"openalex:{openalex_id.upper()}" if openalex_id else ""


def _fingerprint_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def paper_fingerprint(work: dict) -> str:
    """Identify duplicate metadata records even when repositories mint two DOIs."""
    title = _fingerprint_text(work.get("title") or work.get("display_name") or "")
    publication_date = str(work.get("publication_date") or "").strip()
    authors = work.get("authors") or []
    if not authors:
        authors = [
            ((entry.get("author") or {}).get("display_name") or "")
            for entry in (work.get("authorships") or [])
        ]
    author_key = tuple(
        name for name in (_fingerprint_text(str(author)) for author in authors)
        if name
    )
    if not title or not publication_date or not author_key:
        return ""
    return "|".join((title, publication_date, ";".join(author_key)))


def deduplicate_papers(papers: list[dict]) -> list[dict]:
    """Preserve relevance order while collapsing ID and metadata duplicates."""
    unique = []
    identities = set()
    fingerprints = set()
    for paper in papers:
        identity = paper_identity(paper)
        fingerprint = paper_fingerprint(paper)
        if (identity and identity in identities) or (
            fingerprint and fingerprint in fingerprints
        ):
            continue
        unique.append(paper)
        if identity:
            identities.add(identity)
        if fingerprint:
            fingerprints.add(fingerprint)
    return unique


def _pdf_url(work: dict) -> str:
    candidates = [work.get("best_oa_location") or {}]
    candidates.extend(work.get("locations") or [])
    for location in candidates:
        url = location.get("pdf_url") or ""
        if url.startswith(("http://", "https://")):
            return url
    return ""


def compact_work(work: dict) -> dict:
    authorships = []
    for item in work.get("authorships") or []:
        author = item.get("author") or {}
        if not author.get("display_name"):
            continue
        authorships.append({
            "author_position": item.get("author_position") or "middle",
            "is_corresponding": bool(item.get("is_corresponding")),
            "author": {
                "id": author.get("id") or "",
                "display_name": author.get("display_name") or "",
                "orcid": author.get("orcid") or "",
            },
            "institutions": [
                {
                    "id": institution.get("id") or "",
                    "ror": institution.get("ror") or "",
                    "display_name": institution.get("display_name") or "",
                    "country_code": institution.get("country_code") or "",
                    "type": institution.get("type") or "",
                }
                for institution in (item.get("institutions") or [])
            ],
            "raw_affiliation_strings": item.get("raw_affiliation_strings") or [],
        })
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    result = {
        "openalex_id": work.get("id") or "",
        "doi": normalize_doi(work.get("doi") or ""),
        "title": work.get("title") or work.get("display_name") or "",
        "publication_date": work.get("publication_date") or "",
        "publication_year": work.get("publication_year"),
        "type": work.get("type") or "",
        "cited_by_count": int(work.get("cited_by_count") or 0),
        "is_retracted": bool(work.get("is_retracted")),
        "venue": source.get("display_name") or "",
        "landing_page_url": primary.get("landing_page_url") or "",
        "pdf_url": _pdf_url(work),
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "authorships": authorships,
        "topics": [
            item.get("display_name") for item in (work.get("topics") or [])
            if item.get("display_name")
        ][:10],
    }
    result["identity"] = paper_identity(result)
    result["author_count"] = len(authorships)
    result["authors"] = [item["author"]["display_name"] for item in authorships]
    result["corresponding_authors"] = [
        item["author"]["display_name"] for item in authorships
        if item.get("is_corresponding")
    ]
    return result


async def search_historical_works(
    query: str,
    date_from: str,
    date_to: str,
    limit: int = 100,
    open_access_only: bool = False,
) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("检索关键词不能为空")
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    limit = max(1, min(int(limit or 100), 500))
    cache_key = (
        query.casefold(), start.isoformat(), end.isoformat(), limit,
        bool(open_access_only), bool(os.environ.get("OPENALEX_API_KEY", "").strip()),
    )
    cached = _search_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL_SECONDS:
        return cached[1]
    filters = [
        f"from_publication_date:{start.isoformat()}",
        f"to_publication_date:{end.isoformat()}",
        "type:article|preprint",
    ]
    if open_access_only:
        filters.append("open_access.is_oa:true")
    params = {
        "search": query,
        "filter": ",".join(filters),
        # Search relevance is the safer default for historical talent discovery;
        # the explicit date filters already constrain recency.
        "sort": "relevance_score:desc",
        "per_page": min(100, limit),
        "cursor": "*",
    }
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    results = []
    estimated_total = 0
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            while len(deduplicate_papers(results)) < limit:
                payload = await _request_openalex_page(session, params, api_key)
                estimated_total = int((payload.get("meta") or {}).get("count") or 0)
                page = [compact_work(item) for item in (payload.get("results") or [])]
                results.extend(item for item in page if item.get("identity") and not item.get("is_retracted"))
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor or not page:
                    break
                params["cursor"] = cursor
    except OpenAlexQuotaExhausted:
        result = await search_crossref_works(
            query, start, end, limit, open_access_only
        )
        _search_cache[cache_key] = (time.monotonic(), result)
        return result
    unique_results = deduplicate_papers(results)
    papers = unique_results[:limit]
    result = {
        "estimated_total": estimated_total, "returned": len(papers),
        "papers": papers, "metadata_source": "openalex",
        "duplicates_removed": len(results) - len(unique_results),
    }
    _search_cache[cache_key] = (time.monotonic(), result)
    return result


async def download_open_pdf(url: str, destination: Path, max_bytes: int) -> bool:
    """Download only a real HTTP(S) PDF; return False when OA retrieval fails."""
    if not url or urlparse(url).scheme not in {"http", "https"}:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return False
                written = 0
                first = b""
                with destination.open("wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not first:
                            first = chunk[:5]
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("开放 PDF 超过 50MB 限制")
                        output.write(chunk)
                if first != b"%PDF-" or written == 0:
                    destination.unlink(missing_ok=True)
                    return False
                return True
    except Exception:
        destination.unlink(missing_ok=True)
        return False
