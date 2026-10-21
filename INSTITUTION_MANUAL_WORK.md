# Institution setup

Supabase and Firebase are already configured. **Do not rerun `schema.sql` or edit applied migrations.** Keep your existing database, Firebase, GEE, NISAR and Redis credentials. Farmer and institution accounts use the same database.

## Render: fix database certificate verification

1. Supabase > your existing project > **Database Settings > SSL Configuration > Download Certificate**. Download the **server root certificate**. Keep SSL enforcement unchanged.
2. Open the downloaded certificate in a text editor; copy its entire PEM contents, including `BEGIN CERTIFICATE` and `END CERTIFICATE`.
3. Render > backend service > **Environment > Secret Files > Add Secret File**: filename `supabase-ca.crt`; paste the certificate contents.
4. Under **Environment Variables**, set `DATABASE_SSL_CA_FILE=/etc/secrets/supabase-ca.crt`.
5. Add the same file/value to any separately deployed institution workers that connect to this database. Keep the existing `DATABASE_URL` and credentials.
6. Deploy the updated backend code and save/deploy the Render environment changes. Keep start command `uv run honcho -f Procfile.render start`.
7. Confirm logs show **Direct PostgreSQL/PostGIS connection verified** and **Application startup complete**, then open `https://YOUR_SERVICE.onrender.com/health`.

