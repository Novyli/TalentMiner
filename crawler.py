"""Crawler: deep search for author contact info across multiple sources."""

import asyncio
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup


@dataclass
class AuthorProfile:
    name: str
    openalex_id: Optional[str] = None
    orcid: Optional[str] = None
    email: Optional[str] = None
    email_source: Optional[str] = None
    google_scholar_url: Optional[str] = None
    researchgate_url: Optional[str] = None
    twitter_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    website_url: Optional[str] = None
    affiliations: list = field(default_factory=list)
    topics: list = field(default_factory=list)
    cited_by_count: int = 0
    works_count: int = 0
    coauthors: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    match_score: float = 0.0
    match_status: str = "unmatched"
    match_breakdown: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


OPENALEX_BASE = "https://api.openalex.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
BAD_EMAILS = {'noreply', 'no-reply', 'support', 'info', 'contact', 'admin', 'webmaster', 'help', 'sales', 'marketing'}
NON_PERSONAL_EMAIL_TERMS = {
    "committee", "committees", "collaboration", "collaborations",
    "publication", "publications", "office", "secretariat", "team",
    "mailinglist", "newsletter",
}


INSTITUTION_STOPWORDS = {
    "and", "the", "of", "for", "at", "in", "department", "faculty",
    "school", "college", "university", "institute", "institution",
    "laboratory", "laboratories", "lab", "center", "centre", "physics",
    "science", "sciences", "research", "usa", "uk",
}
DISTINCT_INSTITUTION_TOKENS = {
    "jila", "nist", "mit", "eth", "cuhk", "caltech", "stanford",
    "harvard", "princeton", "berkeley",
}
INSTITUTION_ALIASES = {
    "national institute of standards and technology": "nist",
    "massachusetts institute of technology": "mit",
    "eth zurich": "eth",
    "swiss federal institute of technology zurich": "eth",
    "chinese university of hong kong": "cuhk",
    "california institute of technology": "caltech",
}


def _normalized_words(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = value.replace("&", " and ")
    for full_name, alias in INSTITUTION_ALIASES.items():
        value = value.replace(full_name, alias)
    return [
        token for token in re.findall(r"[a-z0-9]+", value)
        if token not in INSTITUTION_STOPWORDS and not token.isdigit()
    ]


def _institution_similarity(left: str, right: str) -> float:
    """Compare institutions while ignoring address and department wording."""
    left_words = _normalized_words(left)
    right_words = _normalized_words(right)
    if not left_words or not right_words:
        return 0.0
    left_text, right_text = " ".join(left_words), " ".join(right_words)
    if left_text == right_text:
        return 1.0
    if left_text in right_text or right_text in left_text:
        return 1.0
    left_set, right_set = set(left_words), set(right_words)
    common = left_set & right_set
    if not common:
        return 0.0
    containment = len(common) / min(len(left_set), len(right_set))
    if len(common) == 1 and not (common & DISTINCT_INSTITUTION_TOKENS):
        containment *= 0.45
    sequence = SequenceMatcher(None, left_text, right_text).ratio()
    return round(max(containment, sequence), 3)


def _canonical_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return "".join(re.findall(r"[a-z]+", value))


def _score_author_match_details(author: dict, context: dict,
                                original_name: str = "") -> tuple[float, dict]:
    """Return a normalized identity score and an explainable breakdown."""
    if not author:
        return 0.0, {}

    display_name = author.get("display_name") or ""
    name_score = 0.0
    if original_name and _name_match_ok(original_name, display_name):
        name_score = 30.0 if _canonical_name(original_name) == _canonical_name(display_name) else 24.0

    paper_institutions = context.get("institutions") or []
    candidate_institutions = [
        (inst or {}).get("display_name", "")
        for inst in (author.get("last_known_institutions") or [])
    ]
    best_institution_match = max(
        (
            _institution_similarity(paper_inst, candidate_inst)
            for paper_inst in paper_institutions
            for candidate_inst in candidate_institutions
        ),
        default=0.0,
    )
    scope = context.get("institution_scope", "global")
    institution_cap = 50.0 if scope == "author" else 15.0
    if best_institution_match >= 0.85:
        institution_score = institution_cap
    elif best_institution_match >= 0.65:
        institution_score = institution_cap * 0.72
    elif best_institution_match >= 0.45:
        institution_score = institution_cap * 0.4
    else:
        institution_score = 0.0

    topic_text = " ".join(
        (topic or {}).get("display_name", "").lower()
        for topic in (author.get("topics") or [])
    )
    topic_hits = sorted({
        keyword.lower() for keyword in (context.get("keywords") or [])
        if keyword and re.search(
            rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])",
            topic_text,
        )
    })
    topic_score = min(len(topic_hits) * 3.0, 15.0)
    score = round(min(100.0, name_score + institution_score + topic_score), 1)
    return score, {
        "name": round(name_score, 1),
        "institution": round(institution_score, 1),
        "institution_similarity": round(best_institution_match, 3),
        "topic": round(topic_score, 1),
        "topic_hits": topic_hits,
        "institution_scope": scope,
    }


