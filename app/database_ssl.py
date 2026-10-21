"""Verified PostgreSQL TLS shared by the API, workers and migration tools."""

import ssl


DATABASE_TLS_SETUP_MESSAGE = (
    "PostgreSQL TLS certificate verification failed. Download your Supabase "
    "server root certificate from Database Settings > SSL Configuration, upload "
    "it as the Render secret file supabase-ca.crt, and set "
    "DATABASE_SSL_CA_FILE=/etc/secrets/supabase-ca.crt on the API and database workers."
)


def database_ssl_context(ca_file=""):
    """Extend system trust with the configured CA; always verify the hostname."""
    context = ssl.create_default_context()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "DATABASE_SSL_CA_FILE must reference a readable PEM CA certificate. "
                "On Render, upload supabase-ca.crt under Environment > Secret Files "
                "and use /etc/secrets/supabase-ca.crt."
            ) from exc
    return context
