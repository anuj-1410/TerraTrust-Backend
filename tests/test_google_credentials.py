import json
import os
import types
from pathlib import Path

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")


def _write_service_account(tmp_path: Path, file_name: str, *, client_email: str) -> Path:
    credentials_path = tmp_path / file_name
    credentials_path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "test-project",
                "private_key_id": "test-key-id",
                "private_key": "-----BEGIN PRIVATE KEY-----\\nTEST\\n-----END PRIVATE KEY-----\\n",
                "client_email": client_email,
                "client_id": "1234567890",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )
    return credentials_path


def test_get_firebase_app_uses_dedicated_credentials_file(monkeypatch, tmp_path):
    credentials_path = _write_service_account(
        tmp_path,
        "firebase.json",
        client_email="firebase@test-project.iam.gserviceaccount.com",
    )

    import app.firebase_auth as firebase_auth

    monkeypatch.setattr(
        firebase_auth.settings,
        "FIREBASE_CREDENTIALS_PATH",
        str(credentials_path),
        raising=False,
    )
    monkeypatch.setattr(firebase_auth.settings, "FIREBASE_PROJECT_ID", "test-project", raising=False)
    monkeypatch.setattr(firebase_auth.settings, "GOOGLE_APPLICATION_CREDENTIALS", "", raising=False)

    certificate_calls: list[str] = []
    initialize_calls: list[tuple[object, dict[str, str]]] = []

    def fake_certificate(path: str) -> str:
        certificate_calls.append(path)
        return f"certificate:{path}"

    def fake_initialize_app(credential: object, options: dict[str, str]):
        initialize_calls.append((credential, options))
        return object()

    firebase_admin_stub = types.SimpleNamespace(
        App=object,
        get_app=lambda: (_ for _ in ()).throw(ValueError()),
        initialize_app=fake_initialize_app,
    )

    monkeypatch.setattr(firebase_auth, "firebase_admin", firebase_admin_stub)
    monkeypatch.setattr(firebase_auth.credentials, "Certificate", fake_certificate)

    firebase_auth.get_firebase_app()

    assert certificate_calls == [str(credentials_path)]
    assert initialize_calls == [
        (
            f"certificate:{credentials_path}",
            {"projectId": firebase_auth.settings.FIREBASE_PROJECT_ID},
        )
    ]


def test_ensure_gee_initialized_uses_dedicated_service_account(monkeypatch, tmp_path):
    credentials_path = _write_service_account(
        tmp_path,
        "gee.json",
        client_email="gee@test-project.iam.gserviceaccount.com",
    )

    import app.gee as gee

    monkeypatch.setattr(gee, "resolve_google_credentials_path", lambda: credentials_path)
    monkeypatch.setattr(gee, "get_gee_project_id", lambda: "gee-project")

    initialize_calls: list[tuple[object, str]] = []
    service_account_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        gee.ee,
        "Number",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            getInfo=lambda: (_ for _ in ()).throw(Exception("not init"))
        ),
    )

    def fake_service_account_credentials(client_email: str, path: str):
        service_account_calls.append((client_email, path))
        return f"gee-credentials:{client_email}:{path}"

    def fake_initialize(*, credentials: object, project: str):
        initialize_calls.append((credentials, project))

    monkeypatch.setattr(gee.ee, "ServiceAccountCredentials", fake_service_account_credentials)
    monkeypatch.setattr(gee.ee, "Initialize", fake_initialize)

    gee.ensure_gee_initialized()

    assert service_account_calls == [
        ("gee@test-project.iam.gserviceaccount.com", str(credentials_path))
    ]
    assert initialize_calls == [
        (
            f"gee-credentials:gee@test-project.iam.gserviceaccount.com:{credentials_path}",
            "gee-project",
        )
    ]


def test_vision_client_initialisation_uses_dedicated_credentials_path(monkeypatch, tmp_path):
    credentials_path = _write_service_account(
        tmp_path,
        "vision.json",
        client_email="vision@test-project.iam.gserviceaccount.com",
    )

    import services.ocr_service as ocr_service

    monkeypatch.setattr(ocr_service, "_vision_client", None)
    monkeypatch.setattr(
        ocr_service,
        "resolve_google_credentials_path",
        lambda: credentials_path,
    )

    loaded_paths: list[str] = []
    client_credentials: list[object] = []

    def fake_from_service_account_file(path: str):
        loaded_paths.append(path)
        return f"vision-credentials:{path}"

    def fake_image_annotator_client(*, credentials: object):
        client_credentials.append(credentials)
        return object()

    monkeypatch.setattr(
        ocr_service.service_account.Credentials,
        "from_service_account_file",
        fake_from_service_account_file,
    )
    monkeypatch.setattr(
        ocr_service.vision,
        "ImageAnnotatorClient",
        fake_image_annotator_client,
    )

    ocr_service._get_vision_client()

    assert loaded_paths == [str(credentials_path)]
    assert client_credentials == [f"vision-credentials:{credentials_path}"]