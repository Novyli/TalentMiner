"""PDF parser: extract text and identify author names from academic papers."""

import re
import unicodedata
from difflib import SequenceMatcher
import fitz

NAME_TOKEN = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’‐‑-]+"
INITIAL_TOKEN = r"(?:[A-Z]\.){1,3}"
PERSON_NAME_PATTERN = re.compile(
    rf"\b((?:{NAME_TOKEN}|{INITIAL_TOKEN})"
    rf"(?:[ \t]+(?:{NAME_TOKEN}|{INITIAL_TOKEN})){{1,3}})\b"
)

# Words that strongly indicate a title, affiliation, section, or organization
# rather than a person's name.
NON_NAME_WORDS = {
    "academy", "accepted", "affiliated", "artificial", "article", "atom",
    "beijing", "center", "centre", "chair", "china", "chinese", "college",
    "communication", "communications", "compilation", "computer", "computing",
    "controlled", "corporation", "dated", "department", "diagonal",
    "engineering", "faculty", "frontier", "gate", "gates", "group", "guided",
    "hospital", "information", "institute", "intelligence", "kingdom",
    "laboratory", "multiqubit", "native", "neutral", "oxford", "phase",
    "physics", "processors", "quantum", "received", "research", "review",
    "science", "sciences", "school", "state", "technology", "united",
    "university", "working",
}
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


def extract_text_from_pdf(filepath: str) -> str:
    doc = fitz.open(filepath)
    full_text = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            full_text.append(text.strip())
    doc.close()
    return "\n\n".join(full_text)


def extract_metadata_from_pdf(filepath: str) -> dict:
    doc = fitz.open(filepath)
    metadata = doc.metadata
    result = {"title": metadata.get("title", ""), "authors": [], "doi": ""}
    first_page = doc[0].get_text()
    doi_match = re.search(r'10\.\d{4,}/[^\s]+', first_page)
    if doi_match:
        result["doi"] = doi_match.group(0).rstrip('.,;')
    if not result["title"]:
        lines = [l.strip() for l in first_page.split('\n') if l.strip()]
        if lines:
            result["title"] = lines[0]
    metadata_authors = _parse_pdf_metadata_authors(metadata.get("author", ""))
    # Multiple names in embedded PDF metadata are normally publisher/arXiv
    # supplied and are more reliable than visual text heuristics.
    if len(metadata_authors) >= 2:
        result["authors"] = metadata_authors
    else:
        detected = extract_author_names(first_page[:5000], title=result["title"])
        result["authors"] = _dedupe_names(metadata_authors + detected)
    doc.close()
    return result


def _dedupe_names(names: list[str]) -> list[str]:
    unique = []
    seen = set()
    for name in names:
        clean = re.sub(r"\s+", " ", name).strip().rstrip(".,;:")
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            unique.append(clean)
    return unique


def _looks_like_person_name(candidate: str, title: str = "") -> bool:
    clean = re.sub(r"\s+", " ", candidate).strip(" ,.;:()[]{}")
    parts = clean.split()
    if not 2 <= len(parts) <= 4:
        return False
    words = [
        re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]", "", part).casefold()
        for part in parts
    ]
    if any(word in NON_NAME_WORDS for word in words if word):
        return False
    if any(len(word) == 1 and not re.fullmatch(r"[A-Z]\.", part)
           for word, part in zip(words, parts)):
        return False
    normalized_title = re.sub(r"\W+", " ", title, flags=re.UNICODE).casefold()
    normalized_candidate = re.sub(r"\W+", " ", clean, flags=re.UNICODE).casefold()
    if normalized_candidate and normalized_candidate in normalized_title:
        return False
    return True


def _parse_pdf_metadata_authors(raw_author: str) -> list[str]:
    if not raw_author:
        return []
    # arXiv and many publishers use semicolons for multiple embedded authors.
    parts = re.split(r"\s*;\s*|\s+\band\b\s+|\s*&\s*", raw_author.strip())
    authors = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip()
        if _looks_like_person_name(part):
            authors.append(part)
    return _dedupe_names(authors)


