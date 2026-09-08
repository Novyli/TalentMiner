import unittest
from types import SimpleNamespace

from graph_builder import build_coauthorship_graph


def profile(name, coauthors=None, affiliations=None, topics=None, **extra):
    data = dict(
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
    data.update(extra)
    return SimpleNamespace(**data)


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

    def test_preserves_candidate_assessment_on_graph_nodes(self):
        graph = build_coauthorship_graph([
            profile(
                "Alice Chen",
                graduation_status="confirmed-upcoming",
                graduation_score=90,
                career_stage="doctoral-student",
                expected_graduation_year=2027,
                china_link_score=45,
                china_link_status="medium",
                graduate_candidates=[{"name": "Lab Student"}],
            )
        ])
        node = graph["nodes"][0]
        self.assertEqual(node["graduation_status"], "confirmed-upcoming")
        self.assertEqual(node["expected_graduation_year"], 2027)
        self.assertEqual(node["china_link_status"], "medium")
        self.assertEqual(graph["stats"]["graduation_candidate_count"], 1)
        self.assertEqual(graph["stats"]["china_linked_count"], 1)
        self.assertEqual(graph["stats"]["official_roster_candidate_count"], 1)

    def test_external_coauthor_has_verified_expansion_path_and_priority(self):
        graph = build_coauthorship_graph([
            profile(
                "Target Author",
                coauthors=["Alice Chen"],
                openalex_id="https://openalex.org/A100",
                coauthor_details=[{
                    "name": "Alice Chen",
                    "openalex_id": "https://openalex.org/A200",
                    "shared_works_count": 4,
                    "recent_shared_works_count": 2,
                    "latest_shared_year": 2026,
                    "value_score": 90,
                    "shared_topics": ["Quantum optics"],
                }],
            )
        ])
        external = next(node for node in graph["nodes"] if node["group"] == 1)
        self.assertEqual(external["openalex_id"], "https://openalex.org/A200")
        self.assertEqual(external["parent_names"], ["Target Author"])
        self.assertEqual(
            external["parent_openalex_ids"], ["https://openalex.org/A100"]
        )
        self.assertEqual(external["shared_works_count"], 4)
        self.assertEqual(external["expansion_value_score"], 90)

    def test_non_physics_coauthor_is_not_expansion_eligible(self):
        graph = build_coauthorship_graph([
            profile(
                "Target Author", openalex_id="https://openalex.org/A100",
                coauthors=["Water Researcher"],
                coauthor_details=[{
                    "name": "Water Researcher",
                    "openalex_id": "https://openalex.org/A300",
                    "shared_works_count": 20,
                    "shared_topics": ["Hydrology and Watershed Management"],
                    "sample_titles": ["Groundwater quality evolution"],
                    "value_score": 100,
                }],
            )
        ])
        external = next(node for node in graph["nodes"] if node["group"] == 1)
        self.assertFalse(external["field_relevant"])

    def test_lab_members_connect_through_lab_entity(self):
        graph = build_coauthorship_graph([
            profile("Guocheng Zhen", coauthors=["Ziao Tang"], lab_members=[{
                "name": "Ziao Tang", "career_stage": "doctoral-student",
                "student_status": "confirmed-student", "student_score": 92,
                "email": "ztang@example.edu", "profile_url": "https://lab.example/ziao",
                "directory_url": "https://lab.example/people",
                "lab_name": "Quantum AI Research Lab",
                "lab_url": "https://lab.example/", "lab_pi": "Xin Wang",
            }])
        ])
        member = next(node for node in graph["nodes"] if node["group"] == 2)
        lab = next(node for node in graph["nodes"] if node["group"] == 3)
        self.assertEqual(member["name"], "Ziao Tang")
        self.assertEqual(member["email"], "ztang@example.edu")
        self.assertEqual(member["student_status"], "confirmed-student")
        self.assertEqual(lab["name"], "Quantum AI Research Lab")
        self.assertEqual(lab["lab_pi"], "Xin Wang")
        self.assertEqual(
            sum(edge["relation"] == "lab-member" for edge in graph["edges"]), 2
        )
        self.assertTrue(any(edge["relation"] == "lab-pi" for edge in graph["edges"]))
        self.assertFalse(any(edge["relation"] == "same-lab" for edge in graph["edges"]))
        self.assertFalse(any(
            node["group"] == 1 and node["name"] == "Ziao Tang"
            for node in graph["nodes"]
        ))
        self.assertEqual(graph["stats"]["lab_member_count"], 1)
        self.assertEqual(graph["stats"]["lab_entity_count"], 1)

    def test_roster_discoverer_does_not_replace_known_lab_pi(self):
        shared_lab = "https://lab.example/"
        directory = "https://lab.example/people"
        graph = build_coauthorship_graph([
            profile("Professor Guo", openalex_id="https://openalex.org/A1", lab_members=[{
                "name": "Fangming Jing", "lab_name": "Quantum Lab",
                "lab_url": shared_lab, "directory_url": directory,
                "lab_pi": "Professor Guo",
            }]),
            profile("Fangming Jing", openalex_id="https://openalex.org/A2", lab_members=[{
                "name": "Ziyuan Chen", "lab_name": "Quantum Lab",
                "lab_url": shared_lab, "directory_url": directory,
                "lab_pi": "",
            }]),
        ])
        lab = next(node for node in graph["nodes"] if node["group"] == 3)
        ziyuan = next(node for node in graph["nodes"] if node["name"] == "Ziyuan Chen")
        self.assertEqual(lab["lab_pi"], "Professor Guo")
        self.assertEqual(ziyuan["lab_pi"], "Professor Guo")
        self.assertEqual(ziyuan["parent_names"], ["Professor Guo"])
        self.assertFalse(any(
            edge["relation"] == "lab-pi" and edge["source"] == "Fangming Jing"
            for edge in graph["edges"]
        ))

    def test_large_graph_does_not_require_scipy_layout(self):
        graph = build_coauthorship_graph([
            profile(f"Author {index}") for index in range(401)
        ])
        self.assertEqual(graph["stats"]["node_count"], 402)
        self.assertEqual(len(graph["nodes"]), 402)

    def test_large_paper_uses_hub_and_suppresses_cliques(self):
        profiles = [
            profile(
                f"Author {index}",
                coauthors=[f"Author {(index + 1) % 20}"],
                affiliations=["Google"],
                topics=["Quantum computing"],
                coauthor_details=[{
                    "name": f"Author {(index + 1) % 20}",
                    "shared_works_count": 1,
                }],
            )
            for index in range(20)
        ]
        graph = build_coauthorship_graph(profiles)

        self.assertEqual(
            sum(edge["relation"] == "paper-author" for edge in graph["edges"]),
            20,
        )
        self.assertFalse(any(
            edge["relation"] in {"same-affiliation", "shared-topic", "co-author"}
            for edge in graph["edges"]
        ))
        self.assertTrue(any(node["group"] == 5 for node in graph["nodes"]))

    def test_large_paper_keeps_only_repeated_top_collaborators(self):
        lead_details = [
            {"name": f"Author {index}", "shared_works_count": index}
            for index in range(1, 12)
        ]
        profiles = [
            profile(
                "Author 0",
                coauthors=[item["name"] for item in lead_details],
                coauthor_details=lead_details,
            )
        ] + [profile(f"Author {index}") for index in range(1, 13)]
        graph = build_coauthorship_graph(profiles)
        strong_edges = [
            edge for edge in graph["edges"] if edge["relation"] == "co-author"
        ]

        self.assertEqual(len(strong_edges), 5)
        self.assertEqual(
            {edge["target"] for edge in strong_edges},
            {"Author 7", "Author 8", "Author 9", "Author 10", "Author 11"},
        )

    def test_aggregate_graph_uses_distinct_source_paper_hubs(self):
        profiles = [
            profile(
                f"Author {index}",
                papers=[{
                    "project_id": "paper-a" if index < 7 else "paper-b",
                    "title": "Paper A" if index < 7 else "Paper B",
                    "doi": "10.1/a" if index < 7 else "10.1/b",
                }],
            )
            for index in range(13)
        ]
        graph = build_coauthorship_graph(profiles, paper_scope="aggregate")
        paper_nodes = [node for node in graph["nodes"] if node["group"] == 5]
        paper_edges = [
            edge for edge in graph["edges"] if edge["relation"] == "paper-author"
        ]

        self.assertEqual({node["name"] for node in paper_nodes}, {"Paper A", "Paper B"})
        self.assertEqual(len(paper_edges), 13)
        self.assertFalse(any(node["name"].startswith("本论文（") for node in paper_nodes))

    def test_aggregate_graph_without_provenance_has_no_fake_paper_hub(self):
        graph = build_coauthorship_graph(
            [profile(f"Author {index}") for index in range(20)],
            paper_scope="aggregate",
        )
        self.assertFalse(any(node["group"] == 5 for node in graph["nodes"]))

    def test_openalex_id_prevents_hyphen_variant_duplicate_nodes(self):
        graph = build_coauthorship_graph([
            profile(
                "Guo-Ping Guo",
                openalex_id="https://openalex.org/A100",
                coauthors=["Xi‐Feng Ren"],
                coauthor_details=[{
                    "name": "Xi‐Feng Ren",
                    "openalex_id": "https://openalex.org/A200",
                }],
            ),
            profile(
                "Xi-Feng Ren",
                openalex_id="https://openalex.org/A200",
                email="renxf@ustc.edu.cn",
                email_confidence=100,
            ),
            profile(
                "Xi‐Feng Ren",
                openalex_id="https://openalex.org/A200",
                discovery_origin="coauthor",
            ),
        ])
        people = [node for node in graph["nodes"] if node["group"] == 0]
        self.assertEqual(len(people), 2)
        self.assertFalse(any(node["group"] == 1 for node in graph["nodes"]))
        ren = next(node for node in people if node["openalex_id"].endswith("A200"))
        self.assertEqual(ren["name"], "Xi-Feng Ren")
        self.assertEqual(ren["email"], "renxf@ustc.edu.cn")


if __name__ == "__main__":
    unittest.main()
