"""Admission checks for actual service-produced demo.md stage history."""


def validate_stage(account, tables):
    def rows(table):
        return tables.get(table, [])

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    if account < 7:
        return
    (campus,) = rows("campuses")
    require(
        campus["control_status"] == "APPROVED",
        "Campus control must be independently approved.",
    )
    for table, pointer in (
        ("campus_boundaries", "current_boundary_id"),
        ("campus_vegetation_masks", "current_mask_id"),
        ("campus_plot_sets", "current_plot_set_id"),
    ):
        selected = next((r for r in rows(table) if r["id"] == campus[pointer]), None)
        require(
            selected and selected["review_status"] == "APPROVED",
            "The current boundary, mask and plot set must be reviewed.",
        )
        if table == "campus_vegetation_masks":
            require(
                selected["processing_status"] == "SUCCEEDED",
                "Mask processing must actually succeed.",
            )
    if account == 7:
        return
    stocks = sorted(rows("campus_tree_audits"), key=lambda r: r["assessment_date"])
    require(
        len(stocks) == (1 if account == 8 else 2),
        "Capture exactly the required accepted stock history.",
    )
    surveys = {r["id"]: r for r in rows("campus_tree_surveys")}
    for stock in stocks:
        require(
            stock["processing_status"] == "SUCCEEDED"
            and stock["review_status"] == "ACCEPTED"
            and stock["result_hash"]
            and stock["stock_tC"] is not None,
            "Stock assessments must be calculated and independently accepted.",
        )
        require(
            stock["survey_id"] in surveys
            and surveys[stock["survey_id"]]["status"] == "ACCEPTED",
            "The underlying field survey must be accepted.",
        )
    require(
        stocks[0]["comparability_status"] == "BASELINE_ONLY"
        and stocks[0]["baseline_audit_id"] is None
        and stocks[0]["removal_tCO2"] is None
        and stocks[0]["release_tCO2"] is None,
        "First stock must be baseline-only without annual removal/release.",
    )
    if account == 8:
        return
    before, after = stocks
    require(
        before["assessment_date"].startswith("2024-")
        and after["assessment_date"].startswith("2025-"),
        "Persistent history requires actual 2024 and 2025 stock assessments.",
    )
    require(
        after["baseline_audit_id"] == before["id"]
        and after["comparability_status"] == "COMPARABLE"
        and all(
            after[field] is not None
            for field in ("signed_change_tCO2", "removal_tCO2", "release_tCO2")
        ),
        "2025 must link its comparable baseline and supported atmospheric attribution.",
    )
    for year, stock in ((2024, before), (2025, after)):
        audits = [
            r
            for r in rows("institution_audits")
            if r["period_start"] == f"{year}-01-01"
            and r["period_end"] == f"{year}-12-31"
        ]
        require(
            len(audits) == 1 and audits[0]["business_status"] == "FINALIZED",
            "Both calendar years require independently finalized annual audits.",
        )
        audit = audits[0]
        snapshot = next(
            (
                r
                for r in rows("institution_calculation_snapshots")
                if r["id"] == audit["reviewed_snapshot_id"]
                and r["audit_id"] == audit["id"]
            ),
            None,
        )
        result = next(
            (
                r
                for r in rows("institution_audit_results")
                if r["id"] == audit["current_result_id"]
                and r["snapshot_id"] == audit["reviewed_snapshot_id"]
            ),
            None,
        )
        require(
            snapshot
            and result
            and snapshot["result_hash"] == result["result_hash"]
            and result["result"]["actual_total"] is not None,
            "Each year needs exact reviewed snapshot/result with complete operational inventory.",
        )
        require(
            any(
                str(link["assessment"]["id"]) == stock["id"]
                for link in snapshot["frozen_input"]["tree_links"]
            ),
            "Each annual result must use that year's actual stock assessment.",
        )
        exports = [
            r
            for r in rows("institution_exports")
            if r["audit_id"] == audit["id"]
            and r["snapshot_id"] == snapshot["id"]
            and r["result_hash"] == result["result_hash"]
            and r["status"] == "SUCCEEDED"
            and not r["draft_watermark"]
        ]
        require(
            {r["format"] for r in exports} == {"PDF", "CSV", "JSON"},
            "Each year needs real final PDF, CSV and JSON exports.",
        )
        require(
            any(
                r["audit_id"] == audit["id"]
                for r in rows("institution_emission_inputs")
            )
            and any(
                r["audit_id"] == audit["id"]
                for r in rows("institution_mitigation_inputs")
            ),
            "Both years need actual emission and applicable mitigation records.",
        )
