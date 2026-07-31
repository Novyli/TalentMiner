"""SQLite persistence layer for TalentMiner crawl results."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "talentminer.db"


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
            sources TEXT,
            email_source TEXT,
            match_score REAL DEFAULT 0,
            match_status TEXT DEFAULT 'unmatched',
            match_breakdown TEXT,
            candidates TEXT,
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
        conn.execute("""
            INSERT INTO authors (project_id, name, email, orcid, openalex_id,
                google_scholar_url, researchgate_url, twitter_url, linkedin_url,
                website_url, affiliations, topics, cited_by_count, works_count,
                coauthors, sources, email_source, match_score, match_status,
                match_breakdown, candidates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            project_id,
            p.get("name", ""),
            p.get("email", ""),
            p.get("orcid", ""),
            p.get("openalex_id", ""),
            p.get("google_scholar_url", ""),
            p.get("researchgate_url", ""),
            p.get("twitter_url", ""),
            p.get("linkedin_url", ""),
            p.get("website_url", ""),
            json.dumps(p.get("affiliations", []), ensure_ascii=False),
            json.dumps(p.get("topics", []), ensure_ascii=False),
            p.get("cited_by_count", 0),
            p.get("works_count", 0),
            json.dumps(p.get("coauthors", []), ensure_ascii=False),
            json.dumps(p.get("sources", []), ensure_ascii=False),
            p.get("email_source", ""),
            p.get("match_score", 0),
            p.get("match_status", "unmatched"),
            json.dumps(p.get("match_breakdown", {}), ensure_ascii=False),
            json.dumps(p.get("candidates", []), ensure_ascii=False),
        ))
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
        for field in ["affiliations", "topics", "coauthors", "sources", "candidates"]:
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
        SELECT a.*, p.title as paper_title, p.doi as paper_doi
        FROM authors a LEFT JOIN projects p ON a.project_id = p.id
        ORDER BY a.project_id, a.id
    """).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        for field in ["affiliations", "topics", "coauthors", "sources", "candidates"]:
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
        results.append(d)
    return results


# Initialize on import
init_db()
