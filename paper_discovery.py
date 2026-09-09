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
ZENODO_DOI_URL = "https://doi.org/10.5281/zenodo.{record_id}"
ZENODO_OPENALEX_SOURCE_ID = "S4306400562"
REPOSITORY_CHECK_CONCURRENCY = 20
REPOSITORY_CHECK_TIMEOUT_SECONDS = 12
REPOSITORY_CHECK_CACHE_TTL_SECONDS = 3600
RELEVANCE_FILTER_VERSION = 1

QUERY_STOP_WORDS = {
    "a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to",
    "using", "via", "with",
}
QUANTUM_COMPUTING_TOPIC = "quantum computing algorithms and architecture"
QUANTUM_COMPUTING_SIGNALS = (
    "quantum algorithm", "quantum arithmetic", "quantum circuit",
    "quantum computer", "quantum computation", "quantum computing",
    "quantum error correction", "quantum machine learning",
    "quantum neural network", "quantum phase estimation", "quantum simulator",
    "quantum teleportation", "quantum walk", "quantum workload",
    "quantum classical", "quantum hpc", "quantum eigensolver",
    "variational quantum", "fault tolerant quantum", "superconducting qubit",
    "spin qubit", "photonic graph state", "magic state", "circuit cutting",
    "gate teleportation", "computational advantage", "qiskit", "qubit",
    "quantum anneal", "superdense coding",
)
QUANTUM_COMPUTING_EVIDENCE = (
    "quantum", "qubit", "qiskit", "circuit", "entangl", "qudit", "rydberg",
    "eigenstate", "phase gate", "magic state",
)
NON_PAPER_TITLE_PREFIXES = (
    "data and source code for ", "dataset for ", "source code for ",
)

_openalex_request_lock = asyncio.Lock()
_last_openalex_request_at = 0.0
_search_cache = {}
_repository_check_cache = {}


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
        "_relevance_abstract": re.sub(
            r"<[^>]+>", " ", item.get("abstract") or ""
        )[:4000],
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
        if zenodo_record_id(paper):
            continue
        if not paper.get("identity") or not paper.get("title") or not paper.get("authorships"):
            continue
        if open_access_only and not paper.get("pdf_url"):
            continue
        candidates.append(paper)
    candidates, relevance_filtered = filter_relevant_papers(candidates, query)
    unique_candidates = deduplicate_papers(candidates)
    papers = unique_candidates[:limit]
    for paper in papers:
        paper.pop("_relevance_abstract", None)
    return {
        "estimated_total": int(message.get("total-results") or 0),
        "returned": len(papers),
        "papers": papers,
        "duplicates_removed": len(candidates) - len(unique_candidates),
        "relevance_filtered": relevance_filtered,
        "metadata_source": "crossref",
        "notice": "OpenAlex 今日额度已耗尽，已使用 Crossref 并执行严格相关性筛选。",
    }


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def zenodo_record_id(work: dict) -> str:
    """Return the numeric Zenodo record identifier embedded in a DOI or URL."""
    doi = normalize_doi(work.get("doi") or "")
    match = re.fullmatch(r"10\.5281/zenodo\.(\d+)", doi)
    if match:
        return match.group(1)
    for key in ("landing_page_url", "pdf_url"):
        match = re.search(r"zenodo\.org/(?:records?|api/records)/(\d+)", work.get(key) or "")
        if match:
            return match.group(1)
    return ""


def is_unusable_repository_record(work: dict) -> bool:
    """Reject metadata-only Zenodo deposits that cannot supply a paper PDF."""
    return bool(zenodo_record_id(work) and not work.get("pdf_url"))


async def _zenodo_record_available(
    session: aiohttp.ClientSession, record_id: str, semaphore: asyncio.Semaphore
) -> bool:
    """Reject confirmed deleted records while retaining transiently unreachable ones."""
    cached = _repository_check_cache.get(record_id)
    if cached and time.monotonic() - cached[0] < REPOSITORY_CHECK_CACHE_TTL_SECONDS:
        return cached[1]
    available = True
    try:
        async with semaphore:
            async with session.get(
                ZENODO_DOI_URL.format(record_id=record_id),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=REPOSITORY_CHECK_TIMEOUT_SECONDS),
            ) as response:
                if response.status in {404, 410}:
                    available = False
                elif response.status == 200:
                    available = True
                else:
                    # Rate limits and server errors are not evidence that a paper vanished.
                    return True
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return True
    _repository_check_cache[record_id] = (time.monotonic(), available)
    return available


