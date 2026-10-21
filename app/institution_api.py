"""Mount the isolated institution API without changing farmer routing/lifespan."""
from app.institution_errors import InstitutionError, error_handler


def install(app):
    from routers import institutions, campuses, campus_trees, institution_audits, institution_evidence, institution_admin, institution_registries, institution_reports, institution_advisory, institution_jobs, institution_privacy
    from app.institution_context import router
    from demo.institution.sessions import router as sessions
    # Literal routes (imports/template, etc.) precede UUID captures at each depth.
    router.routes.sort(key=lambda route: (sum("{" in segment for segment in route.path.split("/")), -len(route.path.split("/")), route.path))
    app.add_exception_handler(InstitutionError, error_handler)
    app.include_router(router, prefix="/api/v1")
    from app.config import settings
    if settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS and settings.DEPLOYMENT_ENV in {"development","test"}:
        app.include_router(sessions, prefix="/api/v1/demo")
