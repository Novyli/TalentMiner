"""Crawler: deep search for author contact info across multiple sources."""

import asyncio
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup


@dataclass
class AuthorProfile:
    name: str
    name_aliases: list = field(default_factory=list)
    openalex_id: Optional[str] = None
    orcid: Optional[str] = None
    email: Optional[str] = None
    email_source: Optional[str] = None
    email_confidence: float = 0.0
    phone: Optional[str] = None
    phone_source: Optional[str] = None
    phone_confidence: float = 0.0
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
    coauthor_details: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    match_score: float = 0.0
    match_status: str = "unmatched"
    match_breakdown: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    institution_records: list = field(default_factory=list)
    institution_sites: list = field(default_factory=list)
    contact_candidates: list = field(default_factory=list)
    search_trail: list = field(default_factory=list)
    career_stage: str = "unknown"
    degree_type: str = ""
    graduation_score: float = 0.0
    graduation_status: str = "insufficient"
    expected_graduation_year: Optional[int] = None
    graduation_evidence: list = field(default_factory=list)
    china_link_score: float = 0.0
    china_link_status: str = "none"
    china_link_evidence: list = field(default_factory=list)
    nationality: str = ""
    nationality_evidence: list = field(default_factory=list)
    academic_timeline: dict = field(default_factory=dict)
    graduate_candidates: list = field(default_factory=list)
    lab_members: list = field(default_factory=list)
    lab_name: str = ""
    lab_url: str = ""
    directory_url: str = ""
    lab_pi: str = ""
    identity_status: str = "unresolved"
    contact_search_status: str = "not-started"
    search_failure_reason: str = ""
    pages_checked: int = 0
    student_score: float = 0.0
    student_status: str = "unknown"
    student_evidence: list = field(default_factory=list)
    discovery_origin: str = "paper-author"
    # Provenance retained when multiple paper projects are shown together.
    paper_count: int = 0
    paper_titles: list = field(default_factory=list)
    paper_dois: list = field(default_factory=list)
    source_files: list = field(default_factory=list)
    source_project_ids: list = field(default_factory=list)
    papers: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


OPENALEX_BASE = "https://api.openalex.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DISABLED_SEARCH_PROVIDERS = set()
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
CHINA_MOBILE_PATTERN = re.compile(r'(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)')
LABELED_PHONE_PATTERN = re.compile(
    r'(?:手机|移动电话|联系电话|电话|办公电话|Tel(?:ephone)?|Phone|Mobile)'
    r'\s*[:：]?\s*(\+?[0-9][0-9()\-\s]{5,22}[0-9])',
    re.IGNORECASE,
)
BAD_EMAILS = {'noreply', 'no-reply', 'support', 'info', 'contact', 'admin', 'webmaster', 'help', 'sales', 'marketing'}
NON_PERSONAL_EMAIL_TERMS = {
    "committee", "committees", "collaboration", "collaborations",
    "publication", "publications", "office", "secretariat", "team",
    "mailinglist", "newsletter", "communication", "communications",
    "media", "press", "outreach", "admission", "admissions", "recruiting",
    "humanresources", "careers", "jobs",
}
QUANTUM_DEPARTMENT_TERMS = {
    "quantum", "quantum information", "quantum science", "quantum technology",
    "quantum optics", "atomic molecular optical", "amo", "量子", "量子信息",
    "量子科学", "量子技术", "量子光学",
}
PHYSICS_DEPARTMENT_TERMS = {
    "physics", "physical sciences", "photonics", "optics", "atomic physics",
    "department of physics", "school of physics", "物理", "物理学院",
    "物理系", "光学", "光子学",
}
QUANTUM_PHYSICS_RELEVANCE_TERMS = {
    "quantum", "qubit", "entanglement", "photon", "photonic", "optics",
    "atomic", "condensed matter", "topological", "superconduct",
    "semiconductor", "spin", "many-body", "many body", "black hole",
    "cosmology", "gravitation", "gravity", "field theory", "particle physics",
    "量子", "光子", "光学", "凝聚态", "拓扑", "超导", "半导体", "引力",
}
DIRECTORY_LINK_TERMS = {
    "people", "person", "faculty", "staff", "directory", "profile", "member",
    "team", "group", "lab", "laboratory", "researcher", "contact",
    "师资", "教师", "人员", "成员", "团队", "实验室", "研究组", "个人主页",
    "联系方式", "量子", "物理",
}
MAX_INSTITUTION_SITES = 3
MAX_OFFICIAL_PAGES = 12
MAX_SEARCH_TRAIL = 60
MAX_GRADUATE_CANDIDATES = 30

DOCTORAL_ROLE_PATTERNS = [
    r"\bph\.?d\.?\s+(?:student|candidate|researcher)\b",
    r"\bdoctoral\s+(?:student|candidate|researcher)\b",
    r"\bgraduate\s+student\b",
    r"博士研究生", r"在读博士", r"博士生", r"博士候选人",
]
MASTERS_ROLE_PATTERNS = [
    r"\bmaster(?:'s|s)?\s+(?:student|candidate)\b",
    r"\bm\.?sc\.?\s+student\b",
    r"硕士研究生", r"在读硕士", r"硕士生",
]
NON_STUDENT_ROLE_PATTERNS = [
    r"\bpostdoc(?:toral)?\b",
    r"\b(?:assistant|associate|full|emeritus)\s+professor\b",
    r"\bprofessor\s+of\b", r"\bfaculty\s+member\b",
    r"\bprincipal investigator\b", r"\bresearch scientist\b",
    r"博士后", r"现任[^。；]{0,20}教授", r"职称[^。；]{0,10}教授",
    r"研究员", r"副研究员",
]
FORMER_STUDENT_PATTERNS = [
    r"\bformer\s+(?:ph\.?d\.?|doctoral|master(?:'s|s)?|graduate)\s+student\b",
    r"\b(?:ph\.?d\.?|doctoral|master(?:'s|s)?)\s+(?:graduate|alumn(?:us|a|i))\b",
    r"\b(?:is|was|former)\s+(?:an?\s+)?alumn(?:us|a)\b",
    r"\bgraduated\s+(?:in|from|with)\b",
    r"曾为[^ 。；]{0,30}(?:博士生|硕士生|研究生)",
    r"(?:已毕业|往届学生|校友)",
]
UPCOMING_GRADUATION_PATTERNS = [
    r"(?:expected|anticipated)\s+(?:graduation|completion)[^.;。；]{0,30}?(20\d{2})",
    r"(?:graduating|graduate)\s+(?:in\s+)?(20\d{2})",
    r"(?:预计|预期|计划)[^。；]{0,12}?(?:毕业|完成学位)[^。；]{0,10}?(20\d{2})",
    r"(20\d{2})[^。；]{0,10}?(?:预计|计划)?毕业",
]
DEFENSE_JOB_PATTERNS = [
    r"\b(?:thesis|dissertation)\s+defen[cs]e\b",
    r"\bjob\s+market\b", r"\bseeking\s+(?:a\s+)?position\b",
    r"博士论文答辩", r"学位论文答辩", r"求职", r"寻找职位",
]
ENROLLMENT_PATTERNS = [
    r"(?:joined|started|enrolled|since)[^.;。；]{0,24}?(20\d{2})",
    r"(20\d{2})[^。；]{0,15}?(?:入学|加入|开始攻读)",
]
EXPLICIT_CHINESE_NATIONALITY_PATTERNS = [
    r"\bnationality\s*[:：]\s*(?:china|chinese|prc)\b",
    r"国籍\s*[:：]\s*中国",
]
CHINA_EDUCATION_PATTERNS = [
    r"(?:b\.?s\.?|bachelor|undergraduate|m\.?s\.?|master|硕士|本科|学士)[^.;。；]{0,100}?(?:china|chinese|中国|清华|北大|北京大学|中国科学技术大学|中科大|浙江大学|复旦大学|上海交通大学|南京大学)",
    r"(?:graduated|degree|education|教育经历|毕业于)[^.;。；]{0,100}?(?:china|中国|清华|北京大学|中国科学技术大学|浙江大学|复旦大学|上海交通大学|南京大学)",
]