def _score_author_match(author: dict, context: dict,
                        original_name: str = "") -> float:
    return _score_author_match_details(author, context, original_name)[0]


def _match_status(score: float, direct: bool = False) -> str:
    if direct:
        return "verified"
    if score >= 75:
        return "verified"
    if score >= 50:
        return "review"
    return "insufficient"


def _name_match_ok(original: str, found_display: str) -> bool:
    """Strictly match names while tolerating initials, accents, and CJK aliases."""
    if not found_display:
        return False

    def latin_parts(value: str) -> list[str]:
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii").lower()
        return re.findall(r"[a-z]+", value)

    original_parts = latin_parts(original)
    found_parts = latin_parts(found_display)
    if len(original_parts) < 2 or len(found_parts) < 2:
        return False
    original_joined = "".join(original_parts)
    found_joined = "".join(found_parts)
    if original_joined == found_joined:
        return True
    # Allow a database display name to contain extra non-Latin aliases, but
    # never accept a result merely because it shares the same surname.
    if all(part in found_parts for part in original_parts):
        return True
    if original_parts[-1] != found_parts[-1]:
        return False
    original_given = original_parts[:-1]
    found_given = found_parts[:-1]
    original_initials = "".join(part[0] for part in original_given)
    found_initials = "".join(part[0] for part in found_given)
    return (
        original_given[0] == found_given[0]
        or (len(original_given[0]) == 1
            and found_given[0].startswith(original_given[0]))
        or (len(found_given[0]) == 1
            and original_given[0].startswith(found_given[0]))
        or (
            original_initials == found_initials
            and (
                any(len(part) == 1 for part in original_given)
                or any(len(part) == 1 for part in found_given)
            )
        )
    )


async def _safe_get(url: str, session: aiohttp.ClientSession, timeout: int = 15) -> Optional[str]:
    """Safe GET request returning text or None."""
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status in (200, 301, 302):
                return await resp.text()
    except Exception:
        pass
    return None


# ─── LEVEL 1: Academic APIs ─────────────────────────────────────────────────


# ─── Institution domain lookup ──────────────────────────────────────────────

INSTITUTION_DOMAINS = {
    "cuhk": "cuhk.edu.hk",
    "chinese university of hong kong": "cuhk.edu.hk",
    "nebius": "nebius.com",
    "mit": "mit.edu",
    "stanford": "stanford.edu",
    "harvard": "harvard.edu",
    "berkeley": "berkeley.edu",
    "oxford": "ox.ac.uk",
    "cambridge": "cam.ac.uk",
    "princeton": "princeton.edu",
    "caltech": "caltech.edu",
    "eth": "ethz.ch",
    "tsinghua": "tsinghua.edu.cn",
    "peking": "pku.edu.cn",
    "pku": "pku.edu.cn",
    "zhejiang": "zju.edu.cn",
    "fudan": "fudan.edu.cn",
    "sjtu": "sjtu.edu.cn",
    "nus": "nus.edu.sg",
    "ntu": "ntu.edu.sg",
    "toronto": "utoronto.ca",
    "ucl": "ucl.ac.uk",
    "imperial": "imperial.ac.uk",
    "columbia": "columbia.edu",
    "yale": "yale.edu",
    "chicago": "uchicago.edu",
    "michigan": "umich.edu",
    "washington": "uw.edu",
    "google": "google.com",
    "microsoft": "microsoft.com",
    "ibm": "ibm.com",
    "meta": "meta.com",
    "nvidia": "nvidia.com",
    "openai": "openai.com",
    "deepmind": "deepmind.com",
    "amazon": "amazon.com",
    "apple": "apple.com",
}


