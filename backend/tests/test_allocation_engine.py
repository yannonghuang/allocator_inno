"""Minimal tests for allocation engine."""
import pytest
from app.services.allocation_engine import run_allocation, _comp_key, _variant_key


def test_simple_allocation():
    # One product P made from components A and B; demand for P; supply of A and B
    data = {
        "bom": [
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "A", "rate": 1},
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "B", "rate": 1},
        ],
        "method_make": [{"bom_id": "BOM_P", "product_id": "P", "location_id": "L1", "preference": 1}],
        "demand": [
            {"demand_id": "D1", "product_id": "P", "customer_id": "C1", "priority": 1, "quantity": 10},
        ],
        "supply": [
            {"supply_id": "S1", "product_id": "A", "location_id": "L1", "qty": 100},
            {"supply_id": "S2", "product_id": "B", "location_id": "L1", "qty": 100},
        ],
        "overrides": [],
    }
    result = run_allocation(data)
    assert "allocation" in result
    assert "feasible_demands" in result
    assert len(result["feasible_demands"]) == 1
    assert result["feasible_demands"][0]["demand_id"] == "D1"
    assert result["feasible_demands"][0]["allocated_qty"] > 0


def test_comp_key():
    assert _comp_key(("A", "L1")) == "A|L1"


def test_variant_key():
    assert _variant_key(("P", "L1")) == "P|L1"


def test_bom_rate_consumption():
    """Spec: make consumes quantity/rate_i of each component. Rate 2 => need 0.5 units of comp per 1 unit output."""
    data = {
        "bom": [
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "A", "rate": 2.0},
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "B", "rate": 1.0},
        ],
        "method_make": [{"bom_id": "BOM_P", "product_id": "P", "location_id": "L1", "preference": 1}],
        "demand": [
            {"demand_id": "D1", "product_id": "P", "customer_id": "C1", "priority": 1, "quantity": 10},
        ],
        "supply": [
            {"supply_id": "S1", "product_id": "A", "location_id": "L1", "qty": 5},
            {"supply_id": "S2", "product_id": "B", "location_id": "L1", "qty": 10},
        ],
        "overrides": [],
    }
    result = run_allocation(data)
    assert len(result["allocation"]) == 1
    a = result["allocation"][0]
    assert a["qty"] > 0
    assert "req_rates" in a
    assert a["req_rates"] == [2.0, 1.0]
    assert result["feasible_demands"][0]["allocated_qty"] == a["qty"]


def test_alt_group_or_and():
    """Alternative groups (OR): one alternative chosen. Requirement set (AND): all components in that set consumed."""
    # P can be made from (A and B) OR (C alone). alt_group x = {A,B}, alt_group y = {C}.
    data = {
        "bom": [
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "A", "rate": 1, "alt_group": "x"},
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "B", "rate": 1, "alt_group": "x"},
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "C", "rate": 1, "alt_group": "y"},
        ],
        "method_make": [{"bom_id": "BOM_P", "product_id": "P", "location_id": "L1", "preference": 1}],
        "demand": [{"demand_id": "D1", "product_id": "P", "customer_id": "C1", "priority": 1, "quantity": 10}],
        "supply": [
            {"supply_id": "S1", "product_id": "A", "location_id": "L1", "qty": 10},
            {"supply_id": "S2", "product_id": "B", "location_id": "L1", "qty": 10},
            # No C supplied -> allocation must use alternative x (A and B)
        ],
        "overrides": [],
    }
    result = run_allocation(data)
    assert len(result["allocation"]) == 1
    a = result["allocation"][0]
    reqs = set(a.get("req_component_ids", []))
    # Should have chosen alternative x (A and B), not y (C)
    assert "A|L1" in reqs and "B|L1" in reqs
    assert "C|L1" not in reqs
    assert a["qty"] > 0


def test_timing_preexisting_vs_dated():
    """Preexisting supply (null date) is period 0; demand with due date only satisfied if output on time."""
    from app.services.allocation_engine import run_allocation
    data = {
        "bom": [
            {"bom_id": "BOM_P", "parent_id": "P", "child_id": "A", "rate": 1},
        ],
        "method_make": [{"bom_id": "BOM_P", "product_id": "P", "location_id": "L1", "preference": 1}],
        "demand": [
            {"demand_id": "D1", "product_id": "P", "customer_id": "C1", "priority": 1, "quantity": 10, "request_due_time": "2024-06-01"},
        ],
        "supply": [
            {"supply_id": "S1", "product_id": "A", "location_id": "L1", "supply_date": None, "qty": 100},
        ],
        "overrides": [],
    }
    result = run_allocation(data)
    assert len(result["allocation"]) > 0
    assert result["allocation"][0].get("output_period") == 0
    assert result["feasible_demands"][0]["allocated_qty"] > 0
