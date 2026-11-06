import pytest


@pytest.mark.anyio
async def test_graph_builder_exposes_deterministic_backbone_nodes():
    from retailq.workflow.graph import build_graph
    graph=build_graph(None,None,None,None,None)
    names=set(graph.get_graph().nodes)
    assert {'resolve','intent','route','retrieve','gate','agent_decide','tools','answer','log',
            'complaint','smalltalk','fallback'}<=names
