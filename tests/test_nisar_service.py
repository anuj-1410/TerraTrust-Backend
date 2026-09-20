import sys
import types

import pytest

from services import nisar_service


class _FakeImage:
    def __init__(self, name):
        self.name = name
        self.children = []
        self.selected_bands = []
        self.renamed_to = None
        self.clipped_to = None

    def select(self, band):
        selected = _FakeImage(f"{self.name}:{band}")
        selected.selected_bands = self.selected_bands + [band]
        return selected

    def rename(self, name):
        self.renamed_to = name
        self.name = name
        return self

    def divide(self, _other):
        return _FakeImage("ratio")

    def clip(self, region):
        self.clipped_to = region
        return self


class _FakeImageFactory:
    def __call__(self, asset_id):
        return _FakeImage(asset_id)

    @staticmethod
    def cat(images):
        image = _FakeImage("cat")
        image.children = images
        return image


class _FakeImageCollection:
    def __init__(self, asset_id):
        self.asset_id = asset_id
        self.bounds = None
        self.date_range = None

    def filterBounds(self, region):
        self.bounds = region
        return self

    def filterDate(self, date_start, date_end):
        self.date_range = (date_start, date_end)
        return self

    def median(self):
        return _FakeImage(f"{self.asset_id}:median")


def test_build_nisar_feature_image_requires_configured_asset(monkeypatch):
    monkeypatch.setattr(nisar_service.settings, "NISAR_GEE_ASSET_ID", "", raising=False)

    with pytest.raises(RuntimeError, match="NISAR_GEE_ASSET_ID"):
        nisar_service.build_nisar_feature_image("region", "2026-01-01", "2026-12-31")


def test_build_nisar_feature_image_uses_calibrated_gee_asset(monkeypatch):
    fake_ee = types.SimpleNamespace(
        Image=_FakeImageFactory(),
        ImageCollection=_FakeImageCollection,
    )
    monkeypatch.setitem(sys.modules, "ee", fake_ee)
    monkeypatch.setattr(
        nisar_service.settings,
        "NISAR_GEE_ASSET_ID",
        "users/terratrust/nisar_calibrated",
        raising=False,
    )
    monkeypatch.setattr(
        nisar_service.settings,
        "NISAR_GEE_ASSET_TYPE",
        "image_collection",
        raising=False,
    )
    monkeypatch.setattr(nisar_service.settings, "NISAR_HH_BAND", "HH", raising=False)
    monkeypatch.setattr(nisar_service.settings, "NISAR_HV_BAND", "HV", raising=False)

    image = nisar_service.build_nisar_feature_image("region", "2026-01-01", "2026-12-31")

    assert image.name == "cat"
    assert image.clipped_to == "region"
    assert [child.name for child in image.children] == [
        "NISAR_HH",
        "NISAR_HV",
        "NISAR_HH_HV_RATIO",
    ]


def test_extract_nisar_backscatter_uses_gee_when_available(monkeypatch):
    monkeypatch.setattr(
        nisar_service.settings,
        "NISAR_GEE_ASSET_ID",
        "users/terratrust/nisar",
        raising=False,
    )
    monkeypatch.setattr(
        nisar_service,
        "_extract_nisar_gee_backscatter",
        lambda _geojson, _start, _end: {
            "available": True,
            "hh_mean_db": -6.2,
            "hv_mean_db": -14.8,
            "hh_hv_ratio": 0.4189,
        },
    )

    result = nisar_service.extract_nisar_backscatter({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})

    assert result["available"] is True
    assert result["hh_mean_db"] == -6.2
    assert result["hv_mean_db"] == -14.8
    assert result["granule_name"] == "users/terratrust/nisar"


def test_extract_nisar_backscatter_falls_back_to_asf_when_gee_unavailable(monkeypatch):
    monkeypatch.setattr(
        nisar_service.settings,
        "NISAR_GEE_ASSET_ID",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        nisar_service,
        "search_nisar_granules",
        lambda **_kwargs: [
            {
                "granule_name": "NISAR_L2_GCOV_123",
                "platform": "NISAR",
                "acquisition_date": "2026-06-15T00:00:00Z",
                "polarisation": "HH+HV",
            }
        ],
    )

    result = nisar_service.extract_nisar_backscatter({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})

    assert result["available"] is True
    assert result["platform"] == "NISAR"
    assert result["granule_name"] == "NISAR_L2_GCOV_123"
    assert result["hh_mean_db"] is None