async def filter_unavailable_repository_records(
    session: aiohttp.ClientSession, papers: list[dict]
) -> tuple[list[dict], int]:
    """Remove Zenodo records whose public DOI redirect confirms 404/410."""
    semaphore = asyncio.Semaphore(REPOSITORY_CHECK_CONCURRENCY)
    checks = []
    for paper in papers:
        record_id = zenodo_record_id(paper)
        if is_unusable_repository_record(paper):
            checks.append(asyncio.sleep(0, result=False))
        elif record_id:
            checks.append(_zenodo_record_available(session, record_id, semaphore))
        else:
            checks.append(asyncio.sleep(0, result=True))
    availability = await asyncio.gather(*checks)
    available = [paper for paper, keep in zip(papers, availability) if keep]
    return available, len(papers) - len(available)


def paper_identity(work: dict) -> str:
    doi = normalize_doi(work.get("doi") or "")
    if doi:
        return f"doi:{doi}"
    openalex_id = (work.get("openalex_id") or work.get("id") or "").rstrip("/").split("/")[-1]
    return f"openalex:{openalex_id.upper()}" if openalex_id else ""


def _fingerprint_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def _query_terms(query: str) -> list[str]:
    """Return stable, meaningful terms for strict topic matching."""
    return [
        term for term in _fingerprint_text(query).split()
        if len(term) > 1 and term not in QUERY_STOP_WORDS
    ]


def _term_present(term: str, text: str) -> bool:
    if term.startswith("comput"):
        return bool(re.search(r"\bcomput(?:e|er|ers|ing|ation|ations|ational)?\b", text))
    if term.endswith("s") and len(term) > 4:
        return bool(re.search(rf"\b{re.escape(term[:-1])}s?\b", text))
    return bool(re.search(rf"\b{re.escape(term)}\b", text))


def _is_quantum_computing_query(terms: list[str]) -> bool:
    return len(terms) == 2 and "quantum" in terms and any(
        term.startswith("comput") for term in terms
    )


def paper_relevance(work: dict, query: str) -> tuple[bool, int, str]:
    """Apply conservative topic matching and explain why a work was retained."""
    terms = _query_terms(query)
    if not terms:
        return True, 50, "检索词过短，未启用严格筛选"

    title = _fingerprint_text(work.get("title") or "")
    topics = _fingerprint_text(" ".join(work.get("topics") or []))
    abstract = _fingerprint_text(work.get("_relevance_abstract") or "")
    query_text = _fingerprint_text(query)

    if _is_quantum_computing_query(terms):
        if any(title.startswith(prefix) for prefix in NON_PAPER_TITLE_PREFIXES):
            return False, 0, "属于数据或源代码记录，不是论文"
        if "quantum inspired" in title or "post quantum" in title:
            return False, 0, "仅涉及量子启发式或后量子密码"
        if (
            any(term in title for term in ("cryptograph", "signcryption", "blockchain"))
            and "quantum information and cryptography" not in topics
            and "quantum computing" not in title
        ):
            return False, 0, "密码学内容缺少量子计算或量子信息证据"
        if "quantum computing" in title or "quantum computation" in title:
            return True, 100, "标题明确包含量子计算"
        if any(signal in title for signal in QUANTUM_COMPUTING_SIGNALS):
            return True, 90, "标题包含量子计算核心概念"
        if QUANTUM_COMPUTING_TOPIC in topics and (
            any(signal in title for signal in QUANTUM_COMPUTING_EVIDENCE)
            or any(signal in abstract for signal in QUANTUM_COMPUTING_SIGNALS)
        ):
            return True, 80, "学术主题与标题或摘要共同指向量子计算"
        return False, 0, "缺少量子计算核心主题"

    if query_text and query_text in title:
        return True, 100, "标题包含完整检索词"
    title_matches = sum(_term_present(term, title) for term in terms)
    topic_matches = sum(_term_present(term, topics) for term in terms)
    combined = " ".join((title, topics))
    combined_matches = sum(_term_present(term, combined) for term in terms)
    if title_matches == len(terms):
        return True, 90, "标题覆盖全部检索词"
    if query_text and query_text in topics:
        return True, 85, "学术主题包含完整检索词"
    if combined_matches == len(terms) and title_matches + topic_matches >= len(terms):
        return True, 75, "标题与学术主题共同覆盖检索词"
    if query_text and query_text in abstract and title_matches + topic_matches:
        return True, 65, "摘要包含完整检索词"
    if len(terms) == 1 and (title_matches or topic_matches):
        return True, 75, "标题或学术主题匹配检索词"
    return False, 0, "与检索主题关联不足"


