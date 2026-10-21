-- Fresh schemas already contain v4.0 production fields. Refuse corrupt lineage.
CREATE TABLE institution_provider_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 job_id uuid NOT NULL REFERENCES processing_jobs(id) DEFERRABLE INITIALLY DEFERRED,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 operation text NOT NULL CHECK(operation IN ('generateContent','embedContent')),
 model text NOT NULL,
 status text NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','REJECTED','UNKNOWN')),
 response jsonb,
 error_code text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(job_id,request_hash)
);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON institution_provider_operations FROM anon; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON institution_provider_operations FROM authenticated; END IF;
END $$;
-- Identity unlinking is limited by a CHECK to deleted institution accounts.
ALTER TABLE users ADD COLUMN institution_deleted_uid_hash text UNIQUE CHECK(institution_deleted_uid_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE users ALTER COLUMN firebase_uid DROP NOT NULL;
ALTER TABLE users ALTER COLUMN phone_number DROP NOT NULL;
ALTER TABLE users ADD CONSTRAINT tt_identity_presence CHECK(
 (firebase_uid IS NOT NULL AND phone_number IS NOT NULL AND institution_deleted_uid_hash IS NULL)
 OR (account_domain='INSTITUTION' AND institution_deleted_uid_hash IS NOT NULL AND firebase_uid IS NULL AND phone_number IS NULL));
-- BSDD wire fields omitted from its physical card; keep actual stock/change
-- separate and nullable. Never infer a release/removal from standing stock.
ALTER TABLE campus_tree_audits ADD COLUMN stock_tC numeric(24,12),
 ADD COLUMN signed_change_tCO2 numeric(24,12), ADD COLUMN stock_gain_tCO2 numeric(24,12),
 ADD COLUMN stock_loss_tCO2 numeric(24,12), ADD COLUMN removal_tCO2 numeric(24,12),
 ADD COLUMN release_tCO2 numeric(24,12);
CREATE UNIQUE INDEX tt_factor_version ON emission_factors(logical_key,version);
CREATE UNIQUE INDEX tt_method_version ON carbon_methodologies(logical_key,version);
CREATE UNIQUE INDEX tt_model_version ON model_registry(logical_key,version);
CREATE UNIQUE INDEX tt_document_version ON knowledge_documents(logical_key,version);
CREATE UNIQUE INDEX tt_policy_version ON institution_deployment_policies(logical_key,version);
ALTER TABLE institution_calculation_snapshots ADD UNIQUE(audit_id,id);
ALTER TABLE institution_audits ADD CONSTRAINT tt_preview_parent FOREIGN KEY(id,preview_snapshot_id) REFERENCES institution_calculation_snapshots(audit_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE institution_audits ADD CONSTRAINT tt_reviewed_parent FOREIGN KEY(id,reviewed_snapshot_id) REFERENCES institution_calculation_snapshots(audit_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE institution_audit_results ADD CONSTRAINT tt_result_snapshot_parent FOREIGN KEY(audit_id,snapshot_id) REFERENCES institution_calculation_snapshots(audit_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE institution_exports ADD CONSTRAINT tt_export_snapshot_parent FOREIGN KEY(audit_id,snapshot_id) REFERENCES institution_calculation_snapshots(audit_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE advisory_queries ADD CONSTRAINT tt_advice_snapshot_parent FOREIGN KEY(audit_id,snapshot_id) REFERENCES institution_calculation_snapshots(audit_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_boundaries ADD UNIQUE(campus_id,id);
ALTER TABLE campus_vegetation_masks ADD UNIQUE(campus_id,boundary_id,id);
ALTER TABLE campus_vegetation_masks ADD CONSTRAINT tt_mask_boundary_parent FOREIGN KEY(campus_id,boundary_id) REFERENCES campus_boundaries(campus_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_plot_sets ADD CONSTRAINT tt_plot_mask_parent FOREIGN KEY(campus_id,boundary_id,mask_id) REFERENCES campus_vegetation_masks(campus_id,boundary_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_tree_audits ADD CONSTRAINT tt_tree_mask_parent FOREIGN KEY(campus_id,boundary_id,mask_id) REFERENCES campus_vegetation_masks(campus_id,boundary_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_tree_audits ADD CONSTRAINT tt_stock_baseline FOREIGN KEY(baseline_audit_id) REFERENCES campus_tree_audits(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE institution_exports ADD CONSTRAINT tt_export_artifact FOREIGN KEY(artifact_id) REFERENCES institution_artifacts(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_vegetation_masks ADD CONSTRAINT tt_mask_artifact FOREIGN KEY(map_artifact_id) REFERENCES institution_artifacts(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE campus_tree_audits ADD CONSTRAINT tt_tree_artifact FOREIGN KEY(map_artifact_id) REFERENCES institution_artifacts(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE emission_factors ADD CONSTRAINT tt_factor_source FOREIGN KEY(source_evidence_id) REFERENCES institution_audit_evidence(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE carbon_methodologies ADD CONSTRAINT tt_method_source FOREIGN KEY(source_evidence_id) REFERENCES institution_audit_evidence(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE model_registry ADD CONSTRAINT tt_model_source FOREIGN KEY(source_evidence_id) REFERENCES institution_audit_evidence(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_documents ADD CONSTRAINT tt_document_source FOREIGN KEY(source_evidence_id) REFERENCES institution_audit_evidence(id) DEFERRABLE INITIALLY DEFERRED;
CREATE FUNCTION tt_method_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pid uuid; source_status text;
BEGIN
 IF session_user='tt_institution_demo_restore' AND current_setting('terratrust.demo_restore',true)='on' THEN
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 IF TG_OP='UPDATE' AND OLD.methodology_id IS DISTINCT FROM NEW.methodology_id THEN
  RAISE EXCEPTION 'Immutable method parent' USING ERRCODE='23514';
 END IF;
 pid:=CASE WHEN TG_OP='DELETE' THEN OLD.methodology_id ELSE NEW.methodology_id END;
 SELECT status INTO source_status FROM carbon_methodologies WHERE id=pid FOR SHARE;
 IF source_status IS DISTINCT FROM 'DRAFT' THEN RAISE EXCEPTION 'Published method child is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER tt_parameter_freeze BEFORE INSERT OR UPDATE OR DELETE ON method_parameters FOR EACH ROW EXECUTE FUNCTION tt_method_child_guard();
CREATE TRIGGER tt_policy_freeze BEFORE INSERT OR UPDATE OR DELETE ON method_quality_policies FOR EACH ROW EXECUTE FUNCTION tt_method_child_guard();
CREATE FUNCTION tt_institution_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE row_data jsonb; parent_status text; pid uuid;
BEGIN
 IF session_user='tt_institution_demo_restore' AND current_setting('terratrust.demo_restore',true)='on' THEN
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 row_data:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 IF TG_TABLE_NAME='campus_tree_measurements' THEN
  pid:=(row_data->>'survey_id')::uuid;
  SELECT status INTO parent_status FROM campus_tree_surveys WHERE id=pid FOR SHARE;
 ELSE
  pid:=(row_data->>'audit_id')::uuid;
  SELECT business_status INTO parent_status FROM institution_audits WHERE id=pid FOR SHARE;
 END IF;
 IF parent_status IS DISTINCT FROM 'DRAFT' THEN RAISE EXCEPTION 'Frozen parent input' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (
   (TG_TABLE_NAME='campus_tree_measurements' AND to_jsonb(OLD)->>'survey_id' IS DISTINCT FROM row_data->>'survey_id')
   OR (TG_TABLE_NAME<>'campus_tree_measurements' AND to_jsonb(OLD)->>'audit_id' IS DISTINCT FROM row_data->>'audit_id')
 ) THEN RAISE EXCEPTION 'Immutable input parent' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['institution_emission_inputs','institution_mitigation_inputs','institution_audit_tree_links','campus_tree_measurements'] LOOP
  EXECUTE format('CREATE TRIGGER tt_child_freeze BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION tt_institution_child_guard()',t);
 END LOOP;
END $$;
ALTER TABLE campus_tree_measurements ADD CHECK(raw_input IS NOT NULL AND jsonb_typeof(raw_input)='object');
ALTER TABLE campus_tree_measurements ADD COLUMN last_survey_input_revision integer NOT NULL DEFAULT 1 CHECK(last_survey_input_revision>0);
ALTER TABLE campus_tree_measurements ADD CHECK(ar_tier IN (1,2,3));
ALTER TABLE campus_tree_measurements ADD CHECK(dbh_cm IS NULL OR dbh_cm>0);
ALTER TABLE campus_tree_measurements ADD CHECK(height_m IS NULL OR height_m>0);
ALTER TABLE campus_sampling_plots ADD CHECK(measured_area_ha>0);
ALTER TABLE campus_boundaries ADD CHECK(area_ha>0);
ALTER TABLE institution_emission_inputs ADD CHECK(quantity IS NULL OR quantity>=0);
ALTER TABLE institution_emission_inputs ADD CHECK(allocation_ratio IS NULL OR allocation_ratio BETWEEN 0 AND 1);
ALTER TABLE idempotency_records ADD CHECK(receipt_state IN ('FULL','TOMBSTONE'));
ALTER TABLE idempotency_records ADD CHECK(recovery_kind IN ('RESOURCE_GET','DELETE_ACK','ACK_ONLY','SECRET_REISSUE'));
ALTER TABLE institution_audit_evidence ADD CHECK(state<>'READY' OR (byte_hash IS NOT NULL AND actual_size_bytes>0));
ALTER TABLE institution_artifacts ADD CHECK(status<>'READY' OR (byte_hash IS NOT NULL AND size_bytes>0));
