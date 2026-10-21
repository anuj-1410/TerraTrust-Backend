ALTER TABLE users ADD COLUMN account_domain TEXT NOT NULL DEFAULT 'FARMER'
    CHECK(account_domain IN ('FARMER','INSTITUTION'));
ALTER TABLE users ADD COLUMN institution_onboarding_status TEXT
    CHECK(institution_onboarding_status IN ('REQUIRED','PENDING_REVIEW','REJECTED','COMPLETE'));
ALTER TABLE users ADD COLUMN institution_onboarding_completed_at TIMESTAMPTZ;
-- The inspected farmer schema has nullable Aadhaar/wallet and no role CHECK.
-- Preserve all current farmer IDs, roles and validation. Institution identity uses
-- INSTITUTION_ADMIN without KYC or wallet. No blanket role rewrite/backfill.
CREATE FUNCTION tt_domain_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.account_domain IS DISTINCT FROM NEW.account_domain THEN
  RAISE EXCEPTION 'Account domain is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER tt_users_domain BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION tt_domain_immutable();
