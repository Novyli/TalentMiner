import unittest

from app import build_openalex_authors_map
from crawler import (
    _extract_arxiv_email_from_feed,
    _institution_similarity,
    _match_status,
    _score_author_match_details,
)
from pdf_parser import extract_context


class IdentityMatchingTests(unittest.TestCase):
    def setUp(self):
        self.paper_affiliation = (
            "JILA, NIST and University of Colorado, and Department of Physics, "
            "University of Colorado, Boulder CO 80309, USA"
        )

    def test_normalizes_long_paper_affiliation(self):
        self.assertEqual(
            _institution_similarity(
                self.paper_affiliation, "University of Colorado Boulder"
            ),
            1.0,
        )
        self.assertEqual(
            _institution_similarity(
                self.paper_affiliation,
                "National Institute of Standards and Technology",
            ),
            1.0,
        )

    def test_scores_name_institution_and_topic_without_popularity_bias(self):
        author = {
            "display_name": "Sun Yool Park",
            "last_known_institutions": [
                {"display_name": "University of Colorado Boulder"}
            ],
            "topics": [{"display_name": "Quantum precision measurement"}],
            "works_count": 5000,
            "cited_by_count": 100000,
        }
        score, breakdown = _score_author_match_details(
            author,
            {
                "institutions": [self.paper_affiliation],
                "institution_scope": "author",
                "keywords": ["quantum"],
            },
            "Sun Yool Park",
        )
        self.assertEqual(score, 83.0)
        self.assertEqual(_match_status(score), "verified")
        self.assertNotIn("works_count", breakdown)
        self.assertNotIn("cited_by_count", breakdown)

    def test_maps_resolved_work_authorship_to_author_ids(self):
        work = {
            "id": "https://openalex.org/W123",
            "authorships": [{
                "author": {
                    "id": "https://openalex.org/A123",
                    "display_name": "Sun Yool Park",
                    "orcid": "https://orcid.org/0000-0000-0000-0001",
                },
                "institutions": [{
                    "display_name": "University of Colorado Boulder"
                }],
                "raw_affiliation_strings": ["JILA, NIST and University of Colorado"],
            }],
        }
        mapping = build_openalex_authors_map(work, ["Sun Yool Park"])
        self.assertEqual(
            mapping["Sun Yool Park"]["openalex_id"],
            "https://openalex.org/A123",
        )
        self.assertIn(
            "University of Colorado Boulder",
            mapping["Sun Yool Park"]["affiliations"],
        )

    def test_keyword_extraction_uses_word_boundaries(self):
        context = extract_context(
            "Details remain available. A rotating quantum circuit is studied."
        )
        self.assertIn("quantum", context["keywords"])
        self.assertIn("circuit", context["keywords"])
        self.assertNotIn("ai", context["keywords"])
        self.assertNotIn("pass", context["keywords"])

    def test_rejects_unrelated_arxiv_email_despite_matching_author_entry(self):
        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2607.21511</id>
            <author><name>Patricia Hector Hernandez</name></author>
            <summary>Legacy HECTOR contact: hector@ifh.de</summary>
          </entry>
        </feed>"""
        result = _extract_arxiv_email_from_feed(
            feed, "Patricia Hector Hernandez"
        )
        self.assertIsNone(result["email"])
        self.assertEqual(result["matched_papers"], 1)
        self.assertEqual(result["rejected_candidates"], 1)

    def test_rejects_arxiv_collaboration_mailbox(self):
        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2607.21511</id>
            <author><name>Eric A. Cornell</name></author>
            <summary>cms-and-lhcb-publication-committees@cern.ch</summary>
          </entry>
        </feed>"""
        result = _extract_arxiv_email_from_feed(feed, "Eric A. Cornell")
        self.assertIsNone(result["email"])
        self.assertEqual(result["rejected_candidates"], 1)

    def test_accepts_arxiv_email_that_identifies_the_author(self):
        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/1234.5678</id>
            <author><name>Eric A. Cornell</name></author>
            <summary>Correspondence: eacornell@example.edu</summary>
          </entry>
        </feed>"""
        result = _extract_arxiv_email_from_feed(feed, "Eric A. Cornell")
        self.assertEqual(result["email"], "eacornell@example.edu")
        self.assertEqual(
            result["source"], "arXiv paper: https://arxiv.org/abs/1234.5678"
        )


if __name__ == "__main__":
    unittest.main()
