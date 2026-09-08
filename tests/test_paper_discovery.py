import json
import tempfile
import unittest
from pathlib import Path

import storage
from app import apply_author_scope, prepare_work_metadata
from paper_discovery import (
    _retry_after_seconds, compact_crossref_work, compact_work,
    deduplicate_papers, normalize_doi, paper_fingerprint, paper_identity,
)


def sample_work():
    return {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1234/TEST",
        "title": "Historical Quantum Paper",
        "publication_date": "2022-03-04",
        "publication_year": 2022,
        "type": "article",
        "cited_by_count": 8,
        "open_access": {"is_oa": True},
        "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
        "authorships": [
            {
                "author_position": "first",
                "is_corresponding": False,
                "author": {
                    "id": "https://openalex.org/A1",
                    "display_name": "Alice Chen",
                    "orcid": "https://orcid.org/0000-0000-0000-0001",
                },
                "institutions": [{
                    "id": "https://openalex.org/I1",
                    "display_name": "Quantum University",
                }],
            },
            {
                "author_position": "last",
                "is_corresponding": True,
                "author": {
                    "id": "https://openalex.org/A2",
                    "display_name": "Bob Li",
                },
                "institutions": [],
            },
        ],
        "topics": [{"display_name": "Quantum error correction"}],
    }


def sample_crossref_work():
    return {
        "DOI": "10.1234/CROSSREF",
        "title": ["Crossref Quantum Paper"],
        "container-title": ["Quantum Journal"],
        "published-online": {"date-parts": [[2025, 7, 3]]},
        "type": "journal-article",
        "is-referenced-by-count": 12,
        "URL": "https://doi.org/10.1234/CROSSREF",
        "author": [{
            "given": "Alice", "family": "Chen",
            "ORCID": "https://orcid.org/0000-0000-0000-0001",
            "affiliation": [{"name": "Quantum University"}],
        }],
        "link": [{
            "URL": "https://example.org/paper.pdf",
            "content-type": "application/pdf",
        }],
        "subject": ["Quantum information"],
    }


class PaperDiscoveryTests(unittest.TestCase):
    def test_deduplicates_repository_records_with_distinct_dois(self):
        first = compact_work(sample_work())
        duplicate = compact_work({
            **sample_work(),
            "id": "https://openalex.org/W456",
            "doi": "https://doi.org/10.1234/CONCEPT-DOI",
        })
        self.assertNotEqual(first["identity"], duplicate["identity"])
        self.assertEqual(paper_fingerprint(first), paper_fingerprint(duplicate))
        self.assertEqual(deduplicate_papers([first, duplicate]), [first])

    def test_keeps_same_title_when_date_or_authors_differ(self):
        first = compact_work(sample_work())
        later = compact_work({
            **sample_work(),
            "id": "https://openalex.org/W456",
            "doi": "https://doi.org/10.1234/LATER",
            "publication_date": "2023-03-04",
        })
        different_author = compact_work({
            **sample_work(),
            "id": "https://openalex.org/W789",
            "doi": "https://doi.org/10.1234/OTHER-AUTHOR",
            "authorships": [{
                "author_position": "first",
                "is_corresponding": False,
                "author": {"id": "A9", "display_name": "Carol Wu"},
                "institutions": [],
            }],
        })
        self.assertEqual(
            deduplicate_papers([first, later, different_author]),
            [first, later, different_author],
        )

    def test_openalex_retry_delay_uses_header_and_bounded_backoff(self):
        self.assertEqual(_retry_after_seconds("7", 0), 7)
        self.assertEqual(_retry_after_seconds("120", 0), 60)
        self.assertEqual(_retry_after_seconds("invalid", 0), 2)
        self.assertEqual(_retry_after_seconds(None, 10), 30)

    def test_compacts_crossref_fallback_to_preview_schema(self):
        paper = compact_crossref_work(sample_crossref_work())
        self.assertEqual(paper["identity"], "doi:10.1234/crossref")
        self.assertEqual(paper["publication_date"], "2025-07-03")
        self.assertEqual(paper["authors"], ["Alice Chen"])
        self.assertEqual(
            paper["authorships"][0]["institutions"][0]["display_name"],
            "Quantum University",
        )
        self.assertEqual(paper["pdf_url"], "https://example.org/paper.pdf")

    def test_compacts_work_and_normalizes_identity(self):
        paper = compact_work(sample_work())
        self.assertEqual(normalize_doi(paper["doi"]), "10.1234/test")
        self.assertEqual(paper_identity(paper), "doi:10.1234/test")
        self.assertEqual(paper["authors"], ["Alice Chen", "Bob Li"])
        self.assertEqual(paper["corresponding_authors"], ["Bob Li"])
        self.assertEqual(paper["pdf_url"], "https://example.org/paper.pdf")

    def test_metadata_fallback_honors_author_scope_and_identity_map(self):
        paper = compact_work(sample_work())
        parsed = prepare_work_metadata(paper, "corresponding")
        self.assertEqual(parsed["authors"], ["Bob Li"])
        self.assertEqual(
            parsed["context"]["openalex_authors_map"]["Bob Li"]["openalex_id"],
            "https://openalex.org/A2",
        )

    def test_crossref_metadata_skips_exhausted_openalex_author_lookup(self):
        paper = compact_crossref_work(sample_crossref_work())
        parsed = prepare_work_metadata(paper, "all")
        identity = parsed["context"]["openalex_authors_map"]["Alice Chen"]
        self.assertTrue(parsed["context"]["skip_openalex_lookup"])
        self.assertEqual(
            identity["orcid"], "https://orcid.org/0000-0000-0000-0001"
        )
        self.assertIn("Crossref", identity["source"])

    def test_downloaded_pdf_result_still_honors_first_author_scope(self):
        paper = compact_work(sample_work())
        parsed = {"authors": ["Alice Chen", "Bob Li"], "author_count": 2}
        result = apply_author_scope(parsed, paper, "first")
        self.assertEqual(result["authors"], ["Alice Chen"])
        self.assertEqual(result["author_count"], 1)


class SearchBatchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "search-batch.db"
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_search_metadata_round_trip(self):
        paper = compact_work(sample_work())
        storage.create_batch_job(
            "history-1",
            [{
                "original_filename": "W123.pdf",
                "stored_path": "/tmp/W123.pdf",
                "source_url": paper["pdf_url"],
                "metadata": paper,
            }],
            source_type="historical-search",
            source_info=json.dumps({"query": "quantum"}),
        )
        job = storage.get_batch_job("history-1")
        self.assertEqual(job["source_type"], "historical-search")
        self.assertEqual(job["items"][0]["metadata"]["title"], paper["title"])
        self.assertEqual(job["items"][0]["source_url"], paper["pdf_url"])


if __name__ == "__main__":
    unittest.main()
