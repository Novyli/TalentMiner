"""Graph builder: construct author relationship networks using NetworkX."""

import networkx as nx
from collections import defaultdict


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


def build_coauthorship_graph(author_profiles: list) -> dict:
    """Build a co-authorship network graph from crawled author profiles.

    Returns a dict with nodes, edges, and graph metadata for frontend rendering.
    """
    # A pair of people can be connected by collaboration, affiliation, and
    # topic at the same time, so each relation type needs its own edge.
    G = nx.MultiGraph()

    # Build author name-to-profile lookup
    name_to_profile = {}
    for p in author_profiles:
        key = p.name.lower().strip()
        name_to_profile[key] = p

    # Add nodes
    for profile in author_profiles:
        node_id = profile.name
        G.add_node(
            node_id,
            name=profile.name,
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
            email=profile.email or "",
            email_source=profile.email_source or "",
            match_score=profile.match_score,
            match_status=getattr(profile, "match_status", "unmatched"),
            match_breakdown=getattr(profile, "match_breakdown", {}),
            candidates=getattr(profile, "candidates", []),
            website_url=profile.website_url or "",
            sources=profile.sources,
            group=0,
        )

    # Build edges from co-authorship data
    for profile in author_profiles:
        src_name = profile.name
        for ca_name in profile.coauthors:
            ca_lower = ca_name.lower().strip()
            # Check if co-author is also in our author list
            if ca_lower in name_to_profile:
                _add_relation(
                    G,
                    src_name,
                    name_to_profile[ca_lower].name,
                    "co-author",
                    1,
                    "OpenAlex 共同署名记录",
                )
            else:
                # Add co-author as an "external" node
                ext_node_id = f"External: {ca_name}"
                if not G.has_node(ext_node_id):
                    G.add_node(
                        ext_node_id,
                        name=ca_name,
                        openalex_id="",
                        orcid="",
                        google_scholar_url="",
                        researchgate_url="",
                        twitter_url="",
                        linkedin_url="",
                        affiliations=[],
                        topics=[],
                        cited_by_count=0,
                        works_count=0,
                        sources=[],
                        group=1,
                    )
                _add_relation(
                    G,
                    src_name,
                    ext_node_id,
                    "co-author",
                    1,
                    "OpenAlex 共同署名记录",
                )

    # Build affiliation-based edges
    affiliation_map = defaultdict(list)
    for profile in author_profiles:
        for aff in profile.affiliations:
            affiliation_map[aff].append(profile.name)

    for aff, members in affiliation_map.items():
        members = list(dict.fromkeys(members))
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
    pos = nx.spring_layout(pair_graph, k=2, iterations=50, seed=42)

    nodes = []
    for node_id, data in G.nodes(data=True):
        x, y = pos.get(node_id, (0, 0))
        node = {
            "id": node_id,
            "name": data.get("name", node_id),
            "group": data.get("group", 0),
            "x": float(x * 400),
            "y": float(y * 400),
            "cited_by_count": data.get("cited_by_count", 0),
            "works_count": data.get("works_count", 0),
            "email": data.get("email", ""),
            "email_source": data.get("email_source", ""),
            "match_score": data.get("match_score", 0),
            "match_status": data.get("match_status", "unmatched"),
            "match_breakdown": data.get("match_breakdown", {}),
            "candidates": data.get("candidates", []),
            "website_url": data.get("website_url", ""),
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

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
    }
