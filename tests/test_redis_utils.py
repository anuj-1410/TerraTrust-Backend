from app.redis_utils import normalise_redis_url, secure_redis_url


def test_normalise_redis_url_maps_cert_none_for_redis_py():
    url = "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=CERT_NONE"

    assert normalise_redis_url(url) == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=none"
    )


def test_normalise_redis_url_leaves_unrelated_query_params_unchanged():
    url = "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=CERT_REQUIRED&decode_responses=true"

    assert normalise_redis_url(url) == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required&decode_responses=true"
    )


def test_secure_redis_url_requires_tls_verification_for_rediss_urls():
    url = "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=CERT_NONE&decode_responses=true"

    assert secure_redis_url(url) == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required&decode_responses=true"
    )


def test_secure_redis_url_adds_required_tls_flag_when_missing():
    url = "rediss://default:secret@example.upstash.io:6379/0"

    assert secure_redis_url(url) == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required"
    )

