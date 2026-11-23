"""API safety tests: bearer-token auth, fail-closed defaults, CORS, bind.

The design API binds to loopback by default and refuses mutating
requests without a valid bearer token. When no token is configured at
all, mutations fail closed with 503 rather than running unprotected.
Read endpoints and /health stay open.
"""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from ai_architect.api import security
from ai_architect.api.__main__ import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    resolve_bind,
)
from ai_architect.api.main import create_app
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from tests.api_auth import API_TOKEN_ENV, TEST_API_TOKEN, clear_token_env

# A mutating request that reaches the handler only when authentication
# passed; the handler rejects the bogus mode before touching any state.
_PROBE = "/design/sessions/no-such-session/provision"
_PROBE_BODY = {"mode": "bogus"}


def _preflight(client, origin, method="POST"):
    return client.options(
        "/design/sessions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


class TokenAuthTests(unittest.TestCase):
    def setUp(self):
        clear_token_env()
        self.addCleanup(clear_token_env)

    def test_unconfigured_token_fails_closed(self):
        client = TestClient(create_app())
        response = client.post(_PROBE, json=_PROBE_BODY)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"]["code"], "api_token_not_configured"
        )

    def test_missing_header_rejected_when_configured(self):
        os.environ[API_TOKEN_ENV] = TEST_API_TOKEN
        client = TestClient(create_app())
        response = client.post(_PROBE, json=_PROBE_BODY)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "unauthorized")
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_wrong_token_rejected(self):
        os.environ[API_TOKEN_ENV] = TEST_API_TOKEN
        client = TestClient(create_app())
        response = client.post(
            _PROBE,
            json=_PROBE_BODY,
            headers={"Authorization": "Bearer not-the-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_non_bearer_scheme_rejected(self):
        os.environ[API_TOKEN_ENV] = TEST_API_TOKEN
        client = TestClient(create_app())
        response = client.post(
            _PROBE,
            json=_PROBE_BODY,
            headers={"Authorization": f"Basic {TEST_API_TOKEN}"},
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_token_passes_guard(self):
        os.environ[API_TOKEN_ENV] = TEST_API_TOKEN
        client = TestClient(create_app())
        response = client.post(
            _PROBE,
            json=_PROBE_BODY,
            headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
        )
        # Auth passed: the handler ran and rejected the bogus mode.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "error")

    def test_token_read_at_request_time_not_cached(self):
        os.environ[API_TOKEN_ENV] = TEST_API_TOKEN
        client = TestClient(create_app())
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
        self.assertEqual(
            client.post(_PROBE, json=_PROBE_BODY, headers=headers).status_code,
            400,
        )
        os.environ[API_TOKEN_ENV] = "rotated-token"
        response = client.post(_PROBE, json=_PROBE_BODY, headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_secrets_never_echoed_in_rejections(self):
        os.environ[API_TOKEN_ENV] = "super-secret-token"
        client = TestClient(create_app())
        response = client.post(
            _PROBE,
            json=_PROBE_BODY,
            headers={"Authorization": "Bearer super-secret-wrong"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("super-secret-token", response.text)
        self.assertNotIn("super-secret-wrong", response.text)

    def test_health_stays_open(self):
        client = TestClient(create_app())
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_read_endpoints_stay_open(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = ckpt_sqlite.CHECKPOINT_DB_PATH
        self.addCleanup(
            setattr, ckpt_sqlite, "CHECKPOINT_DB_PATH", original
        )
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        client = TestClient(create_app())
        response = client.get("/design/sessions?limit=1")
        self.assertEqual(response.status_code, 200)


class CorsTests(unittest.TestCase):
    def setUp(self):
        clear_token_env()
        self.addCleanup(clear_token_env)
        self.addCleanup(os.environ.pop, security.CORS_ORIGINS_ENV, None)

    def test_default_origins_allow_local_dev_ui(self):
        client = TestClient(create_app())
        for origin in security.DEFAULT_CORS_ORIGINS:
            response = _preflight(client, origin)
            self.assertEqual(
                response.headers.get("access-control-allow-origin"), origin
            )

    def test_other_origins_are_not_allowed(self):
        client = TestClient(create_app())
        response = _preflight(client, "http://evil.example")
        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_env_override_replaces_defaults(self):
        os.environ[security.CORS_ORIGINS_ENV] = "http://192.0.2.10:4173"
        client = TestClient(create_app())
        allowed = _preflight(client, "http://192.0.2.10:4173")
        self.assertEqual(
            allowed.headers.get("access-control-allow-origin"),
            "http://192.0.2.10:4173",
        )
        default = _preflight(client, "http://localhost:5173")
        self.assertIsNone(default.headers.get("access-control-allow-origin"))


class BindDefaultsTests(unittest.TestCase):
    def setUp(self):
        for key in ("AI_ARCHITECT_API_HOST", "AI_ARCHITECT_API_PORT"):
            self.addCleanup(os.environ.pop, key, None)
            os.environ.pop(key, None)

    def test_default_bind_is_loopback(self):
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")
        self.assertEqual(resolve_bind(), (DEFAULT_HOST, DEFAULT_PORT))

    def test_env_overrides_bind(self):
        os.environ["AI_ARCHITECT_API_HOST"] = "0.0.0.0"
        os.environ["AI_ARCHITECT_API_PORT"] = "9090"
        self.assertEqual(resolve_bind(), ("0.0.0.0", 9090))

    def test_invalid_port_is_rejected(self):
        os.environ["AI_ARCHITECT_API_PORT"] = "not-a-port"
        with self.assertRaises(ValueError):
            resolve_bind()

    def test_out_of_range_port_is_rejected(self):
        os.environ["AI_ARCHITECT_API_PORT"] = "70000"
        with self.assertRaises(ValueError):
            resolve_bind()


if __name__ == "__main__":
    unittest.main()
