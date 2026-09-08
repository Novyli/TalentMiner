"""TalentMiner: Academic author contact and relationship network builder."""

import asyncio
import csv
import io
import json
import os
import shutil
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime
from dataclasses import fields
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from pdf_parser import (
    extract_metadata_from_pdf, extract_text_from_pdf,
    extract_authors_from_text, extract_context, extract_author_email_map,
    extract_author_affiliation_map,
)
from crawler import (
    crawl_multiple_authors, AuthorProfile, resolve_coauthor_identity,
    crawl_lab_member_contact, _name_match_ok, apply_lab_member_evidence,
)
from graph_builder import build_coauthorship_graph
from paper_discovery import (
    deduplicate_papers, download_open_pdf, paper_identity,
    search_historical_works,
)
from storage import (
    save_project, save_authors, save_graph,
    get_project, get_authors, get_graph,
    list_projects, get_all_authors, get_authors_for_projects,
    create_batch_job, update_batch_job, update_batch_item,
    get_batch_job, recover_incomplete_batch_jobs,
    get_batch_author_occurrences,
    create_expansion_job, update_expansion_job, update_expansion_item,
    get_expansion_job, recover_incomplete_expansion_jobs,
    get_expanded_profiles, get_expansion_targets,
)

app = FastAPI(title="TalentMiner")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def prevent_stale_index(request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/index.html"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
DATA_DIR = Path(os.environ.get("TALENTMINER_DATA_DIR") or Path(__file__).parent)
UPLOAD_DIR = Path(os.environ.get("TALENTMINER_UPLOAD_DIR") or (DATA_DIR / "uploads"))
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
tasks = {}
batch_tasks = {}
batch_worker_lock = asyncio.Lock()
expansion_tasks = {}
expansion_worker_lock = asyncio.Lock()
MAX_BATCH_FILES = 20
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_DISCOVERY_PAPERS = 500


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _person_name_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return "".join(re.findall(r"[a-z0-9]+", value))


def _openalex_key(value: str) -> str:
    return (value or "").rstrip("/").split("/")[-1].upper()


def _scope_known_people(scope_type: str, scope_id: str) -> tuple[set, set]:
    if scope_type == "project":
        profiles = get_authors(scope_id)
    else:
        profiles = get_batch_author_occurrences(scope_id)
    profiles = list(profiles) + get_expanded_profiles(scope_type, scope_id)
    ids = {
        _openalex_key(profile.get("openalex_id") or "")
        for profile in profiles if profile.get("openalex_id")
    }
    names = {
        _person_name_key(profile.get("name") or "")
        for profile in profiles if profile.get("name")
    }
    return ids, names


def _normalize_profile_output(profiles: list[dict]) -> list[dict]:
    """Hide duplicate identities and unsafe legacy aliases without deleting data."""
    selected = {}
    order = []
    for original in profiles:
        profile = dict(original)
        name = profile.get("name") or ""
        safe_aliases = []
        seen_aliases = set()
        for alias in [name] + list(profile.get("name_aliases") or []):
            alias = re.sub(r"\s+", " ", (alias or "").strip())
            alias_key = alias.casefold()
            has_cjk = any("\u4e00" <= char <= "\u9fff" for char in alias)
            if (
                not alias or alias_key in seen_aliases
                or (alias != name and not has_cjk and not _name_match_ok(name, alias))
            ):
                continue
            seen_aliases.add(alias_key)
            safe_aliases.append(alias)
        profile["name_aliases"] = safe_aliases[:5]
        openalex_id = _openalex_key(profile.get("openalex_id") or "")
        identity = f"id:{openalex_id}" if openalex_id else f"name:{_person_name_key(name)}"
        preference = (
            profile.get("contact_search_status", "not-started") != "not-started",
            float(profile.get("email_confidence") or 0),
            float(profile.get("phone_confidence") or 0),
            float(profile.get("match_score") or 0),
            profile.get("discovery_origin", "paper-author") == "paper-author",
        )
        if identity not in selected:
            order.append(identity)
            selected[identity] = (preference, profile)
        elif preference > selected[identity][0]:
            selected[identity] = (preference, profile)
    return [selected[key][1] for key in order]


def _apply_scope_lab_enrichment(authors: list[dict]) -> list[dict]:
    """Propagate roster evidence and expose each lab member as a contact row."""
    profile_fields = {field.name for field in fields(AuthorProfile)}
    profiles = [
        AuthorProfile(**{
            key: value for key, value in author.items()
            if key in profile_fields
        })
        for author in authors
    ]
    apply_lab_member_evidence(profiles)
    known_names = {_person_name_key(profile.name) for profile in profiles}
    derived_members = []
    for owner in profiles:
        for member in owner.lab_members or []:
            name = (member.get("name") or "").strip()
            name_key = _person_name_key(name)
            if not name_key or name_key in known_names:
                continue
            known_names.add(name_key)
            derived_members.append(AuthorProfile(
                name=name,
                email=member.get("email") or None,
                email_source=member.get("email_source") or member.get("profile_url"),
                email_confidence=float(member.get("email_confidence") or 0),
                phone=member.get("phone") or None,
                phone_source=member.get("phone_source") or member.get("profile_url"),
                phone_confidence=float(member.get("phone_confidence") or 0),
                website_url=member.get("profile_url") or None,
                career_stage=member.get("career_stage") or "graduate-student",
                degree_type=member.get("degree_type") or "",
                graduation_score=float(member.get("graduation_score") or 0),
                graduation_status=member.get("graduation_status") or "student",
                expected_graduation_year=member.get("expected_graduation_year"),
                graduation_evidence=member.get("graduation_evidence") or [],
                student_score=float(member.get("student_score") or 82),
                student_status=member.get("student_status") or "confirmed-student",
                student_evidence=member.get("student_evidence") or [],
                lab_name=member.get("lab_name") or "",
                lab_url=member.get("lab_url") or "",
                directory_url=member.get("directory_url") or member.get("source") or "",
                # The profile that discovered a roster is not necessarily the
                # laboratory PI. Keep the PI unknown unless the directory
                # evidence explicitly identified one.
                lab_pi=member.get("lab_pi") or "",
                identity_status=member.get("identity_status") or "roster-only",
                contact_search_status=(
                    member.get("contact_search_status")
                    or ("contact-found" if member.get("email") or member.get("phone") else "not-started")
                ),
                search_failure_reason=member.get("search_failure_reason") or "",
                pages_checked=int(member.get("pages_checked") or 0),
                discovery_origin="lab-member",
                sources=list(dict.fromkeys(filter(None, [
                    f"Official lab roster: {member.get('directory_url') or member.get('source') or ''}",
                    (
                        f"Public contact from lab member profile: {member.get('profile_url')}"
                        if member.get("email") or member.get("phone") else ""
                    ),
                ]))),
            ))
    profiles.extend(derived_members)
    apply_lab_member_evidence(profiles)
    return [profile.to_dict() for profile in profiles]


async def lookup_openalex_work(doi: str = "", title: str = "") -> dict:
    """Resolve a paper first so its authorship IDs can anchor identity."""
    import aiohttp
    headers = {
        "User-Agent": "TalentMiner/1.0 (academic metadata lookup)",
        "Accept": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if doi:
                url = (
                    "https://api.openalex.org/works/doi:"
                    f"{quote(doi, safe='/')}"
                )
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
            if not title:
                return {}
            url = (
                "https://api.openalex.org/works"
                f"?search={quote(title)}&per_page=5"
            )
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
            target = _normalized_title(title)
            ranked = []
            for work in data.get("results") or []:
                candidate = _normalized_title(work.get("title") or "")
                similarity = SequenceMatcher(None, target, candidate).ratio()
                ranked.append((similarity, work))
            ranked.sort(key=lambda item: item[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] >= 0.88 else {}
    except Exception as exc:
        print(f"OpenAlex work lookup failed for {doi or title}: {exc}")
        return {}


def _work_author_names(work: dict) -> list[str]:
    return [
        (authorship.get("author") or {}).get("display_name", "").strip()
        for authorship in (work.get("authorships") or [])
        if (authorship.get("author") or {}).get("display_name", "").strip()
    ]


def build_openalex_authors_map(work: dict, final_names: list[str]) -> dict:
    """Map paper author names to authoritative OpenAlex author IDs by order."""
    authorships = [
        authorship for authorship in (work.get("authorships") or [])
        if (authorship.get("author") or {}).get("display_name")
    ]
    if len(authorships) != len(final_names):
        return {}
    result = {}
    work_id = work.get("id") or ""
    source = work.get("metadata_source") or (
        f"https://openalex.org/{work_id.rstrip('/').split('/')[-1]}"
        if work_id else (
            f"https://doi.org/{work.get('doi')}"
            if work.get("doi") else "论文作者元数据"
        )
    )
    for final_name, authorship in zip(final_names, authorships):
        author = authorship.get("author") or {}
        display_name = author.get("display_name") or ""
        if _surname_key(final_name) != _surname_key(display_name):
            return {}
        affiliations = [
            (institution or {}).get("display_name", "")
            for institution in (authorship.get("institutions") or [])
            if (institution or {}).get("display_name")
        ]
        affiliations.extend(
            value for value in (authorship.get("raw_affiliation_strings") or [])
            if value
        )
        institution_records = [
            {
                "id": (institution or {}).get("id") or "",
                "ror": (institution or {}).get("ror") or "",
                "display_name": (institution or {}).get("display_name") or "",
                "country_code": (institution or {}).get("country_code") or "",
                "type": (institution or {}).get("type") or "",
            }
            for institution in (authorship.get("institutions") or [])
            if (institution or {}).get("id") or (institution or {}).get("display_name")
        ]
        result[final_name] = {
            "display_name": display_name,
            "openalex_id": author.get("id") or "",
            "orcid": author.get("orcid") or "",
            "affiliations": list(dict.fromkeys(affiliations)),
            "institution_records": institution_records,
            "source": source,
        }
    return result


def _surname_key(name: str) -> str:
    normalized = re.sub(r"[-‐‑‒–—―−]", " ", name)
    normalized = unicodedata.normalize("NFKD", normalized)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    parts = re.findall(r"[A-Za-z]+", ascii_name)
    return parts[-1].lower() if parts else ""


def prefer_complete_pdf_names(doi_authors: list[str],
                              pdf_authors: list[str]) -> list[str]:
    """Use fuller PDF spellings when DOI and PDF author order agrees."""
    if not doi_authors:
        return pdf_authors
    if len(doi_authors) != len(pdf_authors):
        return doi_authors
    if not all(_surname_key(doi_name) == _surname_key(pdf_name)
               for doi_name, pdf_name in zip(doi_authors, pdf_authors)):
        return doi_authors
    return [
        pdf_name if len(pdf_name) >= len(doi_name) else doi_name
        for doi_name, pdf_name in zip(doi_authors, pdf_authors)
    ]


async def prepare_pdf(filepath: Path, original_filename: str) -> dict:
    """Parse one stored PDF and build the context used by the crawler."""
    metadata, full_text = await asyncio.gather(
        asyncio.to_thread(extract_metadata_from_pdf, str(filepath)),
        asyncio.to_thread(extract_text_from_pdf, str(filepath)),
    )
    context = extract_context(full_text)
    matched_work = await lookup_openalex_work(
        metadata["doi"], metadata["title"]
    )
    work_authors = _work_author_names(matched_work)
    all_authors = prefer_complete_pdf_names(work_authors, metadata["authors"])
    context["author_email_map"] = extract_author_email_map(full_text, all_authors)
    context["author_institutions_map"] = extract_author_affiliation_map(
        full_text, all_authors
    )
    context["openalex_authors_map"] = build_openalex_authors_map(
        matched_work, all_authors
    )
    context["matched_work_source"] = next(
        iter(context["openalex_authors_map"].values()), {}
    ).get("source", "")
    context["paper_title"] = metadata["title"]
    context["paper_doi"] = metadata["doi"]
    context["source_type"] = "pdf"
    context["source_info"] = original_filename
    return {
        "filename": original_filename,
        "title": metadata["title"],
        "doi": metadata["doi"],
        "authors": all_authors,
        "author_count": len(all_authors),
        "text_preview": full_text[:500] if full_text else "",
        "context": context,
    }


def prepare_work_metadata(work: dict, author_scope: str = "all") -> dict:
    """Build crawler input directly from OpenAlex when no PDF is available."""
    authorships = work.get("authorships") or []
    full_authors = [
        (item.get("author") or {}).get("display_name", "").strip()
        for item in authorships
        if (item.get("author") or {}).get("display_name", "").strip()
    ]
    if author_scope == "first":
        authors = [
            (item.get("author") or {}).get("display_name", "").strip()
            for item in authorships if item.get("author_position") == "first"
        ]
    elif author_scope == "corresponding":
        authors = [
            (item.get("author") or {}).get("display_name", "").strip()
            for item in authorships if item.get("is_corresponding")
        ]
        # Corresponding-author flags are incomplete in scholarly indexes.
        if not authors:
            authors = [
                (item.get("author") or {}).get("display_name", "").strip()
                for item in authorships if item.get("author_position") == "last"
            ]
    else:
        authors = full_authors
    authors = list(dict.fromkeys(filter(None, authors)))
    context = {
        "openalex_authors_map": build_openalex_authors_map(work, full_authors),
        "matched_work_source": (
            work.get("openalex_id") or work.get("id")
            or work.get("metadata_source") or "论文元数据"
        ),
        "paper_title": work.get("title") or "",
        "paper_doi": work.get("doi") or "",
        "source_type": "paper-search",
        "source_info": work.get("openalex_id") or work.get("landing_page_url") or "OpenAlex",
        "institutions": [],
        "keywords": work.get("topics") or [],
        "skip_openalex_lookup": str(work.get("metadata_source") or "").startswith("Crossref"),
    }
    return {
        "filename": work.get("title") or paper_identity(work) or "OpenAlex paper",
        "title": work.get("title") or "",
        "doi": work.get("doi") or "",
        "authors": authors,
        "author_count": len(authors),
        "text_preview": "",
        "context": context,
    }


def apply_author_scope(parsed: dict, work: dict, author_scope: str) -> dict:
    """Apply a requested author subset after PDF enrichment as well."""
    if author_scope == "all":
        return parsed
    scoped = prepare_work_metadata(work, author_scope)["authors"]
    available = {name: name for name in parsed.get("authors") or []}
    available_by_key = {_person_name_key(name): name for name in available}
    selected = [
        available.get(name) or available_by_key.get(_person_name_key(name)) or name
        for name in scoped
    ]
    parsed["authors"] = list(dict.fromkeys(filter(None, selected)))
    parsed["author_count"] = len(parsed["authors"])
    return parsed


def _merge_unique(target: list, values: list):
    for value in values or []:
        if value and value not in target:
            target.append(value)


def aggregate_batch_contacts(occurrences: list[dict]) -> list[dict]:
    """Merge verified identities across papers without merging names alone."""
    merged: dict[str, dict] = {}
    for occurrence in occurrences:
        verified = occurrence.get("match_status") == "verified"
        openalex_id = (occurrence.get("openalex_id") or "").strip()
        orcid = (occurrence.get("orcid") or "").strip()
        if verified and openalex_id:
            key = "openalex:" + openalex_id.rstrip("/").split("/")[-1].lower()
        elif verified and orcid:
            key = "orcid:" + orcid.rstrip("/").split("/")[-1].lower()
        else:
            key = (
                f"paper:{occurrence.get('batch_project_id')}:"
                f"{occurrence.get('id')}"
            )

        paper = {
            "project_id": occurrence.get("batch_project_id") or "",
            "title": occurrence.get("paper_title") or "",
            "doi": occurrence.get("paper_doi") or "",
            "filename": occurrence.get("original_filename") or "",
            "position": occurrence.get("paper_position", 0),
        }
        if key not in merged:
            profile_fields = {field.name for field in fields(AuthorProfile)}
            profile = {
                name: occurrence.get(name)
                for name in profile_fields
                if name in occurrence
            }
            for field_name in [
                "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details", "sources", "candidates",
                "institution_records", "institution_sites", "contact_candidates",
                "search_trail", "graduation_evidence", "china_link_evidence",
                "nationality_evidence", "graduate_candidates", "lab_members",
                "student_evidence",
            ]:
                profile[field_name] = list(profile.get(field_name) or [])
            profile["papers"] = [paper]
            profile["paper_count"] = 1
            profile["paper_titles"] = [paper["title"]] if paper["title"] else []
            profile["paper_dois"] = [paper["doi"]] if paper["doi"] else []
            profile["source_files"] = [paper["filename"]] if paper["filename"] else []
            profile["project_ids"] = [paper["project_id"]] if paper["project_id"] else []
            profile["source_project_ids"] = list(profile["project_ids"])
            merged[key] = profile
            continue

        profile = merged[key]
        if paper["project_id"] not in profile["project_ids"]:
            profile["papers"].append(paper)
            if paper["project_id"]:
                profile["project_ids"].append(paper["project_id"])
            if paper["title"] and paper["title"] not in profile["paper_titles"]:
                profile["paper_titles"].append(paper["title"])
            if paper["doi"] and paper["doi"] not in profile["paper_dois"]:
                profile["paper_dois"].append(paper["doi"])
            if paper["filename"] and paper["filename"] not in profile["source_files"]:
                profile["source_files"].append(paper["filename"])
        profile["paper_count"] = len(profile["papers"])
        profile["source_project_ids"] = list(profile["project_ids"])
        for field_name in [
            "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details", "sources", "candidates",
            "institution_records", "institution_sites", "contact_candidates",
            "search_trail", "graduation_evidence", "china_link_evidence",
            "nationality_evidence", "graduate_candidates", "lab_members",
            "student_evidence",
        ]:
            _merge_unique(profile[field_name], occurrence.get(field_name) or [])
        if float(occurrence.get("email_confidence") or 0) > float(
            profile.get("email_confidence") or 0
        ):
            for field_name in ["email", "email_source", "email_confidence"]:
                profile[field_name] = occurrence.get(field_name)
        if float(occurrence.get("phone_confidence") or 0) > float(
            profile.get("phone_confidence") or 0
        ):
            for field_name in ["phone", "phone_source", "phone_confidence"]:
                profile[field_name] = occurrence.get(field_name)
        profile["works_count"] = max(
            int(profile.get("works_count") or 0),
            int(occurrence.get("works_count") or 0),
        )
        profile["cited_by_count"] = max(
            int(profile.get("cited_by_count") or 0),
            int(occurrence.get("cited_by_count") or 0),
        )
        if float(occurrence.get("graduation_score") or 0) > float(
            profile.get("graduation_score") or 0
        ):
            for field_name in [
                "career_stage", "degree_type", "graduation_score",
                "graduation_status", "expected_graduation_year",
                "academic_timeline",
            ]:
                profile[field_name] = occurrence.get(field_name)
        if float(occurrence.get("china_link_score") or 0) > float(
            profile.get("china_link_score") or 0
        ):
            for field_name in ["china_link_score", "china_link_status"]:
                profile[field_name] = occurrence.get(field_name)
        if occurrence.get("nationality") and not profile.get("nationality"):
            profile["nationality"] = occurrence["nationality"]
    return list(merged.values())


def aggregate_all_contacts(authors: list[dict]) -> list[dict]:
    """Merge contacts across saved papers without conflating unverified names."""
    occurrences = []
    for index, author in enumerate(authors):
        occurrence = dict(author)
        occurrence["batch_project_id"] = (
            author.get("project_id") or author.get("batch_project_id")
            or f"unknown-{index}"
        )
        occurrence["paper_title"] = author.get("paper_title") or ""
        occurrence["paper_doi"] = author.get("paper_doi") or ""
        occurrence["original_filename"] = author.get("paper_source_info") or ""
        occurrence["paper_position"] = index
        occurrences.append(occurrence)
    contacts = aggregate_batch_contacts(occurrences)
    # Do not run name-based display deduplication here: two unverified people
    # with the same name from different papers must remain separate rows.
    contacts.sort(key=lambda item: (
        ((item.get("source_files") or item.get("paper_titles") or [""])[0]).casefold(),
        (item.get("name") or "").casefold(),
    ))
    return contacts


def build_merged_contacts(project_ids: list[str]) -> list[dict]:
    contacts = aggregate_all_contacts(get_authors_for_projects(project_ids))
    for contact in contacts:
        paper_sources = [
            "Merged paper: " + " | ".join(filter(None, [
                paper.get("title") or "",
                paper.get("doi") or "",
                paper.get("filename") or "",
            ]))
            for paper in (contact.get("papers") or [])
        ]
        contact["sources"] = list(dict.fromkeys(
            list(contact.get("sources") or []) + paper_sources
        ))
    return contacts


def repair_contact_merge_projects() -> int:
    """Rebuild persisted merge rows created before provenance columns existed."""
    repaired = 0
    for project in list_projects():
        if project.get("source_type") != "contact-merge":
            continue
        try:
            metadata = json.loads(project.get("source_info") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        project_ids = metadata.get("project_ids") or []
        if not project_ids:
            continue
        existing = get_authors(project["id"])
        if existing and all(
            item.get("paper_count") and
            (item.get("source_files") or item.get("paper_titles"))
            for item in existing
        ):
            continue
        contacts = build_merged_contacts(project_ids)
        if contacts:
            save_authors(project["id"], contacts)
            repaired += 1
    return repaired


def _contacts_csv_response(contacts: list[dict], filename: str):
    output = io.StringIO()
    columns = [
        ("source_files", "来源PDF/文件"),
        ("paper_titles", "来源论文标题"),
        ("paper_dois", "来源DOI"),
        ("source_project_ids", "来源项目ID"),
        ("paper_count", "来源论文数"),
        ("name", "姓名"), ("email", "邮箱"),
        ("email_source", "邮箱证据来源"), ("email_confidence", "邮箱可信度"),
        ("phone", "电话"), ("phone_source", "电话证据来源"),
        ("phone_confidence", "电话可信度"), ("affiliations", "机构"),
        ("orcid", "ORCID"), ("openalex_id", "OpenAlex ID"),
        ("website_url", "个人主页"), ("career_stage", "职业阶段"),
        ("student_status", "学生状态"), ("graduation_status", "毕业状态"),
        ("expected_graduation_year", "预计毕业年份"), ("topics", "研究主题"),
        ("match_status", "身份匹配状态"), ("sources", "全部证据来源"),
    ]
    headers = [label for _, label in columns]
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for contact in contacts:
        row = {}
        for field_name, label in columns:
            value = contact.get(field_name, "")
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value if item)
            elif isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            row[label] = value if value is not None else ""
        writer.writerow(row)
    return PlainTextResponse(
        "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _batch_counts(job: dict) -> dict:
    items = job.get("items") or []
    succeeded = sum(item.get("status") == "completed" for item in items)
    failed = sum(item.get("status") == "failed" for item in items)
    return {
        "completed": succeeded + failed,
        "succeeded": succeeded,
        "failed": failed,
    }


async def _process_batch_item(batch_id: str, item: dict) -> bool:
    item_id = item["id"]
    for attempt in range(int(item.get("attempts") or 0) + 1, 3):
        now = datetime.now().isoformat(timespec="seconds")
        update_batch_item(
            item_id, status="running", attempts=attempt, error="",
            started_at=item.get("started_at") or now,
        )
        try:
            metadata = item.get("metadata") or {}
            stored_path = Path(item["stored_path"]) if item.get("stored_path") else None
            downloaded = False
            if metadata and item.get("source_url") and stored_path and not stored_path.exists():
                downloaded = await download_open_pdf(
                    item["source_url"], stored_path, MAX_PDF_BYTES
                )
            if stored_path and stored_path.exists():
                parsed = await prepare_pdf(stored_path, item["original_filename"])
                if metadata:
                    parsed = apply_author_scope(
                        parsed, metadata, metadata.get("author_scope") or "all"
                    )
                    parsed["context"]["source_type"] = "paper-search-pdf"
                    parsed["context"]["source_info"] = metadata.get("openalex_id") or item["original_filename"]
            elif metadata:
                parsed = prepare_work_metadata(
                    metadata, metadata.get("author_scope") or "all"
                )
            else:
                raise ValueError("论文文件不存在且没有可用元数据")
            if not parsed["authors"]:
                raise ValueError("未能从论文或 OpenAlex 识别作者")
            profiles = await crawl_multiple_authors(
                parsed["authors"], parsed["context"]
            )
            profile_dicts = [profile.to_dict() for profile in profiles]
            project_id = f"{batch_id}-paper-{int(item['position']) + 1:02d}"
            save_project(
                project_id, parsed["title"], parsed["doi"],
                parsed["context"].get("source_type", "pdf"),
                parsed["context"].get("source_info") or item["original_filename"],
            )
            save_authors(project_id, profile_dicts)
            graph = build_coauthorship_graph(profiles)
            save_graph(project_id, graph)
            update_batch_item(
                item_id, status="completed", project_id=project_id,
                title=parsed["title"], doi=parsed["doi"],
                author_count=len(parsed["authors"]),
                profile_count=len(profile_dicts), error="",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return True
        except Exception as exc:
            if attempt < 2:
                update_batch_item(item_id, status="queued", error=str(exc))
                await asyncio.sleep(1)
                continue
            update_batch_item(
                item_id, status="failed", error=str(exc),
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return False
    return False


async def _run_batch_job(batch_id: str):
    async with batch_worker_lock:
        try:
            job = get_batch_job(batch_id)
            if not job:
                return
            update_batch_job(batch_id, status="running", error="")
            for item in job.get("items") or []:
                if item.get("status") not in {"queued", "running"}:
                    continue
                update_batch_job(
                    batch_id, current_filename=item.get("original_filename") or ""
                )
                await _process_batch_item(batch_id, item)
                refreshed = get_batch_job(batch_id)
                update_batch_job(batch_id, **_batch_counts(refreshed))
            refreshed = get_batch_job(batch_id)
            counts = _batch_counts(refreshed)
            if counts["failed"] == 0:
                final_status = "completed"
            elif counts["succeeded"]:
                final_status = "partial"
            else:
                final_status = "failed"
            update_batch_job(
                batch_id, status=final_status, current_filename="", **counts
            )
            if counts["succeeded"]:
                _auto_launch_lab_member_expansion(
                    "batch", batch_id, get_batch_author_occurrences(batch_id)
                )
        except Exception as exc:
            update_batch_job(batch_id, status="failed", error=str(exc))


def _launch_batch_job(batch_id: str):
    existing = batch_tasks.get(batch_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_batch_job(batch_id))
    batch_tasks[batch_id] = task

    def cleanup(done_task):
        batch_tasks.pop(batch_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            # Normal during server shutdown; startup recovery requeues the item.
            pass
        except Exception as exc:
            print(f"Batch task {batch_id} failed: {exc}")

    task.add_done_callback(cleanup)


def _expansion_counts(job: dict) -> dict:
    items = job.get("items") or []
    succeeded = sum(item.get("status") == "completed" for item in items)
    failed = sum(item.get("status") == "failed" for item in items)
    skipped = sum(item.get("status") == "skipped" for item in items)
    return {
        "completed": succeeded + failed + skipped,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }


async def _process_expansion_item(item: dict) -> bool:
    import aiohttp
    item_id = item["id"]
    metadata = item.get("metadata") or {}
    for attempt in range(int(item.get("attempts") or 0) + 1, 3):
        update_expansion_item(
            item_id, status="running", attempts=attempt, error="",
            stage="准备身份核验",
            started_at=item.get("started_at") or datetime.now().isoformat(timespec="seconds"),
        )
        try:
            if metadata.get("candidate_type") == "lab-member":
                update_expansion_item(item_id, stage="实验室成员身份核验与联系方式搜索")
                async with aiohttp.ClientSession() as session:
                    profile = await crawl_lab_member_contact(metadata, session)
                profile_dict = profile.to_dict()
                profile_dict["expansion_parent"] = item.get("parent_name") or ""
                profile_dict["expansion_value_score"] = metadata.get("value_score") or 0
                update_expansion_item(
                    item_id, status="completed",
                    stage="搜索完成",
                    openalex_id=profile.openalex_id or "",
                    profile_json=json.dumps(profile_dict, ensure_ascii=False),
                    error=profile.search_failure_reason or "",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                return True
            async with aiohttp.ClientSession() as session:
                update_expansion_item(item_id, stage="共同论文身份核验")
                identity = await resolve_coauthor_identity(
                    item.get("parent_openalex_id") or "",
                    item.get("name") or "",
                    session,
                    item.get("openalex_id") or "",
                )
            if not identity or not identity.get("openalex_id"):
                raise ValueError("无法从共同署名论文确认该合作者的 OpenAlex 身份")
            name = identity.get("name") or item["name"]
            openalex_id = identity["openalex_id"]
            source = (
                "OpenAlex verified coauthorship with "
                f"{item.get('parent_name') or 'target author'}"
            )
            context = {
                "openalex_authors_map": {name: {
                    "display_name": name,
                    "openalex_id": openalex_id,
                    "orcid": "",
                    "affiliations": [],
                    "institution_records": [],
                    "source": source,
                }},
                "matched_work_source": source,
                "paper_title": "Coauthor network expansion",
                "source_type": "coauthor-expansion",
                "source_info": item.get("parent_name") or "",
                "institutions": [],
                "keywords": identity.get("shared_topics") or [],
            }
            update_expansion_item(item_id, stage="完整联系方式搜索")
            profiles = await crawl_multiple_authors([name], context)
            if not profiles:
                raise ValueError("合作者身份已确认，但未生成完整人物档案")
            profile = profiles[0]
            resolved_id = (profile.openalex_id or "").rstrip("/").split("/")[-1]
            expected_id = openalex_id.rstrip("/").split("/")[-1]
            if resolved_id != expected_id or profile.match_status != "verified":
                raise ValueError("合作者档案身份校验失败")
            profile.discovery_origin = "coauthor"
            profile.sources.append(
                f"Expanded from collaborator network: {item.get('parent_name')} | "
                f"shared works={identity.get('shared_works_count', 0)}"
            )
            profile_dict = profile.to_dict()
            profile_dict["expansion_parent"] = item.get("parent_name") or ""
            profile_dict["expansion_value_score"] = (
                metadata.get("value_score") or identity.get("value_score") or 0
            )
            update_expansion_item(
                item_id, status="completed", openalex_id=openalex_id,
                stage="搜索完成",
                profile_json=json.dumps(profile_dict, ensure_ascii=False),
                error="", completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return True
        except Exception as exc:
            if attempt < 2:
                update_expansion_item(item_id, status="queued", error=str(exc))
                await asyncio.sleep(1)
                continue
            update_expansion_item(
                item_id, status="failed", error=str(exc),
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return False
    return False


async def _run_expansion_job(job_id: str):
    async with expansion_worker_lock:
        try:
            job = get_expansion_job(job_id)
            if not job:
                return
            update_expansion_job(job_id, status="running", error="")
            pending_items = [
                item for item in (job.get("items") or [])
                if item.get("status") in {"queued", "running"}
            ]
            is_lab_job = bool(pending_items) and all(
                (item.get("metadata") or {}).get("candidate_type") == "lab-member"
                for item in pending_items
            )
            if is_lab_job:
                # Lab rosters can contain dozens of names. Three concurrent
                # bounded searches keep the queue moving without flooding
                # academic APIs or institutional sites.
                for index in range(0, len(pending_items), 3):
                    chunk = pending_items[index:index + 3]
                    update_expansion_job(
                        job_id,
                        current_name="、".join(item.get("name") or "" for item in chunk),
                    )
                    await asyncio.gather(*[
                        _process_expansion_item(item) for item in chunk
                    ])
                    update_expansion_job(
                        job_id, **_expansion_counts(get_expansion_job(job_id))
                    )
            for item in ([] if is_lab_job else pending_items):
                if item.get("status") not in {"queued", "running"}:
                    continue
                known_ids, known_names = _scope_known_people(
                    job.get("scope_type") or "", job.get("scope_id") or ""
                )
                item_id_key = _openalex_key(item.get("openalex_id") or "")
                item_name_key = _person_name_key(item.get("name") or "")
                if (
                    (item_id_key and item_id_key in known_ids)
                    or (
                        (item.get("metadata") or {}).get("candidate_type") != "lab-member"
                        and not item_id_key and item_name_key in known_names
                    )
                ):
                    update_expansion_item(
                        item["id"], status="skipped",
                        error="该人物已存在于当前工程，已跳过重复深挖",
                        completed_at=datetime.now().isoformat(timespec="seconds"),
                    )
                    update_expansion_job(
                        job_id, **_expansion_counts(get_expansion_job(job_id))
                    )
                    continue
                update_expansion_job(job_id, current_name=item.get("name") or "")
                await _process_expansion_item(item)
                update_expansion_job(
                    job_id, **_expansion_counts(get_expansion_job(job_id))
                )
            refreshed = get_expansion_job(job_id)
            counts = _expansion_counts(refreshed)
            status = (
                "completed" if counts["failed"] == 0
                else "partial" if counts["succeeded"] else "failed"
            )
            update_expansion_job(
                job_id, status=status, current_name="", **counts
            )
        except Exception as exc:
            update_expansion_job(job_id, status="failed", error=str(exc))


def _launch_expansion_job(job_id: str):
    existing = expansion_tasks.get(job_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_expansion_job(job_id))
    expansion_tasks[job_id] = task

    def cleanup(done_task):
        expansion_tasks.pop(job_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"Expansion task {job_id} failed: {exc}")

    task.add_done_callback(cleanup)


def _lab_member_candidates(profiles: list) -> list[dict]:
    """Build deduplicated roster-member jobs from owner profiles."""
    candidates = []
    seen = set()
    for raw_owner in profiles or []:
        owner = raw_owner.to_dict() if isinstance(raw_owner, AuthorProfile) else raw_owner
        for member in owner.get("lab_members") or []:
            name = (member.get("name") or "").strip()
            key = _person_name_key(name)
            if not key or key in seen or member.get("email") or member.get("phone"):
                continue
            seen.add(key)
            candidates.append({
                **member,
                "candidate_type": "lab-member",
                "name": name,
                "parent_name": member.get("lab_pi") or owner.get("name") or "",
                "parent_openalex_id": owner.get("openalex_id") or "",
                "affiliations": owner.get("affiliations") or [],
                "value_score": (
                    (25 if member.get("profile_url") else 0)
                    + (20 if owner.get("openalex_id") else 0)
                    + (15 if owner.get("affiliations") else 0)
                ),
            })
    candidates.sort(key=lambda item: (-item.get("value_score", 0), item["name"]))
    return candidates[:30]


def _auto_launch_lab_member_expansion(
    scope_type: str, scope_id: str, profiles: list,
) -> str:
    """Automatically queue previously unseen roster-only members."""
    existing = {
        _person_name_key(item.get("name") or "")
        for item in get_expansion_targets(scope_type, scope_id)
        if (item.get("metadata") or {}).get("candidate_type") == "lab-member"
    }
    candidates = [
        item for item in _lab_member_candidates(profiles)
        if _person_name_key(item["name"]) not in existing
    ]
    if not candidates:
        return ""
    job_id = f"lab-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    create_expansion_job(job_id, scope_type, scope_id, candidates)
    _launch_expansion_job(job_id)
    return job_id


@app.on_event("startup")
async def resume_batch_jobs():
    repair_contact_merge_projects()
    for batch_id in recover_incomplete_batch_jobs():
        _launch_batch_job(batch_id)
    for job_id in recover_incomplete_expansion_jobs():
        _launch_expansion_job(job_id)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "TalentMiner"}


@app.post("/api/paper-search/preview")
async def preview_paper_search(data: dict):
    try:
        result = await search_historical_works(
            query=data.get("query", ""),
            date_from=data.get("date_from", ""),
            date_to=data.get("date_to", ""),
            limit=min(int(data.get("limit") or 100), MAX_DISCOVERY_PAPERS),
            open_access_only=bool(data.get("open_access_only")),
        )
        return JSONResponse(result)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/paper-search/batches")
async def create_paper_search_batch(data: dict):
    papers = data.get("papers") or []
    if not papers:
        raise HTTPException(400, "请选择至少一篇论文")
    if len(papers) > MAX_DISCOVERY_PAPERS:
        raise HTTPException(400, f"一次回溯最多处理 {MAX_DISCOVERY_PAPERS} 篇论文")
    author_scope = data.get("author_scope") or "all"
    if author_scope not in {"all", "first", "corresponding"}:
        raise HTTPException(400, "作者范围无效")
    valid_papers = [
        dict(paper) for paper in papers
        if paper_identity(paper) and paper.get("title") and paper.get("authorships")
    ]
    unique_papers = deduplicate_papers(valid_papers)
    if not unique_papers:
        raise HTTPException(400, "所选论文缺少可处理的作者元数据")
    batch_id = f"history-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    batch_dir = UPLOAD_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    items = []
    for position, paper in enumerate(unique_papers):
        paper["author_scope"] = author_scope
        identifier = (paper.get("openalex_id") or "paper").rstrip("/").split("/")[-1]
        filename = f"{identifier}.pdf"
        items.append({
            "original_filename": filename,
            "stored_path": str(batch_dir / f"{position + 1:03d}-{filename}"),
            "source_url": paper.get("pdf_url") or "",
            "metadata": paper,
        })
    source_info = json.dumps({
        "query": data.get("query") or "",
        "date_from": data.get("date_from") or "",
        "date_to": data.get("date_to") or "",
        "author_scope": author_scope,
    }, ensure_ascii=False)
    create_batch_job(batch_id, items, "historical-search", source_info)
    _launch_batch_job(batch_id)
    return JSONResponse(get_batch_job(batch_id), status_code=202)


@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")
    original_filename = Path(file.filename).name
    filepath = UPLOAD_DIR / f"single-{uuid.uuid4().hex[:10]}-{original_filename}"
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return JSONResponse(await prepare_pdf(filepath, original_filename))
    except Exception as e:
        raise HTTPException(500, f"Failed to parse PDF: {str(e)}")


@app.post("/api/batches")
async def create_batch(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "请选择至少一篇 PDF")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(400, f"一次最多上传 {MAX_BATCH_FILES} 篇 PDF")
    invalid = [file.filename for file in files if not file.filename or not file.filename.lower().endswith(".pdf")]
    if invalid:
        raise HTTPException(400, "只支持 PDF 文件")

    batch_id = f"batch-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    batch_dir = UPLOAD_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    stored_items = []
    created_paths = []
    try:
        for position, upload in enumerate(files):
            original_filename = Path(upload.filename).name
            stored_path = batch_dir / f"{position + 1:03d}-{original_filename}"
            created_paths.append(stored_path)
            written = 0
            with open(stored_path, "wb") as destination:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_PDF_BYTES:
                        raise HTTPException(
                            413, f"{original_filename} 超过 50MB 限制"
                        )
                    destination.write(chunk)
            if written == 0:
                raise HTTPException(400, f"{original_filename} 是空文件")
            stored_items.append({
                "original_filename": original_filename,
                "stored_path": str(stored_path),
            })
        create_batch_job(batch_id, stored_items)
        _launch_batch_job(batch_id)
        return JSONResponse(get_batch_job(batch_id), status_code=202)
    except HTTPException:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        batch_dir.rmdir()
        raise
    except Exception as exc:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        if batch_dir.exists():
            batch_dir.rmdir()
        raise HTTPException(500, f"创建批量任务失败: {exc}")
    finally:
        for upload in files:
            await upload.close()


@app.get("/api/batches/{batch_id}")
async def get_batch_status(batch_id: str):
    job = get_batch_job(batch_id)
    if not job:
        raise HTTPException(404, "批量任务不存在")
    return JSONResponse(job)


@app.get("/api/batches/{batch_id}/contacts")
async def get_batch_contacts(batch_id: str):
    job = get_batch_job(batch_id)
    if not job:
        raise HTTPException(404, "批量任务不存在")
    contacts = _batch_contacts_with_expansions(batch_id)
    return JSONResponse({
        "batch_id": batch_id,
        "status": job["status"],
        "contact_count": len(contacts),
        "contacts_with_email": sum(bool(item.get("email")) for item in contacts),
        "contacts_with_phone": sum(bool(item.get("phone")) for item in contacts),
        "contacts": contacts,
    })


@app.get("/api/batches/{batch_id}/csv")
async def export_batch_csv(batch_id: str):
    job = get_batch_job(batch_id)
    if not job:
        raise HTTPException(404, "批量任务不存在")
    contacts = _batch_contacts_with_expansions(batch_id)
    if not contacts:
        raise HTTPException(404, "批量任务还没有可导出的联系人")
    output = io.StringIO()
    fieldnames = [
        "name", "name_aliases", "email", "email_source", "email_confidence", "phone",
        "phone_source", "phone_confidence", "paper_count", "paper_titles",
        "source_files", "website_url", "orcid", "openalex_id",
        "affiliations", "topics", "works_count", "cited_by_count",
        "career_stage", "degree_type", "graduation_score",
        "graduation_status", "expected_graduation_year",
        "graduation_evidence", "china_link_score", "china_link_status",
        "china_link_evidence", "nationality", "nationality_evidence",
        "academic_timeline", "graduate_candidates", "lab_members",
        "lab_name", "lab_url", "directory_url", "lab_pi",
        "identity_status", "contact_search_status", "search_failure_reason",
        "pages_checked",
        "student_score", "student_status", "student_evidence", "match_score",
        "match_status", "sources",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for contact in contacts:
        row = {}
        for field_name in fieldnames:
            value = contact.get(field_name, "")
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value if item)
            elif isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            row[field_name] = value if value is not None else ""
        writer.writerow(row)
    return PlainTextResponse(
        "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{batch_id}-contacts.csv"'
        },
    )


def _batch_contacts_with_expansions(batch_id: str) -> list[dict]:
    occurrences = get_batch_author_occurrences(batch_id)
    for index, profile in enumerate(get_expanded_profiles("batch", batch_id)):
        occurrence = dict(profile)
        occurrence.update({
            "id": f"expanded-{index}",
            "batch_project_id": f"{batch_id}-coauthor-expansion",
            "paper_title": "合作者网络扩展",
            "paper_doi": "",
            "original_filename": "",
            "paper_position": 10000 + index,
        })
        occurrences.append(occurrence)
    contacts = _normalize_profile_output(aggregate_batch_contacts(occurrences))
    return _apply_scope_lab_enrichment(contacts)


@app.post("/api/expansions")
async def create_collaborator_expansion(data: dict):
    scope_type = (data.get("scope_type") or "").strip()
    scope_id = (data.get("scope_id") or "").strip()
    candidates = data.get("candidates") or []
    if scope_type not in {"project", "batch"} or not scope_id:
        raise HTTPException(400, "扩展任务必须关联一个项目或批量任务")
    if not candidates:
        raise HTTPException(400, "没有可扩展的合作者")
    candidate_types = {
        (candidate.get("candidate_type") or "coauthor") for candidate in candidates
    }
    if len(candidate_types) != 1:
        raise HTTPException(400, "一个任务不能混合合作者与实验室成员")
    candidate_type = next(iter(candidate_types))
    max_candidates = 30 if candidate_type == "lab-member" else 5
    if len(candidates) > max_candidates:
        raise HTTPException(
            400,
            f"一次最多处理 {max_candidates} 位"
            + ("实验室成员" if candidate_type == "lab-member" else "高价值合作者"),
        )
    clean_candidates = []
    for candidate in candidates:
        name = (candidate.get("name") or "").strip()
        parent_name = (candidate.get("parent_name") or "").strip()
        parent_id = (candidate.get("parent_openalex_id") or "").strip()
        item_type = candidate.get("candidate_type") or "coauthor"
        if item_type == "coauthor" and candidate.get("field_relevant") is False:
            continue
        if not name or (
            item_type != "lab-member" and (not parent_name or not parent_id)
        ):
            continue
        clean_candidate = {
            **candidate,
            "name": name,
            "openalex_id": (candidate.get("openalex_id") or "").strip(),
            "parent_name": parent_name,
            "parent_openalex_id": parent_id,
            "candidate_type": item_type,
            "value_score": float(candidate.get("value_score") or 0),
            "shared_works_count": int(candidate.get("shared_works_count") or 0),
            "recent_shared_works_count": int(candidate.get("recent_shared_works_count") or 0),
            "latest_shared_year": int(candidate.get("latest_shared_year") or 0),
        }
        clean_candidates.append(clean_candidate)
    if not clean_candidates:
        raise HTTPException(400, "合作者缺少可验证的目标作者关系")
    existing_ids, existing_names = _scope_known_people(scope_type, scope_id)
    submitted_lab_names = {
        _person_name_key(item.get("name") or "")
        for item in get_expansion_targets(scope_type, scope_id)
        if (item.get("metadata") or {}).get("candidate_type") == "lab-member"
    }
    filtered_candidates = []
    submitted = set()
    for candidate in clean_candidates:
        candidate_id = _openalex_key(candidate.get("openalex_id") or "")
        candidate_name = _person_name_key(candidate.get("name") or "")
        identity_key = f"id:{candidate_id}" if candidate_id else f"name:{candidate_name}"
        if candidate_type == "lab-member" and candidate_name in submitted_lab_names:
            continue
        if candidate_type != "lab-member" and (
            (candidate_id and candidate_id in existing_ids)
            or (not candidate_id and candidate_name in existing_names)
            or identity_key in submitted
        ):
            continue
        if identity_key in submitted:
            continue
        submitted.add(identity_key)
        filtered_candidates.append(candidate)
    clean_candidates = filtered_candidates
    if not clean_candidates:
        raise HTTPException(
            409,
            "所选实验室成员已提交搜索"
            if candidate_type == "lab-member"
            else "所选合作者已经完成深度搜集",
        )
    job_id = f"expand-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    create_expansion_job(job_id, scope_type, scope_id, clean_candidates)
    _launch_expansion_job(job_id)
    return JSONResponse(get_expansion_job(job_id), status_code=202)


@app.get("/api/expansions/{job_id}")
async def get_collaborator_expansion(job_id: str):
    job = get_expansion_job(job_id)
    if not job:
        raise HTTPException(404, "合作者扩展任务不存在")
    return JSONResponse(job)


@app.post("/api/analyze-text")
async def analyze_text(data: dict):
    text = data.get("text", "")
    if not text:
        raise HTTPException(400, "No text provided")
    authors = extract_authors_from_text(text)
    context = extract_context(text)
    context["source_type"] = "text"
    context["source_info"] = text[:120]
    return JSONResponse({
        "authors": authors,
        "author_count": len(authors),
        "context": context,
    })


@app.post("/api/crawl-authors")
async def crawl_authors(data: dict):
    authors = data.get("authors", [])
    task_id = data.get("task_id", "default")
    context = data.get("context", {})
    if not authors:
        raise HTTPException(400, "No authors provided")
    if task_id not in tasks:
        tasks[task_id] = {"status": "idle", "progress": 0, "total": 0, "results": []}
    tasks[task_id]["status"] = "running"
    tasks[task_id]["progress"] = 0
    tasks[task_id]["total"] = len(authors)
    tasks[task_id]["results"] = []
    try:
        profiles = await crawl_multiple_authors(authors, context)
        tasks[task_id]["results"] = [p.to_dict() for p in profiles]
        tasks[task_id]["progress"] = len(authors)
        tasks[task_id]["status"] = "completed"
        try:
            save_project(
                task_id,
                context.get("paper_title", ""),
                context.get("paper_doi", ""),
                context.get("source_type", "text"),
                context.get("source_info") or ", ".join(authors[:3]),
            )
            save_authors(task_id, tasks[task_id]["results"])
            lab_job_id = _auto_launch_lab_member_expansion(
                "project", task_id, tasks[task_id]["results"]
            )
            if lab_job_id:
                tasks[task_id]["lab_expansion_job_id"] = lab_job_id
        except Exception as e:
            print(f"DB save failed: {e}")
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)
    return JSONResponse({
        "task_id": task_id,
        "status": tasks[task_id]["status"],
        "profile_count": len(tasks[task_id]["results"]),
        "lab_expansion_job_id": tasks[task_id].get("lab_expansion_job_id", ""),
    })


@app.get("/api/task-status/{task_id}")
async def task_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(404, "Task not found")
    return JSONResponse(tasks[task_id])


@app.post("/api/build-graph")
async def build_graph(data: dict):
    profiles_data = data.get("profiles", [])
    if not profiles_data:
        raise HTTPException(400, "No profiles provided")
    profiles = []
    profile_fields = {f.name for f in fields(AuthorProfile)}
    for p in profiles_data:
        try:
            # Database rows also contain persistence-only columns such as
            # id/project_id/crawled_at. Ignore them when rebuilding old graphs.
            clean_profile = {k: v for k, v in p.items() if k in profile_fields}
            profiles.append(AuthorProfile(**clean_profile))
        except Exception:
            continue
    paper_scope = data.get("paper_scope") or "single"
    if paper_scope not in {"single", "aggregate", "none"}:
        raise HTTPException(400, "Invalid paper scope")
    graph_data = build_coauthorship_graph(profiles, paper_scope=paper_scope)
    try:
        project_id = data.get("project_id")
        if project_id:
            save_graph(project_id, graph_data)
    except Exception:
        pass
    return JSONResponse(graph_data)


@app.post("/api/export-csv")
async def export_csv(data: dict):
    profiles_data = data.get("profiles", [])
    if not profiles_data:
        raise HTTPException(400, "No profiles provided")
    output = io.StringIO()
    fieldnames = [
        "name", "name_aliases", "email", "email_source", "email_confidence", "phone",
        "phone_source", "phone_confidence", "website_url", "orcid",
        "google_scholar_url", "researchgate_url",
        "twitter_url", "linkedin_url", "affiliations", "topics",
        "cited_by_count", "works_count", "match_score", "match_status",
        "match_breakdown", "candidate_links", "institution_sites",
        "contact_candidates", "search_trail", "career_stage", "degree_type",
        "graduation_score", "graduation_status", "expected_graduation_year",
        "graduation_evidence", "china_link_score", "china_link_status",
        "china_link_evidence", "nationality", "nationality_evidence",
        "academic_timeline", "graduate_candidates", "lab_members",
        "lab_name", "lab_url", "directory_url", "lab_pi",
        "identity_status", "contact_search_status", "search_failure_reason",
        "pages_checked",
        "student_score", "student_status", "student_evidence",
        "discovery_origin", "sources"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for p in profiles_data:
        row = {}
        for f in fieldnames:
            if f == "candidate_links":
                val = "; ".join(
                    candidate.get("url", "")
                    for candidate in (p.get("candidates") or [])
                    if candidate.get("url")
                )
            else:
                val = p.get(f, "")
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val if v)
            elif isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            row[f] = val if val else ""
        writer.writerow(row)
    return PlainTextResponse(output.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=authors.csv"})


@app.get("/api/projects")
async def api_list_projects():
    return JSONResponse(list_projects())


@app.post("/api/contact-merges")
async def create_contact_merge(data: dict):
    name = re.sub(r"\s+", " ", (data.get("name") or "").strip())
    project_ids = list(dict.fromkeys(data.get("project_ids") or []))
    if not name:
        raise HTTPException(400, "请输入合并任务名称")
    if not project_ids:
        raise HTTPException(400, "请至少选择一篇论文")
    projects = {
        project["id"]: project for project in list_projects()
        if project.get("source_type") != "contact-merge"
    }
    invalid = [project_id for project_id in project_ids if project_id not in projects]
    if invalid:
        raise HTTPException(400, "选择中包含不存在或不可再次合并的项目")
    occurrences = get_authors_for_projects(project_ids)
    if not occurrences:
        raise HTTPException(400, "所选论文没有已识别联系人")
    contacts = build_merged_contacts(project_ids)
    merge_id = f"merge-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    source_info = json.dumps({
        "name": name,
        "project_ids": project_ids,
        "paper_titles": [projects[item].get("title") or projects[item].get("source_info") or item for item in project_ids],
    }, ensure_ascii=False)
    save_project(merge_id, name, "", "contact-merge", source_info)
    save_authors(merge_id, contacts)
    return JSONResponse({
        "id": merge_id,
        "name": name,
        "paper_count": len(project_ids),
        "contact_count": len(contacts),
        "contacts_with_email": sum(bool(item.get("email")) for item in contacts),
        "contacts_with_phone": sum(bool(item.get("phone")) for item in contacts),
    }, status_code=201)


@app.get("/api/contacts/merged")
async def api_merged_contacts():
    contacts = aggregate_all_contacts(get_all_authors())
    return JSONResponse({
        "contact_count": len(contacts),
        "contacts_with_email": sum(bool(item.get("email")) for item in contacts),
        "contacts_with_phone": sum(bool(item.get("phone")) for item in contacts),
        "paper_count": len({
            project_id for item in contacts for project_id in (item.get("project_ids") or [])
            if project_id
        }),
        "contacts": contacts,
    })


@app.get("/api/contacts/merged/csv")
async def api_merged_contacts_csv():
    contacts = aggregate_all_contacts(get_all_authors())
    if not contacts:
        raise HTTPException(404, "暂无可合并的联系方式")
    return _contacts_csv_response(contacts, "merged-contacts.csv")


@app.get("/api/projects/{project_id}")
async def api_load_project(project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    authors = get_authors(project_id)
    expanded = get_expanded_profiles("project", project_id)
    authors.extend(expanded)
    authors = _normalize_profile_output(authors)
    authors = _apply_scope_lab_enrichment(authors)
    # Rebuild from persisted profiles so old cached graphs receive newer node
    # metadata such as verified coauthor expansion paths.
    profile_fields = {field.name for field in fields(AuthorProfile)}
    profiles = [
        AuthorProfile(**{
            key: value for key, value in author.items()
            if key in profile_fields
        })
        for author in authors
    ]
    is_contact_merge = project.get("source_type") == "contact-merge"
    source_projects = []
    if is_contact_merge:
        try:
            merge_metadata = json.loads(project.get("source_info") or "{}")
        except (json.JSONDecodeError, TypeError):
            merge_metadata = {}
        for source_project_id in merge_metadata.get("project_ids") or []:
            source_project = get_project(source_project_id)
            if source_project:
                source_projects.append(source_project)
    graph = build_coauthorship_graph(
        profiles,
        paper_scope="aggregate" if is_contact_merge else "single",
    )
    return JSONResponse({
        "project": project,
        "authors": authors,
        "graph": graph,
        "contact_merge": is_contact_merge,
        "source_projects": source_projects,
    })


@app.get("/api/projects/{project_id}/csv")
async def api_export_project_csv(project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    authors = get_authors(project_id)
    authors.extend(get_expanded_profiles("project", project_id))
    if project.get("source_type") == "contact-merge":
        authors.sort(key=lambda item: (
            ((item.get("source_files") or item.get("paper_titles") or [""])[0]).casefold(),
            (item.get("name") or "").casefold(),
        ))
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.get("title") or project_id).strip("-")
        return _contacts_csv_response(authors, f"{safe_name or project_id}.csv")
    authors = _normalize_profile_output(authors)
    authors = _apply_scope_lab_enrichment(authors)
    if not authors:
        raise HTTPException(404, f"No authors found for project {project_id}")
    output = io.StringIO()
    fieldnames = [
        "name", "name_aliases", "email", "email_source", "email_confidence", "phone",
        "phone_source", "phone_confidence", "website_url", "orcid",
        "google_scholar_url", "researchgate_url",
        "twitter_url", "linkedin_url", "affiliations", "topics",
        "cited_by_count", "works_count", "match_score", "match_status",
        "match_breakdown", "candidate_links", "institution_sites",
        "contact_candidates", "search_trail", "career_stage", "degree_type",
        "graduation_score", "graduation_status", "expected_graduation_year",
        "graduation_evidence", "china_link_score", "china_link_status",
        "china_link_evidence", "nationality", "nationality_evidence",
        "academic_timeline", "graduate_candidates", "lab_members",
        "lab_name", "lab_url", "directory_url", "lab_pi",
        "identity_status", "contact_search_status", "search_failure_reason",
        "pages_checked",
        "student_score", "student_status", "student_evidence",
        "discovery_origin", "sources"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for p in authors:
        row = {}
        for f in fieldnames:
            if f == "candidate_links":
                val = "; ".join(
                    candidate.get("url", "")
                    for candidate in (p.get("candidates") or [])
                    if candidate.get("url")
                )
            else:
                val = p.get(f, "")
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val if v)
            elif isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            row[f] = val if val else ""
        writer.writerow(row)
    return PlainTextResponse(output.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={project_id}.csv"})


@app.post("/api/search-doi")
async def search_doi(data: dict):
    doi = data.get("doi", "").strip()
    if not doi:
        raise HTTPException(400, "No DOI provided")
    try:
        work = await lookup_openalex_work(doi=doi)
        if not work:
            raise HTTPException(404, f"DOI not found: {doi}")
        authors = _work_author_names(work)
        context = {
            "openalex_authors_map": build_openalex_authors_map(work, authors),
            "matched_work_source": (
                f"https://openalex.org/{(work.get('id') or '').rstrip('/').split('/')[-1]}"
            ),
            "paper_title": work.get("title", ""),
            "paper_doi": doi,
            "source_type": "doi",
            "source_info": doi,
            "institutions": [],
            "keywords": [],
        }
        return JSONResponse({
            "title": work.get("title", ""),
            "doi": doi,
            "authors": authors,
            "author_count": len(authors),
            "publication_date": work.get("publication_date", ""),
            "cited_by_count": work.get("cited_by_count", 0),
            "context": context,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DOI lookup failed: {str(e)}")


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899)
