"""Graph builder: construct author relationship networks using NetworkX."""

import networkx as nx
from collections import defaultdict
import math
import re
import unicodedata


_FIELD_TERMS = (
    "quantum", "qubit", "entanglement", "photon", "optic", "atomic",
    "condensed matter", "topological", "superconduct", "semiconductor",
    "spin", "many-body", "many body", "black hole", "cosmology",
    "gravitation", "gravity", "field theory", "particle physics",
    "量子", "光子", "光学", "凝聚态", "拓扑", "超导", "半导体", "引力",
)

# Pairwise relationships are useful for small teams, but a large consortium
# turns a shared paper, institution, or broad topic into an unreadable clique.
LARGE_PAPER_AUTHOR_LIMIT = 12
LARGE_GROUP_PAIRWISE_LIMIT = 12
LARGE_GRAPH_MIN_SHARED_WORKS = 2
LARGE_GRAPH_TOP_COAUTHORS = 5


def _field_relevant(detail: dict) -> bool:
    explicit = detail.get("field_relevant")
    if explicit is not None:
        return bool(explicit)
    text = " ".join(
        list(detail.get("shared_topics") or [])
        + list(detail.get("sample_titles") or [])
    ).lower()
    return any(term in text for term in _FIELD_TERMS)


def _person_name_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return "".join(re.findall(r"[a-z0-9]+", value))


def _openalex_key(value: str) -> str:
    return (value or "").rstrip("/").split("/")[-1].upper()


def _dedupe_profiles(author_profiles: list) -> list:
    """Prefer paper-author profiles when the same OpenAlex person recurs."""
    selected = {}
    order = []
    for profile in author_profiles:
        identity = _openalex_key(getattr(profile, "openalex_id", ""))
        key = f"id:{identity}" if identity else f"name:{_person_name_key(profile.name)}"
        preference = (
            getattr(profile, "discovery_origin", "paper-author") == "paper-author",
            float(getattr(profile, "email_confidence", 0) or 0),
            float(getattr(profile, "phone_confidence", 0) or 0),
            float(getattr(profile, "match_score", 0) or 0),
        )
        if key not in selected:
            order.append(key)
            selected[key] = (preference, profile)
        elif preference > selected[key][0]:
            selected[key] = (preference, profile)
    return [selected[key][1] for key in order]


def _add_relation(G: nx.MultiGraph, source: str, target: str, relation: str,
                  weight: float, evidence: str) -> None:
    """Add or strengthen one relation type without hiding other relations."""
    if source == target:
        return
    if G.has_edge(source, target, key=relation):
        data = G[source][target][relation]
        data["weight"] = data.get("weight", 0) + weight
        evidence_items = data.setdefault("evidence_items", [])
        if evidence and evidence not in evidence_items:
            evidence_items.append(evidence)
        return
    G.add_edge(
        source,
        target,
        key=relation,
        weight=weight,
        relation=relation,
        evidence_items=[evidence] if evidence else [],
    )