def guess_institution_email(name: str, institution: str) -> Optional[str]:
    """Guess email from name + institution domain."""
    domain = None
    inst_lower = institution.lower()
    for key, dom in INSTITUTION_DOMAINS.items():
        if key in inst_lower or inst_lower in key:
            domain = dom
            break
    if not domain:
        # Try to extract domain-like pattern from institution
        parts = institution.lower().replace(" ", "").replace(",", "").split(".")
        # Common academic TLDs
        for tld in [".edu", ".ac.", ".edu.cn", ".edu.hk", ".edu.sg"]:
            if tld in institution.lower():
                break
        else:
            return None  # Can't guess
    if not domain:
        return None
    # Generate common email patterns
    name_parts = name.lower().replace(",", "").split()
    patterns = []
    if len(name_parts) >= 2:
        first = name_parts[0]
        last = name_parts[-1]
        patterns = [
            f"{first}.{last}@{domain}",
            f"{first[0]}{last}@{domain}",
            f"{first}{last[0]}@{domain}",
            f"{last}@{domain}",
            f"{first[0]}.{last}@{domain}",
            f"{last}.{first}@{domain}",
        ]
    elif len(name_parts) == 1:
        patterns = [f"{name_parts[0]}@{domain}"]
    return patterns[0] if patterns else None

def _profile_from_openalex_result(result: dict, name: str, context: dict,
                                  direct: bool = False) -> AuthorProfile:
    score, breakdown = _score_author_match_details(result, context, name)
    if direct:
        score = 100.0
        breakdown = {
            "direct_work_authorship": 100.0,
            "source": context.get("matched_work_source", "OpenAlex paper authorship"),
        }
    profile = AuthorProfile(
        name=result.get("display_name") or name,
        openalex_id=result.get("id") or "",
        orcid=result.get("orcid") or "",
        affiliations=[
            (inst or {}).get("display_name", "")
            for inst in (result.get("last_known_institutions") or [])
            if (inst or {}).get("display_name")
        ],
        topics=[
            (topic or {}).get("display_name", "")
            for topic in (result.get("topics") or [])[:5]
            if (topic or {}).get("display_name")
        ],
        cited_by_count=result.get("cited_by_count") or 0,
        works_count=result.get("works_count") or 0,
        match_score=score,
        match_status=_match_status(score, direct=direct),
        match_breakdown=breakdown,
    )
    profile.sources.append(
        f"OpenAlex: {profile.works_count} works, {profile.cited_by_count} citations, "
        f"score={profile.match_score} | https://openalex.org/{profile.openalex_id.split('/')[-1]}"
    )
    return profile


def _candidate_summary(profile: AuthorProfile) -> dict:
    return {
        "name": profile.name,
        "openalex_id": profile.openalex_id or "",
        "url": (
            f"https://openalex.org/{profile.openalex_id.split('/')[-1]}"
            if profile.openalex_id else ""
        ),
        "score": profile.match_score,
        "status": profile.match_status,
        "affiliations": profile.affiliations,
        "match_breakdown": profile.match_breakdown,
        "works_count": profile.works_count,
        "cited_by_count": profile.cited_by_count,
    }