def filter_relevant_papers(
    papers: list[dict], query: str
) -> tuple[list[dict], int]:
    """Keep only papers that pass the explainable strict relevance gate."""
    relevant = []
    for paper in papers:
        keep, score, reason = paper_relevance(paper, query)
        if not keep:
            continue
        paper["relevance_score"] = score
        paper["relevance_reason"] = reason
        relevant.append(paper)
    return relevant, len(papers) - len(relevant)


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


def _openalex_abstract(work: dict) -> str:
    """Rebuild OpenAlex's inverted-index abstract for relevance checks."""
    inverted = work.get("abstract_inverted_index") or {}
    positioned = []
    for word, positions in inverted.items():
        for position in positions or []:
            if isinstance(position, int):
                positioned.append((position, word))
    return " ".join(word for _, word in sorted(positioned))


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
        "source_type": source.get("type") or "",
        "landing_page_url": primary.get("landing_page_url") or "",
        "pdf_url": _pdf_url(work),
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "is_accepted": bool(primary.get("is_accepted")),
        "is_published": bool(primary.get("is_published")),
        "has_fulltext": bool(work.get("has_fulltext")),
        "authorships": authorships,
        "topics": [
            item.get("display_name") for item in (work.get("topics") or [])
            if item.get("display_name")
        ][:10],
        "_relevance_abstract": _openalex_abstract(work)[:4000],
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
        RELEVANCE_FILTER_VERSION,
    )
    cached = _search_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL_SECONDS:
        return cached[1]
    filters = [
        f"from_publication_date:{start.isoformat()}",
        f"to_publication_date:{end.isoformat()}",
        "type:article|preprint",
        f"primary_location.source.id:!{ZENODO_OPENALEX_SOURCE_ID}",
    ]
    if open_access_only:
        filters.append("open_access.is_oa:true")
    params = {
        "search": query,
        "filter": ",".join(filters),
        # Search relevance is the safer default for historical talent discovery;
        # the explicit date filters already constrain recency.
        "sort": "relevance_score:desc",
        # Always fetch a full page so deleted repository records do not force
        # dozens of small sequential OpenAlex requests for a small preview.
        "per_page": 100,
        "cursor": "*",
    }
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    results = []
    estimated_total = 0
    unavailable_removed = 0
    relevance_filtered = 0
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            while len(deduplicate_papers(results)) < limit:
                payload = await _request_openalex_page(session, params, api_key)
                estimated_total = int((payload.get("meta") or {}).get("count") or 0)
                raw_page = payload.get("results") or []
                page = [compact_work(item) for item in raw_page]
                page, removed = await filter_unavailable_repository_records(session, page)
                unavailable_removed += removed
                page, removed = filter_relevant_papers(page, query)
                relevance_filtered += removed
                results.extend(item for item in page if item.get("identity") and not item.get("is_retracted"))
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor or not raw_page:
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
    for paper in papers:
        paper.pop("_relevance_abstract", None)
    result = {
        "estimated_total": estimated_total, "returned": len(papers),
        "papers": papers, "metadata_source": "openalex",
        "duplicates_removed": len(results) - len(unique_results),
        "unavailable_removed": unavailable_removed,
        "relevance_filtered": relevance_filtered,
        "notice": "已排除 Zenodo 通用仓库记录，并执行严格相关性筛选。",
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
