# TerraTrust-AR Backend Setup Guide

## Prerequisites

- Python 3.11+
- Redis running locally or as a managed instance
- Node.js 22.11+ LTS for contract tooling
- Supabase with PostgreSQL 15 and PostGIS enabled
- Firebase project with Phone Auth enabled
- Separate Google service account JSON files for Firebase Admin, Cloud Vision, and Earth Engine
- Pinata account for IPFS pinning
- Polygon Amoy admin wallet funded with test POL

## 1. Create and activate a virtual environment

Windows:

```bash
python -m venv venv
venv\Scripts\activate
```

macOS / Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

## 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Install the Chromium browser used by the Layer 2 Playwright boundary scraper:

```bash
playwright install chromium
```

## 3. Configure environment variables

Create `.env` from `.env.example` and fill every required variable.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS / Linux:

```bash
cp .env.example .env
```

Important variables:

| Variable | Description |
|---|---|
| `FIREBASE_PROJECT_ID` | Firebase project ID used by the mobile app |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `DATABASE_URL` | Direct PostgreSQL connection string used for PostGIS geometry queries |
| `FIREBASE_CREDENTIALS_PATH` | Path to the Firebase Admin service account JSON file |
| `GEE_PROJECT_ID` | Google Cloud project ID enabled for Earth Engine |
| `GEE_CREDENTIALS_PATH` | Path to the Earth Engine service account JSON file |
| `CLOUD_VISION_CREDENTIALS_PATH` | Path to the Cloud Vision service account JSON file |
| `GOOGLE_CLOUD_PROJECT` | Legacy fallback for older shared-credential setups |
| `GOOGLE_APPLICATION_CREDENTIALS` | Legacy fallback for older shared-credential setups |
| `NASA_EARTHDATA_USERNAME` | Earthdata username for ASF access |
| `NASA_EARTHDATA_PASSWORD` | Earthdata password for ASF access |
| `ADMIN_WALLET_PRIVATE_KEY` | Polygon Amoy admin private key |
| `ADMIN_WALLET_ADDRESS` | Polygon Amoy admin public wallet |
| `ALCHEMY_POLYGON_AMOY_URL` | Alchemy Polygon Amoy RPC URL |
| `CONTRACT_ADDRESS` | Deployed ERC-1155 contract address |
| `PINATA_JWT` | Pinata JWT used for metadata pinning |
| `PINATA_GATEWAY_URL` | Pinata gateway domain used for certificate URLs |
| `POLYGONSCAN_API_KEY` | PolygonScan API key for verification |
| `LGD_API_BASE` | Government LGD API base URL |
| `REDIS_URL` | Redis broker/result backend URL |

## 4. Configure Google secret files

Provide three separate JSON key files and point each environment variable at the correct file path:

- `FIREBASE_CREDENTIALS_PATH`
- `GEE_CREDENTIALS_PATH`
- `CLOUD_VISION_CREDENTIALS_PATH`

For local development you can keep them anywhere under `backend/`, for example:

- `backend/secrets/firebase-service-account.json`
- `backend/secrets/gee-service-account.json`
- `backend/secrets/cloud-vision-service-account.json`

For Render, upload each JSON through Secret Files and set the env vars to the mounted file paths that Render provides.

Get `DATABASE_URL` from Supabase:

1. Open Supabase Dashboard.
2. Go to `Project Settings` -> `Database`.
3. Copy the PostgreSQL connection string.
4. Convert it to the async format if needed:
    `postgresql://...` -> `postgresql+asyncpg://...`

`DATABASE_URL` is required for the documented PostGIS-backed land, zone, and tree-scan flows.

`FIREBASE_CREDENTIALS_PATH`, `GEE_CREDENTIALS_PATH`, and `CLOUD_VISION_CREDENTIALS_PATH` are loaded independently, so each Google integration can use a different account.

`GEE_PROJECT_ID` should match the project that has Earth Engine enabled.

## 5. Enable PostGIS in Supabase

Run this in the Supabase SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

## 6. Create the documented backend schema

Run the checked-in `schema.sql` file in the Supabase SQL Editor. This file is the canonical backend schema and is kept aligned with the runtime models and database helpers.

Windows PowerShell:

```powershell
Get-Content .\schema.sql -Raw
```

Then paste the output into the Supabase SQL Editor and run it.

## 7. Create required Supabase storage buckets

Create these private buckets:

- `land-documents`
- `evidence-photos`

## 8. Deploy the smart contract

```bash
cd contracts
npm install
npx hardhat compile
npx hardhat run scripts/deploy.js --network polygon_amoy
```

Copy the deployed contract address into `.env` as `CONTRACT_ADDRESS`.

Optional verification:

```bash
npx hardhat verify --network polygon_amoy <CONTRACT_ADDRESS>
```

## 9. Start the API server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## 10. Start the Celery worker

```bash
celery -A tasks.celery_app worker --loglevel=info --concurrency=1
```

Optional Render-style local process orchestration:

```bash
honcho -f Procfile.render start
```

## 11. Run tests

Python tests:

```bash
python -m pytest tests -q
```

Contract tests:

```bash
cd contracts
npx hardhat test
```

## 12. Verify the backend

Open the API docs at http://localhost:8000/docs and confirm these endpoints are present:

- `GET /api/v1/auth/me`
- `POST /api/v1/auth/kyc`
- `POST /api/v1/auth/register-wallet`
- `POST /api/v1/auth/recover-wallet`
- `POST /api/v1/land/verify-document`
- `GET /api/v1/land/fetch-boundary`
- `POST /api/v1/land/fetch-boundary`
- `POST /api/v1/land/register`
- `GET /api/v1/land/list`
- `PATCH /api/v1/land/{land_id}`
- `GET /api/v1/audit/zones`
- `POST /api/v1/audit/submit-samples`
- `GET /api/v1/audit/result/{audit_id}`
- `GET /api/v1/audit/history/{land_id}`
- `GET /api/v1/credits/balance`
- `GET /health`
- `GET /api/v1/status`