def build_coauthorship_graph(
    author_profiles: list, paper_scope: str = "single"
) -> dict:
    """Build a co-authorship network graph from crawled author profiles.

    Returns a dict with nodes, edges, and graph metadata for frontend rendering.
    """
    author_profiles = _dedupe_profiles(author_profiles)
    paper_author_profiles = [
        profile for profile in author_profiles
        if getattr(profile, "discovery_origin", "paper-author") == "paper-author"
    ]
    large_graph_mode = len(paper_author_profiles) > LARGE_PAPER_AUTHOR_LIMIT
    large_paper_mode = paper_scope == "single" and large_graph_mode

    # A pair of people can be connected by collaboration, affiliation, and
    # topic at the same time, so each relation type needs its own edge.
    G = nx.MultiGraph()

    # Build author name-to-profile lookup
    name_to_profile = {}
    id_to_profile = {}
    for p in author_profiles:
        key = _person_name_key(p.name)
        if key:
            name_to_profile[key] = p
        openalex_key = _openalex_key(getattr(p, "openalex_id", ""))
        if openalex_key:
            id_to_profile[openalex_key] = p

    # Add nodes
    for profile in author_profiles:
        node_id = profile.name
        G.add_node(
            node_id,
            name=profile.name,
            name_aliases=getattr(profile, "name_aliases", []),
            openalex_id=profile.openalex_id or "",
            orcid=profile.orcid or "",
            google_scholar_url=profile.google_scholar_url or "",
            researchgate_url=profile.researchgate_url or "",
            twitter_url=profile.twitter_url or "",
            linkedin_url=profile.linkedin_url or "",
            affiliations=profile.affiliations,
            topics=profile.topics,
            cited_by_count=profile.cited_by_count,
            works_count=profile.works_count,
            coauthor_details=getattr(profile, "coauthor_details", []),
            email=profile.email or "",
            email_source=profile.email_source or "",
            email_confidence=getattr(profile, "email_confidence", 0),
            phone=getattr(profile, "phone", None) or "",
            phone_source=getattr(profile, "phone_source", None) or "",
            phone_confidence=getattr(profile, "phone_confidence", 0),
            match_score=profile.match_score,
            match_status=getattr(profile, "match_status", "unmatched"),
            match_breakdown=getattr(profile, "match_breakdown", {}),
            candidates=getattr(profile, "candidates", []),
            website_url=profile.website_url or "",
            institution_sites=getattr(profile, "institution_sites", []),
            contact_candidates=getattr(profile, "contact_candidates", []),
            search_trail=getattr(profile, "search_trail", []),
            career_stage=getattr(profile, "career_stage", "unknown"),
            degree_type=getattr(profile, "degree_type", ""),
            graduation_score=getattr(profile, "graduation_score", 0),
            graduation_status=getattr(profile, "graduation_status", "insufficient"),
            expected_graduation_year=getattr(profile, "expected_graduation_year", None),
            graduation_evidence=getattr(profile, "graduation_evidence", []),
            china_link_score=getattr(profile, "china_link_score", 0),
            china_link_status=getattr(profile, "china_link_status", "none"),
            china_link_evidence=getattr(profile, "china_link_evidence", []),
            nationality=getattr(profile, "nationality", ""),
            nationality_evidence=getattr(profile, "nationality_evidence", []),
            academic_timeline=getattr(profile, "academic_timeline", {}),
            graduate_candidates=getattr(profile, "graduate_candidates", []),
            lab_members=getattr(profile, "lab_members", []),
            lab_name=getattr(profile, "lab_name", ""),
            lab_url=getattr(profile, "lab_url", ""),
            directory_url=getattr(profile, "directory_url", ""),
            lab_pi=getattr(profile, "lab_pi", ""),
            identity_status=getattr(profile, "identity_status", "unresolved"),
            contact_search_status=getattr(profile, "contact_search_status", "not-started"),
            search_failure_reason=getattr(profile, "search_failure_reason", ""),
            pages_checked=getattr(profile, "pages_checked", 0),
            student_score=getattr(profile, "student_score", 0),
            student_status=getattr(profile, "student_status", "unknown"),
            student_evidence=getattr(profile, "student_evidence", []),
            discovery_origin=getattr(profile, "discovery_origin", "paper-author"),
            sources=profile.sources,
            group=(
                2 if getattr(profile, "discovery_origin", "paper-author") == "lab-member"
                else 0
            ),
        )

    # For a large consortium, model the fact that everyone signed the same
    # paper once via a paper hub.  A pairwise author clique would imply a
    # direct working relationship between every possible pair and grows as
    # O(n^2) (299 authors would already create 44,551 links).
    if large_paper_mode:
        paper_node_id = "Paper: current-project"
        G.add_node(
            paper_node_id,
            name=f"本论文（{len(paper_author_profiles)} 位作者）",
            affiliations=[], topics=[], sources=[],
            discovery_origin="paper-entity", group=5,
        )
        for profile in paper_author_profiles:
            _add_relation(
                G, profile.name, paper_node_id, "paper-author", 0.8,
                "该作者署名当前论文",
            )

    # A batch/contact summary contains authors from several independent
    # papers. Preserve that provenance with one hub per actual source paper;
    # never collapse the whole batch into a fictitious single paper.
    if paper_scope == "aggregate":
        paper_nodes = set()
        for profile in paper_author_profiles:
            paper_records = list(getattr(profile, "papers", []) or [])
            if not paper_records:
                project_ids = list(getattr(profile, "source_project_ids", []) or [])
                titles = list(getattr(profile, "paper_titles", []) or [])
                dois = list(getattr(profile, "paper_dois", []) or [])
                files = list(getattr(profile, "source_files", []) or [])
                paper_records = [
                    {
                        "project_id": project_id,
                        "title": titles[index] if index < len(titles) else "",
                        "doi": dois[index] if index < len(dois) else "",
                        "filename": files[index] if index < len(files) else "",
                    }
                    for index, project_id in enumerate(project_ids)
                ]
            for record in paper_records:
                project_id = (record.get("project_id") or "").strip()
                title = (record.get("title") or "").strip()
                doi = (record.get("doi") or "").strip()
                filename = (record.get("filename") or "").strip()
                identity = project_id or doi or filename or title
                if not identity:
                    continue
                paper_node_id = f"Paper source: {identity}"
                if paper_node_id not in paper_nodes:
                    paper_nodes.add(paper_node_id)
                    G.add_node(
                        paper_node_id,
                        name=title or filename or doi or "来源论文",
                        paper_title=title,
                        paper_doi=doi,
                        source_file=filename,
                        source_project_id=project_id,
                        affiliations=[], topics=[],
                        sources=list(filter(None, [doi, filename])),
                        discovery_origin="paper-entity", group=5,
                    )
                _add_relation(
                    G, profile.name, paper_node_id, "paper-author", 0.8,
                    f"作者来源论文：{title or filename or doi}",
                )

    # A laboratory is an entity, not the person whose ORCID happened to reveal
    # its website. Use a hub node to avoid both a misleading discoverer-centred
    # star and an unreadable all-to-all member clique.
    lab_node_by_key = {}
    lab_person_node_by_key = {}
    lab_person_node_by_name = {}
    for profile in author_profiles:
        members = getattr(profile, "lab_members", []) or []
        if not members:
            continue
        first_member = members[0]
        lab_name = first_member.get("lab_name") or "已核验实验室"
        lab_url = first_member.get("lab_url") or ""
        directory_url = first_member.get("directory_url") or first_member.get("source") or ""
        lab_domain = re.sub(r"^www\.", "", re.sub(r"^https?://", "", lab_url).split("/")[0])
        lab_key = (lab_domain, _person_name_key(lab_name))
        lab_id = lab_node_by_key.get(lab_key)
        if not lab_id:
            lab_id = f"Laboratory: {lab_domain or _person_name_key(lab_name)}"
            lab_node_by_key[lab_key] = lab_id
            G.add_node(
                lab_id, name=lab_name, website_url=lab_url,
                directory_url=directory_url, lab_name=lab_name,
                lab_url=lab_url, lab_pi=first_member.get("lab_pi") or "",
                affiliations=[], topics=[], sources=[directory_url],
                discovery_origin="lab-entity", group=3,
            )
        pi_name = first_member.get("lab_pi") or G.nodes[lab_id].get("lab_pi") or ""
        if pi_name and not G.nodes[lab_id].get("lab_pi"):
            G.nodes[lab_id]["lab_pi"] = pi_name
        pi_key = _person_name_key(pi_name)
        if _person_name_key(profile.name) != pi_key:
            _add_relation(
                G, profile.name, lab_id, "lab-member", 0.8,
                f"实验室官网名单：{directory_url or lab_url}",
            )
        if pi_key:
            pi_profile = name_to_profile.get(pi_key)
            if pi_profile:
                pi_id = pi_profile.name
                pi_node = G.nodes[pi_id]
                pi_node["career_stage"] = "faculty"
                pi_node["student_status"] = "confirmed-non-student"
                pi_node["student_score"] = 0
                pi_node["lab_name"] = lab_name
                pi_node["lab_url"] = lab_url
                pi_node["directory_url"] = directory_url
            else:
                pi_id = lab_person_node_by_key.get(pi_key)
                if not pi_id:
                    pi_id = f"Lab PI: {pi_key}"
                    lab_person_node_by_key[pi_key] = pi_id
                    G.add_node(
                        pi_id, name=pi_name, career_stage="faculty",
                        student_status="confirmed-non-student", student_score=0,
                        lab_name=lab_name, lab_url=lab_url,
                        directory_url=directory_url, website_url=lab_url,
                        affiliations=[], topics=[], sources=[directory_url],
                        discovery_origin="lab-pi", group=4,
                    )
            _add_relation(
                G, pi_id, lab_id, "lab-pi", 1.2,
                f"实验室官网明确的负责人：{directory_url or lab_url}",
            )

        for member in members:
            member_key = _person_name_key(member.get("name") or "")
            if not member_key:
                continue
            known_profile = name_to_profile.get(member_key)
            effective_pi = member.get("lab_pi") or pi_name
            anchor_profile = name_to_profile.get(_person_name_key(effective_pi)) or profile
            if known_profile:
                target_id = known_profile.name
                target = G.nodes[target_id]
                if member.get("email") and not target.get("email"):
                    target["email"] = member["email"]
                    target["email_source"] = member.get("email_source") or member.get("profile_url") or ""
                    target["email_confidence"] = member.get("email_confidence") or 90
                if member.get("phone") and not target.get("phone"):
                    target["phone"] = member["phone"]
                    target["phone_source"] = member.get("phone_source") or member.get("profile_url") or ""
                    target["phone_confidence"] = member.get("phone_confidence") or 90
                target["website_url"] = member.get("profile_url") or target.get("website_url", "")
                target["career_stage"] = member.get("career_stage") or target.get("career_stage", "unknown")
                target["student_score"] = max(
                    float(target.get("student_score") or 0),
                    float(member.get("student_score") or 82),
                )
                target["student_status"] = member.get("student_status") or "confirmed-student"
                target["student_evidence"] = member.get("student_evidence") or target.get("student_evidence", [])
                target["lab_name"] = member.get("lab_name") or ""
                target["lab_url"] = member.get("lab_url") or ""
                target["directory_url"] = member.get("directory_url") or member.get("source") or ""
                target["lab_pi"] = effective_pi
                if anchor_profile.name not in target.setdefault("parent_names", []):
                    target["parent_names"].append(anchor_profile.name)
                if (
                    anchor_profile.openalex_id
                    and anchor_profile.openalex_id not in target.setdefault("parent_openalex_ids", [])
                ):
                    target["parent_openalex_ids"].append(anchor_profile.openalex_id)
            else:
                lab_identity = member.get("profile_url") or member.get("email") or member_key
                target_id = lab_person_node_by_key.get(lab_identity)
                if not target_id:
                    target_id = f"Lab: {member_key}:{len(lab_person_node_by_key)}"
                    lab_person_node_by_key[lab_identity] = target_id
                    G.add_node(
                        target_id,
                        name=member.get("name") or "",
                        email=member.get("email") or "",
                        email_source=member.get("email_source") or member.get("profile_url") or "",
                        email_confidence=member.get("email_confidence") or 0,
                        phone=member.get("phone") or "",
                        phone_source=member.get("phone_source") or member.get("profile_url") or "",
                        phone_confidence=member.get("phone_confidence") or 0,
                        website_url=member.get("profile_url") or "",
                        career_stage=member.get("career_stage") or "graduate-student",
                        degree_type=member.get("degree_type") or "",
                        graduation_score=member.get("graduation_score") or 0,
                        graduation_status=member.get("graduation_status") or "student",
                        student_score=member.get("student_score") or 82,
                        student_status=member.get("student_status") or "confirmed-student",
                        student_evidence=member.get("student_evidence") or [],
                        lab_name=member.get("lab_name") or "",
                        lab_url=member.get("lab_url") or "",
                        directory_url=member.get("directory_url") or member.get("source") or "",
                        lab_pi=effective_pi,
                        parent_names=[anchor_profile.name],
                        parent_openalex_ids=[anchor_profile.openalex_id or ""],
                        affiliations=[member.get("institution")] if member.get("institution") else [],
                        topics=[], sources=[member.get("source") or ""],
                        discovery_origin="lab-roster", group=2,
                    )
            lab_person_node_by_name[member_key] = target_id
            _add_relation(
                G, target_id, lab_id, "lab-member", 0.8,
                "实验室官网成员名单：" + (
                    member.get("directory_url") or member.get("source") or "已核验名单"
                ),
            )

    # Build edges from co-authorship data
    for profile in author_profiles:
        src_name = profile.name
        detail_map = {
            _person_name_key(item.get("name") or ""): item
            for item in getattr(profile, "coauthor_details", [])
        }
        coauthor_names = list(dict.fromkeys(profile.coauthors))
        if large_graph_mode:
            # The paper hub already represents one-off consortium membership.
            # Keep only repeated historical collaborations and cap each
            # person's visible neighbourhood so the browser remains usable.
            coauthor_names = [
                name for name in coauthor_names
                if int((detail_map.get(_person_name_key(name)) or {}).get(
                    "shared_works_count", 0
                ) or 0) >= LARGE_GRAPH_MIN_SHARED_WORKS
            ]
            coauthor_names.sort(
                key=lambda name: (
                    int((detail_map.get(_person_name_key(name)) or {}).get(
                        "shared_works_count", 0
                    ) or 0),
                    int((detail_map.get(_person_name_key(name)) or {}).get(
                        "recent_shared_works_count", 0
                    ) or 0),
                    int((detail_map.get(_person_name_key(name)) or {}).get(
                        "latest_shared_year", 0
                    ) or 0),
                ),
                reverse=True,
            )
            coauthor_names = coauthor_names[:LARGE_GRAPH_TOP_COAUTHORS]
        for ca_name in coauthor_names:
            ca_key = _person_name_key(ca_name)
            coauthor_detail = detail_map.get(ca_key) or {}
            coauthor_openalex_key = _openalex_key(
                coauthor_detail.get("openalex_id", "")
            )
            known_profile = (
                id_to_profile.get(coauthor_openalex_key)
                if coauthor_openalex_key else None
            ) or name_to_profile.get(ca_key)
            # Check if co-author is also in our author list
            if known_profile:
                _add_relation(
                    G,
                    src_name,
                    known_profile.name,
                    "co-author",
                    1,
                    "OpenAlex 共同署名记录",
                )
            else:
                lab_target_id = lab_person_node_by_name.get(ca_key)
                if lab_target_id:
                    _add_relation(
                        G, src_name, lab_target_id, "co-author", 1,
                        f"OpenAlex 共同署名记录；该人同时由实验室官网确认",
                    )
                    continue
                # Add co-author as an "external" node
                ext_node_id = (
                    f"External: {coauthor_openalex_key}"
                    if coauthor_openalex_key else f"External: {ca_key or ca_name}"
                )
                if not G.has_node(ext_node_id):
                    G.add_node(
                        ext_node_id,
                        name=ca_name,
                        openalex_id=coauthor_detail.get("openalex_id", ""),
                        orcid="",
                        google_scholar_url="",
                        researchgate_url="",
                        twitter_url="",
                        linkedin_url="",
                        affiliations=[],
                        topics=[],
                        cited_by_count=0,
                        works_count=0,
                        career_stage="unknown",
                        graduation_score=0,
                        graduation_status="insufficient",
                        china_link_score=0,
                        china_link_status="none",
                        parent_names=[profile.name],
                        parent_openalex_ids=[profile.openalex_id or ""],
                        shared_works_count=coauthor_detail.get("shared_works_count", 1),
                        recent_shared_works_count=coauthor_detail.get("recent_shared_works_count", 0),
                        latest_shared_year=coauthor_detail.get("latest_shared_year", 0),
                        shared_topics=coauthor_detail.get("shared_topics", []),
                        sample_titles=coauthor_detail.get("sample_titles", []),
                        expansion_value_score=coauthor_detail.get("value_score", 15),
                        field_relevant=_field_relevant(coauthor_detail),
                        sources=[],
                        group=1,
                    )
                else:
                    external = G.nodes[ext_node_id]
                    if profile.name not in external.setdefault("parent_names", []):
                        external["parent_names"].append(profile.name)
                    if (
                        profile.openalex_id
                        and profile.openalex_id not in external.setdefault("parent_openalex_ids", [])
                    ):
                        external["parent_openalex_ids"].append(profile.openalex_id)
                    if coauthor_detail.get("openalex_id") and not external.get("openalex_id"):
                        external["openalex_id"] = coauthor_detail["openalex_id"]
                    external["shared_works_count"] = max(
                        external.get("shared_works_count", 1),
                        coauthor_detail.get("shared_works_count", 1),
                    )
                    external["recent_shared_works_count"] = max(
                        external.get("recent_shared_works_count", 0),
                        coauthor_detail.get("recent_shared_works_count", 0),
                    )
                    external["latest_shared_year"] = max(
                        external.get("latest_shared_year", 0),
                        coauthor_detail.get("latest_shared_year", 0),
                    )
                    external["expansion_value_score"] = min(
                        100,
                        max(external.get("expansion_value_score", 15),
                            coauthor_detail.get("value_score", 15)) + 10,
                    )
                    external["field_relevant"] = bool(
                        external.get("field_relevant")
                        or _field_relevant(coauthor_detail)
                    )
                _add_relation(
                    G,
                    src_name,
                    ext_node_id,
                    "co-author",
                    1,
                    f"OpenAlex 共同署名记录；样本合作 {coauthor_detail.get('shared_works_count', 1)} 篇",
                )

    # Build affiliation-based edges
    affiliation_map = defaultdict(list)
    for profile in author_profiles:
        for aff in profile.affiliations:
            affiliation_map[aff].append(profile.name)

    for aff, members in affiliation_map.items():
        members = list(dict.fromkeys(members))
        if len(members) > LARGE_GROUP_PAIRWISE_LIMIT:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if G.has_node(members[i]) and G.has_node(members[j]):
                    _add_relation(
                        G,
                        members[i],
                        members[j],
                        "same-affiliation",
                        0.5,
                        f"共同机构：{aff}",
                    )

    # Build topic-based edges
    topic_map = defaultdict(list)
    for profile in author_profiles:
        for topic in profile.topics:
            topic_map[topic].append(profile.name)

    for topic, members in topic_map.items():
        members = list(dict.fromkeys(members))
        if len(members) > LARGE_GROUP_PAIRWISE_LIMIT:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if G.has_node(members[i]) and G.has_node(members[j]):
                    _add_relation(
                        G,
                        members[i],
                        members[j],
                        "shared-topic",
                        0.3,
                        f"共同研究领域：{topic}",
                    )

    return graph_to_json(G)


