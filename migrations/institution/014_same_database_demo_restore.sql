-- Same existing DATABASE_URL/login; bypass only a current resettable demo scope.
-- No roles, passwords, databases or demo identities are created here.
CREATE FUNCTION tt_demo_restore_allowed(table_name text,row_data jsonb)
RETURNS boolean LANGUAGE plpgsql SET search_path=public,pg_temp AS $scope_guard$
DECLARE session_text text; registered record; tenant_text text;
BEGIN
 session_text:=current_setting('terratrust.demo_restore_session',true);
 IF session_text IS NULL OR session_text !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN RETURN false; END IF;
 SELECT r.*,u.id AS target_user_id INTO registered
 FROM demo_checkpoint_registry r
 JOIN demo_login_sessions s ON s.firebase_uid=r.firebase_uid
 JOIN users u ON u.firebase_uid=r.firebase_uid AND u.account_domain='INSTITUTION'
 WHERE s.id=session_text::uuid AND s.id=r.last_session_id
 AND s.status='RESTORING' AND r.state='RESTORING'
 AND s.generation=r.current_generation AND r.account_number BETWEEN 5 AND 8
 AND r.reset_policy='NEXT_LOGIN' AND r.domain='INSTITUTION'
 AND u.id::text=r.source_manifest->>'target_user_id'
 AND r.firebase_uid=r.source_manifest->>'target_firebase_uid'
 AND u.phone_number='+91900000000'||r.account_number::text
 AND r.source_manifest_hash=r.source_manifest->>'manifest_sha256';
 IF NOT FOUND THEN RETURN false; END IF;
 IF table_name='users' THEN
  RETURN row_data->>'id'=registered.target_user_id::text AND row_data->>'account_domain'='INSTITUTION';
 END IF;
 IF table_name NOT IN ('institution_users','institution_registrations','institution_calculation_snapshots','institution_calculation_lines','institution_audit_results','campus_tree_events','audit_events','campus_tree_surveys','institution_audits','campus_tree_audits','institution_audit_evidence','campus_tree_measurements','institution_emission_inputs','institution_mitigation_inputs','institution_audit_tree_links') THEN RETURN false; END IF;
 tenant_text:=row_data->>'institution_id';
 IF tenant_text IS NOT NULL THEN
  RETURN EXISTS(SELECT 1 FROM unnest(registered.owned_tenant_ids) AS owned(id) WHERE owned.id::text=tenant_text);
 END IF;
 RETURN table_name='institution_audit_evidence'
  AND row_data->>'parent_type'='REGISTRATION_DRAFT'
  AND row_data->>'parent_id'=registered.target_user_id::text
  AND row_data->>'owner_user_id'=registered.target_user_id::text;
END $scope_guard$;
REVOKE ALL ON FUNCTION tt_demo_restore_allowed(text,jsonb) FROM PUBLIC;
CREATE OR REPLACE FUNCTION tt_last_owner() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE iid uuid;
BEGIN
 IF tt_demo_restore_allowed(TG_TABLE_NAME,CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END)
 AND (TG_OP<>'UPDATE' OR tt_demo_restore_allowed(TG_TABLE_NAME,to_jsonb(OLD))) THEN
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

CREATE OR REPLACE FUNCTION tt_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_data jsonb; new_data jsonb; frozen boolean; allowed text[];
BEGIN
 IF tt_demo_restore_allowed(TG_TABLE_NAME,CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END)
 AND (TG_OP<>'UPDATE' OR tt_demo_restore_allowed(TG_TABLE_NAME,to_jsonb(OLD))) THEN
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

CREATE OR REPLACE FUNCTION tt_method_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pid uuid; source_status text;
BEGIN

 IF TG_OP='UPDATE' AND OLD.methodology_id IS DISTINCT FROM NEW.methodology_id THEN
  RAISE EXCEPTION 'Immutable method parent' USING ERRCODE='23514';
 END IF;
 pid:=CASE WHEN TG_OP='DELETE' THEN OLD.methodology_id ELSE NEW.methodology_id END;
 SELECT status INTO source_status FROM carbon_methodologies WHERE id=pid FOR SHARE;
 IF source_status IS DISTINCT FROM 'DRAFT' THEN RAISE EXCEPTION 'Published method child is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

CREATE OR REPLACE FUNCTION tt_institution_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE row_data jsonb; parent_status text; pid uuid;
BEGIN
 IF tt_demo_restore_allowed(TG_TABLE_NAME,CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END)
 AND (TG_OP<>'UPDATE' OR tt_demo_restore_allowed(TG_TABLE_NAME,to_jsonb(OLD))) THEN
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
