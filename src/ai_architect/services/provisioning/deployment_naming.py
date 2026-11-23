"""Deterministic AWS-visible naming for one design session.

Resource identities and Terraform module instance names stay stable; only
provider-visible names receive this suffix. The namespace is derived from
the design session id so regenerating a plan for the same session reuses
the same AWS names, while a different session cannot collide with it.
"""

import hashlib
import re

NAMESPACE_LENGTH = 12

NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]{1,12}$")


def deployment_namespace(session_id: str | None) -> str:
    """Return a short, stable, AWS-safe namespace for a design session."""
    normalized = (session_id or "").strip()
    if not normalized:
        return ""
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:NAMESPACE_LENGTH]


def namespaced_name(resource_id: str, namespace: str) -> str:
    """Append a validated namespace to a provider-visible resource name."""
    if not namespace:
        return resource_id
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(
            f"Deployment namespace {namespace!r} is not a safe AWS name suffix."
        )
    return f"{resource_id}-{namespace}"
