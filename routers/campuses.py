"""Campus setup and staged, reviewed assessment preparation."""
from app.institution_context import endpoint
from app.institution_errors import fail
from routers.institutions import prepare
from services.institution import campus_service as service
from services.institution.base import public


@endpoint("POST", "/institutions/{iid}/campuses", model="CampusCreate", status=201, prepare=prepare("institutions", "iid", "W"))
async def create_campus(ctx, body):
    return await service.create(ctx, body)


@endpoint("GET", "/institutions/{iid}/campuses", prepare=prepare("institutions", "iid"), query=("snapshot_id",))
async def list_campuses(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_campuses, released_snapshot
        snapshots = [await released_snapshot(ctx)] if ctx.request.query_params.get("snapshot_id") else await ctx.repo.rows("SELECT s.frozen_input FROM institution_calculation_snapshots s JOIN institution_audits a ON a.reviewed_snapshot_id=s.id WHERE a.institution_id=:iid AND a.business_status IN ('FINALIZED','SUPERSEDED') ORDER BY a.period_end DESC,a.revision DESC,a.id DESC", {"iid": ctx.iid})
        campuses = {}
        for snapshot in snapshots:
            for row in released_campuses(snapshot["frozen_input"]):
                if row["id"] not in campuses:
                    campuses[row["id"]] = row
        return await ctx.page_rows(list(campuses.values()))
    async def serialize(row):
        return await service.resource(ctx, row)
    return await ctx.page("campuses", {"institution_id": ctx.iid}, serialize)


@endpoint("GET", "/campuses/{cid}", prepare=prepare("campuses", "cid"), query=("snapshot_id",))
async def get_campus(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_resource
        return await released_resource(ctx, "campus", ctx.parent["id"])
    return await service.resource(ctx, ctx.parent)


@endpoint("PATCH", "/campuses/{cid}", model="CampusPatch", prepare=prepare("campuses", "cid", "W"))
async def patch_campus(ctx, body):
    return await service.patch(ctx, body)


@endpoint("POST", "/campuses/{cid}/boundaries", model="BoundaryCreate", status=201, prepare=prepare("campuses", "cid", "W"))
async def add_boundary(ctx, body):
    return await service.boundary(ctx, body)


@endpoint("GET", "/campuses/{cid}/boundaries", prepare=prepare("campuses", "cid"), query=("snapshot_id",))
async def boundaries(ctx, _):
    async def serialize(row):
        return public("BoundaryResource", row, overlap_flags=[])
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot, released_resource
        snapshot = await released_snapshot(ctx)
        rows = [link["attribution_snapshot"]["boundary"] for link in snapshot["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"])]
        return await ctx.page_rows(rows, lambda row: released_resource(ctx, "boundary", row["id"]))
    return await ctx.page("campus_boundaries", {"campus_id": ctx.parent["id"]}, serialize)


@endpoint("POST", "/campuses/{cid}/vegetation-assessments", model="MaskCreate", status=202, prepare=prepare("campuses", "cid", "W"))
async def request_mask(ctx, body):
    return await service.request_mask(ctx, body)


@endpoint("GET", "/campuses/{cid}/vegetation-assessments", prepare=prepare("campuses", "cid"), query=("boundary_id", "snapshot_id"))
async def masks(ctx, _):
    async def serialize(row):
        return public("MaskResource", row, quality_flags=row["coverage"].get("quality_flags", []))
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot, released_resource
        snapshot = await released_snapshot(ctx)
        rows = [link["attribution_snapshot"]["mask"] for link in snapshot["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"])]
        if ctx.request.query_params.get("boundary_id"):
            rows = [row for row in rows if str(row["boundary_id"]) == ctx.request.query_params["boundary_id"]]
        return await ctx.page_rows(rows, lambda row: released_resource(ctx, "mask", row["id"]))
    filters = {"campus_id": ctx.parent["id"]}
    if ctx.request.query_params.get("boundary_id"):
        filters["boundary_id"] = ctx.request.query_params["boundary_id"]
    return await ctx.page("campus_vegetation_masks", filters, serialize)


@endpoint("POST", "/campuses/{cid}/sampling-plots/generate", model="PlotGenerate", status=202, prepare=prepare("campuses", "cid", "W"))
async def generate_plots(ctx, body):
    return await service.request_plots(ctx, body)


@endpoint("GET", "/campuses/{cid}/sampling-plots", prepare=prepare("campuses", "cid"), query=("plot_set_id", "snapshot_id"))
async def get_plots(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot
        snapshot = await released_snapshot(ctx)
        rows = [row for link in snapshot["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"]) for row in link["attribution_snapshot"]["plots"]]
        if ctx.request.query_params.get("plot_set_id"):
            rows = [row for row in rows if str(row["plot_set_id"]) == ctx.request.query_params["plot_set_id"]]
        return {**await ctx.page_rows(rows, lambda row: public("PlotResource", row)), "plot_set_id": rows[0]["plot_set_id"] if rows else None}
    pid = ctx.request.query_params.get("plot_set_id") or ctx.parent["current_plot_set_id"]
    if not pid:
        fail(409, "PLOTS_NOT_READY", "No approved plot set is available.")
    group = await ctx.repo.get("campus_plot_sets", pid)
    if str(group["campus_id"]) != str(ctx.parent["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Plot set is unavailable.")
    async def serialize(row):
        return public("PlotResource", row)
    page = await ctx.page("campus_sampling_plots", {"plot_set_id": pid}, serialize)
    return {**page, "plot_set_id": str(pid)}


@endpoint("GET", "/campuses/{cid}/assessment-config", prepare=prepare("campuses", "cid", "W"), query=("method_version", "model_version", "survey_id"))
async def assessment_config(ctx, _):
    return await service.assessment_config(ctx, ctx.parent)