INSTITUTION_STOPWORDS = {
    "and", "the", "of", "for", "at", "in", "department", "faculty",
    "school", "college", "university", "institute", "institution",
    "laboratory", "laboratories", "lab", "center", "centre", "physics",
    "science", "sciences", "research", "technology", "china", "usa", "uk",
}
DISTINCT_INSTITUTION_TOKENS = {
    "jila", "nist", "mit", "eth", "cuhk", "caltech", "stanford",
    "harvard", "princeton", "berkeley",
}
INSTITUTION_ALIASES = {
    "hong kong university of science and technology": "hkust",
    "university of science and technology of china": "ustc",
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


def _author_institution_records(author: dict) -> list[dict]:
    """Return current/recent institution records across OpenAlex schema versions."""
    direct = [
        inst for inst in (author.get("last_known_institutions") or [])
        if (inst or {}).get("id") or (inst or {}).get("display_name")
    ]
    if direct:
        return direct

    affiliations = author.get("affiliations") or []
    latest_year = max(
        (max(item.get("years") or [0]) for item in affiliations),
        default=0,
    )
    recent = []
    for item in affiliations:
        institution = item.get("institution") or {}
        years = item.get("years") or []
        if not institution or not years:
            continue
        if max(years) >= latest_year - 2:
            recent.append(institution)
    return recent


def _dedupe_institution_records(records: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for record in records:
        key = record.get("id") or record.get("ror") or record.get("display_name")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append({
            "id": record.get("id") or "",
            "ror": record.get("ror") or "",
            "display_name": record.get("display_name") or "",
            "country_code": record.get("country_code") or "",
            "type": record.get("type") or "",
        })
    return deduped


def _canonical_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _same_official_domain(url: str, domain: str) -> bool:
    host = _canonical_host(url)
    domain = (domain or "").lower().lstrip(".")
    return bool(host and domain and (host == domain or host.endswith(f".{domain}")))


def _unwrap_search_result_url(href: str) -> str:
    """Extract the destination URL from DuckDuckGo redirect links."""
    if not href:
        return ""
    href = unquote(href)
    parsed = urlparse(href)
    target = (parse_qs(parsed.query).get("uddg") or [""])[0]
    if target:
        return unquote(target)
    if href.startswith("//"):
        return f"https:{href}"
    return href if href.startswith(("http://", "https://")) else ""


def _page_mentions_author(page_text: str, name: str) -> bool:
    if not page_text or not name:
        return False
    if any("\u4e00" <= char <= "\u9fff" for char in name) and name in page_text:
        return True
    canonical_page = _canonical_name(page_text)
    canonical_person = _canonical_name(name)
    if canonical_person and canonical_person in canonical_page:
        return True
    parts = re.findall(
        r"[a-z]+",
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower(),
    )
    without_initials = "".join(part for part in parts if len(part) > 1)
    return bool(
        len(parts) >= 2
        and any(len(part) == 1 for part in parts[1:-1])
        and without_initials
        and without_initials in canonical_page
    )


def _dedupe_name_variants(name: str, aliases: Optional[list[str]] = None) -> list[str]:
    """Keep useful Chinese/romanized variants without broadening identity."""
    variants = []
    seen = set()
    for value in [name] + list(aliases or []):
        value = re.sub(r"\s+", " ", (value or "").strip())
        key = value.casefold()
        if not value or key in seen or not _plausible_person_name(value):
            continue
        seen.add(key)
        variants.append(value)
    return variants[:5]


def _trusted_openalex_name_variants(
    requested_name: str, display_name: str, alternatives: Optional[list[str]] = None
) -> list[str]:
    """Reject OpenAlex aliases that belong to a merged or conflated person."""
    anchor = requested_name or display_name
    trusted = [anchor]
    for alternative in [display_name] + list(alternatives or []):
        if _name_match_ok(anchor, alternative):
            trusted.append(alternative)
    return _dedupe_name_variants(anchor, trusted)


def _page_mentions_any_name(
    page_text: str, name: str, aliases: Optional[list[str]] = None
) -> bool:
    return any(
        _page_mentions_author(page_text, variant)
        for variant in _dedupe_name_variants(name, aliases)
    )


def _first_matching_year(text: str, patterns: list[str]) -> Optional[int]:
    current_year = datetime.now().year
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if not match:
            continue
        try:
            year = int(match.group(1))
        except (IndexError, TypeError, ValueError):
            continue
        if current_year - 10 <= year <= current_year + 8:
            return year
    return None


def _author_local_context(soup: BeautifulSoup, name: str) -> str:
    """Keep role evidence close to the named person on roster pages."""
    snippets = []
    for element in soup.select("h1, h2, h3, h4, h5, p, li, article, section, td"):
        text = element.get_text(" ", strip=True)
        if not text or len(text) > 1200 or not _page_mentions_author(text, name):
            continue
        parent_text = (
            element.parent.get_text(" ", strip=True)
            if element.parent and element.parent.name not in {"body", "html"}
            else text
        )
        candidate = parent_text if len(parent_text) <= 1400 else text
        # A common profile layout is ``<h1>Name</h1><p>Current role</p>``.
        # Include a few adjacent fields without falling back to the entire
        # page.  Whole-page fallback polluted faculty profiles when research
        # or mentee pages mentioned the professor's PhD students.
        if len(candidate) < 120:
            adjacent = [candidate]
            for sibling in list(element.next_siblings)[:6]:
                sibling_name = getattr(sibling, "name", None)
                if not sibling_name:
                    continue
                if sibling_name in {"h1", "h2", "h3", "h4", "h5"}:
                    break
                sibling_text = sibling.get_text(" ", strip=True)
                if sibling_text:
                    adjacent.append(sibling_text)
                if sum(len(item) for item in adjacent) >= 1000:
                    break
            candidate = " ".join(adjacent)
        if candidate not in snippets:
            snippets.append(candidate)
        if len(snippets) >= 4:
            break
    return " ".join(snippets)


def _graduation_status(score: float, explicit_year: bool,
                       career_stage: str) -> str:
    if career_stage == "recent-graduate":
        return "graduated"
    if career_stage in {"faculty", "postdoc", "staff"}:
        return "not-student"
    if explicit_year and score >= 75:
        return "confirmed-upcoming"
    if score >= 70:
        return "likely-upcoming"
    if score >= 40:
        return "student"
    return "insufficient"


def _background_role_priority(background: dict) -> int:
    """Make explicit non-student roles sticky across personal-site pages."""
    stage = (background.get("career_stage") or "unknown").lower()
    if stage in {"faculty", "postdoc", "staff"}:
        return 3
    if stage in {"recent-graduate", "doctoral-student", "masters-student"}:
        return 2
    return 0


def analyze_author_background(
    html: str, page_url: str, name: str, site: Optional[dict] = None
) -> dict:
    """Extract explainable career and China-link evidence from an official page."""
    result = {
        "career_stage": "unknown",
        "degree_type": "",
        "graduation_score": 0.0,
        "graduation_status": "insufficient",
        "expected_graduation_year": None,
        "graduation_evidence": [],
        "china_link_score": 0.0,
        "china_link_status": "none",
        "china_link_evidence": [],
        "nationality": "",
        "nationality_evidence": [],
    }
    if not html:
        return result
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if not _page_mentions_author(page_text, name):
        return result
    local_text = _author_local_context(soup, name) or page_text[:3000]
    lowered = local_text.lower()

    former_student_match = next((
        pattern for pattern in FORMER_STUDENT_PATTERNS
        if re.search(pattern, lowered, re.IGNORECASE)
    ), None)

    non_student_match = next((
        pattern for pattern in NON_STUDENT_ROLE_PATTERNS
        if re.search(pattern, lowered, re.IGNORECASE)
    ), None)
    doctoral_match = next((
        pattern for pattern in DOCTORAL_ROLE_PATTERNS
        if re.search(pattern, lowered, re.IGNORECASE)
    ), None)
    masters_match = next((
        pattern for pattern in MASTERS_ROLE_PATTERNS
        if re.search(pattern, lowered, re.IGNORECASE)
    ), None)
    if former_student_match:
        result["career_stage"] = "recent-graduate"
        result["degree_type"] = "PhD" if doctoral_match else "Master" if masters_match else ""
        result["graduation_score"] = 90
        result["graduation_evidence"].append({
            "type": "official-former-student", "value": "官网明确为前学生/已毕业",
            "source": page_url, "confidence": 98,
        })
    elif non_student_match and not (doctoral_match or masters_match):
        if re.search(r"postdoc|博士后", lowered, re.IGNORECASE):
            result["career_stage"] = "postdoc"
        elif re.search(r"professor|faculty|教授", lowered, re.IGNORECASE):
            result["career_stage"] = "faculty"
        else:
            result["career_stage"] = "staff"
        result["graduation_evidence"].append({
            "type": "current-role", "value": result["career_stage"],
            "source": page_url, "confidence": 85,
        })
    elif doctoral_match:
        result["career_stage"] = "doctoral-student"
        result["degree_type"] = "PhD"
        result["graduation_score"] += 50
        result["graduation_evidence"].append({
            "type": "student-role", "value": "博士生/博士候选人",
            "source": page_url, "confidence": 90,
        })
    elif masters_match:
        result["career_stage"] = "masters-student"
        result["degree_type"] = "Master"
        result["graduation_score"] += 45
        result["graduation_evidence"].append({
            "type": "student-role", "value": "硕士生",
            "source": page_url, "confidence": 90,
        })

    expected_year = _first_matching_year(local_text, UPCOMING_GRADUATION_PATTERNS)
    if expected_year:
        result["expected_graduation_year"] = expected_year
        result["graduation_score"] += 40
        result["graduation_evidence"].append({
            "type": "expected-graduation", "value": str(expected_year),
            "source": page_url, "confidence": 95,
        })
    if any(re.search(pattern, lowered, re.IGNORECASE)
           for pattern in DEFENSE_JOB_PATTERNS):
        result["graduation_score"] += 25
        result["graduation_evidence"].append({
            "type": "defense-or-job-market",
            "value": "出现答辩或求职信号", "source": page_url,
            "confidence": 80,
        })
    enrollment_year = _first_matching_year(local_text, ENROLLMENT_PATTERNS)
    if enrollment_year and result["degree_type"]:
        estimated_year = enrollment_year + (5 if result["degree_type"] == "PhD" else 3)
        if not result["expected_graduation_year"]:
            result["expected_graduation_year"] = estimated_year
        result["graduation_score"] += 10
        result["graduation_evidence"].append({
            "type": "estimated-from-enrollment",
            "value": f"{enrollment_year} 入学；估算 {estimated_year}",
            "source": page_url, "confidence": 40,
        })

    site = site or {}
    country_code = (site.get("country_code") or "").upper()
    if country_code == "CN":
        result["china_link_score"] += 20
        result["china_link_evidence"].append({
            "type": "current-china-institution",
            "value": site.get("institution") or site.get("domain") or "中国机构",
            "source": page_url, "confidence": 90,
        })
    education_match = next((
        re.search(pattern, local_text, re.IGNORECASE)
        for pattern in CHINA_EDUCATION_PATTERNS
        if re.search(pattern, local_text, re.IGNORECASE)
    ), None)
    if education_match:
        evidence_value = re.sub(r"\s+", " ", education_match.group(0))[:180]
        result["china_link_score"] += 45
        result["china_link_evidence"].append({
            "type": "china-education", "value": evidence_value,
            "source": page_url, "confidence": 85,
        })
    nationality_match = next((
        re.search(pattern, local_text, re.IGNORECASE)
        for pattern in EXPLICIT_CHINESE_NATIONALITY_PATTERNS
        if re.search(pattern, local_text, re.IGNORECASE)
    ), None)
    if nationality_match:
        result["nationality"] = "China"
        result["nationality_evidence"].append({
            "type": "explicit-nationality",
            "value": nationality_match.group(0), "source": page_url,
            "confidence": 100,
        })
    result["graduation_score"] = min(result["graduation_score"], 100.0)
    result["graduation_status"] = _graduation_status(
        result["graduation_score"], bool(expected_year), result["career_stage"]
    )
    if result["china_link_score"] >= 60:
        result["china_link_status"] = "strong"
    elif result["china_link_score"] >= 30:
        result["china_link_status"] = "medium"
    elif result["china_link_score"] > 0:
        result["china_link_status"] = "weak"
    return result


def _plausible_person_name(value: str) -> bool:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not value or len(value) > 70 or "@" in value:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff·]{2,8}", value):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'’-]+", value)
    return 2 <= len(words) <= 6 and sum(len(word) for word in words) >= 5


def extract_graduate_candidates_from_page(
    html: str, page_url: str, official_domain: str, target_name: str = ""
) -> list[dict]:
    """Conservatively discover named graduate students on official roster pages."""
    if not html or not _same_official_domain(page_url, official_domain):
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()
    for element in soup.select(
        "li, article, tr, .person, .member, .profile, .people-item, "
        ".people-person, .team-member"
    ):
        text = re.sub(r"\s+", " ", element.get_text(" ", strip=True))
        if not text or len(text) > 700:
            continue
        doctoral = any(re.search(pattern, text, re.IGNORECASE)
                       for pattern in DOCTORAL_ROLE_PATTERNS)
        masters = any(re.search(pattern, text, re.IGNORECASE)
                      for pattern in MASTERS_ROLE_PATTERNS)
        former_student = any(re.search(pattern, text, re.IGNORECASE)
                             for pattern in FORMER_STUDENT_PATTERNS)
        if not doctoral and not masters:
            continue
        name = ""
        for candidate_element in element.select("h2, h3, h4, h5, strong, b, a"):
            candidate_name = candidate_element.get_text(" ", strip=True)
            if _plausible_person_name(candidate_name):
                name = candidate_name
                break
        if not name or (target_name and _canonical_name(name) == _canonical_name(target_name)):
            continue
        email = ""
        mail_link = element.select_one('a[href^="mailto:"]')
        if mail_link:
            possible_email = unquote(mail_link.get("href", "")[7:]).split("?", 1)[0]
            if (
                EMAIL_PATTERN.fullmatch(possible_email)
                and _is_personal_email(possible_email)
                and _email_localpart_matches_author(possible_email, name)
            ):
                email = possible_email
        profile_url = ""
        for link in element.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            if _same_official_domain(href, official_domain) and not href.startswith("mailto:"):
                profile_url = href.split("#", 1)[0]
                break
        if not email and not profile_url:
            continue
        key = (email.lower() if email else _canonical_name(name), profile_url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "name": name,
            "career_stage": (
                "recent-graduate" if former_student else
                "doctoral-student" if doctoral else "masters-student"
            ),
            "degree_type": "PhD" if doctoral else "Master",
            "email": email,
            "profile_url": profile_url,
            "source": page_url,
            "verification_status": (
                "official-alumni-profile" if former_student
                else "official-roster-candidate"
            ),
            "evidence": text[:240],
        })

    # Many Chinese laboratory sites publish a heading followed by a flat list
    # of names, without per-student profile links or repeated role labels.
    # Keep these as name-only roster evidence rather than discarding the list.
    role_headings = []
    for heading in soup.select("h1, h2, h3, h4"):
        heading_text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
        lowered = heading_text.lower()
        if re.search(
            r"graduate students?|doctoral students?|ph\.?d\.? students?|"
            r"研究生|博士生|硕士生|在读学生",
            lowered,
            re.IGNORECASE,
        ):
            role_headings.append((heading, heading_text))
    for heading, heading_text in role_headings:
        heading_level = int(heading.name[1])
        section_elements = []
        for sibling in heading.next_siblings:
            sibling_name = getattr(sibling, "name", None)
            if sibling_name and re.fullmatch(r"h[1-4]", sibling_name):
                sibling_level = int(sibling_name[1])
                if sibling_level <= heading_level:
                    break
            if sibling_name:
                section_elements.append(sibling)
        doctoral = bool(re.search(r"doctoral|ph\.?d|博士", heading_text, re.IGNORECASE))
        masters = bool(re.search(r"master|硕士", heading_text, re.IGNORECASE))
        for section in section_elements:
            for name_element in section.select("b, strong, a"):
                candidate_name = re.sub(
                    r"\s+", " ", name_element.get_text(" ", strip=True)
                )
                if not _plausible_person_name(candidate_name):
                    continue
                if target_name and _canonical_name(candidate_name) == _canonical_name(target_name):
                    continue
                key = (_canonical_name(candidate_name), page_url)
                if key in seen:
                    continue
                seen.add(key)
                profile_url = ""
                profile_link = (
                    name_element if name_element.name == "a"
                    else name_element.select_one("a[href]")
                    or name_element.find_parent("a", href=True)
                )
                if profile_link and profile_link.get("href"):
                    possible_url = urljoin(page_url, profile_link.get("href"))
                    if possible_url.startswith(("http://", "https://")):
                        profile_url = possible_url.split("#", 1)[0]
                candidates.append({
                    "name": candidate_name,
                    "career_stage": (
                        "doctoral-student" if doctoral else
                        "masters-student" if masters else "graduate-student"
                    ),
                    "degree_type": "PhD" if doctoral else "Master" if masters else "",
                    "email": "",
                    "profile_url": profile_url,
                    "source": page_url,
                    "verification_status": "official-roster-name-only",
                    "evidence": heading_text,
                })
                if len(candidates) >= MAX_GRADUATE_CANDIDATES:
                    return candidates
    return candidates[:MAX_GRADUATE_CANDIDATES]


def _is_personal_email(email: str) -> bool:
    local_part = (email or "").split("@", 1)[0].lower()
    compact = "".join(re.findall(r"[a-z]+", local_part))
    if not compact:
        return False
    if any(term in compact for term in NON_PERSONAL_EMAIL_TERMS):
        return False
    return not any(bad.replace("-", "") == compact for bad in BAD_EMAILS)


