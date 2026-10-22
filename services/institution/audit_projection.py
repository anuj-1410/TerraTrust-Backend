"""Read legacy JSON shapes without rewriting frozen audit history."""

from models.institution.common import normalize


def denominators(value):
    """Keep unknowns null; recognize the initial sample's documented aliases."""
    value = value or {}
    return normalize(
        {
            "students": value.get("students", value.get("student_count")),
            "staff": value.get("staff", value.get("staff_count")),
            "floor_area_m2": value.get("floor_area_m2"),
            "source_evidence_ids": value.get("source_evidence_ids") or [],
            "as_of": value.get("as_of"),
        }
    )


def tree_link(value):
    """Atmospheric attribution lives in JSONB, not nonexistent SQL columns."""
    snapshot = value.get("attribution_snapshot") or {}
    comparable = value["comparability_status"] == "COMPARABLE"
    return {
        **value,
        **{
            field: snapshot.get(field) if comparable else None
            for field in ("attributed_removal_tCO2", "attributed_release_tCO2")
        },
    }
