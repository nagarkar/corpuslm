"""The shared pipeline nodes (`src/corpuslm/graph.py`)."""

from corpuslm.graph import CORE_NODES, Node


def test_core_nodes_are_the_shared_stages_in_build_order():
    names = [node.name for node in CORE_NODES]
    assert names[:4] == ["normalize", "pass1", "pass2", "pass3"]
    assert len(set(names)) == len(names)
    assert all(isinstance(node, Node) for node in CORE_NODES)


def test_every_core_dependency_is_a_core_node_or_a_source_kinds_own_stage():
    names = {node.name for node in CORE_NODES}
    own = {"metadata", "captions", "transcript"}  # ytlm's fetching nodes; `transcript` is virtual
    for node in CORE_NODES:
        assert set(node.deps) <= names | own, node.name
