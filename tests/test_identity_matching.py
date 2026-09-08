import unittest
from unittest.mock import AsyncMock, patch

from app import _apply_scope_lab_enrichment, build_openalex_authors_map
from crawler import (
    AuthorProfile,
    _author_institution_records,
    _extract_arxiv_email_from_feed,
    _institution_similarity,
    _match_status,
    _official_search_queries,
    _trusted_openalex_name_variants,
    _score_author_match_details,
    analyze_author_background,
    assess_student_status,
    crawl_lab_member_contact,
    extract_graduate_candidates_from_page,
    extract_official_contact_from_page,
    fetch_openalex_academic_signals,
    resolve_coauthor_identity,
    search_openalex_authors,
    search_verified_personal_websites,
)
from pdf_parser import extract_author_affiliation_map, extract_context


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

    def test_distinguishes_hkust_from_ustc(self):
        self.assertEqual(
            _institution_similarity(
                "The Hong Kong University of Science and Technology (Guangzhou), China",
                "University of Science and Technology of China",
            ),
            0.0,
        )

    def test_maps_special_author_footnote_to_research_affiliation(self):
        text = (
            "Chengkai Zhu¶1, and Xin Wang‖2\n"
            "1QudeLeap Research, Shanghai 200030, China\n"
            "2The Hong Kong University of Science and Technology (Guangzhou), China\n"
            "Abstract\n"
        )
        mapping = extract_author_affiliation_map(
            text, ["Chengkai Zhu", "Xin Wang"]
        )
        self.assertEqual(
            mapping["Chengkai Zhu"],
            ["QudeLeap Research, Shanghai 200030, China"],
        )

    def test_marks_former_phd_student_as_recent_graduate(self):
        result = analyze_author_background(
            "<h1>Chengkai Zhu</h1><p>Former PhD student from Sept 2023 to Jun 2026.</p>",
            "https://lab.example/chengkai", "Chengkai Zhu",
        )
        self.assertEqual(result["career_stage"], "recent-graduate")
        self.assertEqual(result["graduation_status"], "graduated")
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
                    "id": "https://openalex.org/I123",
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
        self.assertEqual(
            mapping["Sun Yool Park"]["institution_records"][0]["id"],
            "https://openalex.org/I123",
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

    def test_uses_recent_openalex_affiliations_when_last_known_is_empty(self):
        records = _author_institution_records({
            "last_known_institutions": None,
            "affiliations": [
                {
                    "institution": {
                        "id": "https://openalex.org/I1",
                        "display_name": "Current Quantum Institute",
                    },
                    "years": [2026, 2025],
                },
                {
                    "institution": {
                        "id": "https://openalex.org/I2",
                        "display_name": "Old University",
                    },
                    "years": [2018],
                },
            ],
        })
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["display_name"], "Current Quantum Institute")

    def test_extracts_contact_only_from_named_official_quantum_page(self):
        html = """
        <html><body>
          <h1>Wei Zhang</h1>
          <p>School of Physics · Quantum Information Laboratory</p>
          <a href="mailto:wei.zhang@pku.edu.cn">Email</a>
          <p>手机：13800138000</p>
        </body></html>
        """
        result = extract_official_contact_from_page(
            html,
            "https://quantum.pku.edu.cn/people/wei-zhang",
            "Wei Zhang",
            "pku.edu.cn",
        )
        self.assertEqual(result["email"], "wei.zhang@pku.edu.cn")
        self.assertGreaterEqual(result["email_confidence"], 75)
        self.assertEqual(result["phone"], "13800138000")
        self.assertEqual(result["department"], "quantum")

    def test_decodes_obfuscated_email_on_named_profile(self):
        result = extract_official_contact_from_page(
            "<h1>Cunxi Yu</h1><p>Email: cunxiyu(at)umd(dot)edu</p>",
            "https://ycunxi.github.io/cunxiyu/", "Cunxi Yu", "ycunxi.github.io",
        )
        candidate = next(item for item in result["candidates"] if item["type"] == "email")
        self.assertEqual(candidate["value"], "cunxiyu@umd.edu")
        self.assertEqual(candidate["owner_type"], "target")

    def test_group_alumni_text_does_not_override_current_professor_role(self):
        result = analyze_author_background(
            "<h1>Cunxi Yu</h1><p>Assistant Professor at UMD.</p>"
            "<h2>Group alumnus</h2><p>Former students</p>",
            "https://example.edu/cunxi", "Cunxi Yu",
        )
        self.assertEqual(result["career_stage"], "faculty")

    def test_rejects_public_mailbox_and_wrong_person_page(self):
        public_mailbox = extract_official_contact_from_page(
            "<h1>Eric Cornell</h1><p>Physics team</p>"
            "<a href='mailto:quantum-team@colorado.edu'>contact</a>",
            "https://www.colorado.edu/physics/eric-cornell",
            "Eric Cornell",
            "colorado.edu",
        )
        self.assertIsNone(public_mailbox["email"])

        unrelated_email = extract_official_contact_from_page(
            "<h1>Eric Cornell</h1><p>Quantum Physics</p>"
            "<p>Assistant: other.person@colorado.edu</p>",
            "https://www.colorado.edu/physics/eric-cornell",
            "Eric Cornell",
            "colorado.edu",
        )
        self.assertIsNone(unrelated_email["email"])
        self.assertEqual(
            unrelated_email["candidates"][0]["status"], "review"
        )

        wrong_person = extract_official_contact_from_page(
            "<h1>Another Researcher</h1><p>Physics</p>"
            "<a href='mailto:eric.cornell@colorado.edu'>email</a>",
            "https://www.colorado.edu/physics/another",
            "Eric Cornell",
            "colorado.edu",
        )
        self.assertFalse(wrong_person["name_matched"])
        self.assertIsNone(wrong_person["email"])

    def test_chinese_name_aliases_expand_official_search_paths(self):
        queries = _official_search_queries(
            "Wei Zhang",
            {"domain": "pku.edu.cn", "country_code": "CN"},
            ["张伟"],
        )
        query_text = "\n".join(item["query"] for item in queries)
        paths = {item["path"] for item in queries}
        self.assertIn('"张伟"', query_text)
        self.assertIn("课题组/实验室成员", paths)
        self.assertIn("研究生/导师链路", paths)
        self.assertIn("答辩/学位公告", paths)

    def test_filters_conflated_openalex_name_alternatives(self):
        di_aliases = _trusted_openalex_name_variants(
            "Di Liu", "Di Liu", ["Liu Di", "Jiuyan Li"]
        )
        guo_aliases = _trusted_openalex_name_variants(
            "Guo-Ping Guo", "Guo‐Ping Guo",
            ["Guo, Guo-Ping", "Guang-Can Guo"],
        )
        self.assertIn("Liu Di", di_aliases)
        self.assertNotIn("Jiuyan Li", di_aliases)
        self.assertIn("Guo, Guo-Ping", guo_aliases)
        self.assertNotIn("Guang-Can Guo", guo_aliases)

    def test_marks_advisor_email_for_review_instead_of_target_contact(self):
        html = """
        <html><body>
          <h1>张伟 Wei Zhang</h1>
          <p>物理学院博士研究生</p>
          <p>导师：李明教授 <a href="mailto:liming@pku.edu.cn">邮箱</a></p>
        </body></html>
        """
        result = extract_official_contact_from_page(
            html,
            "https://physics.pku.edu.cn/students/zhangwei",
            "Wei Zhang",
            "pku.edu.cn",
            ["张伟"],
        )
        self.assertIsNone(result["email"])
        self.assertEqual(result["candidates"][0]["contact_kind"], "advisor")
        self.assertEqual(result["candidates"][0]["status"], "review")
        self.assertIn("导师", result["candidates"][0]["reason"])

    def test_extracts_graduation_and_china_education_with_evidence(self):
        html = """
        <html><body>
          <h1>Li Ming</h1>
          <p>PhD candidate in quantum optics. Expected graduation: 2027.</p>
          <p>Education: Bachelor, Tsinghua University, China.</p>
        </body></html>
        """
        result = analyze_author_background(
            html,
            "https://physics.example.edu/people/li-ming",
            "Li Ming",
            {"institution": "Example University", "country_code": "US"},
        )
        self.assertEqual(result["career_stage"], "doctoral-student")
        self.assertEqual(result["expected_graduation_year"], 2027)
        self.assertEqual(result["graduation_status"], "confirmed-upcoming")
        self.assertEqual(result["china_link_status"], "medium")
        self.assertEqual(result["nationality"], "")
        self.assertTrue(result["graduation_evidence"])
        self.assertTrue(result["china_link_evidence"])

    def test_nationality_requires_explicit_official_statement(self):
        ordinary = analyze_author_background(
            "<h1>Wei Zhang</h1><p>PhD student in quantum physics.</p>",
            "https://physics.example.edu/wei-zhang",
            "Wei Zhang",
        )
        explicit = analyze_author_background(
            "<h1>Wei Zhang</h1><p>PhD student. Nationality: Chinese.</p>",
            "https://physics.example.edu/wei-zhang",
            "Wei Zhang",
        )
        self.assertEqual(ordinary["nationality"], "")
        self.assertEqual(explicit["nationality"], "China")

    def test_discovers_named_students_from_official_roster(self):
        html = """
        <ul>
          <li><h3>Alice Chen</h3><p>PhD student</p>
              <a href="/people/alice-chen">Profile</a>
              <a href="mailto:alice.chen@example.edu">Email</a></li>
          <li><h3>Professor Bob Smith</h3><p>Professor</p></li>
        </ul>
        """
        candidates = extract_graduate_candidates_from_page(
            html, "https://physics.example.edu/team", "example.edu"
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["name"], "Alice Chen")
        self.assertEqual(candidates[0]["email"], "alice.chen@example.edu")
        self.assertEqual(
            candidates[0]["verification_status"], "official-roster-candidate"
        )

    def test_discovers_flat_sectioned_graduate_roster_without_profile_links(self):
        html = """
        <h2>STAFF</h2><p><b>Professor Guo</b></p>
        <h2>GRADUATE STUDENTS</h2>
        <p><b>Zhilong Jia</b><br><b>Hui Yang</b></p>
        <h3>Grade 2022</h3><p><b>Aoran Li</b></p>
        <h2>ALUMNI</h2><p><b>Former Student</b></p>
        """
        candidates = extract_graduate_candidates_from_page(
            html, "https://quantum.example.edu/members", "example.edu"
        )
        self.assertEqual(
            [item["name"] for item in candidates],
            ["Zhilong Jia", "Hui Yang", "Aoran Li"],
        )
        self.assertTrue(all(
            item["verification_status"] == "official-roster-name-only"
            for item in candidates
        ))

    def test_discovers_people_person_cards_used_by_lab_site(self):
        html = """
        <div class="people-person"><h3>Tengxiang Lin</h3>
          <p>PhD Student (co, 2025)</p><a href="/author/tengxiang-lin/">Profile</a></div>
        <div class="people-person"><h3>Ziao Tang</h3>
          <p>PhD Student (co, 2025)</p><a href="/author/ziao-tang/">Profile</a></div>
        """
        candidates = extract_graduate_candidates_from_page(
            html, "https://www.quair.group/people/", "quair.group"
        )
        self.assertEqual([item["name"] for item in candidates], ["Tengxiang Lin", "Ziao Tang"])

    def test_student_assessment_uses_role_before_citations(self):
        student = AuthorProfile(name="Alice", career_stage="doctoral-student", cited_by_count=5000)
        assess_student_status(student)
        self.assertIn(student.student_status, {"confirmed-student", "likely-student"})

        professor = AuthorProfile(name="Bob", career_stage="faculty", cited_by_count=0)
        assess_student_status(professor)
        self.assertEqual(professor.student_status, "confirmed-non-student")

        unknown = AuthorProfile(name="Carol", cited_by_count=0, works_count=2)
        assess_student_status(unknown)
        self.assertEqual(unknown.student_status, "unknown")

        metric_only_senior = AuthorProfile(
            name="Evan", cited_by_count=8000, works_count=300,
            academic_timeline={"earliest_publication_year": 1991},
        )
        assess_student_status(metric_only_senior)
        self.assertEqual(metric_only_senior.student_status, "unknown")

        roster_student = AuthorProfile(
            name="Dana", career_stage="doctoral-student",
            works_count=200, cited_by_count=10000,
            student_evidence=[{"type": "official-lab-roster", "source": "https://lab.example"}],
        )
        assess_student_status(roster_student)
        self.assertEqual(roster_student.student_status, "confirmed-student")
        self.assertGreaterEqual(roster_student.student_score, 85)

    def test_lab_members_are_exposed_as_individual_contact_rows(self):
        contacts = _apply_scope_lab_enrichment([AuthorProfile(
            name="Cunxi Yu", career_stage="faculty",
            lab_members=[{
                "name": "Zhan Song", "career_stage": "doctoral-student",
                "degree_type": "PhD", "email": "zhansong@example.edu",
                "email_source": "https://zhan.example/", "email_confidence": 75,
                "profile_url": "https://zhan.example/", "lab_name": "Cunxi Yu Group",
                "lab_url": "https://lab.example/", "directory_url": "https://lab.example/people",
                "lab_pi": "Cunxi Yu", "student_status": "confirmed-student",
                "student_score": 92,
            }],
        ).to_dict()])
        zhan = next(item for item in contacts if item["name"] == "Zhan Song")
        self.assertEqual(zhan["email"], "zhansong@example.edu")
        self.assertEqual(zhan["discovery_origin"], "lab-member")

    def test_lab_member_discoverer_is_not_assumed_to_be_pi(self):
        contacts = _apply_scope_lab_enrichment([AuthorProfile(
            name="Fangming Jing", orcid="https://orcid.org/0000-0000-0000-0001",
            lab_members=[{
                "name": "Ziyuan Chen", "career_stage": "graduate-student",
                "lab_name": "Quantum Lab", "lab_url": "https://lab.example/",
                "directory_url": "https://lab.example/people", "lab_pi": "",
            }],
        ).to_dict()])
        ziyuan = next(item for item in contacts if item["name"] == "Ziyuan Chen")
        self.assertEqual(ziyuan["lab_pi"], "")


