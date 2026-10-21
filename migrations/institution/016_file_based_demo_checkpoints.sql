-- Forward correction: ordinary business tables hold all demo business data.
-- Existing installations must export registered checkpoints before this phase.
-- The backend supplies this transaction-local UID only after Firebase verification.
CREATE OR REPLACE FUNCTION tt_demo_restore_allowed(table_name text,row_data jsonb)
RETURNS boolean LANGUAGE plpgsql SET search_path=public,pg_temp AS $scope_guard$
DECLARE configured_uid text; target record; tenant_text text;
BEGIN
 configured_uid:=current_setting('terratrust.demo_restore_uid',true);
 IF configured_uid IS NULL OR configured_uid='' THEN RETURN false; END IF;
 SELECT id INTO target FROM users
 WHERE firebase_uid=configured_uid AND account_domain='INSTITUTION'
 AND role='INSTITUTION_ADMIN'
 AND phone_number IN ('+919000000005','+919000000006','+919000000007','+919000000008');
 IF NOT FOUND THEN RETURN false; END IF;
 IF table_name='users' THEN
  RETURN row_data->>'id'=target.id::text AND row_data->>'account_domain'='INSTITUTION';
 END IF;
 IF table_name NOT IN ('institution_users','institution_registrations','institution_calculation_snapshots','institution_calculation_lines','institution_audit_results','campus_tree_events','audit_events','campus_tree_surveys','institution_audits','campus_tree_audits','institution_audit_evidence','campus_tree_measurements','institution_emission_inputs','institution_mitigation_inputs','institution_audit_tree_links') THEN RETURN false; END IF;
 tenant_text:=row_data->>'institution_id';
 IF tenant_text IS NOT NULL THEN
  RETURN EXISTS(SELECT 1 FROM institutions i WHERE i.id::text=tenant_text AND i.created_by=target.id)
   AND NOT EXISTS(SELECT 1 FROM institution_users m WHERE m.institution_id::text=tenant_text AND m.user_id<>target.id);
 END IF;
 RETURN table_name='institution_audit_evidence'
  AND row_data->>'parent_type'='REGISTRATION_DRAFT'
  AND row_data->>'parent_id'=target.id::text
  AND row_data->>'owner_user_id'=target.id::text;
END $scope_guard$;
REVOKE ALL ON FUNCTION tt_demo_restore_allowed(text,jsonb) FROM PUBLIC;

-- No CASCADE: stop if there is an unexpected dependency. No business rows touched.
DROP TABLE IF EXISTS public.demo_login_sessions;
DROP TABLE IF EXISTS public.demo_checkpoint_registry;
