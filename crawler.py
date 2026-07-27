"""Crawler: deep search for author contact info across multiple sources."""

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field, asdict
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

    def to_dict(self):
        return asdict(self)


OPENALEX_BASE = "https://api.openalex.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
BAD_EMAILS = {'noreply', 'no-reply', 'support', 'info', 'contact', 'admin', 'webmaster', 'help', 'sales', 'marketing'}


def _score_author_match(author: dict, context: dict) -> float:
    if not context:
        return 0.0
    score = 0.0
    affiliations = [(inst or {}).get("display_name", "").lower()
                    for inst in (author.get("last_known_institutions") or [])]
    all_aff_text = " ".join(affiliations)
    for inst in (context.get("institutions") or []):
        inst_lower = inst.lower()
        for aff in affiliations:
            if inst_lower in aff or aff in inst_lower:
                score += 80
                break
        if inst_lower in all_aff_text:
            score += 40
    topic_text = " ".join(
        (topic or {}).get("display_name", "").lower()
        for topic in (author.get("topics") or [])
    )
    topic_hits = sum(
        1 for kw in (context.get("keywords") or [])
        if kw.lower() in topic_text
    )
    score += min(topic_hits * 5, 20)
    score += min((author.get("cited_by_count") or 0) / 2000, 20)
    score += min((author.get("works_count") or 0) / 50, 10)
    return score


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

async def search_openalex_authors(name: str, session: aiohttp.ClientSession,
                                   context: Optional[dict] = None) -> list[AuthorProfile]:
    profiles = []
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
                profile = AuthorProfile(
                    name=display_name,
                    openalex_id=result.get("id") or "",
                    orcid=result.get("orcid") or "",
                    affiliations=[(inst or {}).get("display_name", "")
                                  for inst in (result.get("last_known_institutions") or [])],
                    topics=[(t or {}).get("display_name", "")
                            for t in (result.get("topics") or [])[:5]],
                    cited_by_count=result.get("cited_by_count") or 0,
                    works_count=result.get("works_count") or 0,
                    match_score=round(_score_author_match(result, context), 1),
                )
                profile.sources.append(
                    f"OpenAlex: {profile.works_count} works, {profile.cited_by_count} citations, "
                    f"score={profile.match_score} | https://openalex.org/{result.get('id','').split('/')[-1]}"
                )
                scored.append((profile.match_score, profile))
            scored.sort(key=lambda x: x[0], reverse=True)
            profiles = [p for _, p in scored]
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
    """Search arXiv for papers by this author to extract email from metadata."""
    result = {"email": None, "source": None, "papers_found": 0}
    try:
        # Search arXiv API
        query = quote(f'au:"{name}" AND all:"{institution}"')
        url = f"http://export.arxiv.org/api/query?search_query={query}&max_results=5"
        text = await _safe_get(url, session, timeout=15)
        if not text:
            return result
        result["papers_found"] = text.count("<entry>")
        if result["papers_found"] == 0:
            # Try broader search: just the name
            url2 = f"http://export.arxiv.org/api/query?search_query=au:{quote(name)}&max_results=5"
            text = await _safe_get(url2, session, timeout=15)
            if text:
                result["papers_found"] = text.count("<entry>")
        # Extract emails from the XML response
        emails = EMAIL_PATTERN.findall(text or "")
        for e in emails:
            prefix = e.split("@")[0].lower()
            if not any(bad in prefix for bad in BAD_EMAILS):
                result["email"] = e
                result["source"] = f"arXiv paper metadata"
                break
    except Exception:
        pass
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
        # Level 1: OpenAlex - skip low confidence matches
        author_context = dict(context)
        specific_institutions = (
            context.get("author_institutions_map") or {}
        ).get(name)
        if specific_institutions:
            author_context["institutions"] = specific_institutions
        oa_results = await search_openalex_authors(name, session, author_context)
        profile = AuthorProfile(name=name)
        has_paper_institutions = bool(author_context.get("institutions"))
        minimum_match_score = 80 if has_paper_institutions else 20
        if oa_results and oa_results[0].match_score >= minimum_match_score:
            profile = oa_results[0]
        elif oa_results:
            # Keep the paper identity rather than adopting a same-name person
            # whose institution does not match the uploaded publication.
            profile.sources.append(
                f"OpenAlex candidates rejected: best score="
                f"{oa_results[0].match_score} (< {minimum_match_score}); "
                "paper name preserved without unverified profile enrichment."
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

        # Level 2-5: Run in parallel
        scholar_task = search_scholar_deep(name, session)
        rg_task = search_researchgate_deep(name, session)
        social_task = search_social_deep(name, session)

        scholar, rg, social = await asyncio.gather(
            scholar_task, rg_task, social_task
        )

        # Process Google Scholar results
        if scholar.get("url"):
            profile.google_scholar_url = scholar["url"]
            profile.sources.append(f"Google Scholar: {scholar['url']}")
        if scholar.get("email") and not profile.email:
            profile.email = scholar["email"]
            profile.email_source = scholar.get("source", "Google Scholar")
            profile.sources.append(f"Email from: {profile.email_source}")

        # Process ResearchGate
        if rg.get("url"):
            profile.researchgate_url = rg["url"]
            profile.sources.append(f"ResearchGate: {rg['url']}")
        if rg.get("email") and not profile.email:
            profile.email = rg["email"]
            profile.email_source = "ResearchGate profile"
            profile.sources.append(f"Email from ResearchGate: {rg['url']}")

        # Process social
        profile.twitter_url = social.get("twitter_url")
        profile.linkedin_url = social.get("linkedin_url")
        if profile.twitter_url:
            profile.sources.append(f"Twitter/X: {profile.twitter_url}")
        if profile.linkedin_url:
            profile.sources.append(f"LinkedIn: {profile.linkedin_url}")

        # Level 4 (only if no email yet): University contact search
        if not profile.email and profile.affiliations:
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
        if not profile.email:
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
        if not profile.email and profile.orcid:
            orcid_email = await extract_email_from_orcid(profile.orcid, session)
            if orcid_email:
                profile.email = orcid_email
                profile.email_source = f"ORCID: {profile.orcid}"
                profile.sources.append(f"Email from ORCID: {profile.orcid}")

        # Get co-authors
        if profile.openalex_id:
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