def _normalize_phone(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    value = re.sub(r"[^0-9+()\-\s]", "", value)
    return value.strip(" -")


def _deobfuscated_email_candidates(page_text: str) -> list[tuple[str, str]]:
    """Decode common human-readable email forms such as name(at)umd(dot)edu."""
    candidates = []
    pattern = re.compile(
        r"\b([A-Za-z0-9._%+\-]+)\s*"
        r"(?:\(|\[|\{)?\s*(?:at|AT)\s*(?:\)|\]|\})?\s*"
        r"([A-Za-z0-9\-]+(?:\s*(?:\(|\[|\{)?\s*(?:dot|DOT)\s*"
        r"(?:\)|\]|\})?\s*[A-Za-z0-9\-]+)+)"
    )
    for match in pattern.finditer(page_text or ""):
        domain = re.sub(
            r"\s*(?:\(|\[|\{)?\s*(?:dot|DOT)\s*(?:\)|\]|\})?\s*",
            ".", match.group(2),
        )
        email = f"{match.group(1)}@{domain}".lower()
        if EMAIL_PATTERN.fullmatch(email):
            start, end = max(0, match.start() - 140), min(len(page_text), match.end() + 140)
            candidates.append((email, page_text[start:end]))
    return list(dict.fromkeys(candidates))


def _extract_phone_candidates(
    soup: BeautifulSoup, page_text: str
) -> list[tuple[str, str, str]]:
    candidates = []
    for link in soup.select('a[href^="tel:"]'):
        phone = _normalize_phone(unquote(link.get("href", "")[4:]))
        context = link.parent.get_text(" ", strip=True) if link.parent else ""
        if phone and re.search(
            r"手机|移动电话|联系电话|电话|办公电话|Tel|Phone|Mobile",
            context,
            re.IGNORECASE,
        ):
            candidates.append((phone, "tel link", context[:500]))
    for match in LABELED_PHONE_PATTERN.finditer(page_text or ""):
        phone = _normalize_phone(match.group(1))
        if phone:
            start, end = max(0, match.start() - 100), min(len(page_text), match.end() + 100)
            candidates.append((phone, "labeled phone", page_text[start:end]))
    for match in CHINA_MOBILE_PATTERN.finditer(page_text or ""):
        phone = _normalize_phone(match.group(0))
        if phone:
            start, end = max(0, match.start() - 100), min(len(page_text), match.end() + 100)
            candidates.append((phone, "public mobile", page_text[start:end]))
    return list(dict.fromkeys(candidates))


def _department_priority(page_text: str) -> tuple[int, str]:
    lowered = (page_text or "").lower()
    if any(term in lowered for term in QUANTUM_DEPARTMENT_TERMS):
        return 15, "quantum"
    if any(term in lowered for term in PHYSICS_DEPARTMENT_TERMS):
        return 10, "physics"
    return 0, "general"


def extract_official_contact_from_page(
    html: str, page_url: str, name: str, official_domain: str,
    aliases: Optional[list[str]] = None,
) -> dict:
    """Extract attributable public contacts from one official institution page."""
    result = {
        "email": None,
        "phone": None,
        "email_confidence": 0.0,
        "phone_confidence": 0.0,
        "source": page_url,
        "department": "general",
        "candidates": [],
        "links": [],
        "name_matched": False,
    }
    if not html or not _same_official_domain(page_url, official_domain):
        return result
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    for link in soup.select("a[href]"):
        href = urljoin(page_url, link.get("href", ""))
        label = f"{link.get_text(' ', strip=True)} {href}".lower()
        if (
            _same_official_domain(href, official_domain)
            and any(term in label for term in DIRECTORY_LINK_TERMS)
        ):
            result["links"].append(href.split("#", 1)[0])
    result["links"] = list(dict.fromkeys(result["links"]))[:12]
    name_variants = _dedupe_name_variants(name, aliases)
    if not _page_mentions_any_name(page_text, name, aliases):
        return result
    result["name_matched"] = True
    priority_score, department = _department_priority(page_text)
    result["department"] = department

    emails = {}
    for link in soup.select('a[href^="mailto:"]'):
        value = unquote(link.get("href", "")[7:]).split("?", 1)[0].strip()
        if EMAIL_PATTERN.fullmatch(value):
            local_context = link.parent.get_text(" ", strip=True) if link.parent else ""
            emails[value] = {"mailto": True, "context": local_context[:500]}
    for email in EMAIL_PATTERN.findall(page_text):
        match = re.search(re.escape(email), page_text)
        local_context = ""
        if match:
            local_context = page_text[max(0, match.start() - 140):match.end() + 140]
        emails.setdefault(email, {"mailto": False, "context": local_context})
    for email, local_context in _deobfuscated_email_candidates(page_text):
        emails.setdefault(email, {
            "mailto": False, "context": local_context, "obfuscated": True,
        })
    page_heading = " ".join(
        item.get_text(" ", strip=True) for item in soup.select("h1, h2")[:3]
    )
    heading_match = _page_mentions_any_name(page_heading, name, aliases)
    for email, metadata in emails.items():
        from_mailto = metadata["mailto"]
        local_context = metadata.get("context") or ""
        email_domain = email.rsplit("@", 1)[-1].lower()
        personal = _is_personal_email(email)
        domain_match = (
            email_domain == official_domain
            or email_domain.endswith(f".{official_domain}")
            or official_domain.endswith(f".{email_domain}")
        )
        local_match = any(
            _email_localpart_matches_author(email, variant)
            for variant in name_variants
        )
        context_name_match = _page_mentions_any_name(local_context, name, aliases)
        advisor_context = bool(re.search(
            r"导师|指导教师|supervisor|advisor|principal investigator|\bpi\b",
            local_context, re.IGNORECASE,
        ))
        attributable = local_match or (
            domain_match and from_mailto and context_name_match and not advisor_context
        )
        confidence = 35 + priority_score
        confidence += 15 if domain_match else 0
        confidence += 25 if local_match else 0
        confidence += 15 if from_mailto and context_name_match else 0
        confidence += 5 if heading_match else 0
        if not personal:
            confidence = 0
            owner_type = "group-or-public"
            reason = "邮箱名称表明它属于课题组、委员会或公共服务账号"
        elif advisor_context and not local_match:
            confidence = min(confidence, 55)
            owner_type = "advisor-or-other"
            reason = "邮箱附近出现导师/负责人字样，不能归属于目标本人"
        elif not attributable:
            confidence = min(confidence, 60)
            owner_type = "unknown"
            reason = "页面提到目标，但邮箱用户名或附近文字不足以确认归属"
        else:
            owner_type = "target"
            reason = (
                "邮箱用户名与姓名匹配"
                if local_match else "机构邮箱链接与目标姓名出现在同一局部信息块"
            )
        candidate = {
            "type": "email",
            "value": email,
            "confidence": min(float(confidence), 100.0),
            "source": page_url,
            "department": department,
            "owner_type": owner_type,
            "contact_kind": (
                "personal" if owner_type == "target" else
                "advisor" if owner_type == "advisor-or-other" else
                "group/public" if owner_type == "group-or-public" else
                "unverified"
            ),
            "reason": reason,
            "context": re.sub(r"\s+", " ", local_context)[:220],
            "status": (
                "rejected" if not personal
                else "accepted" if attributable and confidence >= 75
                else "review"
            ),
        }
        result["candidates"].append(candidate)
        if (
            attributable
            and confidence >= 75
            and confidence > result["email_confidence"]
        ):
            result["email"] = email
            result["email_confidence"] = min(float(confidence), 100.0)

    for phone, phone_kind, local_context in _extract_phone_candidates(soup, page_text):
        context_name_match = _page_mentions_any_name(local_context, name, aliases)
        advisor_context = bool(re.search(
            r"导师|指导教师|supervisor|advisor|办公室|秘书|office|secretary",
            local_context, re.IGNORECASE,
        ))
        attributable = context_name_match or (heading_match and not advisor_context)
        confidence = 45 + priority_score
        confidence += 20 if phone_kind == "tel link" else 10
        confidence += 20 if context_name_match else 0
        confidence += 10 if heading_match else 0
        if advisor_context and not context_name_match:
            confidence = min(confidence, 55)
        if not attributable:
            confidence = min(confidence, 60)
        confidence = min(float(confidence), 100.0)
        candidate = {
            "type": "phone",
            "value": phone,
            "confidence": confidence,
            "source": page_url,
            "department": department,
            "owner_type": "target" if attributable else (
                "advisor-or-office" if advisor_context else "unknown"
            ),
            "contact_kind": (
                "personal" if attributable else
                "advisor/office" if advisor_context else "unverified"
            ),
            "reason": (
                "号码与目标姓名位于同一局部信息块"
                if context_name_match else
                "目标个人页仅出现一个带标签的号码"
                if attributable else
                "页面包含目标姓名，但号码附近文字不足以确认归属"
            ),
            "context": re.sub(r"\s+", " ", local_context)[:220],
            "status": "accepted" if attributable and confidence >= 75 else "review",
            "kind": phone_kind,
        }
        result["candidates"].append(candidate)
        if attributable and confidence >= 75 and confidence > result["phone_confidence"]:
            result["phone"] = phone
            result["phone_confidence"] = confidence

    return result


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
        for inst in _author_institution_records(author)
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


def _quantum_physics_relevant(values) -> bool:
    """Return whether titles/topics contain a quantum or physics signal."""
    if isinstance(values, str):
        text = values.lower()
    else:
        text = " ".join(str(value) for value in (values or [])).lower()
    return any(term in text for term in QUANTUM_PHYSICS_RELEVANCE_TERMS)


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


async def _safe_get_with_url(
    url: str, session: aiohttp.ClientSession, timeout: int = 15
) -> tuple[Optional[str], str]:
    """GET a page and retain the final URL after official redirects."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                return await resp.text(), str(resp.url)
    except Exception:
        pass
    return None, url


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
    "jila": "jila.colorado.edu",
    "joint institute for laboratory astrophysics": "jila.colorado.edu",
    "university of colorado boulder": "colorado.edu",
    "national institute of standards and technology": "nist.gov",
}


def _fallback_institution_site(institution: str) -> Optional[dict]:
    lowered = (institution or "").lower()
    for key, domain in INSTITUTION_DOMAINS.items():
        if key in lowered or lowered in key:
            return {
                "institution": institution,
                "homepage_url": f"https://{domain}",
                "domain": domain,
                "country_code": "",
                "openalex_id": "",
                "ror": "",
                "source": "curated institution domain",
            }
    return None


async def resolve_institution_sites(
    institution_records: list[dict], affiliations: list[str],
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Resolve verified affiliations to official homepages and domains."""
    sites = []
    records = _dedupe_institution_records(institution_records)
    for record in records[:6]:
        institution_id = (record.get("id") or "").rstrip("/").split("/")[-1]
        data = None
        if institution_id:
            try:
                async with session.get(
                    f"{OPENALEX_BASE}/institutions/{institution_id}",
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
            except Exception:
                data = None
        data = data or record
        homepage = data.get("homepage_url") or ""
        domain = _canonical_host(homepage)
        if not domain:
            fallback = _fallback_institution_site(data.get("display_name") or "")
            if fallback:
                homepage, domain = fallback["homepage_url"], fallback["domain"]
        if domain:
            sites.append({
                "institution": data.get("display_name") or record.get("display_name") or "",
                "homepage_url": homepage,
                "domain": domain,
                "country_code": data.get("country_code") or record.get("country_code") or "",
                "openalex_id": data.get("id") or record.get("id") or "",
                "ror": data.get("ror") or record.get("ror") or "",
                "source": "OpenAlex institution homepage",
            })

    for affiliation in affiliations:
        fallback = _fallback_institution_site(affiliation)
        if fallback:
            sites.append(fallback)

    deduped = []
    seen_domains = set()
    for site in sites:
        domain = site.get("domain") or ""
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        deduped.append(site)
    return deduped[:MAX_INSTITUTION_SITES]


def _official_search_queries(
    name: str, site: dict, aliases: Optional[list[str]] = None
) -> list[dict]:
    domain = site.get("domain") or ""
    variants = _dedupe_name_variants(name, aliases)
    regional = (
        (site.get("country_code") or "").upper() in {"CN", "HK", "MO", "TW"}
        or domain.endswith((".cn", ".hk", ".tw"))
    )
    paths = []
    for variant in variants[:3]:
        if regional:
            paths.extend([
                {"path": "个人主页/联系方式", "query": f'site:{domain} "{variant}" 邮箱 联系方式'},
                {"path": "课题组/实验室成员", "query": f'site:{domain} "{variant}" 课题组 实验室 成员'},
                {"path": "研究生/导师链路", "query": f'site:{domain} "{variant}" 博士生 研究生 导师'},
                {"path": "答辩/学位公告", "query": f'site:{domain} "{variant}" 答辩 学位论文 物理 量子'},
            ])
        paths.extend([
            {"path": "个人主页/联系方式", "query": f'site:{domain} "{variant}" email contact profile'},
            {"path": "课题组/实验室成员", "query": f'site:{domain} "{variant}" lab group member quantum physics'},
        ])
    deduped = []
    seen = set()
    for item in paths:
        if item["query"] in seen:
            continue
        seen.add(item["query"])
        deduped.append(item)
    return deduped[:8 if regional else 5]


async def _search_official_result_urls(
    name: str, site: dict, session: aiohttp.ClientSession,
    aliases: Optional[list[str]] = None,
) -> tuple[list[str], list[dict]]:
    urls = []
    trail = []
    domain = site.get("domain") or ""
    for search_path in _official_search_queries(name, site, aliases):
        query = search_path["query"]
        text = None
        provider = "DuckDuckGo"
        selector = "a.result__a"
        if "DuckDuckGo" not in DISABLED_SEARCH_PROVIDERS:
            search_url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            text = await _safe_get(search_url, session, timeout=12)
            if not text:
                DISABLED_SEARCH_PROVIDERS.add("DuckDuckGo")
        if not text:
            provider = "Bing"
            selector = "li.b_algo h2 a"
            search_url = (
                "https://www.bing.com/search?setlang=en-US&cc=US&q="
                f"{quote(query)}"
            )
            text = await _safe_get(search_url, session, timeout=12)
            if not text:
                DISABLED_SEARCH_PROVIDERS.add("Bing")
        if not text:
            trail.append({
                "stage": "official-search",
                "status": "unavailable",
                "institution": site.get("institution") or "",
                "domain": domain,
                "query": query,
                "path": search_path["path"],
                "reason": "DuckDuckGo 与 Bing 均不可访问，已继续尝试官网首页和站点地图",
            })
            # If the provider itself is unavailable, additional query variants
            # will fail the same way; continue through the official sitemap.
            break
        soup = BeautifulSoup(text, "html.parser")
        found = 0
        for link in soup.select(selector):
            href = _unwrap_search_result_url(link.get("href", ""))
            if href and _same_official_domain(href, domain):
                urls.append(href.split("#", 1)[0])
                found += 1
                if found >= 3:
                    break
        trail.append({
            "stage": "official-search",
            "status": "results" if found else "no-results",
            "institution": site.get("institution") or "",
            "domain": domain,
            "query": query,
            "provider": provider,
            "path": search_path["path"],
            "results": found,
            "reason": (
                f"找到 {found} 个官网内结果"
                if found else "该检索路径没有返回官网内页面"
            ),
        })
    return list(dict.fromkeys(urls)), trail


def _page_url_priority(url: str, name: str) -> int:
    lowered = unquote(url).lower()
    score = 0
    if any(term in lowered for term in ("quantum", "physics", "optics", "photon")):
        score += 30
    if any(term in lowered for term in ("people", "faculty", "profile", "staff", "person", "member", "team", "lab")):
        score += 20
    family_name = (_canonical_name(name.split()[-1]) if name.split() else "")
    if family_name and family_name in _canonical_name(lowered):
        score += 35
    name_parts = re.findall(r"[a-z]+", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower())
    name_without_initials = "".join(part for part in name_parts if len(part) > 1)
    if name_without_initials and name_without_initials in _canonical_name(lowered):
        score += 70
    return score


async def _discover_sitemap_urls(
    base_url: str, name: str, official_domain: str,
    session: aiohttp.ClientSession,
) -> list[str]:
    """Use an institution-provided sitemap to locate deep staff/lab pages."""
    parsed = urlparse(base_url)
    sitemap_urls = [
        urljoin(base_url if base_url.endswith("/") else f"{base_url}/", "sitemap.xml"),
        f"{parsed.scheme or 'https'}://{parsed.netloc}/sitemap.xml",
    ]
    candidates = []
    for sitemap_url in dict.fromkeys(sitemap_urls):
        xml_text = await _safe_get(sitemap_url, session, timeout=12)
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        for element in root.iter():
            if not element.tag.endswith("loc") or not element.text:
                continue
            url = element.text.strip()
            if _same_official_domain(url, official_domain):
                priority = _page_url_priority(url, name)
                if priority > 0:
                    candidates.append((priority, url.split("#", 1)[0]))
        if candidates:
            break
    candidates.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(url for _, url in candidates))[:8]


async def search_official_institution_network(
    name: str, sites: list[dict], session: aiohttp.ClientSession,
    aliases: Optional[list[str]] = None,
) -> dict:
    """Bounded search from institution homepages to department and profile pages."""
    result = {
        "email": None,
        "email_source": None,
        "email_confidence": 0.0,
        "phone": None,
        "phone_source": None,
        "phone_confidence": 0.0,
        "website_url": None,
        "candidates": [],
        "trail": [],
        "background": {},
        "graduate_candidates": [],
    }
    total_pages_read = 0
    site_count = max(1, min(len(sites), MAX_INSTITUTION_SITES))
    per_site_budget = max(2, (MAX_OFFICIAL_PAGES + site_count - 1) // site_count)
    for site in sites[:MAX_INSTITUTION_SITES]:
        if total_pages_read >= MAX_OFFICIAL_PAGES:
            break
        domain = site.get("domain") or ""
        allowed_domains = {domain}
        search_urls, trail = await _search_official_result_urls(
            name, site, session, aliases
        )
        result["trail"].extend(trail)
        seed_urls = search_urls + [site.get("homepage_url") or ""]
        queue = [
            (url, 0) for url in sorted(
                dict.fromkeys(url for url in seed_urls if url),
                key=lambda item: _page_url_priority(item, name),
                reverse=True,
            )
        ]
        visited = set()
        pages_read = 0
        while (
            queue
            and pages_read < per_site_budget
            and total_pages_read < MAX_OFFICIAL_PAGES
        ):
            page_url, depth = queue.pop(0)
            if page_url in visited or not any(
                _same_official_domain(page_url, allowed)
                for allowed in allowed_domains
            ):
                continue
            visited.add(page_url)
            html, final_url = await _safe_get_with_url(
                page_url, session, timeout=12
            )
            pages_read += 1
            total_pages_read += 1
            if not html:
                result["trail"].append({
                    "stage": "official-page",
                    "status": "unavailable",
                    "institution": site.get("institution") or "",
                    "url": page_url,
                })
                continue
            final_domain = _canonical_host(final_url)
            if final_domain and not any(
                _same_official_domain(final_url, allowed)
                for allowed in allowed_domains
            ):
                is_homepage_redirect = (
                    page_url.rstrip("/")
                    == (site.get("homepage_url") or "").rstrip("/")
                )
                if is_homepage_redirect:
                    allowed_domains.add(final_domain)
                else:
                    continue
            if (
                page_url.rstrip("/")
                == (site.get("homepage_url") or "").rstrip("/")
            ):
                sitemap_urls = await _discover_sitemap_urls(
                    final_url, name, final_domain or domain, session
                )
                if sitemap_urls:
                    queue = [
                        (url, depth + 1) for url in sitemap_urls
                        if url not in visited
                    ] + queue
                    result["trail"].append({
                        "stage": "official-sitemap",
                        "status": "results",
                        "institution": site.get("institution") or "",
                        "url": urljoin(final_url.rstrip("/") + "/", "sitemap.xml"),
                        "results": len(sitemap_urls),
                    })
            page_result = extract_official_contact_from_page(
                html, final_url, name, final_domain or domain, aliases
            )
            discovered_students = extract_graduate_candidates_from_page(
                html, final_url, final_domain or domain, target_name=name
            )
            for candidate in discovered_students:
                candidate["institution"] = site.get("institution") or ""
                key = (
                    (candidate.get("email") or "").lower(),
                    _canonical_name(candidate.get("name") or ""),
                )
                existing_keys = {
                    ((item.get("email") or "").lower(),
                     _canonical_name(item.get("name") or ""))
                    for item in result["graduate_candidates"]
                }
                if key not in existing_keys:
                    result["graduate_candidates"].append(candidate)
            if page_result["name_matched"]:
                background = analyze_author_background(
                    html, final_url, name, site
                )
                current = result.get("background") or {}
                if background.get("graduation_score", 0) > current.get("graduation_score", 0):
                    for field_name in [
                        "career_stage", "degree_type", "graduation_score",
                        "graduation_status", "expected_graduation_year",
                    ]:
                        current[field_name] = background.get(field_name)
                if background.get("china_link_score", 0) > current.get("china_link_score", 0):
                    for field_name in ["china_link_score", "china_link_status"]:
                        current[field_name] = background.get(field_name)
                if background.get("nationality"):
                    current["nationality"] = background["nationality"]
                for field_name in [
                    "graduation_evidence", "china_link_evidence",
                    "nationality_evidence",
                ]:
                    current.setdefault(field_name, [])
                    for evidence in background.get(field_name) or []:
                        if evidence not in current[field_name]:
                            current[field_name].append(evidence)
                result["background"] = current
                result["trail"].append({
                    "stage": "official-page",
                    "status": "author-matched",
                    "institution": site.get("institution") or "",
                    "url": final_url,
                    "department": page_result["department"],
                    "results": len(page_result["candidates"]),
                    "reason": (
                        f"页面确认出现目标姓名，发现 {len(page_result['candidates'])} 条联系方式候选"
                    ),
                })
                if not result["website_url"]:
                    result["website_url"] = final_url
                result["candidates"].extend(page_result["candidates"])
                if page_result["email_confidence"] > result["email_confidence"]:
                    result["email"] = page_result["email"]
                    result["email_source"] = final_url
                    result["email_confidence"] = page_result["email_confidence"]
                if page_result["phone_confidence"] > result["phone_confidence"]:
                    result["phone"] = page_result["phone"]
                    result["phone_source"] = final_url
                    result["phone_confidence"] = page_result["phone_confidence"]
            else:
                result["trail"].append({
                    "stage": "official-page",
                    "status": "name-not-found",
                    "institution": site.get("institution") or "",
                    "url": final_url,
                    "reason": "页面可访问，但未确认目标姓名，未从该页采用联系方式",
                })
            if depth < 2:
                next_links = sorted(
                    page_result["links"],
                    key=lambda item: _page_url_priority(item, name),
                    reverse=True,
                )[:5]
                queue.extend((link, depth + 1) for link in next_links)
        result["trail"].append({
            "stage": "institution-network",
            "status": "completed",
            "institution": site.get("institution") or "",
            "domain": domain,
            "pages_read": pages_read,
            "reason": f"已在该机构官网读取 {pages_read} 个候选页面",
        })
    result["candidates"] = sorted(
        result["candidates"],
        key=lambda item: item.get("confidence", 0),
        reverse=True,
    )[:12]
    result["trail"] = result["trail"][:MAX_SEARCH_TRAIL]
    result["graduate_candidates"] = result["graduate_candidates"][:MAX_GRADUATE_CANDIDATES]
    return result


async def search_verified_personal_websites(
    name: str, aliases: list[str], urls: list[str],
    session: aiohttp.ClientSession,
) -> dict:
    """Inspect public websites linked directly by the verified ORCID record."""
    result = {
        "email": None, "email_source": None, "email_confidence": 0.0,
        "phone": None, "phone_source": None, "phone_confidence": 0.0,
        "candidates": [], "trail": [], "graduate_candidates": [],
        "lab_members": [], "background": {}, "labs": [],
    }
    queue = [(url, 0) for url in (urls or [])[:2]]
    visited = set()
    while queue and len(visited) < 8:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        html, final_url = await _safe_get_with_url(url, session, timeout=15)
        if not html:
            result["trail"].append({
                "stage": "orcid-personal-site", "path": "ORCID 关联个人主页",
                "status": "unavailable", "url": url,
                "reason": "ORCID 提供了链接，但页面当前不可访问",
            })
            continue
        domain = _canonical_host(final_url)
        page_result = extract_official_contact_from_page(
            html, final_url, name, domain, aliases
        )
        roster_candidates = extract_graduate_candidates_from_page(
            html, final_url, domain, target_name=name
        )
        soup = BeautifulSoup(html, "html.parser")
        lab_name = ""
        for selector in ["meta[property='og:site_name']", "h1", "title"]:
            element = soup.select_one(selector)
            if not element:
                continue
            lab_name = (
                element.get("content", "") if element.name == "meta"
                else element.get_text(" ", strip=True)
            )
            if lab_name:
                break
        if roster_candidates:
            roster_heading = next((
                re.sub(r"\s+", " ", element.get_text(" ", strip=True))
                for element in soup.select("h1, h2")
                if re.search(
                    r"\b(?:lab|laboratory|research group)\b",
                    element.get_text(" ", strip=True), re.IGNORECASE,
                )
            ), "")
            if roster_heading:
                lab_name = roster_heading
        description = " ".join(
            element.get("content", "")
            for element in soup.select(
                'meta[name="description"], meta[property="og:description"]'
            )
            if element.get("content")
        )
        lab_pi = ""
        pi_match = re.search(
            r"(?:led\s+by|principal\s+investigator\s*[:\-]?)\s+"
            r"(?:Dr\.?\s+|Prof\.?\s+)?"
            r"([A-Z][A-Za-z'\-’]+(?:\s+[A-Z][A-Za-z'\-’]+){1,3})",
            description, re.IGNORECASE,
        )
        pi_candidate = pi_match.group(1) if pi_match else ""
        if not pi_candidate:
            for heading in soup.select("h1, h2, h3, h4, strong"):
                if not re.search(
                    r"\bprincipal\s+investigator\b",
                    heading.get_text(" ", strip=True), re.IGNORECASE,
                ):
                    continue
                sibling = heading.find_next_sibling()
                if not sibling:
                    continue
                sibling_text = re.sub(
                    r"^(?:Dr|Prof)\.?\s+", "",
                    sibling.get_text(" ", strip=True), flags=re.IGNORECASE,
                )
                name_match = re.match(
                    r"([A-Z][A-Za-z'\-’]+(?:\s+[A-Z][A-Za-z'\-’]+){1,3})",
                    sibling_text,
                )
                if name_match:
                    pi_candidate = name_match.group(1)
                    break
        if (
            pi_candidate and name
            and _canonical_name(pi_candidate).startswith(_canonical_name(name))
        ):
            pi_candidate = name
        if pi_candidate and _plausible_person_name(pi_candidate):
            lab_pi = pi_candidate
        meta_author = soup.select_one('meta[name="author"]')
        if not lab_pi and meta_author and _plausible_person_name(
            meta_author.get("content", "")
        ):
            lab_pi = meta_author.get("content", "")
        if lab_pi and (
            not re.search(r"\b(?:lab|laboratory|research group)\b", lab_name, re.I)
            or re.search(r"@\s*(?:utah|university|umd)\b", lab_name, re.I)
        ):
            lab_name = f"{lab_pi} Research Group"
        is_lab_site = bool(
            re.search(r"\b(?:lab|laboratory|research group)\b", f"{lab_name} {description}", re.I)
            or roster_candidates
        )
        if not lab_pi and is_lab_site and roster_candidates:
            # A verified ORCID owner who is also the first named staff member
            # on the linked laboratory roster is strong PI/leader evidence,
            # even when an older site omits an explicit "PI" heading.
            first_staff_name = ""
            for heading in soup.select("h1, h2, h3, h4"):
                if not re.search(r"\bstaff\b|\bfaculty\b|教师|导师", heading.get_text(" ", strip=True), re.I):
                    continue
                sibling = heading.find_next_sibling()
                if sibling:
                    for element in sibling.select("b, strong, a"):
                        possible_name = element.get_text(" ", strip=True)
                        if _plausible_person_name(possible_name):
                            first_staff_name = possible_name
                            break
                break
            if (
                first_staff_name
                and _canonical_name(first_staff_name) == _canonical_name(name)
            ):
                lab_pi = name
                if not re.search(r"\b(?:lab|laboratory|research group)\b", lab_name, re.I):
                    lab_name = f"{name} Research Group"
        lab_record = {
            "name": lab_name, "url": urls[0] if urls else final_url,
            "directory_url": final_url, "pi_name": lab_pi,
            "domain": domain,
        }
        if is_lab_site and lab_name:
            lab_key = (domain, _canonical_name(lab_name))
            existing_lab_keys = {
                (item.get("domain"), _canonical_name(item.get("name") or ""))
                for item in result["labs"]
            }
            if lab_key not in existing_lab_keys:
                result["labs"].append(lab_record)
        existing_roster = {
            _canonical_name(item.get("name") or "")
            for item in result["graduate_candidates"]
        }
        for roster_candidate in roster_candidates:
            roster_candidate.setdefault("lab_name", lab_name)
            roster_candidate.setdefault("lab_url", urls[0] if urls else final_url)
            roster_candidate.setdefault("directory_url", final_url)
            roster_candidate.setdefault("lab_pi", lab_pi)
            key = _canonical_name(roster_candidate.get("name") or "")
            if key not in existing_roster:
                result["graduate_candidates"].append(roster_candidate)
                existing_roster.add(key)
        # ORCID verifies the seed page, not every linked staff profile. Follow
        # only one hop to team/directory pages to avoid attributing a colleague's
        # contact details to the ORCID owner via shared lab navigation text.
        if depth < 1:
            queue.extend(
                (link, depth + 1)
                for link in page_result.get("links") or []
                if link not in visited
            )
        if not page_result["name_matched"]:
            result["trail"].append({
                "stage": "orcid-personal-site", "path": "ORCID 关联个人主页",
                "status": "name-not-found", "url": final_url,
                "results": len(roster_candidates),
                "reason": (
                    "页面正文未确认目标姓名"
                    + (f"，但发现 {len(roster_candidates)} 位官网学生名单候选" if roster_candidates else "")
                ),
            })
            continue
        background = analyze_author_background(html, final_url, name)
        current_background = result["background"]
        incoming_priority = _background_role_priority(background)
        current_priority = _background_role_priority(current_background)
        if (
            incoming_priority > current_priority
            or (
                incoming_priority == current_priority
                and background.get("graduation_score", 0)
                > current_background.get("graduation_score", 0)
            )
        ):
            result["background"] = background
        for candidate in page_result["candidates"]:
            candidate = dict(candidate)
            candidate["source_path"] = "ORCID-linked-personal-site"
            if candidate.get("owner_type") == "target":
                candidate["confidence"] = min(
                    100.0, float(candidate.get("confidence") or 0) + 15
                )
                if candidate["confidence"] >= 75:
                    candidate["status"] = "accepted"
                candidate["reason"] = (
                    f"{candidate.get('reason') or ''}；该主页由已验证 ORCID 直接链接"
                ).strip("；")
            result["candidates"].append(candidate)
            if (
                candidate.get("status") == "accepted"
                and candidate.get("type") == "email"
                and candidate["confidence"] > result["email_confidence"]
            ):
                result["email"] = candidate["value"]
                result["email_source"] = final_url
                result["email_confidence"] = candidate["confidence"]
            if (
                candidate.get("status") == "accepted"
                and candidate.get("type") == "phone"
                and candidate["confidence"] > result["phone_confidence"]
            ):
                result["phone"] = candidate["value"]
                result["phone_source"] = final_url
                result["phone_confidence"] = candidate["confidence"]
        result["trail"].append({
            "stage": "orcid-personal-site", "path": "ORCID 关联个人主页",
            "status": "author-matched", "url": final_url,
            "results": len(page_result["candidates"]) + len(roster_candidates),
            "reason": (
                f"确认目标姓名，发现 {len(page_result['candidates'])} 条联系方式候选、"
                f"{len(roster_candidates)} 位学生名单候选"
            ),
        })

    # A verified ORCID link may lead to a laboratory site. The directory page
    # is evidence that a person belongs to that lab, while each member page is
    # the only safe place to adopt that member's contact details. Never merge a
    # member's email into the original ORCID owner.
    async def enrich_member(candidate: dict) -> dict:
        member = dict(candidate)
        profile_url = member.get("profile_url") or ""
        if not profile_url:
            is_former = member.get("career_stage") == "recent-graduate"
            member.setdefault("student_score", 15.0 if is_former else 82.0)
            member.setdefault(
                "student_status",
                "confirmed-non-student" if is_former else "confirmed-student",
            )
            return member
        html, final_url = await _safe_get_with_url(profile_url, session, timeout=15)
        if not html:
            return member
        domain = _canonical_host(final_url)
        page_result = extract_official_contact_from_page(
            html, final_url, member.get("name") or "", domain,
            [member.get("name") or ""],
        )
        if not page_result.get("name_matched"):
            return member
        # The roster link has already established which person owns this page.
        # Apply that identity evidence before the acceptance threshold; doing it
        # afterwards caused otherwise valid personal-page contacts (often scored
        # 60 on their own) to be silently discarded.
        accepted = []
        for raw_item in page_result.get("candidates") or []:
            if raw_item.get("owner_type") != "target":
                continue
            item = dict(raw_item)
            item["confidence"] = min(
                100, float(item.get("confidence") or 0) + 15
            )
            if item["confidence"] >= 65:
                accepted.append(item)
        accepted = sorted(
            accepted,
            key=lambda item: float(item.get("confidence") or 0),
            reverse=True,
        )
        for item in accepted:
            item["status"] = "accepted"
            if item.get("type") == "email" and not member.get("email"):
                member["email"] = item.get("value") or ""
                member["email_source"] = final_url
                member["email_confidence"] = item.get("confidence") or 0
            elif item.get("type") == "phone" and not member.get("phone"):
                member["phone"] = item.get("value") or ""
                member["phone_source"] = final_url
                member["phone_confidence"] = item.get("confidence") or 0
        background = analyze_author_background(
            html, final_url, member.get("name") or ""
        )
        for field_name in [
            "career_stage", "degree_type", "expected_graduation_year",
            "graduation_score", "graduation_status",
        ]:
            value = background.get(field_name)
            if value not in (None, "", 0, 0.0, "insufficient", "unknown"):
                member[field_name] = value
        member["profile_url"] = final_url
        is_former = member.get("career_stage") == "recent-graduate"
        member["verification_status"] = (
            "official-lab-alumni-profile" if is_former
            else "official-lab-member-profile"
        )
        member["student_score"] = 15.0 if is_former else 92.0
        member["student_status"] = (
            "confirmed-non-student" if is_former else "confirmed-student"
        )
        member["student_evidence"] = [{
            "type": "official-lab-alumni" if is_former else "official-lab-roster",
            "value": (
                "官网明确为前学生/已毕业"
                if is_former else
                member.get("evidence") or member.get("career_stage") or "student"
            ),
            "source": final_url if is_former else (
                member.get("directory_url") or member.get("source") or final_url
            ),
            "confidence": 98 if is_former else 95,
        }]
        return member

    member_candidates = result["graduate_candidates"][:MAX_GRADUATE_CANDIDATES]
    if member_candidates:
        enriched = await asyncio.gather(*[enrich_member(item) for item in member_candidates])
        result["graduate_candidates"] = enriched
        result["lab_members"] = enriched
        result["trail"].append({
            "stage": "lab-member-profiles", "path": "实验室成员个人页",
            "status": "completed", "results": len(enriched),
            "reason": (
                f"从实验室名单受控访问 {len(enriched)} 个成员页；"
                f"获得 {sum(bool(item.get('email') or item.get('phone')) for item in enriched)} 人的公开联系方式"
            ),
        })
    result["graduate_candidates"] = result["graduate_candidates"][:MAX_GRADUATE_CANDIDATES]
    result["lab_members"] = result["lab_members"][:MAX_GRADUATE_CANDIDATES]
    return result


def _profile_from_openalex_result(result: dict, name: str, context: dict,
                                  direct: bool = False) -> AuthorProfile:
    score, breakdown = _score_author_match_details(result, context, name)
    institution_records = _dedupe_institution_records(
        _author_institution_records(result)
    )
    if direct:
        score = 100.0
        breakdown = {
            "direct_work_authorship": 100.0,
            "source": context.get("matched_work_source", "OpenAlex paper authorship"),
        }
    profile = AuthorProfile(
        name=result.get("display_name") or name,
        name_aliases=_trusted_openalex_name_variants(
            name,
            result.get("display_name") or name,
            result.get("display_name_alternatives") or [],
        ),
        openalex_id=result.get("id") or "",
        orcid=result.get("orcid") or "",
        affiliations=[
            (inst or {}).get("display_name", "")
            for inst in institution_records
            if (inst or {}).get("display_name")
        ],
        institution_records=institution_records,
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
            # When two institution-compatible identities are close, prefer
            # the one whose OpenAlex topics match the paper field. Exact-name
            # profiles are sometimes conflated across disciplines and used to
            # beat an initialed but field-correct profile by only a few points.
            if scored and _quantum_physics_relevant(context.get("keywords") or []):
                best_score = scored[0][0]
                aligned = [
                    item for item in scored
                    if item[0] >= best_score - 10
                    and (item[2].match_breakdown.get("topic_hits") or [])
                ]
                if aligned:
                    promoted = max(aligned, key=lambda item: (item[0], item[1]))
                    scored.remove(promoted)
                    scored.insert(0, promoted)
            profiles = [profile for _, _, profile in scored]
    except Exception as e:
        print(f"OpenAlex failed for '{name}': {e}")
    return profiles


async def fetch_openalex_academic_signals(
    openalex_id: str, session: aiohttp.ClientSession
) -> dict:
    """Get coauthors plus publication timing signals in one bounded request."""
    coauthors = set()
    coauthor_details = {}
    years = []
    earliest_year = None
    recent_first_author_titles = []
    current_year = datetime.now().year
    author_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id
    url = (f"{OPENALEX_BASE}/works?filter=authorships.author.id:{author_id}"
           f"&per_page=50&sort=publication_date:desc")
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for work in (data.get("results") or []):
                    year = int(work.get("publication_year") or 0)
                    if year:
                        years.append(year)
                    for authorship in (work.get("authorships") or []):
                        auth = authorship.get("author") or {}
                        n, aid = auth.get("display_name") or "", auth.get("id") or ""
                        if aid and aid != openalex_id:
                            coauthors.add(n)
                            coauthor_id = aid.rstrip("/").split("/")[-1]
                            detail = coauthor_details.setdefault(coauthor_id, {
                                "name": n,
                                "openalex_id": aid,
                                "shared_works_count": 0,
                                "recent_shared_works_count": 0,
                                "latest_shared_year": 0,
                                "sample_titles": [],
                                "shared_topics": [],
                            })
                            detail["shared_works_count"] += 1
                            if year >= current_year - 3:
                                detail["recent_shared_works_count"] += 1
                            detail["latest_shared_year"] = max(
                                detail["latest_shared_year"], year
                            )
                            if work.get("title") and len(detail["sample_titles"]) < 3:
                                detail["sample_titles"].append(work["title"])
                            topic_name = (
                                (work.get("primary_topic") or {}).get("display_name") or ""
                            )
                            if topic_name and topic_name not in detail["shared_topics"]:
                                detail["shared_topics"].append(topic_name)
                        if (
                            aid.rstrip("/").split("/")[-1] == author_id
                            and authorship.get("author_position") == "first"
                            and year >= current_year - 2
                            and work.get("title")
                        ):
                            recent_first_author_titles.append(work["title"])
    except Exception:
        pass
    # The recent-work sample above is intentionally bounded for coauthor and
    # activity analysis.  It cannot establish career length: for prolific
    # authors all 50 sampled works may come from the current year.  Ask
    # OpenAlex separately for the oldest dated work.
    oldest_url = (
        f"{OPENALEX_BASE}/works?filter=authorships.author.id:{author_id}"
        f"&per_page=1&sort=publication_date:asc"
    )
    try:
        async with session.get(
            oldest_url, headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                oldest_data = await resp.json()
                oldest_work = next(iter(oldest_data.get("results") or []), {})
                parsed_year = int(oldest_work.get("publication_year") or 0)
                if parsed_year:
                    earliest_year = parsed_year
    except Exception:
        pass
    ranked_details = []
    for detail in coauthor_details.values():
        topic_text = " ".join(detail["shared_topics"]).lower()
        relevant_topic = _quantum_physics_relevant(
            detail["shared_topics"] + detail["sample_titles"]
        )
        detail["field_relevant"] = relevant_topic
        detail["value_score"] = (
            min(100, round(
                detail["shared_works_count"] * 15
                + detail["recent_shared_works_count"] * 12 + 15
            ))
            if relevant_topic else 0
        )
        ranked_details.append(detail)
    ranked_details.sort(
        key=lambda item: (
            item["value_score"], item["recent_shared_works_count"],
            item["shared_works_count"], item["latest_shared_year"],
        ),
        reverse=True,
    )
    ranked_details = ranked_details[:30]
    return {
        "coauthors": [item["name"] for item in ranked_details] or list(coauthors)[:30],
        "coauthor_details": ranked_details,
        "earliest_publication_year": (
            earliest_year if earliest_year is not None
            else min(years) if years else None
        ),
        "latest_publication_year": max(years) if years else None,
        "recent_first_author_count": len(dict.fromkeys(recent_first_author_titles)),
        "recent_first_author_titles": list(dict.fromkeys(recent_first_author_titles))[:5],
        "works_sampled": len(years),
    }


async def search_openalex_coauthors(openalex_id: str, session: aiohttp.ClientSession) -> list[str]:
    signals = await fetch_openalex_academic_signals(openalex_id, session)
    return signals["coauthors"]


async def resolve_coauthor_identity(
    parent_openalex_id: str, coauthor_name: str,
    session: aiohttp.ClientSession, provided_openalex_id: str = "",
) -> Optional[dict]:
    """Resolve a coauthor only through the verified parent's shared works."""
    if not parent_openalex_id or not coauthor_name:
        return None
    signals = await fetch_openalex_academic_signals(parent_openalex_id, session)
    provided_id = provided_openalex_id.rstrip("/").split("/")[-1]
    matches = []
    for detail in signals.get("coauthor_details") or []:
        detail_id = (detail.get("openalex_id") or "").rstrip("/").split("/")[-1]
        if provided_id and detail_id != provided_id:
            continue
        if _canonical_name(detail.get("name") or "") == _canonical_name(coauthor_name):
            matches.append(detail)
    if not matches:
        parent_id = parent_openalex_id.rstrip("/").split("/")[-1]
        url = (
            f"{OPENALEX_BASE}/works?filter=authorships.author.id:{parent_id}"
            f"&per_page=100&sort=cited_by_count:desc"
        )
        try:
            async with session.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    by_id = {}
                    current_year = datetime.now().year
                    for work in data.get("results") or []:
                        year = int(work.get("publication_year") or 0)
                        for authorship in work.get("authorships") or []:
                            author = authorship.get("author") or {}
                            author_id = author.get("id") or ""
                            display_name = author.get("display_name") or ""
                            if (
                                not author_id
                                or _canonical_name(display_name) != _canonical_name(coauthor_name)
                            ):
                                continue
                            short_id = author_id.rstrip("/").split("/")[-1]
                            if provided_id and short_id != provided_id:
                                continue
                            detail = by_id.setdefault(short_id, {
                                "name": display_name,
                                "openalex_id": author_id,
                                "shared_works_count": 0,
                                "recent_shared_works_count": 0,
                                "latest_shared_year": 0,
                                "sample_titles": [],
                                "shared_topics": [],
                                "value_score": 0,
                            })
                            detail["shared_works_count"] += 1
                            detail["latest_shared_year"] = max(
                                detail["latest_shared_year"], year
                            )
                            if year >= current_year - 3:
                                detail["recent_shared_works_count"] += 1
                            if work.get("title") and len(detail["sample_titles"]) < 3:
                                detail["sample_titles"].append(work["title"])
                    matches.extend(by_id.values())
        except Exception:
            pass
    unique_ids = {
        (item.get("openalex_id") or "").rstrip("/").split("/")[-1]
        for item in matches if item.get("openalex_id")
    }
    if len(unique_ids) != 1:
        return None
    matches.sort(
        key=lambda item: (
            item.get("shared_works_count", 0),
            item.get("latest_shared_year", 0),
        ),
        reverse=True,
    )
    return matches[0]


async def resolve_lab_member_identity(
    name: str, session: aiohttp.ClientSession,
    parent_openalex_id: str = "", provided_openalex_id: str = "",
    affiliations: Optional[list[str]] = None,
) -> dict:
    """Resolve a roster-only person using shared works, then institution context."""
    if parent_openalex_id:
        shared = await resolve_coauthor_identity(
            parent_openalex_id, name, session, provided_openalex_id
        )
        if shared:
            return {
                **shared,
                "identity_status": "verified-shared-work",
                "identity_reason": "姓名与实验室上下文一致，且 OpenAlex 共同论文确认",
            }

    context_affiliations = [item for item in (affiliations or []) if item]
    url = f"{OPENALEX_BASE}/authors?search={quote(name)}&per_page=10"
    candidates = []
    try:
        async with session.get(
            url, headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                return {
                    "identity_status": "unresolved",
                    "identity_reason": "OpenAlex 作者查询不可用",
                }
            data = await response.json()
    except Exception:
        return {
            "identity_status": "unresolved",
            "identity_reason": "OpenAlex 作者查询不可用",
        }

    provided_key = provided_openalex_id.rstrip("/").split("/")[-1]
    for result in data.get("results") or []:
        display_name = result.get("display_name") or ""
        if _canonical_name(display_name) != _canonical_name(name):
            continue
        result_key = (result.get("id") or "").rstrip("/").split("/")[-1]
        if provided_key and result_key != provided_key:
            continue
        institution_records = _author_institution_records(result)
        candidate_affiliations = [
            item.get("display_name") or "" for item in institution_records
            if item.get("display_name")
        ]
        institution_match = any(
            _institution_similarity(expected, actual) >= 0.7
            for expected in context_affiliations
            for actual in candidate_affiliations
        )
        if provided_key or institution_match:
            candidates.append({
                "name": display_name,
                "openalex_id": result.get("id") or "",
                "orcid": result.get("orcid") or "",
                "affiliations": candidate_affiliations,
                "institution_records": institution_records,
                "works_count": int(result.get("works_count") or 0),
                "cited_by_count": int(result.get("cited_by_count") or 0),
            })
    unique_ids = {
        item["openalex_id"].rstrip("/").split("/")[-1]
        for item in candidates if item.get("openalex_id")
    }
    if len(unique_ids) == 1 and provided_key:
        candidate = candidates[0]
        return {
            **candidate,
            "identity_status": "verified-provided-id",
            "identity_reason": "已指定的 OpenAlex ID 与实验室名单姓名及机构一致",
            "shared_works_count": 0,
            "shared_topics": [],
        }
    if len(unique_ids) == 1:
        candidate = candidates[0]
        return {
            "identity_status": "review-institution-match",
            "identity_reason": (
                "找到一个同名且机构一致的 OpenAlex 候选，"
                "但缺少与 PI 共同论文等第二身份证据，未自动采用"
            ),
            "candidate_openalex_id": candidate.get("openalex_id") or "",
            "candidate_works_count": candidate.get("works_count") or 0,
            "candidate_cited_by_count": candidate.get("cited_by_count") or 0,
        }
    return {
        "identity_status": "ambiguous" if candidates else "unresolved",
        "identity_reason": (
            "找到多个同名且机构匹配的 OpenAlex 作者"
            if candidates else "未找到可由共同论文或机构唯一确认的 OpenAlex 身份"
        ),
    }


async def crawl_lab_member_contact(
    candidate: dict, session: aiohttp.ClientSession,
) -> AuthorProfile:
    """Resolve and search one official-roster member with a bounded workflow."""
    name = (candidate.get("name") or "").strip()
    profile = AuthorProfile(
        name=name,
        email=candidate.get("email") or None,
        email_source=candidate.get("email_source") or candidate.get("profile_url"),
        email_confidence=float(candidate.get("email_confidence") or 0),
        phone=candidate.get("phone") or None,
        phone_source=candidate.get("phone_source") or candidate.get("profile_url"),
        phone_confidence=float(candidate.get("phone_confidence") or 0),
        website_url=candidate.get("profile_url") or None,
        career_stage=candidate.get("career_stage") or "graduate-student",
        degree_type=candidate.get("degree_type") or "",
        student_score=float(candidate.get("student_score") or 82),
        student_status=candidate.get("student_status") or "confirmed-student",
        student_evidence=candidate.get("student_evidence") or [{
            "type": "official-lab-roster",
            "value": candidate.get("evidence") or "Graduate student",
            "source": candidate.get("directory_url") or candidate.get("source") or "",
            "confidence": 95,
        }],
        lab_name=candidate.get("lab_name") or "",
        lab_url=candidate.get("lab_url") or "",
        directory_url=candidate.get("directory_url") or candidate.get("source") or "",
        # parent_name is an identity-search anchor/discoverer, not proof that
        # this person is the lab's principal investigator.
        lab_pi=candidate.get("lab_pi") or "",
        discovery_origin="lab-member",
    )
    identity = await resolve_lab_member_identity(
        name, session,
        candidate.get("parent_openalex_id") or "",
        candidate.get("openalex_id") or "",
        candidate.get("affiliations") or [],
    )
    profile.identity_status = identity.get("identity_status") or "unresolved"
    profile.search_failure_reason = identity.get("identity_reason") or ""

    if identity.get("openalex_id"):
        resolved_name = identity.get("name") or name
        source = identity.get("identity_reason") or "Official lab roster identity resolution"
        context = {
            "openalex_authors_map": {resolved_name: {
                "display_name": resolved_name,
                "openalex_id": identity["openalex_id"],
                "orcid": identity.get("orcid") or "",
                "affiliations": identity.get("affiliations") or [],
                "institution_records": identity.get("institution_records") or [],
                "source": source,
            }},
            "matched_work_source": source,
            "paper_title": "Official laboratory member expansion",
            "source_type": "lab-member-expansion",
            "source_info": profile.directory_url,
            "institutions": identity.get("affiliations") or candidate.get("affiliations") or [],
            "keywords": identity.get("shared_topics") or [],
        }
        crawled = await crawl_multiple_authors([resolved_name], context)
        if crawled:
            resolved = crawled[0]
            resolved.name = name
            resolved.discovery_origin = "lab-member"
            for field_name in [
                "career_stage", "degree_type", "student_score", "student_status",
                "student_evidence", "lab_name", "lab_url", "directory_url", "lab_pi",
            ]:
                value = getattr(profile, field_name)
                if value not in (None, "", [], 0, 0.0):
                    setattr(resolved, field_name, value)
            resolved.identity_status = profile.identity_status
            resolved.sources.append(
                f"Official lab roster: {profile.directory_url or profile.lab_url}"
            )
            profile = resolved
    else:
        # Even without an academic ID, a roster-linked personal page is safe to
        # inspect because the official directory established page ownership.
        if profile.website_url:
            personal = await search_verified_personal_websites(
                name, [], [profile.website_url], session
            )
            profile.search_trail.extend(personal.get("trail") or [])
            if personal.get("email"):
                profile.email = personal["email"]
                profile.email_source = personal.get("email_source")
                profile.email_confidence = personal.get("email_confidence") or 0
            if personal.get("phone"):
                profile.phone = personal["phone"]
                profile.phone_source = personal.get("phone_source")
                profile.phone_confidence = personal.get("phone_confidence") or 0

        lab_domain = _canonical_host(profile.lab_url or profile.directory_url)
        if lab_domain:
            official = await search_official_institution_network(
                name, [{
                    "institution": profile.lab_name or "Official laboratory site",
                    "homepage_url": profile.lab_url or profile.directory_url,
                    "domain": lab_domain,
                    "country_code": "CN" if lab_domain.endswith(".cn") else "",
                    "source": "Official laboratory roster",
                }], session, [],
            )
            profile.search_trail.extend(official.get("trail") or [])
            profile.contact_candidates.extend(official.get("candidates") or [])
            if official.get("email") and not profile.email:
                profile.email = official["email"]
                profile.email_source = official.get("email_source")
                profile.email_confidence = official.get("email_confidence") or 0
            if official.get("phone") and not profile.phone:
                profile.phone = official["phone"]
                profile.phone_source = official.get("phone_source")
                profile.phone_confidence = official.get("phone_confidence") or 0

    profile.pages_checked = sum(
        int(item.get("pages_read") or 0) for item in profile.search_trail
        if item.get("stage") == "institution-network"
    ) + sum(
        1 for item in profile.search_trail
        if item.get("stage") in {"orcid-personal-site", "official-page"}
    )
    if profile.email or profile.phone:
        profile.contact_search_status = "contact-found"
        profile.search_failure_reason = ""
    elif profile.identity_status == "ambiguous" or profile.identity_status.startswith("review-"):
        profile.contact_search_status = "identity-review"
    else:
        profile.contact_search_status = "completed-no-contact"
        if not profile.search_failure_reason:
            profile.search_failure_reason = "已完成受控搜索，未找到能安全归属的公开联系方式"
    return profile


def apply_academic_signals(profile: AuthorProfile, signals: dict):
    profile.academic_timeline = {
        key: value for key, value in signals.items()
        if key not in {"coauthors", "coauthor_details"}
    }
    profile.coauthor_details = signals.get("coauthor_details") or []
    recent_first_author_count = int(signals.get("recent_first_author_count") or 0)
    if (
        recent_first_author_count
        and profile.career_stage in {"doctoral-student", "masters-student"}
    ):
        profile.graduation_score = min(
            100.0, profile.graduation_score + min(15, recent_first_author_count * 5)
        )
        profile.graduation_evidence.append({
            "type": "recent-first-author-publications",
            "value": f"近三年第一作者论文 {recent_first_author_count} 篇",
            "source": (
                f"https://openalex.org/{(profile.openalex_id or '').rstrip('/').split('/')[-1]}"
            ),
            "confidence": 75,
        })
        profile.graduation_status = _graduation_status(
            profile.graduation_score,
            any(item.get("type") == "expected-graduation"
                for item in profile.graduation_evidence),
            profile.career_stage,
        )


def assess_student_status(profile: AuthorProfile) -> None:
    """Classify student likelihood with role evidence first, metrics second."""
    score = 0.0
    evidence = []
    stage = (profile.career_stage or "unknown").lower()
    if stage == "recent-graduate":
        profile.student_score = 15.0
        profile.student_status = "confirmed-non-student"
        profile.student_evidence = [{
            "type": "official-former-student", "value": "已毕业/实验室前成员",
            "source": next((item.get("source") for item in profile.graduation_evidence
                            if item.get("source")), profile.website_url or ""),
            "confidence": 98, "weight": -85,
        }]
        return
    strong_non_student = stage in {"faculty", "postdoc", "staff"}
    if strong_non_student:
        profile.student_score = 0.0
        profile.student_status = "confirmed-non-student"
        profile.student_evidence = [{
            "type": "official-non-student-role", "value": stage,
            "source": next((item.get("source") for item in profile.graduation_evidence
                            if item.get("source")), ""),
            "confidence": 90, "weight": -100,
        }]
        return

    official_lab_student = any(
        item.get("type") == "official-lab-roster"
        for item in profile.student_evidence or []
    )
    if stage in {"doctoral-student", "masters-student", "graduate-student"}:
        score += 65
        evidence.append({
            "type": "official-student-role", "value": stage,
            "source": next((item.get("source") for item in profile.graduation_evidence
                            if item.get("source")), profile.website_url or ""),
            "confidence": 90, "weight": 65,
        })
    if official_lab_student:
        score += 25
        evidence.extend(profile.student_evidence)
    if profile.expected_graduation_year:
        score += 15
        evidence.append({
            "type": "expected-graduation", "value": str(profile.expected_graduation_year),
            "source": profile.website_url or "", "confidence": 90, "weight": 15,
        })

    timeline = profile.academic_timeline or {}
    earliest = int(timeline.get("earliest_publication_year") or 0)
    current_year = datetime.now().year
    if earliest and current_year - earliest <= 6:
        score += 10
        evidence.append({
            "type": "short-publication-history", "value": f"学术记录约 {current_year-earliest} 年",
            "source": profile.openalex_id or "OpenAlex", "confidence": 45, "weight": 10,
        })
    elif earliest and current_year - earliest > 10:
        score -= 15
        evidence.append({
            "type": "long-publication-history", "value": f"学术记录约 {current_year-earliest} 年",
            "source": profile.openalex_id or "OpenAlex", "confidence": 60, "weight": -15,
        })
    recent_first = int(timeline.get("recent_first_author_count") or 0)
    if recent_first:
        addition = min(10, recent_first * 3)
        score += addition
        evidence.append({
            "type": "recent-first-author", "value": f"近三年 {recent_first} 篇",
            "source": profile.openalex_id or "OpenAlex", "confidence": 55, "weight": addition,
        })
    # Publication volume and citations are deliberately weak hints. A low
    # count alone must never turn an unknown person into a confirmed student.
    if profile.works_count > 100:
        score -= 10
    elif 0 < profile.works_count <= 20:
        score += 3
    if profile.cited_by_count > 5000:
        score -= 5

    if official_lab_student:
        score = max(score, 85.0)

    profile.student_score = max(0.0, min(100.0, score))
    if profile.student_score >= 80:
        profile.student_status = "confirmed-student"
    elif profile.student_score >= 60:
        profile.student_status = "likely-student"
    elif profile.student_score >= 35:
        profile.student_status = "possible-student"
    elif score < 0:
        # Publication age/volume are weak hints and OpenAlex profiles can be
        # conflated. Without an explicit faculty/staff role, negative metric
        # evidence must not classify someone as a non-student.
        profile.student_status = "unknown"
    else:
        profile.student_status = "unknown"
    profile.student_evidence = evidence


def apply_lab_member_evidence(profiles: list[AuthorProfile]) -> None:
    """Enrich paper authors when a verified lab directory names the same person."""
    member_by_name = {}
    pi_by_name = {}
    for owner in profiles:
        for member in owner.lab_members or owner.graduate_candidates or []:
            key = _canonical_name(member.get("name") or "")
            if key:
                member_by_name[key] = member
            pi_name = member.get("lab_pi") or ""
            pi_key = _canonical_name(pi_name)
            if pi_key:
                pi_by_name[pi_key] = member
    for profile in profiles:
        pi_evidence = pi_by_name.get(_canonical_name(profile.name))
        if pi_evidence:
            profile.career_stage = "faculty"
            profile.graduation_status = "not-student"
            profile.student_score = 0
            profile.student_status = "confirmed-non-student"
            profile.student_evidence = [{
                "type": "official-lab-pi", "value": "Principal Investigator",
                "source": pi_evidence.get("directory_url") or pi_evidence.get("source") or "",
                "confidence": 100, "weight": -100,
            }]
        member = member_by_name.get(_canonical_name(profile.name))
        if member and not pi_evidence:
            profile.career_stage = member.get("career_stage") or profile.career_stage
            profile.degree_type = member.get("degree_type") or profile.degree_type
            if member.get("email") and not profile.email:
                profile.email = member["email"]
                profile.email_source = member.get("email_source") or member.get("profile_url")
                profile.email_confidence = float(member.get("email_confidence") or 90)
            if member.get("phone") and not profile.phone:
                profile.phone = member["phone"]
                profile.phone_source = member.get("phone_source") or member.get("profile_url")
                profile.phone_confidence = float(member.get("phone_confidence") or 90)
            profile.website_url = member.get("profile_url") or profile.website_url
            profile.student_evidence = list(member.get("student_evidence") or [{
                "type": "official-lab-roster",
                "value": member.get("evidence") or profile.career_stage,
                "source": member.get("directory_url") or member.get("source") or "",
                "confidence": 95,
            }])
            profile.graduation_evidence.append({
                "type": "official-lab-roster", "value": member.get("evidence") or profile.career_stage,
                "source": member.get("directory_url") or member.get("source") or "",
                "confidence": 95,
            })
        if not pi_evidence:
            assess_student_status(profile)


async def fetch_orcid_public_profile(
    orcid_url: str, session: aiohttp.ClientSession
) -> dict:
    """Read public ORCID contacts, alternate names, and researcher URLs."""
    result = {"email": None, "name_aliases": [], "websites": []}
    if not orcid_url:
        return result
    orcid_id = orcid_url.rstrip("/").split("/")[-1]
    try:
        async with session.get(f"{ORCID_BASE}/{orcid_id}/person",
                               headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for e in (data.get("emails") or {}).get("email") or []:
                    if e.get("email"):
                        result["email"] = e["email"]
                        break
                person_name = data.get("name") or {}
                credit_name = ((person_name.get("credit-name") or {}).get("value") or "")
                given = ((person_name.get("given-names") or {}).get("value") or "")
                family = ((person_name.get("family-name") or {}).get("value") or "")
                aliases = [credit_name, f"{given} {family}".strip()]
                aliases.extend(
                    item.get("content") or ""
                    for item in ((data.get("other-names") or {}).get("other-name") or [])
                )
                result["name_aliases"] = _dedupe_name_variants("", aliases)
                for item in (
                    (data.get("researcher-urls") or {}).get("researcher-url") or []
                ):
                    url = ((item.get("url") or {}).get("value") or "").strip()
                    if url.startswith(("http://", "https://")):
                        result["websites"].append(url)
                result["websites"] = list(dict.fromkeys(result["websites"]))[:5]
    except Exception:
        pass
    return result


async def extract_email_from_orcid(
    orcid_url: str, session: aiohttp.ClientSession
) -> Optional[str]:
    return (await fetch_orcid_public_profile(orcid_url, session)).get("email")


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
        "candidates": [],
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
        "candidates": [],
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
                result["candidates"].append({
                    "type": "email",
                    "value": email,
                    "source": (entry.findtext(f"{atom}id") or "arXiv").strip(),
                    "status": "rejected",
                    "owner_type": (
                        "group-or-public" if not _is_personal_email(email) else "unknown"
                    ),
                    "contact_kind": (
                        "group/public" if not _is_personal_email(email) else "unverified"
                    ),
                    "confidence": 0,
                    "reason": (
                        "邮箱属于合作组/公共账号"
                        if not _is_personal_email(email)
                        else "邮箱用户名不能证明属于该作者，避免误配同篇论文其他作者"
                    ),
                })
                continue
            result["email"] = email
            entry_id = (entry.findtext(f"{atom}id") or "").strip()
            result["source"] = (
                f"arXiv paper: {entry_id}" if entry_id else "arXiv paper metadata"
            )
            result["candidates"].append({
                "type": "email",
                "value": email,
                "source": entry_id or "arXiv paper metadata",
                "status": "accepted",
                "owner_type": "target",
                "contact_kind": "personal",
                "confidence": 75,
                "reason": "arXiv 条目包含目标作者，且邮箱用户名与作者姓氏匹配",
            })
            return result
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
        if direct_identity and (
            direct_identity.get("openalex_id")
            or context.get("skip_openalex_lookup")
        ):
            fetched_profile = None
            if direct_identity.get("openalex_id"):
                fetched_profile = await fetch_openalex_author(
                    direct_identity["openalex_id"], name, session, author_context
                )
            profile = fetched_profile or AuthorProfile(
                name=name,
                name_aliases=[name],
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
            profile.institution_records = _dedupe_institution_records(
                (direct_identity.get("institution_records") or [])
                + (profile.institution_records or [])
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
        profile.name_aliases = _dedupe_name_variants(
            name, profile.name_aliases
        )
        if specific_institutions:
            # The uploaded paper is authoritative for affiliation in this
            # publication; do not show unrelated affiliations from a namesake.
            profile.affiliations = specific_institutions

        paper_email = (context.get("author_email_map") or {}).get(name)
        if paper_email:
            profile.email = paper_email
            profile.email_source = "Uploaded paper PDF"
            profile.email_confidence = 100.0
            profile.sources.append(f"Email printed in uploaded paper: {paper_email}")
            profile.contact_candidates.append({
                "type": "email", "value": paper_email,
                "source": "Uploaded paper PDF", "status": "accepted",
                "owner_type": "target", "contact_kind": "personal",
                "confidence": 100,
                "reason": "论文解析结果将该邮箱明确映射到此作者",
            })
            profile.search_trail.append({
                "stage": "paper-pdf", "path": "论文正文/作者信息",
                "status": "accepted", "results": 1,
                "reason": "论文中找到并完成作者归属映射",
            })
        else:
            profile.search_trail.append({
                "stage": "paper-pdf", "path": "论文正文/作者信息",
                "status": "no-results", "results": 0,
                "reason": "论文中未找到能明确归属于此作者的邮箱",
            })

        identity_verified = profile.match_status == "verified"
        if identity_verified:
            china_institutions = [
                record.get("display_name") or record.get("id") or "中国机构"
                for record in (profile.institution_records or [])
                if (record.get("country_code") or "").upper() == "CN"
            ]
            if china_institutions:
                profile.china_link_score = 20.0
                profile.china_link_status = "weak"
                profile.china_link_evidence.append({
                    "type": "current-or-recent-china-institution",
                    "value": "；".join(china_institutions),
                    "source": direct_identity.get("source", "OpenAlex") if direct_identity else "OpenAlex",
                    "confidence": 90,
                })
        orcid_profile = {"email": None, "name_aliases": [], "websites": []}
        if identity_verified and profile.orcid:
            orcid_profile = await fetch_orcid_public_profile(
                profile.orcid, session
            )
            orcid_email = orcid_profile.get("email")
            profile.name_aliases = _dedupe_name_variants(
                name,
                profile.name_aliases + (orcid_profile.get("name_aliases") or []),
            )
            if orcid_profile.get("websites") and not profile.website_url:
                profile.website_url = orcid_profile["websites"][0]
            if orcid_email:
                profile.contact_candidates.append({
                    "type": "email", "value": orcid_email,
                    "source": profile.orcid, "status": "accepted",
                    "owner_type": "target", "contact_kind": "personal",
                    "confidence": 100,
                    "reason": "邮箱由已验证作者的公开 ORCID 记录直接提供",
                })
            if orcid_email and not profile.email:
                profile.email = orcid_email
                profile.email_source = f"Public ORCID record: {profile.orcid}"
                profile.email_confidence = 100.0
                profile.sources.append(f"Email from public ORCID: {profile.orcid}")
            profile.search_trail.append({
                "stage": "orcid", "path": "公开 ORCID 联系方式与姓名变体",
                "status": "results" if (
                    orcid_email or orcid_profile.get("name_aliases")
                    or orcid_profile.get("websites")
                ) else "no-results",
                "url": profile.orcid,
                "results": (
                    int(bool(orcid_email))
                    + len(orcid_profile.get("name_aliases") or [])
                    + len(orcid_profile.get("websites") or [])
                ),
                "reason": (
                    f"公开记录：邮箱 {'1' if orcid_email else '0'} 条，"
                    f"姓名变体 {len(orcid_profile.get('name_aliases') or [])} 条，"
                    f"个人链接 {len(orcid_profile.get('websites') or [])} 条"
                ),
            })

        # Follow the verified affiliation graph into official institution,
        # quantum/physics department, lab, directory, and staff pages.
        if identity_verified:
            profile.institution_sites = await resolve_institution_sites(
                profile.institution_records, profile.affiliations, session
            )
            for site in profile.institution_sites:
                profile.sources.append(
                    "Official institution site resolved: "
                    f"{site.get('institution') or site.get('domain')} | "
                    f"{site.get('homepage_url')}"
                )
            if profile.institution_sites:
                official = await search_official_institution_network(
                    name, profile.institution_sites, session,
                    profile.name_aliases,
                )
                profile.search_trail.extend(official.get("trail") or [])
                profile.contact_candidates.extend(official.get("candidates") or [])
                profile.graduate_candidates = official.get("graduate_candidates") or []
                background = official.get("background") or {}
                for field_name in [
                    "career_stage", "degree_type", "graduation_score",
                    "graduation_status", "expected_graduation_year",
                    "china_link_score", "china_link_status", "nationality",
                ]:
                    value = background.get(field_name)
                    if value not in (None, "", 0, 0.0, "none", "insufficient"):
                        setattr(profile, field_name, value)
                for field_name in [
                    "graduation_evidence", "china_link_evidence",
                    "nationality_evidence",
                ]:
                    current_values = getattr(profile, field_name)
                    for evidence in background.get(field_name) or []:
                        if evidence not in current_values:
                            current_values.append(evidence)
                if official.get("website_url"):
                    profile.website_url = official["website_url"]
                    profile.sources.append(
                        f"Official author or department page: {profile.website_url}"
                    )
                if official.get("email") and not profile.email:
                    profile.email = official["email"]
                    profile.email_source = official.get("email_source")
                    profile.email_confidence = official.get("email_confidence", 0)
                    profile.sources.append(
                        "Verified email from official institution page: "
                        f"{profile.email} | {profile.email_source}"
                    )
                if official.get("phone"):
                    profile.phone = official["phone"]
                    profile.phone_source = official.get("phone_source")
                    profile.phone_confidence = official.get("phone_confidence", 0)
                    profile.sources.append(
                        "Public phone from official institution page: "
                        f"{profile.phone} | {profile.phone_source}"
                    )
                matched_pages = sum(
                    1 for item in profile.search_trail
                    if item.get("status") == "author-matched"
                )
                pages_read = sum(
                    item.get("pages_read", 0) for item in profile.search_trail
                    if item.get("stage") == "institution-network"
                )
                profile.sources.append(
                    "Official institution network search: "
                    f"{len(profile.institution_sites)} domains, "
                    f"{pages_read} pages, {matched_pages} author-matched pages"
                )
            else:
                profile.search_trail.append({
                    "stage": "institution-resolution",
                    "path": "学校/院系/课题组官网",
                    "status": "skipped",
                    "reason": "没有从已核验机构记录解析出可安全限定的官方网站域名",
                })

            if orcid_profile.get("websites"):
                personal_sites = await search_verified_personal_websites(
                    name, profile.name_aliases,
                    orcid_profile["websites"], session,
                )
                profile.contact_candidates.extend(
                    personal_sites.get("candidates") or []
                )
                profile.search_trail.extend(personal_sites.get("trail") or [])
                known_students = {
                    _canonical_name(item.get("name") or "")
                    for item in profile.graduate_candidates
                }
                for student in personal_sites.get("graduate_candidates") or []:
                    key = _canonical_name(student.get("name") or "")
                    if key not in known_students:
                        profile.graduate_candidates.append(student)
                        known_students.add(key)
                profile.graduate_candidates = profile.graduate_candidates[
                    :MAX_GRADUATE_CANDIDATES
                ]
                profile.lab_members = personal_sites.get("lab_members") or []
                personal_background = personal_sites.get("background") or {}
                if personal_background.get("career_stage") not in (None, "", "unknown"):
                    profile.career_stage = personal_background["career_stage"]
                    profile.degree_type = personal_background.get("degree_type") or profile.degree_type
                    profile.graduation_score = max(
                        profile.graduation_score,
                        float(personal_background.get("graduation_score") or 0),
                    )
                    profile.graduation_status = personal_background.get("graduation_status") or profile.graduation_status
                    for evidence in personal_background.get("graduation_evidence") or []:
                        if evidence not in profile.graduation_evidence:
                            profile.graduation_evidence.append(evidence)
                if personal_sites.get("email") and not profile.email:
                    profile.email = personal_sites["email"]
                    profile.email_source = personal_sites["email_source"]
                    profile.email_confidence = personal_sites["email_confidence"]
                    profile.sources.append(
                        "Verified email from ORCID-linked personal website: "
                        f"{profile.email} | {profile.email_source}"
                    )
                if (
                    personal_sites.get("phone")
                    and personal_sites["phone_confidence"] > profile.phone_confidence
                ):
                    profile.phone = personal_sites["phone"]
                    profile.phone_source = personal_sites["phone_source"]
                    profile.phone_confidence = personal_sites["phone_confidence"]

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
            profile.search_trail.append({
                "stage": "google-scholar", "path": "Google Scholar 作者主页",
                "status": "profile-found" if scholar.get("url") else "no-results",
                "url": scholar.get("url") or "",
                "results": 1 if scholar.get("url") else 0,
                "reason": (
                    "找到候选作者主页" if scholar.get("url")
                    else "没有找到可确认的作者主页"
                ),
            })
            if scholar.get("email"):
                profile.contact_candidates.append({
                    "type": "email", "value": scholar["email"],
                    "source": scholar.get("source") or scholar.get("url"),
                    "status": "review", "owner_type": "target",
                    "contact_kind": "personal", "confidence": 70,
                    "reason": "邮箱出现在候选 Google Scholar 作者主页中，低于自动采用阈值",
                })

            if rg.get("url"):
                profile.researchgate_url = rg["url"]
                profile.sources.append(f"ResearchGate: {rg['url']}")
            profile.search_trail.append({
                "stage": "researchgate", "path": "ResearchGate 作者主页",
                "status": "profile-found" if rg.get("url") else "no-results",
                "url": rg.get("url") or "",
                "results": 1 if rg.get("url") else 0,
                "reason": (
                    "找到候选作者主页" if rg.get("url")
                    else "没有找到可确认的作者主页"
                ),
            })
            if rg.get("email"):
                profile.contact_candidates.append({
                    "type": "email", "value": rg["email"],
                    "source": rg.get("url") or "ResearchGate",
                    "status": "review", "owner_type": "target",
                    "contact_kind": "personal", "confidence": 65,
                    "reason": "邮箱出现在候选 ResearchGate 作者主页中，低于自动采用阈值",
                })

            profile.twitter_url = social.get("twitter_url")
            profile.linkedin_url = social.get("linkedin_url")
            if profile.twitter_url:
                profile.sources.append(f"Twitter/X: {profile.twitter_url}")
            if profile.linkedin_url:
                profile.sources.append(f"LinkedIn: {profile.linkedin_url}")
            profile.search_trail.append({
                "stage": "public-profiles", "path": "公开职业/社交主页",
                "status": "profile-found" if (
                    profile.twitter_url or profile.linkedin_url
                ) else "no-results",
                "results": int(bool(profile.twitter_url)) + int(bool(profile.linkedin_url)),
                "reason": (
                    "发现公开主页链接，仅作身份辅助，不自动提取联系方式"
                    if profile.twitter_url or profile.linkedin_url
                    else "未发现可确认的 LinkedIn 或 Twitter/X 主页"
                ),
            })
        else:
            profile.sources.append(
                "External profile searches skipped until an OpenAlex identity "
                "candidate is confirmed."
            )

        # Conservative fallback: arXiv only accepts an email attributable to
        # this author in the same paper entry.
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
                arxiv = await search_arxiv_for_email(name, inst_for_search, session)
                profile.contact_candidates.extend(arxiv.get("candidates") or [])
                profile.search_trail.append({
                    "stage": "arxiv", "path": "arXiv 作者历史论文",
                    "status": "accepted" if arxiv.get("email") else "no-verified-contact",
                    "results": arxiv.get("papers_found", 0),
                    "matched_results": arxiv.get("matched_papers", 0),
                    "rejected": arxiv.get("rejected_candidates", 0),
                    "reason": (
                        "找到邮箱，且用户名可归属于目标作者"
                        if arxiv.get("email") else
                        f"检索到 {arxiv.get('papers_found', 0)} 篇，未找到可安全归属的邮箱"
                    ),
                })
                if arxiv.get("email"):
                    profile.email = arxiv["email"]
                    profile.email_source = arxiv.get("source", "arXiv")
                    profile.email_confidence = 75.0
                    profile.sources.append(
                        f"Email from arXiv ({arxiv.get('papers_found',0)} papers): {profile.email}"
                    )
            else:
                profile.search_trail.append({
                    "stage": "arxiv", "path": "arXiv 作者历史论文",
                    "status": "skipped",
                    "reason": "缺少已核验机构信息，未进行容易产生同名误配的宽泛检索",
                })
        elif identity_verified:
            profile.search_trail.append({
                "stage": "arxiv", "path": "arXiv 作者历史论文",
                "status": "skipped",
                "reason": "已有更高可信度邮箱，未用 arXiv 低优先级结果覆盖",
            })

        # Get co-authors
        if identity_verified and profile.openalex_id:
            academic_signals = await fetch_openalex_academic_signals(
                profile.openalex_id, session
            )
            profile.coauthors = academic_signals["coauthors"]
            apply_academic_signals(profile, academic_signals)
            profile.sources.append(
                f"Co-authors: {len(profile.coauthors)} via OpenAlex"
            )

        profile.contact_candidates = sorted(
            profile.contact_candidates,
            key=lambda item: (
                item.get("status") == "accepted",
                float(item.get("confidence") or 0),
            ),
            reverse=True,
        )[:30]
        profile.search_trail = profile.search_trail[:MAX_SEARCH_TRAIL]

        assess_student_status(profile)

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
    apply_lab_member_evidence(results)
    return results
