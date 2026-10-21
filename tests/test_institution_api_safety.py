from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.config import settings
from app.institution_api import install
from app.institution_context import unique_object, json_depth
from scripts.institution.migrate import bundles
import pytest


def test_disabled_api_fails_before_auth_database_or_provider(monkeypatch):
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", False)
    app = FastAPI()
    install(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/institutions")
    assert response.status_code == 503
    assert response.json()["code"] == "INSTITUTION_FLOW_DISABLED"


def test_openapi_has_no_dangling_local_defs_and_static_import_route_wins():
    app = FastAPI()
    install(app)
    schema = app.openapi()
    import json
    assert '"$ref": "#/$defs/' not in json.dumps(schema)
    from app.institution_context import router
    paths = [route.path for route in router.routes]
    assert paths.index("/institution-audits/{aid}/imports/template") < paths.index("/institution-audits/{aid}/imports/{batch_id}")
    import re
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            declared = {parameter["name"] for parameter in operation.get("parameters", []) if parameter["in"] == "path" and parameter["required"]}
            assert set(re.findall(r"\{([^}]+)\}", path)) <= declared
    mutation = schema["paths"]["/api/v1/institutions/{iid}"]["patch"]
    assert {"InstitutionFirebaseBearer": []} in mutation["security"]
    assert schema["components"]["securitySchemes"]["InstitutionFirebaseBearer"]["scheme"] == "bearer"
    assert any(parameter["name"] == "Idempotency-Key" and parameter["required"] for parameter in mutation["parameters"])
    query = schema["paths"]["/api/v1/institutions/{iid}/analytics"]["get"]
    assert any(parameter["name"] == "campus_ids" and parameter["schema"]["type"] == "array" for parameter in query["parameters"])


def test_duplicate_json_fields_are_rejected_and_depth_is_bounded():
    with pytest.raises(ValueError):
        unique_object([("quantity","1"),("quantity","2")])
    assert json_depth({"a":[{"b":1}]}) == 4


def test_migration_artifacts_are_ordered_and_checksummed_without_database():
    migrations = bundles()
    assert [v for v, _, _ in migrations] == list(range(1,17))
    assert all(len(hashed) == 64 for _, _, hashed in migrations)
    assert [p.name for p in migrations[8][1]] == ["009_constraints_access.sql", "009_generated_foreign_keys.sql"]


def test_generated_physical_columns_match_the_repository_types():
    """Catch bind/type mismatches that a PostgreSQL syntax parser cannot find."""
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    tables = json.loads((root / "repositories/institution/table_definitions.json").read_text(encoding="utf-8"))
    sql = "\n".join(path.read_text(encoding="utf-8") for path in (root / "migrations/institution").glob("*.sql"))
    physical = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S))
    for table, fields in tables.items():
        if table == "users":
            continue
        assert table in physical
        for name, spec in fields.items():
            expected = "vector" if name == "embedding" else re.sub(r"TEXT \([^)]*\)", "TEXT", spec["type"])
            assert re.search(r"^\s+" + re.escape(name) + r"\s+" + re.escape(expected) + r"(?=\s|,)", physical[table], re.M | re.I), f"Physical type mismatch: {table}.{name} expected {expected}"
