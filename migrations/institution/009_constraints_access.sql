-- Additional relational and immutable-history guarantees.
CREATE UNIQUE INDEX tt_member_identity ON institution_users(institution_id,user_id);
CREATE UNIQUE INDEX tt_invite_token ON institution_invitations(token_hash);
CREATE UNIQUE INDEX tt_invite_pending ON institution_invitations(institution_id,target_phone_e164) WHERE status='PENDING';
CREATE UNIQUE INDEX tt_tree_tag ON campus_trees(campus_id,stable_tag);
CREATE UNIQUE INDEX tt_measurement_uuid ON campus_tree_measurements(institution_id,local_uuid);
CREATE UNIQUE INDEX tt_survey_tree ON campus_tree_measurements(survey_id,tree_id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX tt_registration_revision ON institution_registrations(logical_registration_id,revision);
CREATE UNIQUE INDEX tt_registration_working ON institution_registrations(logical_registration_id) WHERE status IN ('DRAFT','SUBMITTED','UNDER_REVIEW');
CREATE UNIQUE INDEX tt_survey_revision ON campus_tree_surveys(logical_survey_id,revision);
CREATE UNIQUE INDEX tt_survey_working ON campus_tree_surveys(logical_survey_id) WHERE status IN ('DRAFT','SUBMITTED','UNDER_REVIEW');
CREATE UNIQUE INDEX tt_audit_revision ON institution_audits(logical_audit_id,revision);
CREATE UNIQUE INDEX tt_audit_working ON institution_audits(logical_audit_id) WHERE business_status IN ('DRAFT','SUBMITTED','UNDER_REVIEW');
CREATE UNIQUE INDEX tt_audit_final ON institution_audits(logical_audit_id) WHERE business_status='FINALIZED';
ALTER TABLE institution_audit_periods ADD CONSTRAINT tt_period_overlap EXCLUDE USING gist(institution_id WITH =,period_range WITH &&);
ALTER TABLE institution_audit_periods ADD CHECK(period_end>=period_start);
CREATE UNIQUE INDEX tt_tree_link ON institution_audit_tree_links(audit_id,campus_tree_audit_id);
CREATE UNIQUE INDEX tt_tree_link_campus ON institution_audit_tree_links(audit_id,campus_id);
CREATE UNIQUE INDEX tt_final_audit ON institution_audit_results(audit_id);
CREATE UNIQUE INDEX tt_final_snapshot ON institution_audit_results(snapshot_id);
CREATE UNIQUE INDEX tt_snapshot ON institution_calculation_snapshots(audit_id,input_hash,result_hash);
CREATE UNIQUE INDEX tt_import_row ON institution_import_rows(batch_id,row_number);
CREATE UNIQUE INDEX tt_evidence_link ON institution_evidence_links(evidence_id,parent_type,parent_id);
CREATE UNIQUE INDEX tt_review_case ON review_cases(resource_type,resource_id,revision,input_hash);
CREATE UNIQUE INDEX tt_outbox_job ON job_outbox(job_id);
CREATE UNIQUE INDEX tt_job_active ON processing_jobs(domain,resource_id,job_type,input_hash,COALESCE(fixture_generation,0)) WHERE status IN ('QUEUED','RUNNING');
CREATE UNIQUE INDEX tt_receipt_scope ON idempotency_records(COALESCE(institution_id,'00000000-0000-0000-0000-000000000000'::uuid),user_id,http_method,normalized_route,idempotency_key,COALESCE(fixture_generation,0));
CREATE UNIQUE INDEX tt_feedback ON advisory_feedback(query_id,user_id);
CREATE UNIQUE INDEX tt_notification ON institution_notifications(recipient_user_id,event_id);
CREATE UNIQUE INDEX tt_privacy_open ON institution_deletion_requests(user_id) WHERE status IN ('REQUESTED','NEEDS_ACTION','APPROVED','PROCESSING','FAILED_RETRYABLE');
CREATE INDEX tt_job_lease ON processing_jobs(status,lease_expires_at,deadline_at);
CREATE INDEX tt_outbox_pending ON job_outbox(status,next_attempt_at);
CREATE INDEX tt_lexical ON knowledge_chunks USING gin(lexical_search_vector);
ALTER TABLE knowledge_embeddings ADD CHECK(dimension>0 AND vector_dims(embedding)=dimension);
CREATE UNIQUE INDEX tt_embedding ON knowledge_embeddings(chunk_id,corpus_version,embedding_version);

CREATE FUNCTION tt_last_owner() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE iid uuid;
BEGIN
 IF session_user='tt_institution_demo_restore' AND current_setting('terratrust.demo_restore',true)='on' THEN
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 iid := OLD.institution_id;
 PERFORM id FROM institutions WHERE id=iid FOR UPDATE;
 IF OLD.status='ACTIVE' AND OLD.permission='OWNER' AND
    (TG_OP='DELETE' OR NEW.status<>'ACTIVE' OR NEW.permission<>'OWNER') AND
    NOT EXISTS(SELECT 1 FROM institution_users WHERE institution_id=iid AND id<>OLD.id AND status='ACTIVE' AND permission='OWNER') THEN
  RAISE EXCEPTION 'LAST_OWNER_REQUIRED' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER tt_owner_guard BEFORE UPDATE OR DELETE ON institution_users FOR EACH ROW EXECUTE FUNCTION tt_last_owner();

CREATE FUNCTION tt_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_data jsonb; new_data jsonb; frozen boolean; allowed text[];
BEGIN
 IF session_user='tt_institution_demo_restore' AND current_setting('terratrust.demo_restore',true)='on' THEN
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 old_data:=to_jsonb(OLD);
 frozen:=TG_TABLE_NAME IN ('institution_calculation_snapshots','institution_calculation_lines','institution_audit_results','campus_tree_events','audit_events')
   OR (TG_TABLE_NAME IN ('emission_factors','carbon_methodologies','model_registry','knowledge_documents','institution_deployment_policies') AND old_data->>'status'<>'DRAFT')
   OR (TG_TABLE_NAME IN ('institution_registrations','campus_tree_surveys') AND old_data->>'status'<>'DRAFT')
   OR (TG_TABLE_NAME='institution_audits' AND old_data->>'business_status'<>'DRAFT')
   OR (TG_TABLE_NAME='campus_tree_audits' AND old_data->>'processing_status'='SUCCEEDED')
   OR (TG_TABLE_NAME='institution_audit_evidence' AND old_data->>'state'='READY');
 IF NOT frozen THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Immutable history' USING ERRCODE='23514'; END IF;
 allowed:=ARRAY['updated_at','status','business_status','processing_status','review_status','reviewed_by','reviewed_at','review_reason','latest_review_reason','reviewed_snapshot_id','preview_snapshot_id','current_result_id','finalized_by','finalized_at','error','reviewer_id','validated_at','superseded_by','bytes_deleted_at','purge_policy_version'];
 IF (old_data-allowed) IS DISTINCT FROM (to_jsonb(NEW)-allowed) THEN
  RAISE EXCEPTION 'Immutable business content' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['institution_calculation_snapshots','institution_calculation_lines','institution_audit_results','campus_tree_events','audit_events','emission_factors','carbon_methodologies','model_registry','knowledge_documents','institution_deployment_policies','institution_registrations','campus_tree_surveys','institution_audits','campus_tree_audits','institution_audit_evidence'] LOOP
  EXECUTE format('CREATE TRIGGER tt_freeze BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION tt_frozen_guard()',t);
 END LOOP;
END $$;
DO $$ DECLARE t text; BEGIN
 FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename NOT IN ('users','land_parcels','carbon_audits','sampling_zones','ar_tree_scans','wallet_recovery_requests','blockchain_transactions') AND (tablename LIKE 'institution_%' OR tablename LIKE 'campus_%' OR tablename IN ('processing_jobs','job_outbox','audit_events','idempotency_records','review_cases','operator_grants','emission_factors','carbon_methodologies','model_registry','method_parameters','method_quality_policies') OR tablename LIKE 'advisory_%' OR tablename LIKE 'knowledge_%') LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN EXECUTE format('REVOKE ALL ON %I FROM anon',t); END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN EXECUTE format('REVOKE ALL ON %I FROM authenticated',t); END IF;
 END LOOP;
END $$;
