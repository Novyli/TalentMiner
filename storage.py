"""SQLite persistence layer for TalentMiner crawl results."""

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(os.environ.get("TALENT_DB_PATH") or (Path(__file__).parent / "talentminer.db"))


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT,
            doi TEXT,
            source_type TEXT,
            source_info TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            name_aliases TEXT,
            email TEXT,
            orcid TEXT,
            openalex_id TEXT,
            google_scholar_url TEXT,
            researchgate_url TEXT,
            twitter_url TEXT,
            linkedin_url TEXT,
            website_url TEXT,
            affiliations TEXT,
            topics TEXT,
            cited_by_count INTEGER DEFAULT 0,
            works_count INTEGER DEFAULT 0,
            coauthors TEXT,
            coauthor_details TEXT,
            sources TEXT,
            email_source TEXT,
            email_confidence REAL DEFAULT 0,
            phone TEXT,
            phone_source TEXT,
            phone_confidence REAL DEFAULT 0,
            match_score REAL DEFAULT 0,
            match_status TEXT DEFAULT 'unmatched',
            match_breakdown TEXT,
            candidates TEXT,
            institution_records TEXT,
            institution_sites TEXT,
            contact_candidates TEXT,
            search_trail TEXT,
            career_stage TEXT DEFAULT 'unknown',
            degree_type TEXT,
            graduation_score REAL DEFAULT 0,
            graduation_status TEXT DEFAULT 'insufficient',
            expected_graduation_year INTEGER,
            graduation_evidence TEXT,
            china_link_score REAL DEFAULT 0,
            china_link_status TEXT DEFAULT 'none',
            china_link_evidence TEXT,
            nationality TEXT,
            nationality_evidence TEXT,
            academic_timeline TEXT,
            graduate_candidates TEXT,
            lab_members TEXT,
            student_score REAL DEFAULT 0,
            student_status TEXT DEFAULT 'unknown',
            student_evidence TEXT,
            discovery_origin TEXT DEFAULT 'paper-author',
            paper_count INTEGER DEFAULT 0,
            paper_titles TEXT,
            paper_dois TEXT,
            source_files TEXT,
            source_project_ids TEXT,
            crawled_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_cache (
            project_id TEXT PRIMARY KEY,
            graph_data TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batch_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            current_filename TEXT,
            error TEXT,
            source_type TEXT DEFAULT 'upload',
            source_info TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            source_url TEXT,
            metadata TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            project_id TEXT,
            title TEXT,
            doi TEXT,
            author_count INTEGER DEFAULT 0,
            profile_count INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            error TEXT,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE(batch_id, position),
            FOREIGN KEY (batch_id) REFERENCES batch_jobs(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_items_batch "
        "ON batch_items(batch_id, position)"
    )
    batch_job_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(batch_jobs)").fetchall()
    }
    if "source_type" not in batch_job_columns:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN source_type TEXT DEFAULT 'upload'")
    if "source_info" not in batch_job_columns:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN source_info TEXT")
    batch_item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(batch_items)").fetchall()
    }
    if "source_url" not in batch_item_columns:
        conn.execute("ALTER TABLE batch_items ADD COLUMN source_url TEXT")
    if "metadata" not in batch_item_columns:
        conn.execute("ALTER TABLE batch_items ADD COLUMN metadata TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expansion_jobs (
            id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            current_name TEXT,
            error TEXT,
            recovery_count INTEGER DEFAULT 0,
            last_recovery_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expansion_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            openalex_id TEXT,
            parent_name TEXT,
            parent_openalex_id TEXT,
            value_score REAL DEFAULT 0,
            metadata TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER DEFAULT 0,
            profile_json TEXT,
            stage TEXT,
            error TEXT,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE(job_id, position),
            FOREIGN KEY (job_id) REFERENCES expansion_jobs(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expansion_scope "
        "ON expansion_jobs(scope_type, scope_id, created_at)"
    )
    expansion_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(expansion_jobs)").fetchall()
    }
    if "recovery_count" not in expansion_columns:
        conn.execute(
            "ALTER TABLE expansion_jobs ADD COLUMN recovery_count INTEGER DEFAULT 0"
        )
    if "last_recovery_at" not in expansion_columns:
        conn.execute("ALTER TABLE expansion_jobs ADD COLUMN last_recovery_at TEXT")
    if "skipped" not in expansion_columns:
        conn.execute(
            "ALTER TABLE expansion_jobs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0"
        )
    expansion_item_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(expansion_items)").fetchall()
    }
    if "stage" not in expansion_item_columns:
        conn.execute("ALTER TABLE expansion_items ADD COLUMN stage TEXT")
    # Lightweight migrations for databases created by older versions.
    author_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(authors)").fetchall()
    }
    if "email_source" not in author_columns:
        conn.execute("ALTER TABLE authors ADD COLUMN email_source TEXT")
    if "match_score" not in author_columns:
        conn.execute("ALTER TABLE authors ADD COLUMN match_score REAL DEFAULT 0")
    if "match_status" not in author_columns:
        conn.execute(
            "ALTER TABLE authors ADD COLUMN match_status TEXT DEFAULT 'unmatched'"
        )
    if "match_breakdown" not in author_columns:
        conn.execute("ALTER TABLE authors ADD COLUMN match_breakdown TEXT")
    if "candidates" not in author_columns:
        conn.execute("ALTER TABLE authors ADD COLUMN candidates TEXT")
    for column, definition in [
        ("name_aliases", "TEXT"),
        ("email_confidence", "REAL DEFAULT 0"),
        ("phone", "TEXT"),
        ("phone_source", "TEXT"),
        ("phone_confidence", "REAL DEFAULT 0"),
        ("institution_records", "TEXT"),
        ("institution_sites", "TEXT"),
        ("contact_candidates", "TEXT"),
        ("search_trail", "TEXT"),
        ("career_stage", "TEXT DEFAULT 'unknown'"),
        ("degree_type", "TEXT"),
        ("graduation_score", "REAL DEFAULT 0"),
        ("graduation_status", "TEXT DEFAULT 'insufficient'"),
        ("expected_graduation_year", "INTEGER"),
        ("graduation_evidence", "TEXT"),
        ("china_link_score", "REAL DEFAULT 0"),
        ("china_link_status", "TEXT DEFAULT 'none'"),
        ("china_link_evidence", "TEXT"),
        ("nationality", "TEXT"),
        ("nationality_evidence", "TEXT"),
        ("academic_timeline", "TEXT"),
        ("graduate_candidates", "TEXT"),
        ("lab_members", "TEXT"),
        ("student_score", "REAL DEFAULT 0"),
        ("student_status", "TEXT DEFAULT 'unknown'"),
        ("student_evidence", "TEXT"),
        ("discovery_origin", "TEXT DEFAULT 'paper-author'"),
        ("coauthor_details", "TEXT"),
        ("paper_count", "INTEGER DEFAULT 0"),
        ("paper_titles", "TEXT"),
        ("paper_dois", "TEXT"),
        ("source_files", "TEXT"),
        ("source_project_ids", "TEXT"),
    ]:
        if column not in author_columns:
            conn.execute(f"ALTER TABLE authors ADD COLUMN {column} {definition}")
    conn.commit()
    conn.close()


