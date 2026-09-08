import tempfile
import unittest
import json
from pathlib import Path

import storage
from app import (
    aggregate_all_contacts, aggregate_batch_contacts,
    _normalize_profile_output, _scope_known_people,
)


class BatchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "batch-test.db"
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_interrupted_item_is_requeued_for_restart(self):
        storage.create_batch_job("batch-1", [
            {"original_filename": "one.pdf", "stored_path": "/tmp/one.pdf"},
            {"original_filename": "two.pdf", "stored_path": "/tmp/two.pdf"},
        ])
        job = storage.get_batch_job("batch-1")
        storage.update_batch_job("batch-1", status="running")
        storage.update_batch_item(
            job["items"][0]["id"], status="running", attempts=1
        )
        storage.update_batch_item(
            job["items"][1]["id"], status="completed"
        )

        resumable = storage.recover_incomplete_batch_jobs()
        recovered = storage.get_batch_job("batch-1")

        self.assertEqual(resumable, ["batch-1"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["items"][0]["status"], "queued")
        self.assertEqual(recovered["items"][0]["attempts"], 0)
        self.assertEqual(recovered["items"][1]["status"], "completed")

    def test_candidate_assessment_fields_round_trip(self):
        storage.save_project("candidate-project", "Quantum paper")
        storage.save_authors("candidate-project", [{
            "name": "Alice Chen",
            "career_stage": "doctoral-student",
            "degree_type": "PhD",
            "graduation_score": 90,
            "graduation_status": "confirmed-upcoming",
            "expected_graduation_year": 2027,
            "graduation_evidence": [{"type": "expected-graduation", "value": "2027"}],
            "china_link_score": 45,
            "china_link_status": "medium",
            "china_link_evidence": [{"type": "china-education", "value": "Tsinghua"}],
            "academic_timeline": {"recent_first_author_count": 2},
            "graduate_candidates": [{"name": "Another Student"}],
            "lab_members": [{"name": "Lab Student", "email": "lab@example.edu"}],
            "student_score": 92,
            "student_status": "confirmed-student",
            "student_evidence": [{"type": "official-lab-roster"}],
        }])
        author = storage.get_authors("candidate-project")[0]
        self.assertEqual(author["career_stage"], "doctoral-student")
        self.assertEqual(author["expected_graduation_year"], 2027)
        self.assertEqual(author["graduation_evidence"][0]["value"], "2027")
        self.assertEqual(author["academic_timeline"]["recent_first_author_count"], 2)
        self.assertEqual(author["graduate_candidates"][0]["name"], "Another Student")
        self.assertEqual(author["lab_members"][0]["email"], "lab@example.edu")
        self.assertEqual(author["student_status"], "confirmed-student")

    def test_scope_known_people_includes_base_authors_and_expansions(self):
        storage.save_project("project-1", "Quantum paper")
        storage.save_authors("project-1", [{
            "name": "Xi-Feng Ren",
            "openalex_id": "https://openalex.org/A200",
        }])
        ids, names = _scope_known_people("project", "project-1")
        self.assertIn("A200", ids)
        self.assertIn("xifengren", names)

    def test_expansion_job_recovers_and_persists_profiles(self):
        storage.create_expansion_job("expand-1", "batch", "batch-1", [{
            "name": "Alice Chen",
            "parent_name": "Target Author",
            "parent_openalex_id": "https://openalex.org/A100",
            "value_score": 90,
        }])
        job = storage.get_expansion_job("expand-1")
        item_id = job["items"][0]["id"]
        storage.update_expansion_job("expand-1", status="running")
        storage.update_expansion_item(item_id, status="running", attempts=1)
        self.assertEqual(storage.recover_incomplete_expansion_jobs(), ["expand-1"])
        recovered = storage.get_expansion_job("expand-1")
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["recovery_count"], 1)
        self.assertTrue(recovered["last_recovery_at"])
        self.assertEqual(recovered["items"][0]["status"], "queued")

        profile = {
            "name": "Alice Chen",
            "openalex_id": "https://openalex.org/A200",
            "match_status": "verified",
            "discovery_origin": "coauthor",
        }
        storage.update_expansion_item(
            item_id, status="completed",
            profile_json=json.dumps(profile),
        )
        expanded = storage.get_expanded_profiles("batch", "batch-1")
        self.assertEqual(expanded, [profile])

    def test_same_expanded_identity_merges_coauthor_and_lab_member_evidence(self):
        storage.create_expansion_job("coauthor-job", "project", "p1", [{
            "name": "Alice Chen", "openalex_id": "https://openalex.org/A123",
            "parent_name": "Parent", "parent_openalex_id": "https://openalex.org/A1",
        }])
        coauthor_item = storage.get_expansion_job("coauthor-job")["items"][0]
        storage.update_expansion_item(
            coauthor_item["id"], status="completed",
            profile_json=json.dumps({
                "name": "Alice Chen", "openalex_id": "https://openalex.org/A123",
                "discovery_origin": "coauthor", "coauthors": ["Parent"],
            }),
        )
        storage.create_expansion_job("lab-job", "project", "p1", [{
            "candidate_type": "lab-member", "name": "Alice Chen",
            "openalex_id": "https://openalex.org/A123",
        }])
        lab_item = storage.get_expansion_job("lab-job")["items"][0]
        storage.update_expansion_item(
            lab_item["id"], status="completed",
            profile_json=json.dumps({
                "name": "Alice Chen", "openalex_id": "https://openalex.org/A123",
                "discovery_origin": "lab-member", "lab_name": "Example Lab",
                "contact_search_status": "contact-found",
                "email": "alice@example.edu", "email_confidence": 95,
            }),
        )
        profiles = storage.get_expanded_profiles("project", "p1")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["discovery_origin"], "lab-member")
        self.assertEqual(profiles[0]["email"], "alice@example.edu")
        self.assertEqual(profiles[0]["coauthors"], ["Parent"])

    def test_selected_project_authors_include_paper_provenance(self):
        storage.save_project("paper-a", "Paper A", "10.1/a", "pdf", "a.pdf")
        storage.save_project("paper-b", "Paper B", "10.1/b", "pdf", "b.pdf")
        storage.save_authors("paper-a", [{
            "name": "Alice Chen", "openalex_id": "https://openalex.org/A1",
            "match_status": "verified",
        }])
        storage.save_authors("paper-b", [{
            "name": "Bob Li", "openalex_id": "https://openalex.org/A2",
            "match_status": "verified",
        }])
        selected = storage.get_authors_for_projects(["paper-b"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["name"], "Bob Li")
        self.assertEqual(selected[0]["paper_title"], "Paper B")
        self.assertEqual(selected[0]["paper_source_info"], "b.pdf")


class BatchAggregationTests(unittest.TestCase):
    def test_profile_output_filters_polluted_alias_and_duplicate_identity(self):
        profiles = _normalize_profile_output([
            {
                "name": "Di Liu", "openalex_id": "https://openalex.org/A100",
                "name_aliases": ["Liu Di", "Jiuyan Li"],
                "discovery_origin": "paper-author", "email": "di@example.edu",
                "email_confidence": 100,
            },
            {
                "name": "Di‐Liu", "openalex_id": "https://openalex.org/A100",
                "name_aliases": [], "discovery_origin": "coauthor",
            },
        ])
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["email"], "di@example.edu")
        self.assertIn("Liu Di", profiles[0]["name_aliases"])
        self.assertNotIn("Jiuyan Li", profiles[0]["name_aliases"])

    def _occurrence(self, project_id, row_id, **overrides):
        data = {
            "id": row_id,
            "batch_project_id": project_id,
            "paper_title": f"Paper {project_id}",
            "paper_doi": "",
            "original_filename": f"{project_id}.pdf",
            "paper_position": row_id,
            "name": "Alex Kim",
            "openalex_id": "https://openalex.org/A123",
            "orcid": "",
            "match_status": "verified",
            "email": "",
            "email_source": "",
            "email_confidence": 0,
            "phone": "",
            "phone_source": "",
            "phone_confidence": 0,
            "works_count": 10,
            "cited_by_count": 20,
            "affiliations": [],
            "topics": [],
            "coauthors": [],
            "sources": [],
            "candidates": [],
            "institution_records": [],
            "institution_sites": [],
            "contact_candidates": [],
            "search_trail": [],
        }
        data.update(overrides)
        return data

    def test_verified_openalex_identity_merges_across_papers(self):
        contacts = aggregate_batch_contacts([
            self._occurrence("p1", 1, email="alex@old.edu", email_confidence=75),
            self._occurrence(
                "p2", 2, email="alex@official.edu", email_confidence=95,
                affiliations=["Official University"],
            ),
        ])

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["paper_count"], 2)
        self.assertEqual(contacts[0]["email"], "alex@official.edu")
        self.assertEqual(contacts[0]["affiliations"], ["Official University"])

    def test_same_unverified_name_is_not_merged(self):
        contacts = aggregate_batch_contacts([
            self._occurrence("p1", 1, match_status="insufficient"),
            self._occurrence("p2", 2, match_status="insufficient"),
        ])

        self.assertEqual(len(contacts), 2)

    def test_all_contacts_merge_verified_identity_across_saved_projects(self):
        contacts = aggregate_all_contacts([
            self._occurrence(
                "p1", 1, paper_title="Paper One",
                paper_doi="10.1/one", email="alex@old.edu", email_confidence=75,
            ),
            self._occurrence(
                "p2", 2, paper_title="Paper Two",
                paper_doi="10.1/two", email="alex@official.edu", email_confidence=95,
            ),
        ])
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["paper_count"], 2)
        self.assertEqual(contacts[0]["email"], "alex@official.edu")
        self.assertEqual(contacts[0]["paper_titles"], ["Paper One", "Paper Two"])
        self.assertEqual(contacts[0]["paper_dois"], ["10.1/one", "10.1/two"])
        self.assertEqual(contacts[0]["source_project_ids"], ["p1", "p2"])

    def test_all_contacts_does_not_merge_unverified_same_name(self):
        contacts = aggregate_all_contacts([
            self._occurrence("p1", 1, match_status="review"),
            self._occurrence("p2", 2, match_status="review"),
        ])
        self.assertEqual(len(contacts), 2)


if __name__ == "__main__":
    unittest.main()