async def fetch_openalex_author(openalex_id: str, name: str,
                                session: aiohttp.ClientSession,
                                context: Optional[dict] = None) -> Optional[AuthorProfile]:
    author_id = (openalex_id or "").rstrip("/").split("/")[-1]
    if not author_id:
        return None
    try:
        async with session.get(
            f"{OPENALEX_BASE}/authors/{author_id}",
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            result = await resp.json()
            return _profile_from_openalex_result(
                result, name, context or {}, direct=True
            )
    except Exception as exc:
        print(f"OpenAlex direct author lookup failed for '{name}': {exc}")
        return None


async def search_openalex_authors(name: str, session: aiohttp.ClientSession,
                                   context: Optional[dict] = None) -> list[AuthorProfile]:
    profiles = []
    context = context or {}
    url = f"{OPENALEX_BASE}/authors?search={quote(name)}&per_page=10"
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return profiles
            data = await resp.json()
            if not data:
                return profiles
            scored = []
            for result in (data.get("results") or []):
                display_name = result.get("display_name") or ""
                if not _name_match_ok(name, display_name):
                    continue  # Skip if names don't match at all
                profile = _profile_from_openalex_result(
                    result, name, context, direct=False
                )
                scored.append((
                    profile.match_score,
                    profile.cited_by_count,
                    profile,
                ))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            profiles = [profile for _, _, profile in scored]
    except Exception as e:
        print(f"OpenAlex failed for '{name}': {e}")
    return profiles


async def search_openalex_coauthors(openalex_id: str, session: aiohttp.ClientSession) -> list[str]:
    coauthors = set()
    author_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id
    url = (f"{OPENALEX_BASE}/works?filter=authorships.author.id:{author_id}"
           f"&per_page=25&sort=cited_by_count:desc")
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for work in (data.get("results") or []):
                    for authorship in (work.get("authorships") or []):
                        auth = authorship.get("author") or {}
                        n, aid = auth.get("display_name") or "", auth.get("id") or ""
                        if aid and aid != openalex_id:
                            coauthors.add(n)
    except Exception:
        pass
    return list(coauthors)[:30]


async def extract_email_from_orcid(orcid_url: str, session: aiohttp.ClientSession) -> Optional[str]:
    if not orcid_url:
        return None
    orcid_id = orcid_url.rstrip("/").split("/")[-1]
    try:
        async with session.get(f"{ORCID_BASE}/{orcid_id}/person",
                               headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for e in (data.get("emails") or {}).get("email") or []:
                    if e.get("email"):
                        return e["email"]
    except Exception:
        pass
    return None


# ─── LEVEL 2: Google Scholar (deep) ─────────────────────────────────────────

async def search_scholar_deep(name: str, session: aiohttp.ClientSession) -> dict:
    """Find Scholar profile AND scrape it for email."""
    result = {"url": None, "email": None, "source": None}
    search_url = (f"https://scholar.google.com/citations"
                  f"?view_op=search_authors&mauthors={quote(name)}&hl=en")
    try:
        text = await _safe_get(search_url, session, timeout=15)
        if not text:
            return result
        soup = BeautifulSoup(text, "html.parser")
        profile_link = soup.select_one("h3.gs_ai_name a")
        if not profile_link:
            return result
        href = profile_link.get("href", "")
        if not href:
            return result
        profile_url = f"https://scholar.google.com{href}" if href.startswith("/") else href
        result["url"] = profile_url
        result["source"] = profile_url
        # Now VISIT the profile page and extract email
        profile_text = await _safe_get(profile_url, session, timeout=15)
        if not profile_text:
            return result
        emails = EMAIL_PATTERN.findall(profile_text)
        for e in emails:
            if "@" in e and "." in e.split("@")[-1]:
                prefix = e.split("@")[0].lower()
                if not any(bad in prefix for bad in BAD_EMAILS):
                    result["email"] = e
                    result["source"] = f"Google Scholar profile: {profile_url}"
                    break
    except Exception:
        pass
    return result


# ─── LEVEL 3: ResearchGate profile scraping ─────────────────────────────────

async def search_researchgate_deep(name: str, session: aiohttp.ClientSession) -> dict:
    """Find ResearchGate profile and try to extract email."""
    result = {"url": None, "email": None}
    search_url = f"https://www.researchgate.net/search/publication?q={quote(name)}"
    try:
        text = await _safe_get(search_url, session, timeout=15)
        if not text:
            return result
        soup = BeautifulSoup(text, "html.parser")
        for link in soup.select('a[href*="/profile/"]'):
            href = link.get("href", "")
            if href and "/profile/" in href:
                result["url"] = (f"https://www.researchgate.net{href}"
                                 if href.startswith("/") else href)
                break
        if result["url"]:
            # Visit profile
            profile_text = await _safe_get(result["url"], session, timeout=15)
            if profile_text:
                emails = EMAIL_PATTERN.findall(profile_text)
                for e in emails:
                    if "@" in e and "." in e.split("@")[-1]:
                        prefix = e.split("@")[0].lower()
                        if not any(bad in prefix for bad in BAD_EMAILS):
                            result["email"] = e
                            break
    except Exception:
        pass
    return result


# ─── LEVEL 4: University website contact search ─────────────────────────────

async def search_university_contact(name: str, affiliation: str,
                                     session: aiohttp.ClientSession) -> dict:
    """Search university websites for contact/people pages with email."""
    result = {"email": None, "source": None, "url": None}
    if not affiliation:
        return result
    
    queries = [
        f'"{name}" email {affiliation}',
        f'{name} {affiliation} contact',
        f'site:.edu "{name}" email',
        f'{affiliation} {name} faculty',
    ]
    
    for q in queries[:3]:  # Limit to 3 queries
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote(q)}"
            text = await _safe_get(url, session, timeout=10)
            if not text:
                continue
            soup = BeautifulSoup(text, "html.parser")
            # Extract emails from search results
            all_text = soup.get_text()
            emails = EMAIL_PATTERN.findall(all_text)
            for e in emails:
                if "@" in e and "." in e.split("@")[-1]:
                    prefix = e.split("@")[0].lower()
                    if not any(bad in prefix for bad in BAD_EMAILS):
                        result["email"] = e
                        result["source"] = f"University web search: {q}"
                        break
            if result["email"]:
                break
            # Also grab first search result link
            for link in soup.select("a.result__a"):
                href = link.get("href", "")
                if ".edu" in href or affiliation.lower().split()[0] in href.lower():
                    result["url"] = href
                    break
            if result["url"] and not result["email"]:
                # Try visiting the actual page
                page_text = await _safe_get(result["url"], session, timeout=10)
                if page_text:
                    page_emails = EMAIL_PATTERN.findall(page_text)
                    for e in page_emails:
                        if "@" in e and "." in e.split("@")[-1]:
                            prefix = e.split("@")[0].lower()
                            if not any(bad in prefix for bad in BAD_EMAILS):
                                result["email"] = e
                                result["source"] = result["url"]
                                break
            if result["email"]:
                break
        except Exception:
            continue
    return result


# ─── LEVEL 5: Social media ──────────────────────────────────────────────────

async def search_social_deep(name: str, session: aiohttp.ClientSession) -> dict:
    """Search Twitter/X and LinkedIn profiles."""
    result = {"twitter_url": None, "linkedin_url": None}
    platforms = [("linkedin.com/in/", "linkedin_url"), ("twitter.com/", "twitter_url"), ("x.com/", "twitter_url")]
    for platform, key in platforms:
        if result[key]:
            continue
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote(f'{name} {platform} researcher')}"
            text = await _safe_get(url, session, timeout=10)
            if not text:
                continue
            soup = BeautifulSoup(text, "html.parser")
            for link in soup.select("a.result__a"):
                href = link.get("href", "")
                if platform in href and not result[key]:
                    result[key] = href
                    break
        except Exception:
            pass
    return result


