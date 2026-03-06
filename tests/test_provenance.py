"""Tests for architect.provenance module."""

import json
import os

from architect.provenance import (
    EdgeType,
    NodeType,
    ProvenanceGraph,
    content_id,
    get_provenance_graph,
    reset_provenance_graph,
)


class TestContentId:
    def test_deterministic(self):
        assert content_id("hello") == content_id("hello")

    def test_different_content_different_id(self):
        assert content_id("hello") != content_id("world")

    def test_returns_16_char_hex(self):
        cid = content_id("test")
        assert len(cid) == 16
        int(cid, 16)  # valid hex


class TestProvenanceGraph:
    def test_add_entity(self):
        g = ProvenanceGraph()
        eid = g.add_entity("goal", content="Build a dashboard")
        assert eid in g.nodes
        node = g.get_node(eid)
        assert node.node_type == NodeType.ENTITY.value
        assert node.label == "goal"
        assert node.content == "Build a dashboard"

    def test_add_activity(self):
        g = ProvenanceGraph()
        aid = g.add_activity("decompose", content="decompose_goal()")
        assert aid in g.nodes
        node = g.get_node(aid)
        assert node.node_type == NodeType.ACTIVITY.value

    def test_add_agent(self):
        g = ProvenanceGraph()
        agid = g.add_agent("planner_llm")
        assert agid in g.nodes
        node = g.get_node(agid)
        assert node.node_type == NodeType.AGENT.value
        assert node.label == "planner_llm"

    def test_was_generated_by(self):
        g = ProvenanceGraph()
        eid = g.add_entity("plan", content="[steps]")
        aid = g.add_activity("decompose")
        g.was_generated_by(eid, aid)
        assert len(g.edges) == 1
        edge = g.edges[0]
        assert edge.edge_type == EdgeType.WAS_GENERATED_BY.value
        assert edge.source == eid
        assert edge.target == aid

    def test_used(self):
        g = ProvenanceGraph()
        aid = g.add_activity("generate_code")
        eid = g.add_entity("prompt", content="Write a script...")
        g.used(aid, eid)
        edge = g.edges[0]
        assert edge.edge_type == EdgeType.USED.value
        assert edge.source == aid
        assert edge.target == eid

    def test_was_associated_with(self):
        g = ProvenanceGraph()
        aid = g.add_activity("llm_call")
        agid = g.add_agent("orchestrator_llm")
        g.was_associated_with(aid, agid)
        edge = g.edges[0]
        assert edge.edge_type == EdgeType.WAS_ASSOCIATED_WITH.value
        assert edge.source == aid
        assert edge.target == agid

    def test_was_derived_from(self):
        g = ProvenanceGraph()
        eid1 = g.add_entity("code_v1", content="print('hello')")
        eid2 = g.add_entity("code_v2", content="print('world')")
        g.was_derived_from(eid2, eid1)
        edge = g.edges[0]
        assert edge.edge_type == EdgeType.WAS_DERIVED_FROM.value
        assert edge.source == eid2
        assert edge.target == eid1

    def test_content_addressed_ids(self):
        g = ProvenanceGraph()
        eid1 = g.add_entity("same_label", content="same content")
        eid2 = g.add_entity("different_label", content="same content")
        # Same content -> same ID (content-addressed)
        assert eid1 == eid2

    def test_different_content_different_ids(self):
        g = ProvenanceGraph()
        eid1 = g.add_entity("entity", content="content A")
        eid2 = g.add_entity("entity", content="content B")
        assert eid1 != eid2

    def test_entity_without_content_uses_label(self):
        g = ProvenanceGraph()
        eid = g.add_entity("unique_label")
        expected = content_id("unique_label")
        assert eid == expected

    def test_metadata_on_nodes(self):
        g = ProvenanceGraph()
        eid = g.add_entity("plan", content="test", step_id=1, attempt=2)
        node = g.get_node(eid)
        assert node.metadata == {"step_id": 1, "attempt": 2}

    def test_metadata_on_edges(self):
        g = ProvenanceGraph()
        eid = g.add_entity("x", content="a")
        aid = g.add_activity("y", content="b")
        g.was_generated_by(eid, aid, timestamp="2024-01-01")
        edge = g.edges[0]
        assert edge.metadata == {"timestamp": "2024-01-01"}

    def test_to_dict(self):
        g = ProvenanceGraph()
        eid = g.add_entity("goal", content="test goal")
        aid = g.add_activity("decompose", content="decompose()")
        g.was_generated_by(eid, aid)
        d = g.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert eid in d["nodes"]
        assert aid in d["nodes"]
        assert len(d["edges"]) == 1

    def test_save_to_disk(self, tmp_path):
        output_path = os.path.join(str(tmp_path), ".state", "provenance.json")
        g = ProvenanceGraph(output_path=output_path)
        eid = g.add_entity("goal", content="test")
        aid = g.add_activity("decompose", content="run")
        g.was_generated_by(eid, aid)
        g.save()

        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)
        assert eid in data["nodes"]
        assert len(data["edges"]) == 1

    def test_save_explicit_path(self, tmp_path):
        g = ProvenanceGraph()
        g.add_entity("test", content="data")
        explicit_path = os.path.join(str(tmp_path), "custom", "prov.json")
        g.save(explicit_path)
        assert os.path.exists(explicit_path)

    def test_save_no_path_is_noop(self):
        g = ProvenanceGraph()
        g.add_entity("test", content="data")
        g.save()  # should not raise

    def test_get_node_missing(self):
        g = ProvenanceGraph()
        assert g.get_node("nonexistent") is None

    def test_cross_attempt_linking(self):
        """When a step fails and gets rewritten, the new attempt links to the old error."""
        g = ProvenanceGraph()
        # First attempt: code -> error
        code_v1 = g.add_entity("code_v1", content="bad code")
        error_v1 = g.add_entity("error_v1", content="NameError: x")
        llm_call_1 = g.add_activity("llm_call", content="attempt_1_call")
        g.was_generated_by(code_v1, llm_call_1)

        # Second attempt derives from the error
        rewrite = g.add_activity("rewrite", content="rewrite_from_error")
        g.used(rewrite, error_v1)
        code_v2 = g.add_entity("code_v2", content="fixed code")
        g.was_generated_by(code_v2, rewrite)
        g.was_derived_from(code_v2, code_v1)

        # Verify the chain
        assert len(g.edges) == 4
        derived_edges = [e for e in g.edges if e.edge_type == "wasDerivedFrom"]
        assert len(derived_edges) == 1
        assert derived_edges[0].source == code_v2
        assert derived_edges[0].target == code_v1


class TestSingleton:
    def test_get_provenance_graph_returns_same_instance(self):
        reset_provenance_graph()
        g1 = get_provenance_graph()
        g2 = get_provenance_graph()
        assert g1 is g2
        reset_provenance_graph()

    def test_reset_clears_singleton(self):
        reset_provenance_graph()
        g1 = get_provenance_graph()
        reset_provenance_graph()
        g2 = get_provenance_graph()
        assert g1 is not g2
        reset_provenance_graph()