def extract_author_names(text: str, title: str = "") -> list:
    """Extract likely person names, preferring explicit author markup/sections."""
    authors = []

    # Priority 1: Bold markers **Name** (from pasted Markdown)
    bold_pattern = re.compile(r'\*\*([^*]+?)\*\*')
    for m in bold_pattern.findall(text):
        name = m.strip()
        if (2 < len(name) < 80
            and not re.match(r'^[\d\s,.，。、；;！!？?]+$', name)
            and not any(x in name.lower() for x in
                       ['university', '大学', '学院', 'lab', '实验室', 'inc', 'corp', '公司',
                        'doi', 'abstract', 'figure', 'table', 'reference', 'introduction'])):
            parts = re.split(r'[,，、]|\s+(?:and|&)\s+', name)
            for p in parts:
                p = p.strip()
                if 2 < len(p) < 80 and _looks_like_person_name(p):
                    authors.append(p)
    if authors:
        return _dedupe_names(authors)[:50]

    # Priority 2: Explicit "Authors:" section
    author_sec = re.search(
        r'(?:authors?|Authors?|AUTHORS?)[:\s]*(.+?)(?:\n\n|\n[A-Z]|\nAbstract|\n摘要)',
        text, re.DOTALL
    )
    if author_sec:
        for part in re.split(r'[,;，；\n]+|\s+(?:and|&)\s+', author_sec.group(1)):
            part = re.sub(r'[\d*†‡§¶#]', '', part).strip()
            part = re.sub(r'\s+', ' ', part)
            if 3 < len(part) < 100 and _looks_like_person_name(part, title):
                part = re.sub(r'\s*\([^)]*\)', '', part)
                if part and not re.match(r'^(and|et\s+al|or|the|for|with)$', part, re.I):
                    authors.append(part.strip())

    # Priority 3: Only inspect the document header. Stop before the first
    # obvious body marker; never scan the full paper for capitalized phrases.
    header = text[:5000]
    boundaries = []
    for pattern in [
        r"\n\s*abstract\b", r"\n\s*(?:I\.\s*)?introduction\b",
        r"\n\s*摘要", r"\n\s*received\s*:", r"\n\s*\(dated\s*:",
    ]:
        match = re.search(pattern, header, re.IGNORECASE)
        if match:
            boundaries.append(match.start())
    if boundaries:
        header = header[:min(boundaries)]

    # Remove numeric affiliation markers while retaining punctuation that
    # separates adjacent author names.
    header = re.sub(r"(?<=\D)\d+(?:\s*,\s*\d+)*", " ", header)
    for match in PERSON_NAME_PATTERN.finditer(header):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        if _looks_like_person_name(candidate, title):
            authors.append(candidate)

    # Priority 4: Chinese names (only if no bold)
    cn = re.compile(r'(?:[\u4e00-\u9fff]{2,4})\s*[,，、]\s*(?:[\u4e00-\u9fff]{2,4})')
    for m in cn.findall(header[:2000]):
        for part in re.split(r'[,，、]\s*', m):
            if 2 <= len(part) <= 4:
                authors.append(part)

    return _dedupe_names(authors)[:50]


def extract_authors_from_text(text: str) -> list:
    return extract_author_names(text)


