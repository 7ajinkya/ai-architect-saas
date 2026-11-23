"""Local entry point for the design API: demo-safe defaults.

Binds to the loopback interface (127.0.0.1) by default so the API is
never exposed beyond the local machine, and loads ``.env`` so the
operator's local token configuration is picked up.

Run: python -m ai_architect.api
Override: AI_ARCHITECT_API_HOST / AI_ARCHITECT_API_PORT (deliberate).
"""

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def resolve_bind() -> tuple[str, int]:
    """Resolve host/port from the environment, loopback by default."""
    host = (os.environ.get("AI_ARCHITECT_API_HOST") or "").strip()
    if not host:
        host = DEFAULT_HOST
    raw_port = (os.environ.get("AI_ARCHITECT_API_PORT") or "").strip()
    if not raw_port:
        return host, DEFAULT_PORT
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(
            f"AI_ARCHITECT_API_PORT must be an integer, got {raw_port!r}."
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError(
            f"AI_ARCHITECT_API_PORT must be between 1 and 65535, got {port}."
        )
    return host, port


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("AI_ARCHITECT_API_TOKEN", "").strip():
        print(
            "WARNING: AI_ARCHITECT_API_TOKEN is not set; mutating API "
            "endpoints will refuse requests (503) until it is configured."
        )
    host, port = resolve_bind()
    import uvicorn

    uvicorn.run("ai_architect.api.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
