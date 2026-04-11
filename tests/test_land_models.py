from datetime import datetime, timezone
from uuid import uuid4

from models.land import LandListItem


def test_land_list_item_coerces_native_sqlalchemy_types_to_strings():
    land_id = uuid4()
    audit_id = uuid4()
    registered_at = datetime(2026, 4, 11, 3, 39, 21, tzinfo=timezone.utc)

    item = LandListItem(
        id=land_id,
        farm_name="Main Demo Farm",
        survey_number="DEMO-004-47",
        district="Pune",
        taluka="Baramati",
        village="Morgaon",
        state="Maharashtra",
        area_hectares=5.25,
        is_verified=True,
        boundary_source="MANUAL",
        registered_at=registered_at,
        last_audit_year=2024,
        current_audit_id=audit_id,
        current_audit_status="MINTED",
        thumbnail_url="https://example.com/thumb.png",
    )

    assert item.id == str(land_id)
    assert item.current_audit_id == str(audit_id)
    assert item.registered_at == registered_at.isoformat()
