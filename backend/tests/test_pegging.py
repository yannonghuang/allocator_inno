"""Tests for pegging API: inv->demand edge sum must be <= node qty."""
import pytest
from app.api.pegging import _build_inventory_graph, _pegging_verify_inv_demand_edges
from app.database import SessionLocal
from app.models import AllocationRun


def test_pegging_verify_helper_ok():
    """When each inv node's demand edge sum <= node qty, _verify marks ok=True."""
    nodes = {
        "P|L|1": {"id": "P|L|1", "qty": 30},
        "demand|D1": {"id": "demand|D1"},
    }
    edges = [
        {"from": "P|L|1", "to": "demand|D1", "qty": 10},
        {"from": "P|L|1", "to": "demand|D2", "qty": 20},
    ]
    nodes["demand|D2"] = {"id": "demand|D2"}
    verify_list = _pegging_verify_inv_demand_edges(nodes, edges)
    assert len(verify_list) == 1
    assert verify_list[0]["node_id"] == "P|L|1"
    assert verify_list[0]["node_qty"] == 30
    assert verify_list[0]["demand_edge_sum"] == 30
    assert verify_list[0]["ok"] is True


def test_pegging_verify_helper_fail():
    """When demand edge sum > node qty, _verify marks ok=False."""
    nodes = {"P|L|1": {"id": "P|L|1", "qty": 30}}
    edges = [
        {"from": "P|L|1", "to": "demand|D1", "qty": 40},
        {"from": "P|L|1", "to": "demand|D2", "qty": 50},
    ]
    verify_list = _pegging_verify_inv_demand_edges(nodes, edges)
    assert len(verify_list) == 1
    assert verify_list[0]["demand_edge_sum"] == 90
    assert verify_list[0]["ok"] is False


@pytest.mark.integration
def test_pegging_inv_demand_edges_sum_to_node_qty():
    """
    Integration: with a real DB, build graph for a case/run that has allocation,
    then assert every inv node's inv->demand edge sum <= node qty.
    Skip if no case/run exists (run with: pytest -m integration when DB is populated).
    """
    db = SessionLocal()
    try:
        run = db.query(AllocationRun).first()
        if not run:
            pytest.skip("No allocation run in DB")
        case_id = run.case_id
        nodes, edges, _supply_qty, _dates = _build_inventory_graph(db, case_id, run.id)
        verify_list = _pegging_verify_inv_demand_edges(nodes, edges)
        for v in verify_list:
            assert v["ok"], f"node {v['node_id']}: demand_edge_sum={v['demand_edge_sum']} > node_qty={v['node_qty']}"
    finally:
        db.close()