def _ascii_alnum(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return re.sub(
        r"[^a-z0-9]", "",
        normalized.encode("ascii", "ignore").decode("ascii").lower(),
    )


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return bool(needle) and all(char in iterator for char in needle)


def _email_name_score(email: str, name: str) -> float:
    local = _ascii_alnum(email.split("@", 1)[0])
    raw_parts = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", name)
    parts = [_ascii_alnum(part) for part in raw_parts if _ascii_alnum(part)]
    if len(parts) < 2:
        return 0.0
    surname = parts[-1]
    given = "".join(parts[:-1])
    variants = {
        given + surname,
        surname + given,
        parts[0][:1] + surname,
        surname + parts[0][:1],
        "".join(part[:1] for part in parts),
    }
    if local in variants:
        return 1.0
    if local.startswith(surname):
        remainder = local[len(surname):]
        if len(remainder) >= 1 and _is_subsequence(remainder, given):
            return 0.92
    if local.endswith(surname):
        prefix = local[:-len(surname)]
        if len(prefix) >= 1 and _is_subsequence(prefix, given):
            return 0.9
    return max(SequenceMatcher(None, local, variant).ratio()
               for variant in variants)


def extract_author_email_map(text: str, authors: list[str]) -> dict[str, str]:
    """Match public emails printed in a paper to their most likely authors."""
    emails = _dedupe_names(EMAIL_PATTERN.findall(text[:12000]))
    mapping = {}
    for email in emails:
        ranked = sorted(
            [(_email_name_score(email, name), name) for name in authors],
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.78:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            continue
        mapping[ranked[0][1]] = email
    return mapping


def _extract_numbered_affiliation_map(text: str) -> dict[str, str]:
    """Extract numbered affiliation blocks from the first page."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text[:7000].splitlines()]
    affiliations = {}
    current_number = ""
    current = ""
    for line in lines:
        numbered = re.match(r"^(\d{1,2})\s*(.+)$", line)
        if numbered:
            if current and current_number:
                affiliations[current_number] = current
            current_number = numbered.group(1)
            current = numbered.group(2).strip()
            continue
        if current:
            if re.match(r"^\(?(?:Dated|Received|Accepted|Abstract)\b", line, re.I):
                affiliations[current_number] = current
                current = ""
                current_number = ""
                break
            if line and len(line) < 180:
                current = f"{current} {line}".strip()
                if line.endswith((".", "China", "USA", "Kingdom")):
                    affiliations[current_number] = current
                    current = ""
                    current_number = ""
    if current and current_number:
        affiliations[current_number] = current
    return {
        number: affiliation
        for number, affiliation in affiliations.items()
        if any(word in affiliation.lower() for word in (
            "university", "academy", "institute", "laboratory",
            "department", "center", "centre", "sciences", "research",
        ))
    }


def extract_author_affiliation_map(text: str,
                                   authors: list[str]) -> dict[str, list[str]]:
    """Map paper authors to numbered affiliations printed after their names."""
    numbered = _extract_numbered_affiliation_map(text)
    if not numbered:
        return {}
    header = text[:5000]
    positions = []
    for author in authors:
        position = header.find(author)
        if position >= 0:
            positions.append((position, author))
    positions.sort()
    mapping = {}
    for index, (position, author) in enumerate(positions):
        start = position + len(author)
        end = positions[index + 1][0] if index + 1 < len(positions) else start + 40
        marker_text = header[start:end]
        markers = [
            marker for marker in re.findall(r"\d{1,2}", marker_text)
            if marker in numbered
        ]
        affiliations = _dedupe_names([numbered[marker] for marker in markers])
        if affiliations:
            mapping[author] = affiliations
    return mapping


def extract_context(text: str) -> dict:
    """Extract institutions and topic keywords from academic text."""
    institutions = set()
    for m in re.findall(r'来自\s*([A-Za-z]+(?:[\s()()A-Za-z]+)*?)\s*的', text):
        for part in re.split(r'[,，、\s]+', m.strip()):
            part = part.strip()
            if part and len(part) >= 2:
                institutions.add(part)
    for m in re.findall(r'(?:at|from)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*?(?:\s+(?:University|Institute|College|Lab|Center)))', text):
        institutions.add(m.strip())
    for m in re.findall(r'([\u4e00-\u9fff]+(?:大学|研究院|研究所|公司|实验室|中心))', text):
        institutions.add(m)
    for affiliation in _extract_numbered_affiliation_map(text).values():
        institutions.add(affiliation)
    # Restrict topic extraction to the title/abstract area and require word
    # boundaries for English terms. This avoids hits such as "ai" inside an
    # unrelated word or generic terms from references and body text.
    keywords = set()
    context_text = text[:12000]
    chinese_pattern = re.compile(r'(?:量子|编译器?|图表示|电路|调优)')
    for match in chinese_pattern.findall(context_text):
        keywords.add(match.lower())
    english_terms = [
        "bayesian optimization", "deep learning", "machine learning",
        "reinforcement learning", "neural network", "quantum", "circuit",
        "compiler", "transformer", "llm",
    ]
    for term in english_terms:
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            context_text,
            re.IGNORECASE,
        ):
            keywords.add(term.lower())
    # Name → institution mapping (from "来自XXX的**Name**" pattern)
    name_inst_map = {}
    for m in re.finditer(
        r'来自\s*([A-Za-z]+(?:\s*[（）()]?[A-Za-z]+)*?)\s*的\s*\*\*([^*]+)\*\*',
        text
    ):
        inst = m.group(1).strip()
        names_blob = m.group(2).strip()
        for nm in re.split(r'[,，、]\s*', names_blob):
            nm = nm.strip()
            if nm:
                name_inst_map[nm] = inst

    return {
        "institutions": list(institutions),
        "keywords": list(keywords),
        "name_institution_map": name_inst_map,
    }