# ─── FULL CRAWL PIPELINE ────────────────────────────────────────────────────


# ─── LEVEL 6: arXiv API search ─────────────────────────────────────────────

async def search_arxiv_for_email(name: str, institution: str,
                                  session: aiohttp.ClientSession) -> dict:
    """Search arXiv, accepting only email addresses attributable to the author."""
    result = {
        "email": None,
        "source": None,
        "papers_found": 0,
        "matched_papers": 0,
        "rejected_candidates": 0,
    }
    try:
        # Search arXiv API
        query = quote(f'au:"{name}" AND all:"{institution}"')
        url = f"http://export.arxiv.org/api/query?search_query={query}&max_results=5"
        text = await _safe_get(url, session, timeout=15)
        if not text:
            return result
        parsed = _extract_arxiv_email_from_feed(text, name)
        result.update(parsed)
        if result["papers_found"] == 0:
            # Try broader search: just the name
            url2 = f"http://export.arxiv.org/api/query?search_query=au:{quote(name)}&max_results=5"
            text = await _safe_get(url2, session, timeout=15)
            if text:
                result.update(_extract_arxiv_email_from_feed(text, name))
    except Exception:
        pass
    return result


def _email_localpart_matches_author(email: str, name: str) -> bool:
    """Conservatively require an arXiv email local-part to identify the author."""
    local_part = email.split("@", 1)[0].lower()
    compact_local = "".join(re.findall(r"[a-z]+", local_part))
    if not compact_local:
        return False
    if any(term in compact_local for term in NON_PERSONAL_EMAIL_TERMS):
        return False
    if any(bad.replace("-", "") in compact_local for bad in BAD_EMAILS):
        return False

    name_parts = re.findall(
        r"[a-z]+",
        unicodedata.normalize("NFKD", name or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower(),
    )
    if not name_parts:
        return False

    # Requiring the family name deliberately favors a missing email over
    # assigning another author's or a collaboration's address.
    family_name = name_parts[-1]
    return len(family_name) >= 2 and family_name in compact_local


def _extract_arxiv_email_from_feed(text: str, name: str) -> dict:
    """Parse Atom entries separately and keep only author-attributable emails."""
    result = {
        "email": None,
        "source": None,
        "papers_found": 0,
        "matched_papers": 0,
        "rejected_candidates": 0,
    }
    if not text:
        return result
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return result

    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{atom}entry")
    result["papers_found"] = len(entries)
    for entry in entries:
        author_names = [
            (author.findtext(f"{atom}name") or "").strip()
            for author in entry.findall(f"{atom}author")
        ]
        if not any(_name_match_ok(name, author_name) for author_name in author_names):
            continue
        result["matched_papers"] += 1

        entry_text = " ".join(entry.itertext())
        for email in dict.fromkeys(EMAIL_PATTERN.findall(entry_text)):
            if not _email_localpart_matches_author(email, name):
                result["rejected_candidates"] += 1
                continue
            result["email"] = email
            entry_id = (entry.findtext(f"{atom}id") or "").strip()
            result["source"] = (
                f"arXiv paper: {entry_id}" if entry_id else "arXiv paper metadata"
            )
            return result
    return result


async def search_homepage_for_email(name: str, institution: str,
                                     session: aiohttp.ClientSession) -> dict:
    """Search for personal academic homepage and extract email."""
    result = {"email": None, "source": None, "url": None}
    queries = [
        f'"{name}" {institution} homepage',
        f'site:.edu "{name}" contact',
        f'{institution} {name} faculty email',
    ]
    for q in queries[:2]:
        try:
            text = await _safe_get(
                f"https://html.duckduckgo.com/html/?q={quote(q)}",
                session, timeout=10
            )
            if not text:
                continue
            soup = BeautifulSoup(text, "html.parser")
            all_text = soup.get_text()
            # Extract emails
            emails = EMAIL_PATTERN.findall(all_text)
            for e in emails:
                prefix = e.split("@")[0].lower()
                if not any(bad in prefix for bad in BAD_EMAILS):
                    result["email"] = e
                    result["source"] = f"Homepage search: {q}"
                    break
            if result["email"]:
                break
            # Grab first result link for deeper search
            for link in soup.select("a.result__a"):
                href = link.get("href", "")
                if not href:
                    continue
                # Visit the linked page
                page = await _safe_get(href, session, timeout=10)
                if page:
                    page_emails = EMAIL_PATTERN.findall(page)
                    for e in page_emails:
                        prefix = e.split("@")[0].lower()
                        if not any(bad in prefix for bad in BAD_EMAILS):
                            result["email"] = e
                            result["source"] = href
                            break
                if result["email"]:
                    break
        except Exception:
            continue
    return result


async def search_institution_directory(name: str, institution: str,
                                        session: aiohttp.ClientSession) -> dict:
    """Search institutional directory for email (e.g., cuhk.edu.hk people page)."""
    result = {"email": None, "source": None}
    if not institution:
        return result
    # Try to guess the domain for a site:-restricted search
    for key, dom in INSTITUTION_DOMAINS.items():
        if key in institution.lower():
            query = f'site:{dom} "{name}" email'
            try:
                text = await _safe_get(
                    f"https://html.duckduckgo.com/html/?q={quote(query)}",
                    session, timeout=10
                )
                if not text:
                    continue
                soup = BeautifulSoup(text, "html.parser")
                all_text = soup.get_text()
                emails = EMAIL_PATTERN.findall(all_text)
                for e in emails:
                    prefix = e.split("@")[0].lower()
                    if dom in e and not any(bad in prefix for bad in BAD_EMAILS):
                        result["email"] = e
                        result["source"] = f"Directory search: site:{dom}"
                        break
                if result["email"]:
                    break
            except Exception:
                continue
    return result


async def crawl_author_full(name: str, context: Optional[dict] = None) -> AuthorProfile:
    """Deep crawl for one author across all available sources."""
    context = context or {}
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Level 1: Prefer the author ID attached to the resolved paper.
        author_context = dict(context)
        specific_institutions = (
            context.get("author_institutions_map") or {}
        ).get(name)
        if specific_institutions:
            author_context["institutions"] = specific_institutions
            author_context["institution_scope"] = "author"
        else:
            author_context["institution_scope"] = "global"

        direct_identity = (
            context.get("openalex_authors_map") or {}
        ).get(name)
        profile = AuthorProfile(name=name)
        oa_results = []
        if direct_identity and direct_identity.get("openalex_id"):
            profile = await fetch_openalex_author(
                direct_identity["openalex_id"], name, session, author_context
            ) or AuthorProfile(
                name=name,
                openalex_id=direct_identity.get("openalex_id"),
                orcid=direct_identity.get("orcid"),
                affiliations=direct_identity.get("affiliations") or [],
                match_score=100.0,
                match_status="verified",
                match_breakdown={
                    "direct_work_authorship": 100.0,
                    "source": direct_identity.get("source", "OpenAlex paper authorship"),
                },
            )
            profile.sources.append(
                "Identity verified from the matched paper authorship: "
                f"{direct_identity.get('source', 'OpenAlex')}"
            )
            profile.candidates = [_candidate_summary(profile)]
        else:
            oa_results = await search_openalex_authors(
                name, session, author_context
            )
            candidate_summaries = [
                _candidate_summary(candidate) for candidate in oa_results[:3]
            ]
            if oa_results and oa_results[0].match_status == "verified":
                profile = oa_results[0]
                profile.candidates = candidate_summaries
            elif oa_results:
                best = oa_results[0]
                profile.match_score = best.match_score
                profile.match_status = best.match_status
                profile.match_breakdown = best.match_breakdown
                profile.candidates = candidate_summaries
                profile.sources.append(
                    "OpenAlex candidate not automatically adopted: "
                    f"best score={best.match_score}, status={best.match_status}. "
                    "Paper identity preserved for review."
                )
            else:
                profile.match_status = "not-found"
                profile.sources.append(
                    "No name-compatible OpenAlex candidate was found."
                )

        # Preserve the exact name supplied by the paper. External databases
        # may add aliases or return a differently formatted display name.
        profile.name = name
        if specific_institutions:
            # The uploaded paper is authoritative for affiliation in this
            # publication; do not show unrelated affiliations from a namesake.
            profile.affiliations = specific_institutions

        paper_email = (context.get("author_email_map") or {}).get(name)
        if paper_email:
            profile.email = paper_email
            profile.email_source = "Uploaded paper PDF"
            profile.sources.append(f"Email printed in uploaded paper: {paper_email}")

        identity_verified = profile.match_status == "verified"
        if identity_verified:
            # Only enrich external profiles after identity verification.
            scholar_task = search_scholar_deep(name, session)
            rg_task = search_researchgate_deep(name, session)
            social_task = search_social_deep(name, session)
            scholar, rg, social = await asyncio.gather(
                scholar_task, rg_task, social_task
            )

            if scholar.get("url"):
                profile.google_scholar_url = scholar["url"]
                profile.sources.append(f"Google Scholar: {scholar['url']}")
            if scholar.get("email") and not profile.email:
                profile.email = scholar["email"]
                profile.email_source = scholar.get("source", "Google Scholar")
                profile.sources.append(f"Email from: {profile.email_source}")

            if rg.get("url"):
                profile.researchgate_url = rg["url"]
                profile.sources.append(f"ResearchGate: {rg['url']}")
            if rg.get("email") and not profile.email:
                profile.email = rg["email"]
                profile.email_source = "ResearchGate profile"
                profile.sources.append(f"Email from ResearchGate: {rg['url']}")

            profile.twitter_url = social.get("twitter_url")
            profile.linkedin_url = social.get("linkedin_url")
            if profile.twitter_url:
                profile.sources.append(f"Twitter/X: {profile.twitter_url}")
            if profile.linkedin_url:
                profile.sources.append(f"LinkedIn: {profile.linkedin_url}")
        else:
            profile.sources.append(
                "External profile searches skipped until an OpenAlex identity "
                "candidate is confirmed."
            )

        # Level 4 (only if no email yet): University contact search
        if identity_verified and not profile.email and profile.affiliations:
            aff = profile.affiliations[0]
            uni_result = await search_university_contact(name, aff, session)
            if uni_result.get("email"):
                profile.email = uni_result["email"]
                profile.email_source = uni_result.get("source", f"University search: {aff}")
                profile.sources.append(f"Email from: {profile.email_source}")
            if uni_result.get("url"):
                profile.website_url = uni_result["url"]
                profile.sources.append(f"Web: {uni_result['url']}")

        # Level 6: Real email verification (arXiv, homepage, directory)
        if identity_verified and not profile.email:
            # Determine best institution: name_match > context > affiliations
            name_inst_map = context.get("name_institution_map") or {}
            inst_for_search = name_inst_map.get(name)
            if not inst_for_search and specific_institutions:
                inst_for_search = specific_institutions[0]
            if not inst_for_search:
                insts = context.get("institutions") or []
                inst_for_search = insts[0] if insts else ""
            if not inst_for_search and profile.affiliations:
                inst_for_search = profile.affiliations[0]

            if inst_for_search:
                # Step A: arXiv paper search (real email metadata)
                arxiv = await search_arxiv_for_email(name, inst_for_search, session)
                if arxiv.get("email"):
                    profile.email = arxiv["email"]
                    profile.email_source = arxiv.get("source", "arXiv")
                    profile.sources.append(
                        f"Email from arXiv ({arxiv.get('papers_found',0)} papers): {profile.email}"
                    )

                # Step B: Homepage search (real scraped email)
                if not profile.email:
                    hp = await search_homepage_for_email(name, inst_for_search, session)
                    if hp.get("email"):
                        profile.email = hp["email"]
                        profile.email_source = hp.get("source", "Homepage")
                        profile.sources.append(f"Email from homepage: {profile.email}")

                # Step C: Institution directory search
                if not profile.email:
                    dir_result = await search_institution_directory(
                        name, inst_for_search, session
                    )
                    if dir_result.get("email"):
                        profile.email = dir_result["email"]
                        profile.email_source = dir_result.get("source", "Directory")
                        profile.sources.append(f"Email from directory: {profile.email}")

                # Never put a pattern-generated address into the contact field.
                # A missing email is safer than an invented address.

        # Level 7: ORCID email (if still no email)
        if identity_verified and not profile.email and profile.orcid:
            orcid_email = await extract_email_from_orcid(profile.orcid, session)
            if orcid_email:
                profile.email = orcid_email
                profile.email_source = f"ORCID: {profile.orcid}"
                profile.sources.append(f"Email from ORCID: {profile.orcid}")

        # Get co-authors
        if identity_verified and profile.openalex_id:
            coauthors = await search_openalex_coauthors(profile.openalex_id, session)
            profile.coauthors = coauthors
            profile.sources.append(f"Co-authors: {len(coauthors)} via OpenAlex")

        return profile


async def crawl_multiple_authors(names: list[str], context: Optional[dict] = None) -> list[AuthorProfile]:
    results = []
    for i in range(0, len(names), 3):
        batch = names[i:i + 3]
        tasks = [crawl_author_full(name, context) for name in batch]
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if isinstance(r, AuthorProfile):
                results.append(r)
        if i + 3 < len(names):
            await asyncio.sleep(1.5)
    return results