The provided logs show a TLS trust failure during API startup, not a Node build or port configuration failure. No SQL or database schema change is needed. Official steps: [Supabase certificate verification](https://supabase.com/docs/guides/database/connecting-to-postgres), [Render secret files](https://render.com/docs/configure-environment-variables#secret-files).

## 0. Correct the previous demo-table setup

Migration files record schema history; their count is not a count of demo tables. Keep applied files/checksums unchanged. Migration 016 removes the only two demo control tables and creates no tables. No further demo migration is needed for the in-memory reset implementation.

1. Stop the local API and institution workers. Keep your normal database backup.
2. In backend PowerShell, export any already registered checkpoints **before** removing their control tables:

```powershell
$env:ENVIRONMENT = "development"
$env:INSTITUTION_SCHEMA_ENABLED = "true"
$env:ENABLE_INSTITUTION_DEMO_ACCOUNTS = "true"
.venv-review\Scripts\python.exe -m demo.institution.checkpoint_cli export-legacy --actor-firebase-uid OPERATOR_UID
```

3. Replace `OPERATOR_UID` with your registration-operations operator's Firebase UID. Expected: `EXPORTED N private local checkpoints; database unchanged.` If it reports zero, no checkpoints had been registered; prepare them using section 6. Stop on any export error.
4. Open [.institution-private/supabase/institution_demo_cleanup.sql](.institution-private/supabase/institution_demo_cleanup.sql). Copy **all** its SQL into **Supabase > SQL Editor > New query > Run**. This applies forward migration **016** and removes only `demo_login_sessions` and `demo_checkpoint_registry`; it retains business data, Firebase accounts and earlier migration checksums.
5. Confirm both table names are absent in Table Editor and migration version **16** appears in `institution_schema_migrations`.
6. Back up `.institution-private/demo/` with the invitation key. Install the same checkpoint files on every API instance. Restart the updated backend using one API process for local demo testing, like the farmer demo middleware. Demo login markers are cleared when that process restarts. Both demo flags remain `false` in production.

## 1. Prepare the backend

1. Open PowerShell in `C:\anujDrive\TerraTrust\backend`.
2. Use the installed Python:

```powershell
$ttPython = ".venv-review\Scripts\python.exe"
& $ttPython --version
```

3. If dependencies need installing, run:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-review"
uv sync --frozen --python $ttPython
```

4. Preserve `.env`; add missing keys from `.env.example`. Do not overwrite it.

| Variable | Enter |
|---|---|
| `ENVIRONMENT` | `development` for laptop testing; `production` on the deployed backend |
| `INSTITUTION_SCHEMA_ENABLED` | `true` after migrations 001–016 are installed |
| `ENABLE_INSTITUTION_FLOW` | `false` during setup; `true` after checks pass |
| `ENABLE_INSTITUTION_ADVISOR` | `false` unless you complete section 5 |
| `ENABLE_INSTITUTION_DEMO_ACCOUNTS` | `true` only for local demo testing; `false` in production |
| `ENABLE_FARMER_DEMO_ACCOUNTS` | Independent farmer-demo switch; `false` in production |
| `INSTITUTION_PRIVACY_POLICY_URL` | Your published HTTPS privacy page |
| `INSTITUTION_SUPPORT_URL` | Your published HTTPS support page |
| `INSTITUTION_ACCOUNT_DELETION_URL` | Your published HTTPS account-deletion page |
| `INSTITUTION_REPORT_FONT_PATH` | Path to your licensed Unicode `.ttf` font; install on export workers too |
| `INSTITUTION_REPORT_FONT_SHA256` | Font checksum from the command below |

```powershell
(Get-FileHash -LiteralPath 'C:\YOUR_FOLDER\report.ttf' -Algorithm SHA256).Hash.ToLower()
```

5. Create the invitation encryption key **once**; skip if `.institution-private/receipt.key` already exists:

```powershell
& $ttPython -m scripts.institution.create_receipt_key
```

6. Back up that key privately. Every API instance must use the same file. Do not regenerate it during deployment.

## 2. Confirm Supabase and calculation values

1. Supabase > **SQL Editor > New query**: run this read-only check:

```sql
SELECT version FROM public.institution_schema_migrations ORDER BY version;
SELECT id, public FROM storage.buckets
WHERE id IN ('institution-evidence', 'institution-reports');
```

2. Confirm versions **1–16** and both buckets with `public=false`.
3. If starter settings/factors have not been applied, review [.institution-private/supabase/institution_settings_and_factors.sql](.institution-private/supabase/institution_settings_and_factors.sql), copy it into a new query and click **Run**. Skip if already applied. It replaces starter capture settings; preserve any custom values first.
4. Starter limits: 100 m² plots; 3 plots per `TREE_AREA`; 5–300 cm DBH at 1.3 m reference height; 1–60 m height; 20 m GPS accuracy; 5 m location tolerance; 1 photo; 366-day observation window; 45-day seasonal tolerance; 50% minimum coverage; 0.01 tCO₂e balance tolerance. Training requires at least **9 eligible tree samples**.
5. In **Table Editor > institution_calculation_settings > CURRENT > config**, fill actual `campus_processing` values before tree processing: reviewed GEE mask asset ID/update time, tree-class value, source date, native resolution, actual stratum geometry, inclusion probability and design weight. Use the supported `REVIEWED_GEE_MASK_ASSET_V1` structure from `/docs` > `MethodConfig`. The supplied JSON leaves this `null`; do not invent site data.
6. In **institution_formula_values**, add the verified electricity factor for your reporting period and applicable waste/water/mitigation factors. Include units, source, geography, dates and `enabled=true`. Current electricity is deliberately disabled; **0.710 kg CO₂/kWh is historical FY2024–25 only**, not a current-year default.
7. Included diesel/petrol/CNG/LPG factors quantify direct CO₂ only. Use blend-specific factors when needed. Sources: [NSE Annexure I](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Industry%20Standards%20Note%20on%20BRSR%20with%20Annexure.pdf), [NITI Annexure E / historical CEA v21](https://www.niti.gov.in/sites/default/files/2026-02/Scenarios-Towards-Viksit-Bharat-and-Net-Zero-Sectoral-Insights-Power.pdf).
8. To change an existing coefficient directly:

```sql
UPDATE public.institution_formula_values
SET value = 2.68,
    source_label = 'NSE BRSR Annexure I; direct combustion CO2 only'
WHERE logical_key = 'TT_STARTER_STATIONARY_DIESEL_GENERATOR';
```

9. Refresh the methodology catalog before creating a new audit. Existing drafts/finalized results retain their selected version. IDs/checksums are automatic; no model/method/corpus UUID environment values are needed.

## 3. Check and start

1. Run offline checks:

```powershell
& $ttPython scripts/institution/run_offline_tests.py
& $ttPython -m scripts.institution.preflight
```

2. Resolve every preflight error, then set `ENABLE_INSTITUTION_FLOW=true` in `.env`.
3. Start the API and institution workers; leave the terminal open:

```powershell
& $ttPython scripts/run_local.py
```

4. Open `http://localhost:8000/docs`. Logs are in `.local-runtime/`.
5. If no existing farmer worker serves this Redis broker, use `scripts/run_local.py --include-farmer-worker` instead. Run only **one institution beat scheduler** against the broker/database.

## 4. Operator permissions

1. Sign in operator/reviewer accounts with verified Firebase phone authentication and call `/auth/me` to create their backend identities.
2. Copy their UIDs from **Firebase Authentication > Users**.
3. Preview each grant; replace uppercase placeholders:

```powershell
& $ttPython -m scripts.institution.bootstrap_operator --firebase-uid TARGET_UID --grantor-firebase-uid GRANTOR_UID --capability REGISTRATION_OPERATIONS --expires-at FUTURE_UTC_TIMESTAMP --reason SETUP --dry-run
```

4. Check the output, then repeat with `--apply` instead of `--dry-run`.
5. Grant required operational capabilities: `REGISTRATION_OPERATIONS`, `PRIVACY_OPERATIONS`, `METHOD_REVIEW`, `FACTOR_REVIEW`; add `KNOWLEDGE_REVIEW` for the advisor. Scope business-review grants to the actual institution/case and use independent reviewers.

## 5. Optional Gemini advisor

1. Keep `ENABLE_INSTITUTION_ADVISOR=false` during preparation.
2. Set `GEMINI_API_KEY` from Google AI Studio; set available generation/embedding model IDs in `LLM_MODEL` and `EMBEDDING_MODEL`, and its supported output size in `EMBEDDING_DIMENSION`.
3. Supabase > **Storage > institution-evidence > knowledge**: upload licensed, searchable public-guidance PDFs.
4. **Table Editor > institution_knowledge_sources > Insert row**: enter `logical_key`, `title`, `publisher`, `object_key=knowledge/YOUR_FILE.pdf`, `license`, `geography_codes`, `topic_codes`, `public_licensed=true`, `enabled=true`.
5. Index all enabled sources:

```powershell
& $ttPython -m scripts.institution.sync_knowledge --firebase-uid OPERATOR_UID
```

6. Wait for its job to show `SUCCEEDED` in `processing_jobs`.
7. Run `& $ttPython -m scripts.institution.preflight --advisor`; test citations and missing-source responses, then set `ENABLE_INSTITUTION_ADVISOR=true` and restart the backend. Reindex after PDF/source/embedding changes.

## 6. Optional institution demos

1. Set `ENVIRONMENT=development` and `ENABLE_INSTITUTION_DEMO_ACCOUNTS=true`.
2. Confirm Firebase phone-test accounts:

| Account | Phone | OTP | Starting state | Next login |
|---|---|---|---|---|
| 5 | +919000000005 | 555555 | Fresh institution | Reset |
| 6 | +919000000006 | 666666 | Approved, no campus | Reset |
| 7 | +919000000007 | 777777 | Campus/mask/plots ready | Reset |
| 8 | +919000000008 | 888888 | Biomass baseline accepted | Reset |
| 9 | +919000000009 | 999999 | Completed 2024/2025 history | Persistent |

3. Provision each account; inspect `--dry-run`, then repeat with `--apply`:

```powershell
& $ttPython -m demo.institution.checkpoint_cli provision-user --account 5 --firebase-uid ACTUAL_UID --actor-firebase-uid OPERATOR_UID --dry-run
```

4. Build each real starting state through normal workflows using [demo.md](demo.md).
5. Capture and validate each completed starting state; replace the account/UID/path for each account:

```powershell
& $ttPython -m demo.institution.checkpoint_cli capture --account 9 --firebase-uid ACTUAL_UID --actor-firebase-uid OPERATOR_UID --manifest-version 1 --output .institution-private/account-9-v1
& $ttPython -m demo.institution.checkpoint_cli validate --manifest .institution-private/account-9-v1/manifest.json --actor-firebase-uid OPERATOR_UID
& $ttPython -m demo.institution.checkpoint_cli register --manifest .institution-private/account-9-v1/manifest.json --actor-firebase-uid OPERATOR_UID --dry-run
```

6. Register with `--apply` after checking the preview. This installs `.institution-private/demo/account-N.json`, without demo database tables. Accounts 5–8 reset during normal `/auth/me` on new sign-in; account 9 persists. Refresh/resume preserves the current session; the API process stores only the sign-in marker.
7. Clear old device queues after a reset. The existing demo-session endpoints/header remain compatible, but an extra handoff/header is not required to trigger the reset. The backend-only mobile patch/checker remain in `frontend-integration/`; apply manually only if still needed, checking for conflicts first.

## 7. Test on your phone using USB

1. Enable **Developer options > USB debugging**, connect the phone and accept its authorization prompt.
2. Keep phone/laptop internet and Redis available. Start the backend using section 3.
3. In another PowerShell terminal:

```powershell
adb devices
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8081 tcp:8081
```

4. Confirm the phone shows `device`; open `http://localhost:8000/health` in phone Chrome.
5. Manually set mobile `.env`: `API_BASE_URL=http://localhost:8000` without `/api/v1`; set its farmer/institution demo flags independently. Keep `AUDIT_SKIP_SPECIES_DETECTION=false` for real capture.
6. In the mobile project, run `npm ci` once, then `npm start`.
7. In another mobile-project terminal, run `npm run android -- --no-packager`.
8. Rebuild after mobile `.env` changes. Repeat `adb reverse` after USB reconnection. No Ubuntu setup is needed.
9. Test farmer login/land/audit/wallet; institution registration/review, campus/tree sampling, biomass calculation, annual audit, export/share; demo resets and account-9 persistence.
10. Stop the local API/workers with **Ctrl+C**. Before production, set `ENVIRONMENT=production`, both demo flags `false`, install the preserved key/font on deployment instances, and enable only features that passed live testing.

Latest offline regression: **72 farmer/shared + 210 institution tests passed (282 total)**, including real in-memory TLS trust/hostname checks. Live database, provider and phone acceptance must be checked in your configured environment.
