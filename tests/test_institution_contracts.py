import json
from decimal import Decimal
from uuid import uuid4

import pytest

from app.institution_errors import InstitutionError
from models.institution.common import canonical, measurement_hash, numeric, decimal_text, digest
from models.institution.contracts import parse, MODELS
from services.shared.allometry_service import dbh, chave_height
from services.shared.carbon_stock_service import stock
from services.shared.sampling_service import polygon, point
from services.advisory.retrieval_service import reciprocal_rank, applicable
from services.advisory.advisory_service import validate_answer, abstention, redact


@pytest.mark.parametrize("value", [1, 0.2, "1e2", "01", "NaN", "1.0000000000001", "1000000000000", "-Infinity"])
def test_accounting_wire_rejects_non_decimal_or_overflow(value):
    with pytest.raises(ValueError):
        decimal_text(value)


def test_decimal_derivation_rounds_half_even_only_at_output():
    assert numeric(Decimal("1.0000000000005")) == "1.000000000000"
    assert numeric(Decimal("1.0000000000015")) == "1.000000000002"
    assert numeric(Decimal("-0.0000000000001")) == "0.000000000000"


def test_mcj1_preserves_decimal_spelling_and_ecmascript_numbers():
    assert canonical({"b": -0.0, "a": [1e-7, 1e-6, 1e21, 1e20]}) == b'{"a":[1e-7,0.000001,1e+21,100000000000000000000],"b":0}'
    assert measurement_hash({"dbh_cm": "10"}) != measurement_hash({"dbh_cm": "10.0"})
    assert measurement_hash({"dbh_cm":"10", "client_content_hash":"ignored"}) == measurement_hash({"dbh_cm":"10"})
    with pytest.raises(ValueError):
        canonical({"number":9007199254740993})


def test_mcj1_utf16_order_and_invalid_surrogates():
    assert canonical({"\ue000": 1, "\U00010000": 2}).decode().startswith('{"\U00010000"')
    with pytest.raises((ValueError, UnicodeError)):
        canonical({"a":"\ud800"})


def test_contracts_forbid_unknown_fields_and_boolean_number_coercion():
    with pytest.raises(ValueError):
        parse("FeedbackRequest", {"useful": "true", "reason": None})
    with pytest.raises(ValueError):
        parse("FeedbackRequest", {"useful": True, "reason": None, "tenant": "other"})
    with pytest.raises(ValueError):
        parse("NotificationPatch", {"read": False})


def test_circumference_preserves_raw_and_derives_at_precision_50():
    raw = {"measurement_form":"CIRCUMFERENCE", "circumference_cm":"100", "dbh_cm":None}
    before = digest(raw)
    assert str(dbh(raw)).startswith("31.8309886183790671537767526745")
    assert digest(raw) == before
    with pytest.raises(ValueError):
        chave_height(Decimal("0.5"), Decimal("0"), Decimal("10"))


def test_baseline_stock_is_not_annual_removal():
    result = stock(Decimal("100"), Decimal("0.5"), {})
    assert result["stock_tC"] == "50.000000000000"
    assert result["stock_co2_t"] == "183.333333333333"
    assert result["removal_tCO2"] is None and result["release_tCO2"] is None
    assert result["comparability_status"] == "BASELINE_ONLY"


@pytest.mark.parametrize("geo", [
    {"type":"Point","coordinates":[200,30]},
    {"type":"Point","coordinates":[True,30]},
    {"type":"Point","coordinates":[73,19,5]},
    {"type":"Point","coordinates":[float("nan"),19]},
])
def test_gps_rejects_invalid_coordinates(geo):
    with pytest.raises(InstitutionError):
        point(geo)


def test_boundary_rejects_self_intersection_and_empty_geometry():
    with pytest.raises(InstitutionError):
        polygon({"type":"Polygon","coordinates":[[[0,0],[1,1],[1,0],[0,1],[0,0]]]})
    with pytest.raises(InstitutionError):
        polygon({"type":"MultiPolygon","coordinates":[]})


def test_rrf_uses_actual_ranks_and_stable_ties():
    ordered, scores = reciprocal_rank([["a","b"],["b","c"]])
    assert ordered == ["b","a","c"]
    assert scores["b"] == 1/62 + 1/61


def test_expired_or_private_guidance_never_passes_applicability():
    document = {"status":"ACTIVE", "config":{"visibility":"PUBLIC_APPROVED", "topic_codes":["SOLAR"]}, "license":"approved", "valid_from":None, "valid_to":"2025-01-01", "geography_codes":["IN"]}
    assert not applicable(document, "2026-01-01", ["IN"], ["SOLAR"])
    document["valid_to"] = None
    assert applicable(document, "2026-01-01", ["IN"], ["SOLAR"])
    document["config"]["visibility"] = "PRIVATE"
    assert not applicable(document, "2026-01-01", ["IN"], [])


def test_generated_facts_and_citations_cannot_override_snapshot():
    fact = {"fact_id":"F_ACTUAL_TOTAL", "value":"10.000000000000", "unit":"tCO2e", "snapshot_hash":"a"*64}
    answer = abstention([fact], "No applicable source.")
    assert validate_answer(answer, [fact], [], {})["supported"] is False
    answer["facts"][0] = {**fact, "value":"0"}
    with pytest.raises(ValueError):
        validate_answer(answer, [fact], [], {})
    answer = abstention([fact], "No applicable source.")
    answer["document_claims"] = [{"text":"Guaranteed savings.","citation_ids":["fabricated"]}]
    with pytest.raises(ValueError):
        validate_answer(answer, [fact], [], {})


def test_provider_redaction_removes_email_phone_and_private_urls():
    output = redact("Phone +919876543210, mail person@example.com https://private.test/file?token=secret")
    assert "9876543210" not in output and "person@example.com" not in output and "token=secret" not in output
