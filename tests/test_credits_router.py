import importlib
import sys
import types


def _install_fastapi_stub():
    fastapi_stub = types.ModuleType("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code, detail, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    class _APIRouter:
        def get(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    fastapi_stub.APIRouter = _APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency
    fastapi_stub.Query = lambda default=None, **_kwargs: default
    fastapi_stub.HTTPException = _HTTPException
    fastapi_stub.status = types.SimpleNamespace(
        HTTP_400_BAD_REQUEST=400,
        HTTP_503_SERVICE_UNAVAILABLE=503,
    )
    sys.modules["fastapi"] = fastapi_stub


def _install_web3_stub():
    web3_stub = types.ModuleType("web3")

    class _Web3:
        @staticmethod
        def is_address(value):
            return isinstance(value, str) and value.startswith("0x") and len(value) == 42

        @staticmethod
        def to_checksum_address(value):
            return value

        class HTTPProvider:
            def __init__(self, _url):
                self.url = _url

        def __init__(self, _provider=None):
            self.eth = types.SimpleNamespace(contract=lambda **_kwargs: None)

        def is_connected(self):
            return True

    web3_stub.Web3 = _Web3
    sys.modules["web3"] = web3_stub


def _install_supporting_stubs():
    config_stub = types.ModuleType("app.config")
    config_stub.settings = types.SimpleNamespace(
        ALCHEMY_POLYGON_AMOY_URL="https://polygon.example",
        CONTRACT_ADDRESS="0x1234567890123456789012345678901234567890",
    )
    sys.modules["app.config"] = config_stub

    dependencies_stub = types.ModuleType("app.dependencies")
    dependencies_stub.get_current_user = lambda: None
    sys.modules["app.dependencies"] = dependencies_stub

    rate_limit_stub = types.ModuleType("app.rate_limit")

    class _RateLimitSpec:
        def __init__(self, scope, limit, window_seconds, error_message):
            self.scope = scope
            self.limit = limit
            self.window_seconds = window_seconds
            self.error_message = error_message

    rate_limit_stub.RateLimitSpec = _RateLimitSpec
    rate_limit_stub.enforce_rate_limit = lambda *_args, **_kwargs: None
    sys.modules["app.rate_limit"] = rate_limit_stub

    blockchain_models_stub = types.ModuleType("models.blockchain")

    class _SimpleModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    blockchain_models_stub.CreditHistory = _SimpleModel
    blockchain_models_stub.BalanceResponse = _SimpleModel
    sys.modules["models.blockchain"] = blockchain_models_stub

    ipfs_stub = types.ModuleType("services.ipfs_service")
    ipfs_stub.to_gateway_url = lambda value: f"https://gateway.example/ipfs/{value}" if value else None
    sys.modules["services.ipfs_service"] = ipfs_stub

    minting_stub = types.ModuleType("services.minting_service")
    minting_stub.load_contract_abi = lambda: []
    sys.modules["services.minting_service"] = minting_stub


class _Response:
    def __init__(self, data):
        self.data = data


class _FakeTableQuery:
    def __init__(self, table_name, stores):
        self._table_name = table_name
        self._stores = stores
        self._filters = []
        self._single = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        rows = [
            row.copy()
            for row in self._stores.get(self._table_name, [])
            if all(row.get(field) == value for field, value in self._filters)
        ]
        if self._single:
            return _Response(rows[0] if rows else None)
        return _Response(rows)


class _FakeSupabaseClient:
    def __init__(self, stores):
        self._stores = stores

    def table(self, table_name):
        return _FakeTableQuery(table_name, self._stores)


def _load_credits_module(stores):
    _install_fastapi_stub()
    _install_web3_stub()
    _install_supporting_stubs()

    database_stub = types.ModuleType("app.database")
    database_stub.supabase_client = _FakeSupabaseClient(stores)
    sys.modules["app.database"] = database_stub

    sys.modules.pop("routers.credits", None)
    return importlib.import_module("routers.credits")


def test_get_balance_returns_zero_when_wallet_not_registered():
    credits = _load_credits_module({"carbon_audits": [], "land_parcels": []})

    response = credits.get_balance(
        page=1,
        limit=20,
        current_user={"id": "user-1", "wallet_address": None},
    )

    assert response.balance_ctt == 0.0
    assert response.history == []
    assert response.total == 0


def test_get_balance_falls_back_to_history_when_chain_is_unavailable(monkeypatch):
    credits = _load_credits_module(
        {
            "carbon_audits": [
                {
                    "user_id": "user-1",
                    "audit_year": 2026,
                    "status": "MINTED",
                    "credits_issued": 12.4,
                    "land_id": "land-1",
                    "tx_hash": "0xabc",
                    "ipfs_metadata_cid": "cid-1",
                    "ipfs_url": None,
                    "minted_at": "2026-04-10T10:00:00Z",
                },
                {
                    "user_id": "user-1",
                    "audit_year": 2025,
                    "status": "COMPLETE_NO_CREDITS",
                    "credits_issued": 0,
                    "land_id": "land-1",
                    "tx_hash": None,
                    "ipfs_metadata_cid": None,
                    "ipfs_url": None,
                    "minted_at": None,
                },
            ],
            "land_parcels": [{"id": "land-1", "farm_name": "North Field"}],
        }
    )

    monkeypatch.setattr(credits, "_get_contract", lambda: (_ for _ in ()).throw(RuntimeError("rpc down")))

    response = credits.get_balance(
        page=1,
        limit=20,
        current_user={
            "id": "user-1",
            "wallet_address": "0x1234567890123456789012345678901234567890",
        },
    )

    assert response.balance_ctt == 12.4
    assert response.total == 2
    assert response.history[0].land_name == "North Field"
