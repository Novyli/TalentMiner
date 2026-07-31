import unittest
from types import SimpleNamespace

from graph_builder import build_coauthorship_graph


def profile(name, coauthors=None, affiliations=None, topics=None):
    return SimpleNamespace(
        name=name,
        openalex_id="",
        orcid="",
        google_scholar_url="",
        researchgate_url="",
        twitter_url="",
        linkedin_url="",
        affiliations=affiliations or [],
        topics=topics or [],
        cited_by_count=0,
        works_count=0,
        email="",
        email_source="",
        match_score=0,
        website_url="",
        sources=[],
        coauthors=coauthors or [],
    )


class GraphBuilderTests(unittest.TestCase):
    def test_keeps_multiple_relation_types_between_same_people(self):
        graph = build_coauthorship_graph([
            profile(
                "Alice",
                coauthors=["Bob"],
                affiliations=["JILA"],
                topics=["Precision metrology"],
            ),
            profile(
                "Bob",
                coauthors=["Alice"],
                affiliations=["JILA"],
                topics=["Precision metrology"],
            ),
        ])

        alice_bob_edges = [
            edge for edge in graph["edges"]
            if {edge["source"], edge["target"]} == {"Alice", "Bob"}
        ]
        self.assertEqual(
            {edge["relation"] for edge in alice_bob_edges},
            {"co-author", "same-affiliation", "shared-topic"},
        )
        self.assertEqual(graph["stats"]["edge_count"], 3)
        self.assertEqual(graph["stats"]["pair_count"], 1)

        evidence = {
            edge["relation"]: edge["evidence"]
            for edge in alice_bob_edges
        }
        self.assertIn("JILA", evidence["same-affiliation"])
        self.assertIn("Precision metrology", evidence["shared-topic"])


if __name__ == "__main__":
    unittest.main()