class CoauthorResolutionTests(unittest.IsolatedAsyncioTestCase):
    class _Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self.payload

    class _Session:
        def __init__(self, *payloads):
            self.payloads = list(payloads)
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return CoauthorResolutionTests._Response(self.payloads.pop(0))

    async def test_openalex_timeline_uses_oldest_work_not_recent_sample_minimum(self):
        session = self._Session(
            {"results": [{
                "title": "Recent work", "publication_year": 2026,
                "authorships": [],
            }]},
            {"results": [{"title": "Oldest work", "publication_year": 1988}]},
        )
        signals = await fetch_openalex_academic_signals(
            "https://openalex.org/A123", session
        )
        self.assertEqual(signals["earliest_publication_year"], 1988)
        self.assertEqual(signals["latest_publication_year"], 2026)
        self.assertIn("sort=publication_date:asc", session.urls[1])

    async def test_openalex_prefers_close_field_aligned_identity(self):
        common = {
            "last_known_institutions": [{
                "id": "I1", "display_name": "Example University"
            }],
            "works_count": 100, "cited_by_count": 1000,
        }
        session = self._Session({"results": [
            {
                **common, "id": "https://openalex.org/A-WRONG",
                "display_name": "Zejun Liu",
                "topics": [{"display_name": "Hydrology and Watersheds"}],
            },
            {
                **common, "id": "https://openalex.org/A-RIGHT",
                "display_name": "Z.-C. Liu",
                "topics": [{"display_name": "Quantum many-body physics"}],
            },
        ]})
        profiles = await search_openalex_authors(
            "Zejun Liu", session, {
                "institutions": ["Example University"],
                "institution_scope": "author", "keywords": ["quantum"],
            }
        )
        self.assertEqual(profiles[0].openalex_id, "https://openalex.org/A-RIGHT")

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_faculty_homepage_is_not_overwritten_by_students_on_research_page(self, mocked):
        mocked.side_effect = [
            (
                "<main><h1>Y. Jun Xu</h1><p>Professor of Hydrology</p>"
                "<a href='/xu/research.php'>Research</a></main>",
                "https://faculty.example/xu/",
            ),
            (
                "<header><p>Y. Jun Xu</p></header><main><h1>Research</h1>"
                "<p>Jun chaired committees for 18 PhD students.</p></main>",
                "https://faculty.example/xu/research.php",
            ),
        ]
        result = await search_verified_personal_websites(
            "Y. Jun Xu", [], ["https://faculty.example/xu/"], object()
        )
        self.assertEqual(result["background"]["career_stage"], "faculty")

    @patch("crawler.resolve_lab_member_identity", new_callable=AsyncMock)
    async def test_lab_member_existing_public_contact_completes_without_guessing(self, resolved):
        resolved.return_value = {
            "identity_status": "unresolved",
            "identity_reason": "No unique OpenAlex identity",
        }
        profile = await crawl_lab_member_contact({
            "name": "Alice Chen", "email": "alice@example.edu",
            "email_source": "https://lab.example/alice",
            "email_confidence": 95, "directory_url": "https://lab.example/people",
            "lab_name": "Example Lab", "parent_name": "PI Name",
        }, object())
        self.assertEqual(profile.contact_search_status, "contact-found")
        self.assertEqual(profile.email, "alice@example.edu")
        self.assertEqual(profile.identity_status, "unresolved")

    @patch("crawler.crawl_multiple_authors", new_callable=AsyncMock)
    @patch("crawler.resolve_lab_member_identity", new_callable=AsyncMock)
    async def test_verified_lab_member_reuses_full_contact_crawl(self, resolved, crawled):
        resolved.return_value = {
            "identity_status": "verified-shared-work",
            "identity_reason": "shared work",
            "name": "Alice Chen",
            "openalex_id": "https://openalex.org/A123",
            "affiliations": ["Example University"],
        }
        crawled.return_value = [AuthorProfile(
            name="Alice Chen", openalex_id="https://openalex.org/A123",
            email="alice@example.edu", email_confidence=90,
            match_status="verified",
        )]
        profile = await crawl_lab_member_contact({
            "name": "Alice Chen", "directory_url": "https://lab.example/people",
            "lab_name": "Example Lab", "parent_name": "PI Name",
            "parent_openalex_id": "https://openalex.org/A100",
        }, object())
        self.assertEqual(profile.identity_status, "verified-shared-work")
        self.assertEqual(profile.contact_search_status, "contact-found")
        self.assertEqual(profile.discovery_origin, "lab-member")

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_personal_faculty_site_detects_pi_and_external_student_pages(self, mocked):
        mocked.side_effect = [
            ("<h1>Cunxi Yu</h1><p>Assistant Professor</p><a href='/people.html'>People</a>",
             "https://faculty.example/"),
            ("<h2>Principal Investigator</h2><p>Dr. Cunxi Yu</p>"
             "<h2>PhD Students</h2><p><a href='https://alice.example/'><b>Alice Chen</b></a></p>",
             "https://faculty.example/people.html"),
            ("<h1>Alice Chen</h1><a href='mailto:alice@alice.example'>Email</a>",
             "https://alice.example/"),
        ]
        result = await search_verified_personal_websites(
            "Cunxi Yu", [], ["https://faculty.example/"], object()
        )
        self.assertEqual(result["lab_members"][0]["profile_url"], "https://alice.example/")
        self.assertEqual(result["lab_members"][0]["email"], "alice@alice.example")
        self.assertEqual(result["lab_members"][0]["lab_pi"], "Cunxi Yu")
        self.assertEqual(result["lab_members"][0]["lab_name"], "Cunxi Yu Research Group")

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_old_lab_site_infers_orcid_owner_from_first_staff_entry(self, mocked):
        mocked.side_effect = [
            ("<h1>Silicon Quantum Computing Lab</h1><a href='/members'>Members</a>",
             "https://lab.example/"),
            ("<h2>STAFF</h2><p><b>Guoping Guo</b><b>Other Staff</b></p>"
             "<h2>GRADUATE STUDENTS</h2><p><b>Alice Chen</b></p>",
             "https://lab.example/members"),
        ]
        result = await search_verified_personal_websites(
            "Guoping Guo", [], ["https://lab.example/"], object()
        )
        self.assertEqual(result["lab_members"][0]["lab_pi"], "Guoping Guo")

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_roster_link_bonus_is_applied_before_contact_threshold(self, mocked):
        mocked.side_effect = [
            ("<h1>Lab</h1><a href='/people'>People</a>", "https://lab.example/"),
            ("<h2>PhD Students</h2><p><a href='https://zhan.example/'><b>Zhan Song</b></a></p>",
             "https://lab.example/people"),
            ("<h1>Zhan Song</h1><a href='mailto:zhansong@example.edu'>Email</a>",
             "https://zhan.example/"),
        ]
        result = await search_verified_personal_websites(
            "Lab Owner", [], ["https://lab.example/"], object()
        )
        self.assertEqual(result["lab_members"][0]["email"], "zhansong@example.edu")
        self.assertGreaterEqual(result["lab_members"][0]["email_confidence"], 65)

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_lab_directory_enriches_each_member_from_own_profile(self, mocked):
        mocked.side_effect = [
            ("<h1>Lab</h1><a href='/people/'>People</a>", "https://lab.example/"),
            ("<h1>People</h1><div class='people-person'><h3>Tengxiang Lin</h3>"
             "<p>PhD Student</p><a href='/author/tengxiang/'>Profile</a></div>"
             "<div class='people-person'><h3>Ziao Tang</h3><p>PhD Student</p>"
             "<a href='/author/ziao/'>Profile</a></div>", "https://lab.example/people/"),
            ("<h1>Tengxiang Lin</h1><a href='mailto:tengxiang@lab.example'>Email</a>",
             "https://lab.example/author/tengxiang/"),
            ("<h1>Ziao Tang</h1><a href='mailto:ziao@lab.example'>Email</a>",
             "https://lab.example/author/ziao/"),
        ]
        result = await search_verified_personal_websites(
            "Lab Owner", [], ["https://lab.example/"], object()
        )
        contacts = {item["name"]: item.get("email") for item in result["lab_members"]}
        self.assertEqual(contacts["Tengxiang Lin"], "tengxiang@lab.example")
        self.assertEqual(contacts["Ziao Tang"], "ziao@lab.example")
        self.assertIsNone(result["email"])
    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_orcid_personal_site_follows_group_members_roster(self, mocked):
        mocked.side_effect = [
            (
                "<h1>Guo-Ping Guo Quantum Lab</h1>"
                "<a href='/members'>Group Members</a>",
                "https://quantum.example.edu/",
            ),
            (
                "<h2>GRADUATE STUDENTS</h2>"
                "<p><b>Zhilong Jia</b><br><b>Hui Yang</b></p>",
                "https://quantum.example.edu/members",
            ),
        ]
        result = await search_verified_personal_websites(
            "Guo-Ping Guo", [], ["https://quantum.example.edu/"], object()
        )
        self.assertEqual(
            [item["name"] for item in result["graduate_candidates"]],
            ["Zhilong Jia", "Hui Yang"],
        )
        self.assertEqual(mocked.await_count, 2)

    @patch("crawler._safe_get_with_url", new_callable=AsyncMock)
    async def test_accepts_contact_from_orcid_linked_named_personal_site(self, mocked):
        mocked.return_value = (
            "<h1>张伟 Wei Zhang</h1><p>Quantum physics</p>"
            "<a href='mailto:wei.zhang@gmail.com'>Email</a>",
            "https://weizhang.example/about",
        )
        result = await search_verified_personal_websites(
            "Wei Zhang", ["张伟"], ["https://weizhang.example"], object()
        )
        self.assertEqual(result["email"], "wei.zhang@gmail.com")
        self.assertGreaterEqual(result["email_confidence"], 75)
        self.assertEqual(result["candidates"][0]["contact_kind"], "personal")
        self.assertIn("ORCID", result["candidates"][0]["reason"])

    @patch("crawler.fetch_openalex_academic_signals", new_callable=AsyncMock)
    async def test_resolves_only_identity_present_in_parent_shared_works(self, mocked):
        mocked.return_value = {
            "coauthor_details": [{
                "name": "Alice Chen",
                "openalex_id": "https://openalex.org/A200",
                "shared_works_count": 3,
                "latest_shared_year": 2026,
            }]
        }
        resolved = await resolve_coauthor_identity(
            "https://openalex.org/A100", "Alice Chen", object()
        )
        rejected = await resolve_coauthor_identity(
            "https://openalex.org/A100", "Different Person", object()
        )
        self.assertEqual(resolved["openalex_id"], "https://openalex.org/A200")
        self.assertIsNone(rejected)

    @patch("crawler.fetch_openalex_academic_signals", new_callable=AsyncMock)
    async def test_rejects_ambiguous_same_name_ids(self, mocked):
        mocked.return_value = {
            "coauthor_details": [
                {"name": "Alex Kim", "openalex_id": "https://openalex.org/A1"},
                {"name": "Alex Kim", "openalex_id": "https://openalex.org/A2"},
            ]
        }
        resolved = await resolve_coauthor_identity(
            "https://openalex.org/A100", "Alex Kim", object()
        )
        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
