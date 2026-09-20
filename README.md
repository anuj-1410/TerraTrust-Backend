<div align="center">

# 🌿 TerraTrust-AR

### AI-Powered Spatial Computing System for Autonomous Carbon Credit Verification

*Empowering Indian smallholder agroforestry farmers with zero-cost, tamper-proof carbon credit verification — right from their Android phones.*

---

![React Native](https://img.shields.io/badge/React_Native-CLI-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Python](https://img.shields.io/badge/Python-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Polygon](https://img.shields.io/badge/Polygon-PoS_Blockchain-8247E5?style=for-the-badge&logo=polygon&logoColor=white)
![Google Earth Engine](https://img.shields.io/badge/Google_Earth_Engine-Satellite_Fusion-4285F4?style=for-the-badge&logo=google&logoColor=white)
![ARCore](https://img.shields.io/badge/Google_ARCore-3--Tier_AR-FF6F00?style=for-the-badge&logo=google&logoColor=white)
![License](https://img.shields.io/badge/License-Academic-green?style=for-the-badge)

<br/>

> **B.Tech Final Year Mini Project — Group 2**
> Department of Artificial Intelligence & Cyber Security (AICS)
> Shri Ramdeobaba College of Engineering & Management, Nagpur — May 2026

</div>

---

## 🌍 The Problem We're Solving

India's smallholder agroforestry farmers collectively sequester **millions of tonnes of CO₂** every year — yet they receive **zero economic benefit** from carbon markets. The reason? Traditional carbon credit verification costs tens of thousands of rupees per farm, requires specialist consultants, expensive LiDAR equipment, and months of paperwork.

**TerraTrust-AR eliminates every single one of those barriers.**

A farmer with a ₹8,000 Android phone can now verify their land, scan their trees with AR, and receive cryptographically-verifiable carbon credits — all in under an hour, entirely for free.

---

## ✨ Key Highlights

| 🔬 Science-Grade Accuracy | 🔗 Tamper-Proof Records | 📶 Offline-First | ₹0 Per Farmer |
|:---:|:---:|:---:|:---:|
| Chave et al. (2014) allometric equations + NISAR L-band SAR | ERC-1155 tokens + IPFS evidence on Polygon PoS | Full MMKV persistence; resume interrupted audits | 100% free-tier cloud infrastructure |

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ANDROID MOBILE APP                           │
│   React Native CLI + TypeScript + Kotlin ARCore Modules        │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │Auth/KYC  │  │  Land    │  │  AR Tree │  │   Credits &   │  │
│  │Firebase  │  │  Verify  │  │  Scanner │  │   History     │  │
│  │Phone OTP │  │  OCR+WMS │  │  ARCore  │  │   CTT Wallet  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
└───────┼─────────────┼─────────────┼─────────────────┼──────────┘
        │             │  HTTPS + Firebase ID Token     │
┌───────▼─────────────▼─────────────▼─────────────────▼──────────┐
│                  PYTHON FASTAPI BACKEND                         │
│              (Render.com + Celery + Honcho)                     │
│                                                                  │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│   │  Land Doc    │    │  Satellite   │    │   Blockchain     │  │
│   │  OCR + LGD   │    │  Fusion GEE  │    │   Minting Svc    │  │
│   │  BhuNaksha   │    │  XGBoost ML  │    │   web3.py        │  │
│   └──────────────┘    └──────────────┘    └──────────────────┘  │
└──────────┬──────────────────┬──────────────────┬────────────────┘
           │                  │                  │
    ┌──────▼──────┐   ┌───────▼──────┐   ┌──────▼────────────┐
    │  Supabase   │   │ Google Earth │   │ Polygon PoS +     │
    │ PostgreSQL  │   │    Engine    │   │ IPFS via Pinata   │
    │  + PostGIS  │   │ NISAR·S1·S2  │   │ ERC-1155 Tokens   │
    └─────────────┘   │  GEDI·SRTM  │   └───────────────────┘
                      └─────────────┘
```

---

## 🚀 Four Pillars of TerraTrust-AR

### 🏛️ Pillar 1 — Government Land Verification
OCR-based processing of **7/12 Extract** and **Record of Rights** documents using Google Cloud Vision API, combined with a **three-layer boundary fetching architecture**:

- **Layer 1** — Maharashtra LGD REST API → BhuNaksha WMS official cadastral polygon
- **Layer 2** — Playwright headless browser fallback for other NIC BhuNaksha states
- **Layer 3** — Manual map upload with OpenCV contour georeferencing

Every boundary is validated by PostGIS `ST_IsValid()`, cross-matched against KYC data with 80% fuzzy name matching, and stored in EPSG:4326.

---

### 📡 Pillar 2 — AR Tree Scanning (3-Tier System)

The measurement system adapts to whatever Android device a farmer has:

| Tier | Hardware Required | Method | Expected Error |
|------|-----------------|--------|---------------|
| **Tier 1** | ToF / LiDAR depth sensor | ARCore RAW_DEPTH + RANSAC cylinder fit | ±2–3 cm |
| **Tier 2** | Any ARCore-capable device | Motion-based SLAM depth estimation | ±4–5 cm |
| **Tier 3** | Any Android phone | Manual string circumference (Kotlin bridge) | <1% |

On-device **TensorFlow Lite** model identifies 11 approved agroforestry species (Teak, Bamboo, Eucalyptus, Mango, Neem, etc.) from the camera feed — no internet required.

---

### 🛰️ Pillar 3 — Six-Layer Satellite Fusion Engine

Processed entirely on **Google Earth Engine** — zero data download to backend servers:

```
Feature Stack (12 features in production phase):
┌─────────────────────────────────────────────────────────────┐
│  NISAR L-band SAR (July 2025)  │  HH · HV · HH/HV ratio   │
│  Sentinel-1 C-band SAR         │  VH · VV · VH/VV ratio    │
│  Sentinel-2 Optical            │  NDVI · EVI · Red-Edge    │
│  NASA GEDI LiDAR               │  rh98 canopy height       │
│  SRTM Topographic              │  Elevation · Slope        │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
   XGBoost Gradient Boosted Regression
   (100 trees · lr=0.05 · 70% sample rate)
         │
         ▼
   Per-pixel biomass prediction at 10m resolution
   across entire verified farm boundary
```

**Biomass → Carbon Credits** via the Chave et al. (2014) pantropical allometric equation:

```
AGB = 0.0673 × (ρ × D² × H)^0.976
```
Then: `Carbon Credits = ΔAGB × 0.47 (IPCC fraction) × 3.667 (CO₂ ratio)`

| Satellite Configuration | Estimated Biomass Error |
|---|---|
| Sentinel-1 only | ~35% |
| + Sentinel-2 + GEDI + SRTM (dev phase) | ~12% |
| + NISAR L-band (production, June 2026) | **~5–6%** ✅ |

---

### 🔗 Pillar 4 — Blockchain Tokenisation

```solidity
// ERC-1155 on Polygon PoS (Amoy Testnet → Mainnet)
function mintAudit(
    address farmer,
    uint256 auditId,
    uint256 creditAmount,   // deci-CTT units
    string memory ipfsUri,
    string memory landId,
    uint256 auditYear
) external onlyOwner { ... }
```

- **Carbon Ton Tokens (CTT)** — Fungible ERC-1155 (Token ID: 1), tradable on carbon markets
- **Audit Certificate NFTs** — Non-fungible per-audit proof, IPFS CID stored permanently on-chain
- **Double-minting prevention** — `keccak256(landId + auditYear)` mapping, reverts on duplicate
- **Credit retirement** — `retireCredits()` burns tokens and emits `CreditRetired` event with timestamp

The farmer's private key **never leaves their device** — generated with ethers.js, secured in Android Keystore via `react-native-keychain`.

---

## 📱 Getting Started

### Prerequisites

- Node.js ≥ 18, JDK 17, Android Studio (Hedgehog+)
- ARCore-compatible Android device (API 26+)
- Python 3.11+ for backend

> Complete environment setup: [React Native Environment Guide](https://reactnative.dev/docs/set-up-your-environment)

---

### 📦 Installation

```bash
# Clone the repository
git clone https://github.com/your-org/terratrust-ar.git
cd terratrust-ar

# Install JS dependencies
npm install

# Install iOS CocoaPods (iOS future scope)
bundle install && bundle exec pod install
```

### 🔑 Environment Configuration

Create `android/local.properties`:
```properties
GOOGLE_MAPS_API_KEY=your_maps_key_here
```

Create `.env` in project root:
```env
FIREBASE_PROJECT_ID=your_project_id
ALCHEMY_RPC_KEY=your_alchemy_key
BACKEND_BASE_URL=https://your-render-app.onrender.com
```

---

### ▶️ Running the App

**Step 1 — Start Metro bundler:**
```bash
npm start
```

**Step 2 — Build and run on Android:**
```bash
npm run android
# OR
yarn android
```

> **Hot reload** is enabled via [Fast Refresh](https://reactnative.dev/docs/fast-refresh). Press `R` twice in the emulator to force reload.

---

### 🐍 Backend Setup

```bash
cd backend
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Fill in: SUPABASE_URL, SUPABASE_KEY, FIREBASE_CREDENTIALS_PATH,
#           ADMIN_WALLET_PRIVATE_KEY, CONTRACT_ADDRESS, PINATA_JWT

# Run migrations (Supabase SQL Editor)
# Enable PostGIS: CREATE EXTENSION IF NOT EXISTS postgis;

# Start FastAPI + Celery via Honcho
honcho start
```

Backend runs at `http://localhost:8000` with auto-generated Swagger docs at `/docs`.

---

### ⛓️ Smart Contract Deployment

```bash
cd blockchain
npm install

# Run all contract tests (must pass 100%)
npx hardhat test

# Deploy to Polygon Amoy testnet
npx hardhat run scripts/deploy.js --network polygon_amoy

# Optional: Verify on PolygonScan
npx hardhat verify --network polygon_amoy <CONTRACT_ADDRESS>
```

---

## 🗂️ Project Structure

```
terratrust-ar/
│
├── 📱 src/
│   ├── navigation/          # RootNavigator, AuthStack, MainTabs
│   ├── features/
│   │   ├── auth/            # Firebase OTP, KYC, wallet creation
│   │   ├── land/            # OCR, LGD API, BhuNaksha WMS
│   │   ├── scanning/        # ARCore 3-tier, species TFLite, MMKV
│   │   └── credits/         # CTT balance, audit history, retirement
│   ├── store/               # Redux Toolkit slices + redux-persist
│   └── services/            # API client, ethers.js wallet
│
├── 🤖 android/
│   └── app/src/main/java/
│       └── ARModule.kt      # Kotlin ARCore depth + RANSAC cylinder
│
├── 🐍 backend/
│   ├── routers/             # auth, land, audit, credits
│   ├── services/
│   │   ├── ocr_service.py   # Google Cloud Vision + OpenCV
│   │   ├── boundary.py      # LGD + BhuNaksha 3-layer fetcher
│   │   ├── gee_fusion.py    # Earth Engine 6-layer XGBoost pipeline
│   │   └── minting.py       # web3.py Polygon ERC-1155 minter
│   └── tasks/
│       └── celery_tasks.py  # Async satellite fusion + minting jobs
│
├── ⛓️ blockchain/
│   ├── contracts/
│   │   └── TerraTrustToken.sol   # ERC-1155 + Ownable + retire()
│   ├── test/
│   │   └── TerraTrustToken.test.js
│   └── scripts/deploy.js
│
└── 🤖 ml/
    └── species_classifier/  # TFLite model (224×224 RGB, 11 species)
```

---

## 🧪 Testing

```bash
# React Native unit tests
npm test

# Backend pytest (Chave equation, credit calc, API endpoints)
cd backend && pytest -v

# Smart contract tests (Hardhat)
cd blockchain && npx hardhat test

# E2E: Use Firebase test phone numbers (OTP bypass for CI)
```

**Field-validated AR accuracy:**
- Tier 1 (ToF): avg. 2.1 cm error on 25 cm reference (8.4%)
- Tier 2 (SLAM): avg. 4.3 cm error on 25 cm reference
- Tier 3 (String): <1% error when carefully measured

---

## 🛰️ Satellite Data Sources

| Source | Agency | Data Type | Access |
|--------|--------|-----------|--------|
| NISAR | NASA-ISRO | L-band SAR (GCOV) | Alaska Satellite Facility |
| Sentinel-1 | ESA/Copernicus | C-band SAR GRD | Google Earth Engine |
| Sentinel-2 | ESA/Copernicus | Multispectral SR | Google Earth Engine |
| GEDI | NASA/LARSE | LiDAR canopy height | Google Earth Engine |
| SRTM | NASA | 30m DEM | Google Earth Engine (public domain) |

---

## 📊 System Metrics (v3.1)

| Metric | Value |
|--------|-------|
| Target biomass estimation error (with NISAR) | 5–6% |
| Dev-phase error (Sentinel stack, 9 features) | ~12% |
| States with full automated boundary fetching | Maharashtra (36 districts) |
| Minimum supported Android API level | 26 (Android 8.0) |
| Offline resilience | Full MMKV session recovery |
| Infrastructure cost per farmer (MVP scale) | ₹0 |
| Blockchain network | Polygon Amoy Testnet → Mainnet |
| Smart contract double-minting protection | keccak256 on-chain guard |

---

## 👨‍💻 Team

| Name | Role |
|------|------|
| **Abhishek Shrivastav** | Satellite Fusion Engine & GEE Pipeline |
| **Anuj Agrawal** | Blockchain & Smart Contract Architecture |
| **Anuj Parwal** | AR Tree Scanning & Kotlin ARCore Module |
| **Deepanshu Nanure** | React Native App & Land Verification |

**Project Guide:** Dr. Avinash Agrawal
**Department:** AI & Cyber Security (AICS), RCOEM Nagpur

---

## 🔮 Roadmap

- [x] **June 2026** — Enable calibrated NISAR production fusion via `NISAR_PRODUCTION_READY=true` + `NISAR_GEE_ASSET_ID`
- [ ] **Q3 2026** — Submit for Verra VM0047 methodology certification
- [ ] **Q3 2026** — Multi-state LGD integration (KA, AP, TG, TN, UP)
- [ ] **Q4 2026** — iOS port (ARKit bridge + alternative map renderer)
- [ ] **2027** — Buyer-facing CTT marketplace & retirement dashboard
- [ ] **2027** — Polygon mainnet deployment

---

## 📚 Key References

- Chave et al. (2014) — Pantropical allometric equations for tree biomass
- Dubayah et al. (2020) — NASA GEDI LiDAR for above-ground biomass
- Chen & Guestrin (2016) — XGBoost gradient boosted trees
- Gorelick et al. (2017) — Google Earth Engine
- NASA-ISRO (2025) — NISAR Mission Overview
- OpenZeppelin — ERC-1155 Multi Token Standard

Full references available in `docs/report.pdf`.

---

## ⚖️ License

This project was developed as part of the B.Tech curriculum at RCOEM Nagpur for academic purposes. All satellite data used is freely distributed under the respective agency terms (NASA, ESA, ISRO).

---

<div align="center">

*Built with 🌱 to give Indian farmers the carbon market access they deserve.*

**TerraTrust-AR · RCOEM Nagpur · AICS Department · 2025–2026**

</div>