def graph_to_json(G: nx.Graph) -> dict:
    """Convert NetworkX graph to JSON-serializable format for D3.js."""
    # Layout and centrality use unique person pairs; parallel relation edges
    # remain available for rendering but do not inflate graph statistics.
    pair_graph = nx.Graph()
    pair_graph.add_nodes_from(G.nodes(data=True))
    for src, dst, data in G.edges(data=True):
        if pair_graph.has_edge(src, dst):
            pair_graph[src][dst]["weight"] += data.get("weight", 1)
        else:
            pair_graph.add_edge(src, dst, weight=data.get("weight", 1))
    if pair_graph.number_of_nodes() >= 400:
        # NetworkX switches large spring layouts to SciPy. The browser runs
        # its own D3 simulation anyway, so deterministic circular seed
        # coordinates keep large batch graphs dependency-free.
        node_ids = list(pair_graph.nodes())
        total = max(1, len(node_ids))
        pos = {
            node_id: (
                math.cos(2 * math.pi * index / total),
                math.sin(2 * math.pi * index / total),
            )
            for index, node_id in enumerate(node_ids)
        }
    else:
        pos = nx.spring_layout(pair_graph, k=2, iterations=50, seed=42)

    nodes = []
    for node_id, data in G.nodes(data=True):
        x, y = pos.get(node_id, (0, 0))
        node = {
            "id": node_id,
            "name": data.get("name", node_id),
            "name_aliases": data.get("name_aliases", []),
            "group": data.get("group", 0),
            "openalex_id": data.get("openalex_id", ""),
            "parent_names": data.get("parent_names", []),
            "parent_openalex_ids": data.get("parent_openalex_ids", []),
            "shared_works_count": data.get("shared_works_count", 0),
            "recent_shared_works_count": data.get("recent_shared_works_count", 0),
            "latest_shared_year": data.get("latest_shared_year", 0),
            "shared_topics": data.get("shared_topics", []),
            "sample_titles": data.get("sample_titles", []),
            "expansion_value_score": data.get("expansion_value_score", 0),
            "field_relevant": data.get("field_relevant"),
            "x": float(x * 400),
            "y": float(y * 400),
            "cited_by_count": data.get("cited_by_count", 0),
            "works_count": data.get("works_count", 0),
            "email": data.get("email", ""),
            "email_source": data.get("email_source", ""),
            "email_confidence": data.get("email_confidence", 0),
            "phone": data.get("phone", ""),
            "phone_source": data.get("phone_source", ""),
            "phone_confidence": data.get("phone_confidence", 0),
            "match_score": data.get("match_score", 0),
            "match_status": data.get("match_status", "unmatched"),
            "match_breakdown": data.get("match_breakdown", {}),
            "candidates": data.get("candidates", []),
            "website_url": data.get("website_url", ""),
            "institution_sites": data.get("institution_sites", []),
            "contact_candidates": data.get("contact_candidates", []),
            "search_trail": data.get("search_trail", []),
            "career_stage": data.get("career_stage", "unknown"),
            "degree_type": data.get("degree_type", ""),
            "graduation_score": data.get("graduation_score", 0),
            "graduation_status": data.get("graduation_status", "insufficient"),
            "expected_graduation_year": data.get("expected_graduation_year"),
            "graduation_evidence": data.get("graduation_evidence", []),
            "china_link_score": data.get("china_link_score", 0),
            "china_link_status": data.get("china_link_status", "none"),
            "china_link_evidence": data.get("china_link_evidence", []),
            "nationality": data.get("nationality", ""),
            "nationality_evidence": data.get("nationality_evidence", []),
            "academic_timeline": data.get("academic_timeline", {}),
            "graduate_candidates": data.get("graduate_candidates", []),
            "lab_members": data.get("lab_members", []),
            "student_score": data.get("student_score", 0),
            "student_status": data.get("student_status", "unknown"),
            "student_evidence": data.get("student_evidence", []),
            "lab_name": data.get("lab_name", ""),
            "lab_url": data.get("lab_url", ""),
            "directory_url": data.get("directory_url", ""),
            "lab_pi": data.get("lab_pi", ""),
            "identity_status": data.get("identity_status", "unresolved"),
            "contact_search_status": data.get("contact_search_status", "not-started"),
            "search_failure_reason": data.get("search_failure_reason", ""),
            "pages_checked": data.get("pages_checked", 0),
            "discovery_origin": data.get("discovery_origin", "paper-author"),
            "affiliations": data.get("affiliations", []),
            "topics": data.get("topics", []),
            "google_scholar_url": data.get("google_scholar_url", ""),
            "researchgate_url": data.get("researchgate_url", ""),
            "twitter_url": data.get("twitter_url", ""),
            "linkedin_url": data.get("linkedin_url", ""),
            "orcid": data.get("orcid", ""),
            "sources": data.get("sources", []),
        }
        nodes.append(node)

    edges = []
    edge_iter = (
        G.edges(keys=True, data=True)
        if G.is_multigraph()
        else ((src, dst, data.get("relation", "unknown"), data)
              for src, dst, data in G.edges(data=True))
    )
    for src, dst, relation_key, data in edge_iter:
        evidence_items = data.get("evidence_items", [])
        edge = {
            "source": src,
            "target": dst,
            "weight": data.get("weight", 1),
            "relation": data.get("relation", relation_key),
            "evidence": data.get("evidence", "") or "；".join(evidence_items),
            "evidence_items": evidence_items,
        }
        edges.append(edge)

    # Compute network statistics
    stats = {}
    if G.number_of_nodes() > 0:
        stats["node_count"] = G.number_of_nodes()
        stats["edge_count"] = G.number_of_edges()
        stats["pair_count"] = pair_graph.number_of_edges()
        if pair_graph.number_of_edges() > 0:
            stats["density"] = round(nx.density(pair_graph), 4)
        degrees = dict(pair_graph.degree())
        if degrees:
            stats["max_degree"] = max(degrees.values())
            stats["avg_degree"] = round(sum(degrees.values()) / len(degrees), 2)
        try:
            stats["central_nodes"] = sorted(
                [(n, d) for n, d in nx.degree_centrality(pair_graph).items()],
                key=lambda x: x[1], reverse=True
            )[:5]
        except Exception:
            stats["central_nodes"] = []
        stats["graduation_candidate_count"] = sum(
            data.get("graduation_status") in {"confirmed-upcoming", "likely-upcoming"}
            for _, data in G.nodes(data=True)
        )
        stats["china_linked_count"] = sum(
            float(data.get("china_link_score") or 0) > 0
            for _, data in G.nodes(data=True)
        )
        stats["official_roster_candidate_count"] = sum(
            len(data.get("graduate_candidates") or [])
            for _, data in G.nodes(data=True)
        )
        stats["student_candidate_count"] = sum(
            data.get("student_status") in {
                "confirmed-student", "likely-student", "possible-student"
            }
            for _, data in G.nodes(data=True)
        )
        stats["lab_member_count"] = sum(
            data.get("group") == 2 for _, data in G.nodes(data=True)
        )
        stats["lab_entity_count"] = sum(
            data.get("group") == 3 for _, data in G.nodes(data=True)
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
    }
