"""Shared helpers for API tests.

The design API requires a bearer token on mutating endpoints. Tests
authenticate with a throwaway token by default; tests that exercise
the unauthenticated paths manipulate the environment explicitly (see
``tests/test_api_security.py``).
"""

import os

from fastapi.testclient import TestClient

API_TOKEN_ENV = "AI_ARCHITECT_API_TOKEN"
TEST_API_TOKEN = "test-token"


def set_test_token(token: str = TEST_API_TOKEN, **extra_env) -> None:
    os.environ[API_TOKEN_ENV] = token
    for key, value in extra_env.items():
        os.environ[key] = value


def clear_token_env() -> None:
    os.environ.pop(API_TOKEN_ENV, None)


def api_client(app, token: str = TEST_API_TOKEN) -> TestClient:
    """A TestClient authenticated for mutating endpoints by default."""
    os.environ[API_TOKEN_ENV] = token
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client
