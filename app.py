"""TalentMiner: Academic author contact and relationship network builder."""

import asyncio
import csv
import io
import json
import os
import shutil
import re
import unicodedata
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
from crawler import crawl_multiple_authors, AuthorProfile
from graph_builder import build_coauthorship_graph
from storage import (
    save_project, save_authors, save_graph,
    get_project, get_authors, get_graph,
    list_projects, get_all_authors,
)

app = FastAPI(title="TalentMiner")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)
tasks = {}


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


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
    source = (
        f"https://openalex.org/{work_id.rstrip('/').split('/')[-1]}"
        if work_id else "OpenAlex paper authorship"
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
        result[final_name] = {
            "display_name": display_name,
            "openalex_id": author.get("id") or "",
            "orcid": author.get("orcid") or "",
            "affiliations": list(dict.fromkeys(affiliations)),
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


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "TalentMiner"}


@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")
    filepath = UPLOAD_DIR / file.filename
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        metadata = extract_metadata_from_pdf(str(filepath))
        full_text = extract_text_from_pdf(str(filepath))
        context = extract_context(full_text)
        matched_work = await lookup_openalex_work(
            metadata["doi"], metadata["title"]
        )
        work_authors = _work_author_names(matched_work)
        # DOI metadata is preferred. Otherwise use embedded PDF metadata and
        # the tightly-scoped first-page fallback from pdf_parser.
        all_authors = prefer_complete_pdf_names(
            work_authors, metadata["authors"]
        )
        context["author_email_map"] = extract_author_email_map(
            full_text, all_authors
        )
        context["author_institutions_map"] = extract_author_affiliation_map(
            full_text, all_authors
        )
        context["openalex_authors_map"] = build_openalex_authors_map(
            matched_work, all_authors
        )
        context["matched_work_source"] = (
            next(iter(context["openalex_authors_map"].values()), {}).get("source", "")
        )
        context["paper_title"] = metadata["title"]
        context["paper_doi"] = metadata["doi"]
        context["source_type"] = "pdf"
        context["source_info"] = file.filename
        return JSONResponse({
            "filename": file.filename,
            "title": metadata["title"],
            "doi": metadata["doi"],
            "authors": all_authors,
            "author_count": len(all_authors),
            "text_preview": full_text[:500] if full_text else "",
            "context": context,
        })
    except Exception as e:
        raise HTTPException(500, f"Failed to parse PDF: {str(e)}")


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
        except Exception as e:
            print(f"DB save failed: {e}")
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)
    return JSONResponse({
        "task_id": task_id,
        "status": tasks[task_id]["status"],
        "profile_count": len(tasks[task_id]["results"]),
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
    graph_data = build_coauthorship_graph(profiles)
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
        "name", "email", "orcid", "google_scholar_url", "researchgate_url",
        "twitter_url", "linkedin_url", "affiliations", "topics",
        "cited_by_count", "works_count", "match_score", "match_status",
        "match_breakdown", "candidate_links", "sources"
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


@app.get("/api/projects/{project_id}")
async def api_load_project(project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    return JSONResponse({
        "project": project,
        "authors": get_authors(project_id),
        "graph": get_graph(project_id),
    })


@app.get("/api/projects/{project_id}/csv")
async def api_export_project_csv(project_id: str):
    authors = get_authors(project_id)
    if not authors:
        raise HTTPException(404, f"No authors found for project {project_id}")
    output = io.StringIO()
    fieldnames = [
        "name", "email", "orcid", "google_scholar_url", "researchgate_url",
        "twitter_url", "linkedin_url", "affiliations", "topics",
        "cited_by_count", "works_count", "match_score", "match_status",
        "match_breakdown", "candidate_links", "sources"
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
