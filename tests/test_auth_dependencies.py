import importlib
import sys
import types

import pytest


def _install_fastapi_stub():
    fastapi_stub = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def get(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def post(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    fastapi_stub.APIRouter = APIRouter
    fastapi_stub.Depends = lambda dependency: dependency
    fastapi_stub.Header = lambda default=None: default
    fastapi_stub.HTTPException = HTTPException
    fastapi_stub.status = types.SimpleNamespace(
        HTTP_400_BAD_REQUEST=400,
        HTTP_200_OK=200,
        HTTP_401_UNAUTHORIZED=401,
        HTTP_409_CONFLICT=409,
        HTTP_500_INTERNAL_SERVER_ERROR=500,
        HTTP_503_SERVICE_UNAVAILABLE=503,
    )
    sys.modules["fastapi"] = fastapi_stub
    return HTTPException


def _install_models_stub():
    models_user_stub = types.ModuleType("models.user")

    class _SimpleModel:
        def __init__(self, **kwargs):
            for field_name in getattr(self.__class__, "__annotations__", {}):
                default_value = getattr(self.__class__, field_name, None)
                setattr(self, field_name, kwargs.get(field_name, default_value))

            for key, value in kwargs.items():
                setattr(self, key, value)

    class AuthMeResponse(_SimpleModel):
        user_id: str | None = None
        firebase_uid: str | None = None
        phone_number: str | None = None
        full_name: str | None = None
        wallet_address: str | None = None
        kyc_completed: bool = False
        wallet_recovery_status: str | None = None
        wallet_recovery_requested_at: str | None = None

    class KYCRequest(_SimpleModel):
        full_name: str | None = None
        aadhaar_number: str | None = None

    class KYCResponse(_SimpleModel):
        status: str = "success"
        user_id: str | None = None

    class WalletRegisterRequest(_SimpleModel):
        wallet_address: str | None = None

    class WalletRegisterResponse(_SimpleModel):
        status: str = "success"

    class WalletRecoveryRequest(_SimpleModel):
        new_wallet_address: str | None = None

    class WalletRecoveryResponse(_SimpleModel):
        status: str = "pending"
        message: str = "Wallet recovery request submitted"

    models_user_stub.AuthMeResponse = AuthMeResponse
    models_user_stub.KYCRequest = KYCRequest
    models_user_stub.KYCResponse = KYCResponse
    models_user_stub.WalletRegisterRequest = WalletRegisterRequest
    models_user_stub.WalletRegisterResponse = WalletRegisterResponse
    models_user_stub.WalletRecoveryRequest = WalletRecoveryRequest
    models_user_stub.WalletRecoveryResponse = WalletRecoveryResponse
    sys.modules["models.user"] = models_user_stub


def _load_auth_modules():
    http_exception = _install_fastapi_stub()
    _install_models_stub()

    web3_stub = types.ModuleType("web3")

    class _Web3Stub:
        @staticmethod
        def is_address(value):
            return isinstance(value, str) and value.startswith("0x") and len(value) == 42

        @staticmethod
        def to_checksum_address(value):
            return value

    web3_stub.Web3 = _Web3Stub
    sys.modules["web3"] = web3_stub

    app_database_stub = types.ModuleType("app.database")
    app_database_stub.supabase_client = None
    sys.modules["app.database"] = app_database_stub

    firebase_stub = types.ModuleType("app.firebase_auth")
    firebase_stub.verify_firebase_token = lambda _token: {}
    sys.modules["app.firebase_auth"] = firebase_stub

    sys.modules.pop("app.dependencies", None)
    sys.modules.pop("routers.auth", None)

    dependencies = importlib.import_module("app.dependencies")
    auth_router = importlib.import_module("routers.auth")
    return dependencies, auth_router, http_exception


dependencies, auth_router, HTTPException = _load_auth_modules()


WALLET_ONE = "0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa"
WALLET_TWO = "0xBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBb"


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeUsersQuery:
    def __init__(self, users_store):
        self._users_store = users_store
        self._action = None
        self._payload = None
        self._filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def update(self, payload):
        self._action = "update"
        self._payload = payload
        return self

    def insert(self, payload):
        self._action = "insert"
        self._payload = payload
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def ilike(self, field, value):
        self._filters.append(("ilike", field, value))
        return self

    def limit(self, count):
        self._limit = count
        return self

    def _matches(self, row):
        for operator, field, value in self._filters:
            row_value = row.get(field)
            if operator == "eq" and row_value != value:
                return False
            if operator == "ilike" and (row_value or "").lower() != value.lower():
                return False
        return True

    def execute(self):
        if self._action == "select":
            rows = [row.copy() for row in self._users_store if self._matches(row)]
            if self._limit is not None:
                rows = rows[: self._limit]
            return _FakeResponse(rows)

        if self._action == "update":
            for row in self._users_store:
                if self._matches(row):
                    row.update(self._payload)
            return _FakeResponse([])

        if self._action == "insert":
            new_row = {
                "id": self._payload.get("id", "generated-user-id"),
                "full_name": None,
                "kyc_completed": False,
                "wallet_address": None,
            }
            new_row.update(self._payload)
            self._users_store.append(new_row)
            return _FakeResponse([])

        raise AssertionError(f"Unsupported fake query action: {self._action}")


class _FakeSupabaseClient:
    def __init__(self, users_store, recovery_store=None):
        self._users_store = users_store
        self._recovery_store = recovery_store or []

    def table(self, table_name):
        if table_name == "users":
            return _FakeUsersQuery(self._users_store)
        if table_name == "wallet_recovery_requests":
            return _FakeWalletRecoveryQuery(self._recovery_store)
        raise AssertionError(f"Unsupported fake table: {table_name}")


class _FakeWalletRecoveryQuery:
    def __init__(self, recovery_store):
        self._recovery_store = recovery_store
        self._filters = []
        self._limit = None
        self._order_field = None
        self._descending = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def order(self, field, desc=False):
        self._order_field = field
        self._descending = desc
        return self

    def limit(self, count):
        self._limit = count
        return self

    def execute(self):
        rows = [
            row.copy()
            for row in self._recovery_store
            if all(row.get(field) == value for field, value in self._filters)
        ]
        if self._order_field is not None:
            rows.sort(
                key=lambda row: row.get(self._order_field) or "",
                reverse=self._descending,
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResponse(rows)


def test_provision_user_refetches_row_after_insert(monkeypatch):
    users_store = []
    fake_client = _FakeSupabaseClient(users_store)
    monkeypatch.setattr(dependencies, "supabase_client", fake_client)

    user = dependencies._provision_user("firebase-1", "+919999999999")

    assert user["firebase_uid"] == "firebase-1"
    assert user["phone_number"] == "+919999999999"
    assert users_store[0]["kyc_completed"] is False


def test_provision_user_refetches_row_after_update(monkeypatch):
    users_store = [
        {
            "id": "user-1",
            "firebase_uid": "firebase-1",
            "phone_number": "+910000000000",
            "full_name": "Farmer One",
            "kyc_completed": True,
            "wallet_address": None,
        }
    ]
    fake_client = _FakeSupabaseClient(users_store)
    monkeypatch.setattr(dependencies, "supabase_client", fake_client)

    user = dependencies._provision_user("firebase-1", "+919999999999")

    assert user["id"] == "user-1"
    assert user["phone_number"] == "+919999999999"


def test_fetch_user_by_attaches_latest_wallet_recovery_state(monkeypatch):
    users_store = [
        {
            "id": "user-1",
            "firebase_uid": "firebase-1",
            "phone_number": "+919999999999",
            "full_name": "Farmer One",
            "kyc_completed": True,
            "wallet_address": WALLET_ONE,
        }
    ]
    recovery_store = [
        {
            "user_id": "user-1",
            "status": "REJECTED",
            "requested_at": "2026-04-09T10:00:00Z",
        },
        {
            "user_id": "user-1",
            "status": "PENDING",
            "requested_at": "2026-04-10T10:00:00Z",
        },
    ]
    fake_client = _FakeSupabaseClient(users_store, recovery_store)
    monkeypatch.setattr(dependencies, "supabase_client", fake_client)

    user = dependencies._fetch_user_by("id", "user-1")

    assert user["wallet_recovery_status"] == "PENDING"
    assert user["wallet_recovery_requested_at"] == "2026-04-10T10:00:00Z"


def test_get_current_user_returns_503_for_profile_provisioning_errors(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "verify_firebase_token",
        lambda _token: {"uid": "firebase-1", "phone_number": "+919999999999"},
    )
    monkeypatch.setattr(
        dependencies,
        "_provision_user",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(HTTPException) as exc_info:
        dependencies.get_current_user("Bearer test-token")

    assert exc_info.value.status_code == 503


def test_register_wallet_is_idempotent_when_same_wallet_is_already_saved(monkeypatch):
    users_store = [
        {
            "id": "user-1",
            "firebase_uid": "firebase-1",
            "phone_number": "+919999999999",
            "full_name": "Farmer One",
            "kyc_completed": True,
            "wallet_address": WALLET_ONE,
        }
    ]
    fake_client = _FakeSupabaseClient(users_store)
    monkeypatch.setattr(auth_router, "supabase_client", fake_client)

    response = auth_router.register_wallet(
        auth_router.WalletRegisterRequest(wallet_address=WALLET_ONE.lower()),
        current_user={"id": "user-1", "wallet_address": WALLET_ONE},
    )

    assert response.status == "success"
    assert users_store[0]["wallet_address"] == WALLET_ONE


def test_register_wallet_rejects_wallet_replacement(monkeypatch):
    users_store = [
        {
            "id": "user-1",
            "firebase_uid": "firebase-1",
            "phone_number": "+919999999999",
            "full_name": "Farmer One",
            "kyc_completed": True,
            "wallet_address": WALLET_ONE,
        }
    ]
    fake_client = _FakeSupabaseClient(users_store)
    monkeypatch.setattr(auth_router, "supabase_client", fake_client)

    with pytest.raises(HTTPException) as exc_info:
        auth_router.register_wallet(
            auth_router.WalletRegisterRequest(wallet_address=WALLET_TWO),
            current_user={"id": "user-1", "wallet_address": WALLET_ONE},
        )

    assert exc_info.value.status_code == 409


def test_register_wallet_rejects_case_insensitive_duplicate_for_other_user(monkeypatch):
    users_store = [
        {
            "id": "user-2",
            "firebase_uid": "firebase-2",
            "phone_number": "+918888888888",
            "full_name": "Farmer Two",
            "kyc_completed": True,
            "wallet_address": WALLET_ONE,
        }
    ]
    fake_client = _FakeSupabaseClient(users_store)
    monkeypatch.setattr(auth_router, "supabase_client", fake_client)

    with pytest.raises(HTTPException) as exc_info:
        auth_router.register_wallet(
            auth_router.WalletRegisterRequest(wallet_address=WALLET_ONE.lower()),
            current_user={"id": "user-1", "wallet_address": None},
        )

    assert exc_info.value.status_code == 409