def save_project(project_id: str, title: str = "", doi: str = "",
                  source_type: str = "", source_info: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO projects (id, title, doi, source_type, source_info, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
        (project_id, title, doi, source_type, source_info)
    )
    conn.commit()
    conn.close()


def save_authors(project_id: str, profiles: list):
    conn = get_db()
    # Delete existing authors for this project
    conn.execute("DELETE FROM authors WHERE project_id = ?", (project_id,))
    for p in profiles:
        record = {
            "project_id": project_id,
            "name": p.get("name", ""), "email": p.get("email", ""),
            "name_aliases": json.dumps(p.get("name_aliases", []), ensure_ascii=False),
            "orcid": p.get("orcid", ""), "openalex_id": p.get("openalex_id", ""),
            "google_scholar_url": p.get("google_scholar_url", ""),
            "researchgate_url": p.get("researchgate_url", ""),
            "twitter_url": p.get("twitter_url", ""),
            "linkedin_url": p.get("linkedin_url", ""),
            "website_url": p.get("website_url", ""),
            "affiliations": json.dumps(p.get("affiliations", []), ensure_ascii=False),
            "topics": json.dumps(p.get("topics", []), ensure_ascii=False),
            "cited_by_count": p.get("cited_by_count", 0),
            "works_count": p.get("works_count", 0),
            "coauthors": json.dumps(p.get("coauthors", []), ensure_ascii=False),
            "coauthor_details": json.dumps(p.get("coauthor_details", []), ensure_ascii=False),
            "sources": json.dumps(p.get("sources", []), ensure_ascii=False),
            "email_source": p.get("email_source", ""),
            "email_confidence": p.get("email_confidence", 0),
            "phone": p.get("phone", ""), "phone_source": p.get("phone_source", ""),
            "phone_confidence": p.get("phone_confidence", 0),
            "match_score": p.get("match_score", 0),
            "match_status": p.get("match_status", "unmatched"),
            "match_breakdown": json.dumps(p.get("match_breakdown", {}), ensure_ascii=False),
            "candidates": json.dumps(p.get("candidates", []), ensure_ascii=False),
            "institution_records": json.dumps(p.get("institution_records", []), ensure_ascii=False),
            "institution_sites": json.dumps(p.get("institution_sites", []), ensure_ascii=False),
            "contact_candidates": json.dumps(p.get("contact_candidates", []), ensure_ascii=False),
            "search_trail": json.dumps(p.get("search_trail", []), ensure_ascii=False),
            "career_stage": p.get("career_stage", "unknown"),
            "degree_type": p.get("degree_type", ""),
            "graduation_score": p.get("graduation_score", 0),
            "graduation_status": p.get("graduation_status", "insufficient"),
            "expected_graduation_year": p.get("expected_graduation_year"),
            "graduation_evidence": json.dumps(p.get("graduation_evidence", []), ensure_ascii=False),
            "china_link_score": p.get("china_link_score", 0),
            "china_link_status": p.get("china_link_status", "none"),
            "china_link_evidence": json.dumps(p.get("china_link_evidence", []), ensure_ascii=False),
            "nationality": p.get("nationality", ""),
            "nationality_evidence": json.dumps(p.get("nationality_evidence", []), ensure_ascii=False),
            "academic_timeline": json.dumps(p.get("academic_timeline", {}), ensure_ascii=False),
            "graduate_candidates": json.dumps(p.get("graduate_candidates", []), ensure_ascii=False),
            "lab_members": json.dumps(p.get("lab_members", []), ensure_ascii=False),
            "student_score": p.get("student_score", 0),
            "student_status": p.get("student_status", "unknown"),
            "student_evidence": json.dumps(p.get("student_evidence", []), ensure_ascii=False),
            "discovery_origin": p.get("discovery_origin", "paper-author"),
            "paper_count": int(p.get("paper_count") or 0),
            "paper_titles": json.dumps(p.get("paper_titles", []), ensure_ascii=False),
            "paper_dois": json.dumps(p.get("paper_dois", []), ensure_ascii=False),
            "source_files": json.dumps(p.get("source_files", []), ensure_ascii=False),
            "source_project_ids": json.dumps(
                p.get("source_project_ids") or p.get("project_ids") or [],
                ensure_ascii=False,
            ),
        }
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        conn.execute(
            f"INSERT INTO authors ({columns}) VALUES ({placeholders})",
            tuple(record.values()),
        )
    conn.commit()
    conn.close()


def save_graph(project_id: str, graph_data: dict):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO graph_cache (project_id, graph_data, created_at) "
        "VALUES (?, ?, datetime('now','localtime'))",
        (project_id, json.dumps(graph_data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def get_project(project_id: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_authors(project_id: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM authors WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        for field in [
            "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details", "sources", "candidates",
            "institution_records", "institution_sites", "contact_candidates",
            "search_trail", "graduation_evidence", "china_link_evidence",
            "nationality_evidence", "graduate_candidates", "lab_members",
            "student_evidence",
            "paper_titles", "paper_dois", "source_files", "source_project_ids",
        ]:
            try:
                d[field] = json.loads(d[field]) if d[field] else []
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        try:
            d["match_breakdown"] = (
                json.loads(d["match_breakdown"]) if d.get("match_breakdown") else {}
            )
        except (json.JSONDecodeError, TypeError):
            d["match_breakdown"] = {}
        try:
            d["academic_timeline"] = (
                json.loads(d["academic_timeline"])
                if d.get("academic_timeline") else {}
            )
        except (json.JSONDecodeError, TypeError):
            d["academic_timeline"] = {}
        results.append(d)
    return results


def get_graph(project_id: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM graph_cache WHERE project_id = ?", (project_id,)).fetchone()
    conn.close()
    if row:
        return json.loads(row["graph_data"])
    return None


def list_projects() -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_authors() -> list:
    """Get all authors across all projects for CSV export."""
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, p.title as paper_title, p.doi as paper_doi,
               p.source_info as paper_source_info
        FROM authors a LEFT JOIN projects p ON a.project_id = p.id
        ORDER BY a.project_id, a.id
    """).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        for field in [
            "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details", "sources", "candidates",
            "institution_records", "institution_sites", "contact_candidates",
            "search_trail", "graduation_evidence", "china_link_evidence",
            "nationality_evidence", "graduate_candidates", "lab_members",
            "student_evidence",
            "paper_titles", "paper_dois", "source_files", "source_project_ids",
        ]:
            try:
                d[field] = json.loads(d[field]) if d[field] else []
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        try:
            d["match_breakdown"] = (
                json.loads(d["match_breakdown"]) if d.get("match_breakdown") else {}
            )
        except (json.JSONDecodeError, TypeError):
            d["match_breakdown"] = {}
        try:
            d["academic_timeline"] = (
                json.loads(d["academic_timeline"])
                if d.get("academic_timeline") else {}
            )
        except (json.JSONDecodeError, TypeError):
            d["academic_timeline"] = {}
        results.append(d)
    return results


def get_authors_for_projects(project_ids: list[str]) -> list:
    """Return author occurrences with paper provenance for selected projects."""
    project_ids = list(dict.fromkeys(filter(None, project_ids)))
    if not project_ids:
        return []
    placeholders = ",".join("?" for _ in project_ids)
    conn = get_db()
    rows = conn.execute(
        f"""SELECT a.*, p.title as paper_title, p.doi as paper_doi,
                   p.source_info as paper_source_info
            FROM authors a JOIN projects p ON a.project_id = p.id
            WHERE a.project_id IN ({placeholders})
            ORDER BY a.project_id, a.id""",
        project_ids,
    ).fetchall()
    conn.close()
    results = []
    list_fields = [
        "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details",
        "sources", "candidates", "institution_records", "institution_sites",
        "contact_candidates", "search_trail", "graduation_evidence",
        "china_link_evidence", "nationality_evidence", "graduate_candidates",
        "lab_members", "student_evidence", "paper_titles", "paper_dois",
        "source_files", "source_project_ids",
    ]
    for row in rows:
        item = dict(row)
        for field in list_fields:
            try:
                item[field] = json.loads(item[field]) if item.get(field) else []
            except (json.JSONDecodeError, TypeError):
                item[field] = []
        for field in ["match_breakdown", "academic_timeline"]:
            try:
                item[field] = json.loads(item[field]) if item.get(field) else {}
            except (json.JSONDecodeError, TypeError):
                item[field] = {}
        results.append(item)
    return results


def create_batch_job(batch_id: str, items: list[dict], source_type: str = "upload",
                     source_info: str = ""):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO batch_jobs
               (id, status, total, completed, succeeded, failed, source_type, source_info)
               VALUES (?, 'queued', ?, 0, 0, 0, ?, ?)""",
            (batch_id, len(items), source_type, source_info),
        )
        for position, item in enumerate(items):
            conn.execute(
                """INSERT INTO batch_items
                   (batch_id, position, original_filename, stored_path,
                    source_url, metadata, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
                (
                    batch_id,
                    position,
                    item.get("original_filename", ""),
                    item.get("stored_path", ""),
                    item.get("source_url", ""),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def update_batch_job(batch_id: str, **values):
    allowed = {
        "status", "completed", "succeeded", "failed", "skipped",
        "current_filename", "error",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = get_db()
    conn.execute(
        f"UPDATE batch_jobs SET {assignments}, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (*values.values(), batch_id),
    )
    conn.commit()
    conn.close()


def update_batch_item(item_id: int, **values):
    allowed = {
        "status", "project_id", "title", "doi", "author_count",
        "profile_count", "attempts", "error", "started_at", "completed_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = get_db()
    conn.execute(
        f"UPDATE batch_items SET {assignments} WHERE id = ?",
        (*values.values(), item_id),
    )
    conn.commit()
    conn.close()


def get_batch_job(batch_id: str) -> dict:
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM batch_jobs WHERE id = ?", (batch_id,)
    ).fetchone()
    if not job:
        conn.close()
        return {}
    items = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position",
        (batch_id,),
    ).fetchall()
    conn.close()
    result = dict(job)
    result["items"] = []
    for item in items:
        value = dict(item)
        try:
            value["metadata"] = json.loads(value.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            value["metadata"] = {}
        result["items"].append(value)
    return result


def recover_incomplete_batch_jobs() -> list[str]:
    """Move interrupted work back to the queue and return resumable job IDs."""
    conn = get_db()
    conn.execute(
        """UPDATE batch_items
           SET status = 'queued', attempts = MAX(attempts - 1, 0)
           WHERE status = 'running'"""
    )
    conn.execute(
        """UPDATE batch_jobs
           SET status = 'queued', current_filename = NULL,
               updated_at = datetime('now','localtime')
           WHERE status = 'running'"""
    )
    rows = conn.execute(
        "SELECT id FROM batch_jobs WHERE status = 'queued' ORDER BY created_at"
    ).fetchall()
    conn.commit()
    conn.close()
    return [row["id"] for row in rows]


def get_batch_author_occurrences(batch_id: str) -> list[dict]:
    """Return author rows annotated with their batch paper provenance."""
    conn = get_db()
    rows = conn.execute(
        """SELECT a.*, bi.project_id AS batch_project_id,
                  bi.original_filename, bi.position AS paper_position,
                  p.title AS paper_title, p.doi AS paper_doi
           FROM batch_items bi
           JOIN projects p ON p.id = bi.project_id
           JOIN authors a ON a.project_id = bi.project_id
           WHERE bi.batch_id = ? AND bi.status = 'completed'
           ORDER BY bi.position, a.id""",
        (batch_id,),
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        item = dict(row)
        for field in [
            "name_aliases", "affiliations", "topics", "coauthors", "coauthor_details", "sources", "candidates",
            "institution_records", "institution_sites", "contact_candidates",
            "search_trail", "graduation_evidence", "china_link_evidence",
            "nationality_evidence", "graduate_candidates", "lab_members",
            "student_evidence",
        ]:
            try:
                item[field] = json.loads(item[field]) if item.get(field) else []
            except (json.JSONDecodeError, TypeError):
                item[field] = []
        try:
            item["match_breakdown"] = (
                json.loads(item["match_breakdown"])
                if item.get("match_breakdown") else {}
            )
        except (json.JSONDecodeError, TypeError):
            item["match_breakdown"] = {}
        try:
            item["academic_timeline"] = (
                json.loads(item["academic_timeline"])
                if item.get("academic_timeline") else {}
            )
        except (json.JSONDecodeError, TypeError):
            item["academic_timeline"] = {}
        results.append(item)
    return results


def create_expansion_job(job_id: str, scope_type: str, scope_id: str,
                         candidates: list[dict]):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO expansion_jobs
               (id, scope_type, scope_id, status, total)
               VALUES (?, ?, ?, 'queued', ?)""",
            (job_id, scope_type, scope_id, len(candidates)),
        )
        for position, candidate in enumerate(candidates):
            conn.execute(
                """INSERT INTO expansion_items
                   (job_id, position, name, openalex_id, parent_name,
                    parent_openalex_id, value_score, metadata, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
                (
                    job_id, position, candidate.get("name", ""),
                    candidate.get("openalex_id", ""),
                    candidate.get("parent_name", ""),
                    candidate.get("parent_openalex_id", ""),
                    candidate.get("value_score", 0),
                    json.dumps(candidate, ensure_ascii=False),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def update_expansion_job(job_id: str, **values):
    allowed = {
        "status", "completed", "succeeded", "failed",
        "skipped", "current_name", "error",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = get_db()
    conn.execute(
        f"UPDATE expansion_jobs SET {assignments}, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (*values.values(), job_id),
    )
    conn.commit()
    conn.close()


def update_expansion_item(item_id: int, **values):
    allowed = {
        "status", "openalex_id", "attempts", "profile_json", "error",
        "stage", "started_at", "completed_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = get_db()
    conn.execute(
        f"UPDATE expansion_items SET {assignments} WHERE id = ?",
        (*values.values(), item_id),
    )
    conn.commit()
    conn.close()


def get_expansion_job(job_id: str) -> dict:
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM expansion_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not job:
        conn.close()
        return {}
    rows = conn.execute(
        "SELECT * FROM expansion_items WHERE job_id = ? ORDER BY position",
        (job_id,),
    ).fetchall()
    conn.close()
    result = dict(job)
    result["items"] = []
    for row in rows:
        item = dict(row)
        for field_name in ["metadata", "profile_json"]:
            try:
                item[field_name] = (
                    json.loads(item[field_name]) if item.get(field_name) else {}
                )
            except (json.JSONDecodeError, TypeError):
                item[field_name] = {}
        result["items"].append(item)
    return result


def recover_incomplete_expansion_jobs() -> list[str]:
    conn = get_db()
    conn.execute(
        """UPDATE expansion_items
           SET status = 'queued', attempts = MAX(attempts - 1, 0)
           WHERE status = 'running'"""
    )
    conn.execute(
        """UPDATE expansion_jobs
           SET status = 'queued', current_name = NULL,
               recovery_count = COALESCE(recovery_count, 0) + 1,
               last_recovery_at = datetime('now','localtime'),
               updated_at = datetime('now','localtime')
           WHERE status = 'running'"""
    )
    rows = conn.execute(
        "SELECT id FROM expansion_jobs WHERE status = 'queued' ORDER BY created_at"
    ).fetchall()
    conn.commit()
    conn.close()
    return [row["id"] for row in rows]


def get_expanded_profiles(scope_type: str, scope_id: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT ei.profile_json
           FROM expansion_items ei
           JOIN expansion_jobs ej ON ej.id = ei.job_id
           WHERE ej.scope_type = ? AND ej.scope_id = ?
             AND ei.status = 'completed' AND ei.profile_json IS NOT NULL
           ORDER BY ej.created_at, ei.position""",
        (scope_type, scope_id),
    ).fetchall()
    conn.close()
    profiles = []
    index_by_key = {}
    for row in rows:
        try:
            profile = json.loads(row["profile_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        key = (
            (profile.get("openalex_id") or "").rstrip("/").split("/")[-1]
            or profile.get("name", "").lower()
        )
        if not key:
            continue
        if key not in index_by_key:
            index_by_key[key] = len(profiles)
            profiles.append(profile)
            continue
        index = index_by_key[key]
        current = profiles[index]
        current_preference = (
            current.get("contact_search_status", "not-started") != "not-started",
            float(current.get("email_confidence") or 0),
            float(current.get("phone_confidence") or 0),
        )
        new_preference = (
            profile.get("contact_search_status", "not-started") != "not-started",
            float(profile.get("email_confidence") or 0),
            float(profile.get("phone_confidence") or 0),
        )
        primary, secondary = (
            (profile, current) if new_preference > current_preference
            else (current, profile)
        )
        merged = dict(primary)
        for field_name in [
            "name_aliases", "affiliations", "topics", "coauthors",
            "coauthor_details", "sources", "candidates", "institution_records",
            "institution_sites", "contact_candidates", "search_trail",
            "graduation_evidence", "china_link_evidence", "nationality_evidence",
            "graduate_candidates", "lab_members", "student_evidence",
        ]:
            values = list(merged.get(field_name) or [])
            for value in secondary.get(field_name) or []:
                if value not in values:
                    values.append(value)
            merged[field_name] = values
        for field_name, value in secondary.items():
            if merged.get(field_name) in (None, "", [], {}, 0, 0.0) and value not in (
                None, "", [], {}, 0, 0.0
            ):
                merged[field_name] = value
        if "lab-member" in {
            current.get("discovery_origin"), profile.get("discovery_origin")
        }:
            merged["discovery_origin"] = "lab-member"
        profiles[index] = merged
    return profiles


def get_expansion_targets(scope_type: str, scope_id: str) -> list[dict]:
    """Return submitted expansion targets, including queued/running items."""
    conn = get_db()
    rows = conn.execute(
        """SELECT ei.name, ei.openalex_id, ei.status, ei.metadata
           FROM expansion_items ei
           JOIN expansion_jobs ej ON ej.id = ei.job_id
           WHERE ej.scope_type = ? AND ej.scope_id = ?""",
        (scope_type, scope_id),
    ).fetchall()
    conn.close()
    targets = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["metadata"] = {}
        targets.append(item)
    return targets


# Initialize on import
init_db()
