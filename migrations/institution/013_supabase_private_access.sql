-- Backend-only institution tables: close default public/Data API grants.
-- Existing farmer tables, their policies and schema-wide defaults are untouched.
DO $institution_acl$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY[
 'institutions','institution_users','institution_invitations','institution_registration_authorizations','institution_registrations',
 'campuses','campus_boundaries','campus_vegetation_masks','campus_plot_sets','campus_sampling_plots','campus_trees','campus_tree_events','campus_tree_surveys','campus_tree_measurements','campus_tree_audits',
 'institution_audit_periods','institution_audits','institution_audit_tree_links','institution_emission_inputs','institution_mitigation_inputs','institution_calculation_snapshots','institution_calculation_lines','institution_audit_results','institution_import_batches','institution_import_rows','institution_exports','institution_artifacts',
 'institution_audit_evidence','institution_evidence_links','advisory_queries','advisory_retrievals','advisory_recommendations','advisory_scenarios','advisory_feedback',
 'operator_grants','review_cases','processing_jobs','job_outbox','audit_events','idempotency_records','institution_notifications','institution_deletion_requests','institution_provider_operations',
 'emission_factors','carbon_methodologies','model_registry','method_parameters','method_quality_policies','knowledge_documents','knowledge_chunks','knowledge_corpus_versions','knowledge_embeddings','institution_deployment_policies',
 'demo_checkpoint_registry','demo_login_sessions','institution_schema_migrations'] LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t);
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN EXECUTE format('REVOKE ALL ON public.%I FROM anon',t); END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN EXECUTE format('REVOKE ALL ON public.%I FROM authenticated',t); END IF;
 END LOOP;
END $institution_acl$;
REVOKE ALL ON FUNCTION tt_last_owner(),tt_frozen_guard(),tt_method_child_guard(),tt_institution_child_guard() FROM PUBLIC;
