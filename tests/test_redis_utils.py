from app.redis_utils import normalise_redis_url


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
