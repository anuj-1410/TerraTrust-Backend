"""Synthetic arithmetic cases only; never deployed factors or measured history."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace

import pytest

from models.institution.common import digest, normalize
from models.institution.contracts import parse
from services.institution.accounting_service import calculate_inventory, validate_lines, CATEGORIES
from services.institution.mitigation_service import calculate_action
from services.institution.factor_service import convert


def uid():
    return str(uuid4())


def inventory():
    aid, logical, mid, fid, cid, eid = [uid() for _ in range(6)]
    declarations = [{"category":c,"state":"REPORTED" if c == "ELECTRICITY" else "ZERO_CONFIRMED","reason":"Synthetic reference case only", "evidence_ids":[eid]} for c in CATEGORIES]
    coverage = {"consolidation_approach":"OPERATIONAL_CONTROL","included_scopes":["SCOPE_1","SCOPE_2","SCOPE_3"],"categories":declarations,"exclusions":[]}
    coverage.update(excluded_pools=[],included_services=["ELECTRICITY"])
    line = {"id":uid(),"category":"ELECTRICITY","subcategory":"GRID","campus_id":cid,"source_id":"test-meter","quantity":"1000","unit":"kWh","activity_start":"2024-01-01","activity_end":"2024-12-31","source_type":"REPORTED","estimate_notes":None,"evidence_ids":[eid],"included_in_input_id":None,"allocation_ratio":None,"factor_id":fid,"details":{"grid_code":"SYNTHETIC","geography_code":"TEST","meter_id":"test-meter","included_submeter_ids":[],"total_load_kWh":None}}
    factor = {"id":fid,"status":"APPROVED","data_origin":"SYNTHETIC","logical_key":"TEST_ONLY","version":1,"title":"Synthetic arithmetic test","publisher":"Unit test","source_version":"test","license":"test","applicability_notes":"Not a real factor","artifact_hash":"b"*64,"source_byte_hash":"c"*64,"source_evidence_id":eid,"geography_codes":["TEST"],"valid_from":"2024-01-01","valid_to":"2024-12-31","config":{"category":"ELECTRICITY","subcategory":"GRID","process":"GRID","fuel":None,"material":None,"value":"1.2","numerator_unit":"kgCO2e","denominator_unit":"kWh","gas_basis":"COMBINED_CO2E","scope":"SCOPE_2","purpose":"INVENTORY","boundary":"test","exclusions":[],"mass_basis":None,"standard_conditions":None,"gwp":None,"uncertainty":None}}
    factor["validated_at"] = "2025-01-01T00:00:00.000000Z"
    audit = {"id":aid,"logical_audit_id":logical,"revision":1,"input_revision":1,"method_version":mid,"period_start":"2024-01-01","period_end":"2024-12-31","timezone":"Asia/Kolkata","campus_ids":[cid],"coverage":coverage}
    return {"audit":audit,"emissions":[line],"mitigations":[],"tree_links":[],"factors":{fid:factor},"parameters":{},"method":{"id":mid,"config":{"epsilon_tCO2e":"0","uncertainty_policy":{"method_key":"UNKNOWN"}}},"input_hash":"a"*64}


def test_grid_unit_conversion_and_absent_tree_change_are_explicit():
    frozen = inventory()
    result, errors = calculate_inventory(frozen)
    assert not errors
    assert result["actual_total"] == "1.200000000000"
    assert result["by_scope"]["SCOPE_2"] == "1.200000000000"
    assert result["removal"] is None and result["inventory_balance"] is None
    assert result["balance_status"] is None
    assert result["uncertainty"]["type"] == "UNKNOWN"
    parse("ResultResource",{**result,"id":result["snapshot_id"]})


def test_repeated_snapshot_has_same_numerical_hash_despite_new_result_ids():
    frozen = inventory()
    before = digest(frozen)
    a, _ = calculate_inventory(frozen)
    b, _ = calculate_inventory(frozen)
    assert a["snapshot_id"] != b["snapshot_id"]
    assert a["result_hash"] == b["result_hash"]
    assert before == digest(frozen)


def test_missing_factor_is_unknown_instead_of_zero():
    frozen = inventory()
    frozen["emissions"][0]["factor_id"] = None
    result, errors = calculate_inventory(frozen)
    assert errors and result["actual_total"] is None
    assert result["quantified_actual_subtotal"] == "0.000000000000"
    assert result["by_category"]["ELECTRICITY"] is None


def test_main_meter_plus_submeter_contributes_once_and_allocation_cannot_exceed_one():
    frozen = inventory()
    parent = frozen["emissions"][0]
    child = {**deepcopy(parent),"id":uid(),"source_id":"submeter","quantity":"250","included_in_input_id":parent["id"],"allocation_ratio":"0.25"}
    frozen["emissions"].append(child)
    result, errors = calculate_inventory(frozen)
    assert not errors and result["actual_total"] == "1.200000000000"
    assert result["calculation_lines"][1]["included_contribution"] is False
    child["allocation_ratio"] = "1.1"
    assert any(e["code"] == "INVALID_ALLOCATION" for e in validate_lines(frozen["audit"],frozen["emissions"]))


def test_annual_and_monthly_activity_overlap_is_blocked():
    frozen = inventory()
    second = {**deepcopy(frozen["emissions"][0]),"id":uid(),"activity_end":"2024-01-31"}
    assert any(e["code"] == "OVERLAPPING_ACTIVITY" for e in validate_lines(frozen["audit"],frozen["emissions"]+[second]))


def test_avoided_solar_is_not_subtracted_again_from_actual_balance():
    frozen = inventory()
    original = frozen["emissions"][0]
    factor = deepcopy(frozen["factors"][original["factor_id"]])
    factor.update(id=uid())
    factor["config"]["purpose"] = "AVOIDED"
    frozen["factors"][factor["id"]] = factor
    actual = {original["id"]:{"input_id":original["id"],"contribution_amount":"0.1","linked_parent_id":None}}
    line = {"action":"SOLAR","baseline":{"displaced_self_kWh":"1000","eligible_export_kWh":"0","displaced_grid_factor_id":factor["id"]},"project":{"generated_kWh":"1000","self_used_kWh":"1000","exported_kWh":"0","losses_kWh":"0","project_component_input_ids":[original["id"]]}}
    avoided, baseline, project, _, _ = calculate_action(line,frozen["factors"],{},actual,[original])
    assert baseline == Decimal("1.2") and project == Decimal("0.1") and avoided == Decimal("1.1")
    # Actual inventory is independently computed and remains its measured value.
    result, _ = calculate_inventory(frozen)
    assert result["actual_total"] == "1.200000000000"


@pytest.mark.parametrize("source,target",[("L","kg"),("std_m3","m3"),("kWh","MJ")])
def test_unapproved_physical_conversions_cannot_be_inferred(source,target):
    with pytest.raises(ValueError):
        convert(Decimal("1"),source,target)


@pytest.mark.asyncio
async def test_calculation_worker_stores_wire_lines_in_the_physical_dictionary():
    from repositories.institution.base import columns
    from tasks.institution_tasks import publish
    frozen = inventory()
    output = calculate_inventory(frozen)
    stored = []
    class Repo:
        async def insert(self,table,values):
            assert set(values) <= columns(table)
            stored.append((table,values))
            return values
        async def update(self,*args,**kwargs):
            return args[2]
        async def find(self,*args,**kwargs):
            return []
    job = {"job_type":"institution_calculation","frozen_input":frozen,"institution_id":uid(),"id":uid(),"source_input_revision":1,"input_hash":frozen["input_hash"]}
    resource = {**frozen["audit"],"business_status":"DRAFT"}
    sid = await publish(Repo(),job,{},resource,output)
    snapshot = next(values for table,values in stored if table == "institution_calculation_snapshots")
    assert snapshot["id"] == sid
    line = next(values for table,values in stored if table == "institution_calculation_lines")
    assert line["input_type"] == "EMISSION" and line["result_unit"] == "tCO2e"
