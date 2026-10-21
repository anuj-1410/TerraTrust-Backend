-- demo.md supersedes the old four-stage mapping. No identities/data are seeded.
ALTER TABLE demo_checkpoint_registry DROP CONSTRAINT demo_checkpoint_registry_account_number_check;
ALTER TABLE demo_checkpoint_registry DROP CONSTRAINT demo_checkpoint_registry_reset_policy_check;
ALTER TABLE demo_checkpoint_registry ADD CONSTRAINT tt_demo_account_range CHECK(account_number BETWEEN 5 AND 9);
ALTER TABLE demo_checkpoint_registry ADD CONSTRAINT tt_demo_reset_policy CHECK(
 (account_number BETWEEN 5 AND 8 AND reset_policy='NEXT_LOGIN') OR
 (account_number=9 AND reset_policy='PERSISTENT'));
-- Old account 8 was fully completed. It must be recaptured at the new baseline
-- stage before another reset; never reinterpret its historical manifest.
UPDATE demo_checkpoint_registry SET checkpoint_key='BIOMASS_COMPLETE_FORMS_PENDING',
 state='NOT_READY',updated_at=now() WHERE account_number=8 AND checkpoint_key='ACCOUNTING_COMPLETE';
ALTER TABLE demo_checkpoint_registry ADD CONSTRAINT tt_demo_stage_key CHECK(
 (account_number=5 AND checkpoint_key='INSTITUTION_FRESH') OR
 (account_number=6 AND checkpoint_key='INSTITUTION_APPROVED') OR
 (account_number=7 AND checkpoint_key='CAMPUS_READY') OR
 (account_number=8 AND checkpoint_key='BIOMASS_COMPLETE_FORMS_PENDING') OR
 (account_number=9 AND checkpoint_key='ACCOUNTING_COMPLETE_TWO_YEARS'));
