"""Exercise real TLS handshakes in memory; no live database or network calls."""

from datetime import datetime, timedelta, timezone
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from app.database_ssl import database_ssl_context


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline test CA")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "db.test.invalid")])
        )
        .issuer_name(root_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, None, None),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("db.test.invalid")]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    root_path, cert_path, key_path = [
        tmp_path / name for name in ("ca.pem", "server.pem", "server.key")
    ]
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(
        server.public_bytes(serialization.Encoding.PEM)
        + root.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    return root_path, server_context


def handshake(client_context, server_context, hostname="db.test.invalid"):
    client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    done = set()
    for _ in range(20):
        for endpoint in (client, server):
            if endpoint not in done:
                try:
                    endpoint.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                else:
                    done.add(endpoint)
        for outgoing, incoming in ((client_out, server_in), (server_out, client_in)):
            data = outgoing.read()
            if data:
                incoming.write(data)
        if len(done) == 2:
            return
    raise AssertionError("In-memory TLS handshake did not finish")


def test_default_context_requires_verified_hostname():
    context = database_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_project_ca_fixes_untrusted_chain_without_removing_system_roots(certificates):
    ca_path, server = certificates
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(database_ssl_context(), server)
    context = database_ssl_context(str(ca_path))
    handshake(context, server)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    system_roots = set(database_ssl_context().get_ca_certs(binary_form=True))
    assert system_roots <= set(context.get_ca_certs(binary_form=True))


def test_project_ca_does_not_allow_wrong_database_hostname(certificates):
    ca_path, server = certificates
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(database_ssl_context(str(ca_path)), server, "wrong.test.invalid")


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_invalid_ca_file_gives_render_setup_instruction(tmp_path, missing):
    path = tmp_path / "ca.pem"
    if not missing:
        path.write_text("invalid certificate", encoding="utf-8")
    with pytest.raises(
        RuntimeError, match="DATABASE_SSL_CA_FILE.*readable PEM"
    ) as error:
        database_ssl_context(str(path))
    assert "/etc/secrets/supabase-ca.crt" in str(error.value)
