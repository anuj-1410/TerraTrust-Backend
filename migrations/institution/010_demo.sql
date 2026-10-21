-- Control records only. No demo users, fake results, approvals or grants are seeded.
CREATE TABLE demo_checkpoint_registry (
 account_number integer PRIMARY KEY CHECK(account_number BETWEEN 5 AND 8),
 firebase_uid text UNIQUE NOT NULL,
 domain text NOT NULL CHECK(domain='INSTITUTION'),
 checkpoint_key text NOT NULL,
 reset_policy text NOT NULL CHECK(reset_policy='NEXT_LOGIN'),
 owned_tenant_ids uuid[] NOT NULL DEFAULT '{}',
 manifest_version integer NOT NULL CHECK(manifest_version>0),
 source_manifest_hash text NOT NULL CHECK(source_manifest_hash ~ '^[0-9a-f]{64}$'),
 source_manifest jsonb NOT NULL,
 current_generation integer NOT NULL DEFAULT 0,
 state text NOT NULL CHECK(state IN ('NOT_READY','READY','RESTORING','FAILED')),
 last_session_id uuid,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE demo_login_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 firebase_uid text NOT NULL REFERENCES demo_checkpoint_registry(firebase_uid) ON DELETE RESTRICT,
 login_session_id uuid NOT NULL,
 request_hash text NOT NULL,
 checkpoint_key text NOT NULL,
 generation integer NOT NULL,
 auth_time bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('RESTORING','READY','FAILED')),
 reset_performed boolean NOT NULL DEFAULT false,
 safe_error jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz,
 UNIQUE(firebase_uid,login_session_id)
);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON demo_checkpoint_registry,demo_login_sessions FROM anon; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON demo_checkpoint_registry,demo_login_sessions FROM authenticated; END IF;
END $$;
