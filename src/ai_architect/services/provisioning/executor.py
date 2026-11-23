"""Approval-gated Terraform execution adapter (Slice 4D: plan/apply).

Default-deny by construction:

- ``plan`` is a read-only preview and runs without a provisioning
  approval; its stored review is what the human authorizes next.
- ``apply`` needs a stored provisioning approval that matches the
  exact Terraform tree it authorizes (tree_hash + spec_id), the
  explicit confirmation string ``APPLY``, and a stored plan review
  bound to that exact saved plan file and workspace fingerprint.
- Apply consumes the exact reviewed plan: plan mode saves
  ``ai-architect.tfplan`` into the (durable) workspace and records its
  sha256 in the stored review; apply hash-verifies that file against
  the review's binding and passes it to ``terraform apply``
  explicitly. A missing, unreadable, or mismatched saved plan refuses
  before any subprocess and can never silently degrade into a fresh
  plan.
- ``destroy`` is a separate reviewed operation: it saves its own plan
  file, requires its own ``DESTROY`` confirmation, and refuses without a
  durable workspace and a hash-bound destroy plan.
- A successful apply records Terraform outputs and an HTTP smoke result;
  neither can turn a successful apply into a failure.
- No ambient credentials: the child process receives a bare
  environment plus ONLY explicitly supplied entries. Nothing is read
  from the operator's environment implicitly, nothing is persisted,
  and all output is sanitized before it is stored or returned.

This module is deliberately separate from
services.provisioning.terraform_cli, whose fmt/init/validate
allowlist stays untouched: that module can never run plan or apply
even by caller mistake, and this module can never apply anything
without a matching approval.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ai_architect.domain.provisioning.models import ResourceKind

_APPROVED_SUBCOMMANDS = frozenset({"plan", "apply"})

_APPLY_CONFIRMATION = "APPLY"

# Explicitly forwarded operator environment entries only. Never read
# implicitly: the caller names each entry it forwards.
FORWARDABLE_AWS_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
)

_TIMEOUT_SECONDS = 600
_MAX_OUTPUT = 8000

_SECRET_PATTERN = re.compile(
    r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----|"
    r"(?i:aws_secret_access_key|aws_session_token|password|secret|token)"
    r"\s*[=:]\s*\S+)",
)

_PLAN_SUMMARY_RE = re.compile(
    r"Plan:\s*(?P<add>\d+)\s+to add,\s*(?P<change>\d+)\s+to change,\s*"
    r"(?P<destroy>\d+)\s+to destroy",
)


class ExecutionRefused(ValueError):
    """Refusal to run Terraform: missing approval, mismatch, or policy."""


# Local deployment artifacts are resolved ONLY under this server-side
# directory, expressed relative to the repository root. It is a constant,
# never request input: neither the UI nor any API caller can name a
# directory, so no arbitrary filesystem path can reach staging.
ARTIFACT_SOURCE_DIRPARTS = ("infra", "lambda", "dist")

# Basename-only zip reference, mirroring the module-catalog contract for
# code_artifact_path. No directory component can be expressed here.
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.zip$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The exact reviewed plan file: plan mode writes it with
# ``terraform plan -out`` into the workspace, records its sha256 in the
# stored review, and apply consumes that exact file by name. Constant,
# never caller input.
SAVED_PLAN_FILENAME = "ai-architect.tfplan"

# Destroy is a second, separately reviewed operation. Its plan is saved
# under a distinct name so an apply plan can never be replayed as a
# destroy (or vice versa).
DESTROY_PLAN_FILENAME = "ai-architect-destroy.tfplan"
_DESTROY_CONFIRMATION = "DESTROY"

_SMOKE_TIMEOUT_SECONDS = 10

# Durable workspace convention (S2): <repo>/var/terraform/<session>/<tree>/
# The marker file is the only thing that makes a directory a workspace;
# it binds the directory to exactly one plan tree fingerprint.
WORKSPACE_MARKER_FILENAME = ".ai-architect-workspace.json"
_WORKSPACE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def workspace_path(session_id: str, tree_hash: str) -> Path:
    """Durable, inspectable workspace path for one session's plan tree.

    ``<repo>/var/terraform/<session_id>/<tree_hash[:16]>``. Both path
    segments are validated here: the session id and tree hash must be
    safe path segments, so no caller input can steer workspace creation
    outside the repository's own var/terraform tree.
    """
    if (
        not isinstance(session_id, str)
        or not _WORKSPACE_SEGMENT_RE.fullmatch(session_id)
    ):
        raise ExecutionRefused(
            "Terraform workspace refused: the session id is not a safe "
            "path segment."
        )
    if (
        not isinstance(tree_hash, str)
        or not _WORKSPACE_SEGMENT_RE.fullmatch(tree_hash)
    ):
        raise ExecutionRefused(
            "Terraform workspace refused: the plan tree hash is not a "
            "safe path segment."
        )
    return (
        _repository_root()
        / "var" / "terraform" / session_id / tree_hash[:16]
    )


def workspace_fingerprint(files: dict[str, str], manifest=None) -> str:
    """Canonical fingerprint binding a workspace to exactly one tree.

    Covers every file path/content pair plus every artifact (name,
    sha256) pair: a regenerated tree or a changed artifact manifest can
    never reuse another tree's workspace.
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[name]).encode("utf-8"))
        digest.update(b"\0")
    for name, sha256 in _normalize_manifest(manifest):
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(sha256).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _prepare_durable_workspace(
    workdir: Path, fingerprint: str | None
) -> None:
    """Validate (or adopt) a durable workspace before writing into it.

    Refuses a path that exists as a file, a non-empty directory without
    a marker, an unreadable marker, or a marker for a different tree
    fingerprint. Without the marker, existing content is somebody
    else's and is never touched.
    """
    if not workdir.exists():
        workdir.mkdir(parents=True, exist_ok=True)
        return
    if not workdir.is_dir():
        raise ExecutionRefused(
            "Terraform workspace refused: the workspace path exists and "
            "is not a directory."
        )
    marker_path = workdir / WORKSPACE_MARKER_FILENAME
    if not marker_path.exists():
        if any(workdir.iterdir()):
            raise ExecutionRefused(
                "Terraform workspace refused: the directory is not empty "
                "and carries no ai-architect workspace marker; refusing "
                "to write into it."
            )
        return
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExecutionRefused(
            "Terraform workspace refused: the workspace marker is "
            f"unreadable ({exc})."
        ) from exc
    if (
        not isinstance(marker, dict)
        or marker.get("kind") != "ai-architect-terraform-workspace"
        or marker.get("fingerprint") != fingerprint
    ):
        raise ExecutionRefused(
            "Terraform workspace refused: the existing workspace was not "
            "created for this exact plan tree."
        )


def _normalize_manifest(manifest) -> list:
    """Canonical (name, sha256) pairs, sorted by name.

    Missing manifest means no local artifacts. Anything else malformed
    is a caller error, refused loudly rather than staged partially.
    """
    if manifest is None:
        return []
    if not isinstance(manifest, list):
        raise ValueError(
            "Artifact manifest must be a list of {name, sha256} entries."
        )
    normalized = []
    for entry in manifest:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise ValueError(
                "Artifact manifest entries must be {name, sha256} records."
            )
        normalized.append((entry["name"], entry["sha256"]))
    return sorted(normalized)


def artifact_manifest_for_spec(spec) -> list:
    """Project the approved code artifacts out of an InfraSpec.

    Only Lambda resources carry a deployment package, referenced by the
    human-supplied basename plus sha256 the contracts validated. Pure
    projection: no filesystem reads, so the manifest is safe to persist
    in the plan record and bind into the approval.
    """
    manifest = []
    for resource in sorted(spec.resources, key=lambda item: item.resource_id):
        if resource.kind is not ResourceKind.LAMBDA:
            continue
        supplied = {
            param.name: param.value for param in resource.parameters
        }
        if (
            "code_artifact_path" in supplied
            and "code_artifact_sha256" in supplied
        ):
            manifest.append(
                {
                    "name": supplied["code_artifact_path"],
                    "sha256": supplied["code_artifact_sha256"],
                }
            )
    return manifest


def load_verified_artifacts(manifest, *, artifact_dir=None) -> dict:
    """Read and hash-verify every manifest artifact. Returns name->bytes.

    Each entry resolves to exactly ``artifact_dir/<basename>`` (default
    the server-side artifact directory); anything that is not a bare
    ``*.zip`` basename, any malformed hash, any missing or unreadable
    file, and any byte mismatch against the approved hash refuses the
    whole workspace before any subprocess starts. Raises ExecutionRefused
    (a refusal, not a crash); raises ValueError only for a malformed
    manifest, which is a caller bug.
    """
    base = (
        Path(artifact_dir)
        if artifact_dir is not None
        else _repository_root().joinpath(*ARTIFACT_SOURCE_DIRPARTS)
    ).resolve()
    staged: dict[str, bytes] = {}
    for name, sha256 in _normalize_manifest(manifest):
        if not _ARTIFACT_NAME_RE.fullmatch(name):
            raise ExecutionRefused(
                "Artifact staging refused: "
                f"{name!r} is not a bare *.zip basename."
            )
        if not _SHA256_RE.fullmatch(sha256):
            raise ExecutionRefused(
                "Artifact staging refused: the approved hash for "
                f"{name!r} is malformed."
            )
        candidate = (base / name).resolve()
        if candidate.parent != base:
            raise ExecutionRefused(
                "Artifact staging refused: "
                f"{name!r} escapes the artifact directory."
            )
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise ExecutionRefused(
                "Artifact staging refused: "
                f"{name!r} is missing or unreadable ({exc})."
            ) from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ExecutionRefused(
                "Artifact staging refused: "
                f"{name!r} does not match its approved hash; the file "
                "changed since the plan was approved."
            )
        if name in staged and staged[name] != data:
            raise ExecutionRefused(
                "Artifact staging refused: "
                f"conflicting bytes for {name!r}."
            )
        staged[name] = data
    return staged


def _normalize_plan_file(plan_file) -> dict | None:
    """Validate the saved-plan binding shape, or return None.

    A binding is exactly the reviewed ``ai-architect.tfplan`` name plus
    its sha256: apply consumes that file by name, so no other name can
    ever be bound.
    """
    if plan_file is None:
        return None
    if (
        not isinstance(plan_file, dict)
        or plan_file.get("name") != SAVED_PLAN_FILENAME
        or not isinstance(plan_file.get("sha256"), str)
        or not _SHA256_RE.fullmatch(plan_file["sha256"])
    ):
        raise ValueError(
            "Saved-plan binding must be {name: "
            + f"{SAVED_PLAN_FILENAME!r}, sha256: 64 lowercase hex}}."
        )
    return {
        "name": SAVED_PLAN_FILENAME,
        "sha256": plan_file["sha256"],
    }


def build_provisioning_approval(
    *,
    approved: bool,
    note: str,
    tree_hash: str,
    spec_id: str,
    artifact_id: str,
    artifacts=None,
    session_id: str = "",
    plan_file=None,
    workspace_hash: str = "",
) -> dict:
    """Build the persisted provisioning-approval record. Plain JSON.

    ``artifacts`` is the plan's artifact manifest ([{name, sha256}]):
    binding it here means a regenerated plan (new tree or new bytes)
    always requires a fresh approval, even when the hashes above are
    somehow unchanged. ``plan_file`` is the reviewed saved-plan binding
    recorded by plan mode, and ``workspace_hash`` is the canonical
    fingerprint of the files+artifacts the approval authorizes: apply
    re-checks both, so an approval can never authorize a different
    saved plan or a drifted workspace.
    """
    normalized_plan_file = _normalize_plan_file(plan_file)
    normalized_workspace_hash = str(workspace_hash or "")
    if normalized_workspace_hash and not _SHA256_RE.fullmatch(
        normalized_workspace_hash
    ):
        raise ValueError(
            "Workspace binding must be the 64-character canonical "
            "workspace fingerprint."
        )
    return {
        "approved": bool(approved),
        "note": note or "",
        "tree_hash": tree_hash,
        "spec_id": spec_id,
        "artifact_id": artifact_id,
        "artifacts": [
            {"name": name, "sha256": sha256}
            for name, sha256 in _normalize_manifest(artifacts)
        ],
        "session_id": str(session_id or ""),
        "plan_file": normalized_plan_file,
        "workspace_hash": normalized_workspace_hash,
        "recorded_at": datetime.utcnow().isoformat(),
    }


def invalidate_provisioning_approval(approval, reason: str) -> dict:
    """Return a persisted approval record that authorizes nothing.

    Used when a fresh plan is generated: the previous human verdict no
    longer covers the current artifact. The record is kept (auditable)
    with ``approved`` forced false and an explicit reason.
    """
    record = dict(approval) if isinstance(approval, dict) else {}
    record.update({
        "approved": False,
        "superseded_at": datetime.utcnow().isoformat(),
        "superseded_reason": reason or "",
    })
    return record


def require_provisioning_approval(
    approval,
    terraform_artifact: dict,
    *,
    session_id: str | None = None,
    review=None,
) -> dict:
    """Return the approval record if it authorizes this exact tree.

    Raises ExecutionRefused naming the mismatch otherwise: missing or
    negative record, or a tree_hash/spec_id/artifact_id/artifacts list
    that does not match the stored Terraform artifact. Drift between
    approval and apply is impossible by construction: the bindings must
    be equal. Records written before the artifacts binding existed
    carry no manifest, so they authorize only artifact-free plans;
    anything else must be re-approved.

    Apply additionally passes ``session_id`` and the stored ``review``:
    then this approval must also belong to the same session and bind the
    review's exact saved-plan file plus the canonical workspace
    fingerprint of the stored tree. Plan mode does not require an
    approval at all, so it never calls this with a review.
    """
    if not isinstance(approval, dict) or not approval.get("approved"):
        raise ExecutionRefused(
            "Terraform execution requires a recorded provisioning "
            "approval: none exists or it was not approved."
        )
    if approval.get("consumed_at"):
        raise ExecutionRefused(
            "Terraform execution refused: this approval was already "
            "consumed by an apply and can never authorize another run. "
            "Review the plan and re-approve it explicitly."
        )
    if not isinstance(terraform_artifact, dict):
        raise ExecutionRefused(
            "Terraform execution requires a stored Terraform artifact: "
            "none exists."
        )
    for key in ("tree_hash", "spec_id", "artifact_id"):
        expected = terraform_artifact.get(key)
        actual = approval.get(key)
        if not expected or actual != expected:
            raise ExecutionRefused(
                "Terraform execution refused: the approval does not "
                f"match the stored Terraform artifact ({key} differs). "
                "Re-approve the current plan explicitly."
            )
    expected_artifacts = _normalize_manifest(
        terraform_artifact.get("artifacts")
    )
    actual_artifacts = _normalize_manifest(approval.get("artifacts"))
    if actual_artifacts != expected_artifacts:
        raise ExecutionRefused(
            "Terraform execution refused: the approval does not match "
            "the stored Terraform artifact (artifacts differ). "
            "Re-approve the current plan explicitly."
        )
    if session_id is not None and approval.get("session_id") != session_id:
        raise ExecutionRefused(
            "Terraform execution refused: the approval was recorded for "
            "a different session than this plan tree. Re-approve the "
            "current plan explicitly."
        )
    if review is not None:
        try:
            approved_plan_file = _normalize_plan_file(
                approval.get("plan_file")
            )
            reviewed_plan_file = (
                _normalize_plan_file(review.get("plan_file"))
                if isinstance(review, dict)
                else None
            )
        except ValueError as exc:
            raise ExecutionRefused(
                "Terraform execution refused: the stored saved-plan "
                f"binding is malformed ({exc}). Re-approve the current "
                "plan explicitly."
            ) from exc
        if (
            approved_plan_file is None
            or reviewed_plan_file is None
            or approved_plan_file != reviewed_plan_file
        ):
            raise ExecutionRefused(
                "Terraform execution refused: the approval does not bind "
                "the exact saved plan reviewed for this tree. Re-approve "
                "the current plan explicitly."
            )
        expected_workspace_hash = workspace_fingerprint(
            dict(terraform_artifact.get("files") or {}),
            terraform_artifact.get("artifacts"),
        )
        if approval.get("workspace_hash") != expected_workspace_hash:
            raise ExecutionRefused(
                "Terraform execution refused: the approval does not bind "
                "the exact workspace fingerprint of the stored plan "
                "tree. Re-approve the current plan explicitly."
            )
    return approval


def consume_provisioning_approval(approval: dict) -> dict:
    """Return the approval marked consumed: approvals are single-use.

    An apply consumes its approval so a spent authorization can never
    authorize a second mutation; re-approving the reviewed plan issues
    a fresh record.
    """
    return {**approval, "consumed_at": datetime.utcnow().isoformat()}


def require_provisioning_review(
    provisioning_result, terraform_artifact: dict
) -> dict:
    """Return the stored review that lets an apply proceed on this tree.

    Apply executes only a plan whose live ``terraform plan`` ran and was
    stored ok against the current artifact: mode "plan", status
    "plan_ok", same tree_hash, and a binding to the exact saved plan
    file it produced ({name, sha256}). Anything else — no review, a
    failed review, a review of a different tree, or a review without
    the saved-plan binding — is refused with the concrete reason; run
    plan mode and review its output first.
    """
    if (
        not isinstance(terraform_artifact, dict)
        or not terraform_artifact.get("tree_hash")
    ):
        raise ExecutionRefused(
            "Terraform apply refused: no stored plan tree to review."
        )
    if not isinstance(provisioning_result, dict):
        raise ExecutionRefused(
            "Terraform apply refused: no reviewed plan is stored; run "
            "plan mode and review its output before applying."
        )
    status = provisioning_result.get("status")
    if (
        provisioning_result.get("mode") != "plan"
        or status != "plan_ok"
    ):
        raise ExecutionRefused(
            "Terraform apply refused: the stored provision result is "
            f"not a successful plan review (mode "
            f"{provisioning_result.get('mode')!r}, status {status!r}); "
            "run plan mode and review its output before applying."
        )
    if (
        provisioning_result.get("tree_hash")
        != terraform_artifact.get("tree_hash")
    ):
        raise ExecutionRefused(
            "Terraform apply refused: the stored plan review is for a "
            "different plan tree; run plan mode again on the current "
            "plan and review it before applying."
        )
    plan_file = provisioning_result.get("plan_file")
    if (
        not isinstance(plan_file, dict)
        or plan_file.get("name") != SAVED_PLAN_FILENAME
        or not isinstance(plan_file.get("sha256"), str)
        or not _SHA256_RE.fullmatch(plan_file.get("sha256"))
    ):
        raise ExecutionRefused(
            "Terraform apply refused: the stored plan review does not "
            "bind the exact saved plan file it reviewed; run plan mode "
            "again and review its output before applying."
        )
    return provisioning_result


def require_destroy_plan_review(
    provisioning_result, terraform_artifact: dict
) -> dict:
    """Return the stored destroy-plan review for this exact tree.

    Destruction executes only after its own ``terraform plan -destroy``
    ran and was stored ok against the current artifact: mode
    "destroy_plan", status "destroy_plan_ok", same tree_hash, and a
    binding to :data:`DESTROY_PLAN_FILENAME` with its sha256. An apply
    review can never authorize a destroy and vice versa.
    """
    if (
        not isinstance(terraform_artifact, dict)
        or not terraform_artifact.get("tree_hash")
    ):
        raise ExecutionRefused(
            "Terraform destroy refused: no stored plan tree to review."
        )
    if not isinstance(provisioning_result, dict):
        raise ExecutionRefused(
            "Terraform destroy refused: no reviewed destroy plan is "
            "stored; run destroy-plan mode and review its output first."
        )
    status = provisioning_result.get("status")
    if (
        provisioning_result.get("mode") != "destroy_plan"
        or status != "destroy_plan_ok"
    ):
        raise ExecutionRefused(
            "Terraform destroy refused: the stored provision result is "
            f"not a successful destroy-plan review (mode "
            f"{provisioning_result.get('mode')!r}, status {status!r}); "
            "run destroy-plan mode and review its output first."
        )
    if (
        provisioning_result.get("tree_hash")
        != terraform_artifact.get("tree_hash")
    ):
        raise ExecutionRefused(
            "Terraform destroy refused: the stored destroy-plan review "
            "is for a different plan tree; run destroy-plan mode again "
            "on the current plan and review it before destroying."
        )
    plan_file = provisioning_result.get("plan_file")
    if (
        not isinstance(plan_file, dict)
        or plan_file.get("name") != DESTROY_PLAN_FILENAME
        or not isinstance(plan_file.get("sha256"), str)
        or not _SHA256_RE.fullmatch(plan_file.get("sha256"))
    ):
        raise ExecutionRefused(
            "Terraform destroy refused: the stored destroy-plan review "
            "does not bind the exact saved destroy plan file it "
            "reviewed; run destroy-plan mode again and review its "
            "output before destroying."
        )
    return provisioning_result


def build_provisioning_result(
    plan: dict, mode: str, outcome: dict
) -> dict:
    """One plain-JSON result record for a stored provisioning run.

    ``plan_file`` carries the plan-mode binding ({name, sha256} of the
    saved plan file the review covers); apply outcomes do not bind one,
    so it is None there. ``outputs`` and ``smoke`` are the post-apply
    inspection records served to the operator; they are None for modes
    that do not inspect a deployment.
    """
    return {
        "status": outcome["status"],
        "mode": mode,
        "artifact_id": plan.get("artifact_id", ""),
        "tree_hash": plan.get("tree_hash", ""),
        "exit_codes": outcome.get("exit_codes", {}),
        "output": outcome.get("output", ""),
        "summary": outcome.get("summary"),
        "plan_file": outcome.get("plan_file"),
        "outputs": outcome.get("outputs"),
        "smoke": outcome.get("smoke"),
        "recorded_at": datetime.utcnow().isoformat(),
    }


def build_pending_apply_record(plan: dict) -> dict:
    """The apply_running marker persisted before an apply starts.

    While this marker is stored, no further apply is allowed on the
    session: an interrupted apply is an explicit, inspectable state
    (workspace under var/terraform), never a silent retry.
    """
    return build_provisioning_result(
        plan, "apply", {"status": "apply_running"}
    )


def _bound_output(text: str, limit: int) -> str:
    """Bound output while preserving both ends.

    Terraform puts progress at the start and the failure reason at the
    end, so a head-only cut hides exactly what the operator needs.
    """
    if len(text) <= limit:
        return text
    marker = "\n[truncated]\n"
    budget = max(0, limit - len(marker))
    tail = budget // 3
    head = budget - tail
    return text[:head] + marker + text[-tail:]


def _sanitize(text: str) -> str:
    """Strip credential material and bound the length. Never logs env."""
    if not text:
        return ""
    cleaned = _SECRET_PATTERN.sub("[redacted-secret]", text)
    return _bound_output(cleaned, _MAX_OUTPUT).strip()


def _safe_relative(value: str) -> str:
    """Validate a plan-tree relative path. Raises ExecutionRefused."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ExecutionRefused(
            f"Terraform workspace refused: {value!r} is not a relative path."
        )
    parts = value.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ExecutionRefused(
            f"Terraform workspace refused: {value!r} escapes the workspace."
        )
    return value


def _saved_plan_record(
    workdir: Path, *, filename: str = SAVED_PLAN_FILENAME
) -> dict | None:
    """Hash the saved plan file, or None when it is absent/unreadable."""
    try:
        data = (workdir / filename).read_bytes()
    except OSError:
        return None
    return {
        "name": filename,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _require_saved_plan(
    workdir: Path,
    expected_sha256,
    *,
    filename: str = SAVED_PLAN_FILENAME,
    operation: str = "apply",
) -> None:
    """Refuse unless the exact reviewed saved plan is present.

    ``expected_sha256`` is the binding recorded by plan mode in the
    stored review; the file must exist in this same workspace and hash-
    match it. Existence alone never proves the file is the reviewed
    plan, so a missing, malformed, or mismatched binding refuses here,
    before any subprocess starts.
    """
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256_RE.fullmatch(expected_sha256)
    ):
        raise ExecutionRefused(
            f"Terraform {operation} refused: no reviewed saved plan is "
            "bound to this operation; run plan mode and review its "
            "output first."
        )
    plan_path = workdir / filename
    if not plan_path.is_file():
        raise ExecutionRefused(
            f"Terraform {operation} refused: the reviewed saved plan "
            f"{filename!r} is missing from the workspace; run "
            "plan mode again and review its output before applying."
        )
    try:
        data = plan_path.read_bytes()
    except OSError as exc:
        raise ExecutionRefused(
            f"Terraform {operation} refused: the reviewed saved plan "
            f"{filename!r} is unreadable ({exc}); run plan "
            "mode again and review its output before applying."
        ) from exc
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ExecutionRefused(
            f"Terraform {operation} refused: the saved plan file does not "
            "match the reviewed plan; run plan mode again and review its "
            "output before applying."
        )


def _write_workspace(
    files: dict[str, str],
    staged_artifacts: dict,
    *,
    workdir: Path | None = None,
    fingerprint: str | None = None,
) -> Path:
    """Materialize a workspace: plan files plus verified bytes.

    With ``workdir=None`` an isolated temp directory is created and the
    caller removes it. With an explicit durable ``workdir`` the marker
    protocol applies: the directory must be absent, empty, or carry a
    marker for exactly this tree fingerprint; anything else refuses
    before a single byte is written, and nothing is ever removed.
    ``staged_artifacts`` maps bare basenames to already hash-verified
    bytes (see load_verified_artifacts); nothing else from the
    filesystem can enter.
    """
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="ai-architect-tf-"))
    else:
        _prepare_durable_workspace(workdir, fingerprint)
        marker = {
            "kind": "ai-architect-terraform-workspace",
            "fingerprint": fingerprint,
            "created_at": datetime.utcnow().isoformat(),
        }
        (workdir / WORKSPACE_MARKER_FILENAME).write_text(
            json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8"
        )
    for relative, content in sorted(files.items()):
        _safe_relative(relative)
        target = workdir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    for name, data in sorted(staged_artifacts.items()):
        _safe_relative(name)
        (workdir / name).write_bytes(data)
    return workdir


def _minimal_env(extra: dict[str, str] | None = None) -> dict:
    """Bare child environment plus ONLY explicitly supplied entries."""
    # The home-directory variables are required on Windows: without
    # them terraform's provider installer cannot locate its plugin
    # cache and init fails before anything else runs.
    keep = ("PATH", "SystemRoot", "TEMP", "TMP", "HOME",
            "PATHEXT", "ComSpec", "WINDIR", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["CHECKPOINT_DISABLE"] = "1"
    for key, value in (extra or {}).items():
        if key in FORWARDABLE_AWS_ENV and isinstance(value, str) and value:
            env[key] = value
    return env


def terraform_available() -> bool:
    """Whether the terraform executable exists on PATH."""
    return shutil.which("terraform") is not None


def _parse_plan_summary(output: str) -> dict | None:
    """Extract the add/change/destroy line from plan output, if present."""
    match = _PLAN_SUMMARY_RE.search(output or "")
    if not match:
        return None
    return {
        "add": int(match.group("add")),
        "change": int(match.group("change")),
        "destroy": int(match.group("destroy")),
    }


def _read_outputs(workdir: Path, env: dict) -> dict:
    """Read root outputs after apply, never failing the apply itself.

    The rendered root ``outputs.tf`` exposes every selected module's
    declared outputs under ``<instance_id>__<name>``. This is a
    read-only inspection step: a Terraform failure here is recorded as
    ``status: "unavailable"`` rather than turning a successful apply
    into a failure.
    """
    try:
        observed = subprocess.run(
            ["terraform", "output", "-json", "-no-color"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "error": _sanitize(str(exc))}
    if observed.returncode != 0:
        return {
            "status": "unavailable",
            "error": _sanitize(observed.stderr or observed.stdout),
        }
    try:
        values = json.loads(observed.stdout)
    except (TypeError, ValueError) as exc:
        return {"status": "unavailable", "error": _sanitize(str(exc))}
    if not isinstance(values, dict):
        return {
            "status": "unavailable",
            "error": "terraform output -json did not return an object.",
        }
    return {"status": "ok", "values": values}


def _smoke_http(outputs: dict) -> dict:
    """HTTP-probe the first API endpoint output, if one is present.

    Only a reachable, non-5xx response counts as a passing smoke check.
    No endpoint output means the design has no HTTP surface to probe,
    which is recorded explicitly rather than treated as a failure.
    """
    values = outputs.get("values") if outputs.get("status") == "ok" else None
    if not isinstance(values, dict):
        return {
            "status": "not_checked",
            "reason": "Terraform outputs were unavailable.",
        }
    candidates = []
    for name, entry in values.items():
        value = entry.get("value") if isinstance(entry, dict) else entry
        if (
            name.endswith("__api_endpoint")
            and isinstance(value, str)
            and value.startswith(("http://", "https://"))
        ):
            candidates.append((name, value))
    candidates.sort()
    if not candidates:
        return {
            "status": "not_applicable",
            "reason": "The deployed stack exposes no HTTP endpoint output.",
        }
    output_name, url = candidates[0]
    try:
        with urllib.request.urlopen(
            url, timeout=_SMOKE_TIMEOUT_SECONDS
        ) as response:
            status_code = response.status
            response.read(1)
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {
            "status": "unreachable",
            "output": output_name,
            "url": url,
            "error": _sanitize(str(exc)),
        }
    return {
        "status": "ok" if status_code < 500 else "failed",
        "output": output_name,
        "url": url,
        "http_status": status_code,
    }


def _record_deployment(
    workdir: Path, result: dict, filename: str = "deployment.json"
) -> None:
    """Persist deployment metadata beside terraform's own state file.

    Best-effort only: terraform's terraform.tfstate in the same
    directory is the authoritative machine record; this adds the
    executor's own outcome for human inspection.
    """
    record = {
        "status": result.get("status"),
        "exit_codes": result.get("exit_codes", {}),
        "output": result.get("output", ""),
        "smoke": result.get("smoke"),
        "recorded_at": datetime.utcnow().isoformat(),
    }
    try:
        (workdir / filename).write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def seed_state_from_workspace(
    source_dir: str | Path, target_dir: str | Path
) -> bool:
    """Carry a prior partial-apply state into a new workspace.

    Only Terraform's own ``terraform.tfstate`` is copied, only between
    workspaces of the same session, and never over an existing target
    state. A corrected plan can then reconcile the resources a failed
    apply already created instead of trying to create them again.
    """
    source = Path(source_dir) / "terraform.tfstate"
    target = Path(target_dir) / "terraform.tfstate"
    if not source.is_file() or target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


def run_terraform_plan(
    files: dict[str, str],
    *,
    extra_env: dict[str, str] | None = None,
    artifact_manifest=None,
    artifact_dir=None,
    workspace_dir=None,
    seed_state_from: str | Path | None = None,
) -> dict:
    """Run ``terraform init`` + ``terraform plan`` on an isolated copy.

    The caller must already hold a matching provisioning approval;
    this function executes only. ``artifact_manifest`` ([{name, sha256}],
    as bound in the approval) stages hash-verified local artifacts
    alongside the tree; ``artifact_dir`` overrides the server-side
    artifact directory (tests only — production callers never pass it).
    ``workspace_dir`` switches from the throwaway temp workspace to a
    durable, inspectable one (marker protocol; nothing is removed
    afterward), so the terraform state and the reviewable plan file
    survive the run. ``seed_state_from`` optionally points at a prior
    workspace whose state is carried into this one first (a failed
    apply's partial state), so the review covers reconciliation, not
    duplicate creation. A successful plan records the saved plan file's
    ``{name, sha256}`` binding in the result, so the stored review can
    bind exactly those bytes for apply. Returns a plain-JSON result
    with sanitized output. Raises ExecutionRefused when terraform is
    absent. Any other failure is a recorded result, never an
    exception: a failed plan is data, not a crash.
    """
    if not terraform_available():
        raise ExecutionRefused(
            "Terraform executable is not installed; plan was not run "
            "and nothing is claimed to have been planned."
        )
    if not files:
        raise ExecutionRefused(
            "Terraform plan refused: the stored plan has no files."
        )
    staged = load_verified_artifacts(
        artifact_manifest, artifact_dir=artifact_dir
    )
    durable = None if workspace_dir is None else Path(workspace_dir)
    workdir = _write_workspace(
        files,
        staged,
        workdir=durable,
        fingerprint=workspace_fingerprint(files, artifact_manifest),
    )
    seeded = (
        seed_state_from_workspace(seed_state_from, workdir)
        if seed_state_from is not None
        else False
    )
    seed_note = (
        "[ai-architect] Reused Terraform state from this session's "
        "previous failed apply; resources created then are not recreated.\n"
        if seeded
        else ""
    )
    try:
        env = _minimal_env(extra_env)
        init = subprocess.run(
            ["terraform", "init", "-no-color", "-input=false"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False, env=env,
        )
        if init.returncode != 0:
            return {
                "status": "plan_init_failed",
                "exit_codes": {"init": init.returncode},
                "output": _sanitize(
                    seed_note + f"{init.stdout}\n{init.stderr}"
                ),
                "summary": None,
            }
        plan = subprocess.run(
            ["terraform", "plan", "-no-color", "-input=false",
             "-out", SAVED_PLAN_FILENAME],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False, env=env,
        )
        raw_output = seed_note + f"{plan.stdout}\n{plan.stderr}"
        # The add/change/destroy line is parsed from the full output
        # before sanitization truncates it; long plans would otherwise
        # lose exactly the line the operator must review.
        summary = _parse_plan_summary(raw_output)
        output = _sanitize(raw_output)
        if plan.returncode != 0:
            return {
                "status": "plan_failed",
                "exit_codes": {
                    "init": init.returncode, "plan": plan.returncode
                },
                "output": output,
                "summary": summary,
            }
        # Exit 0 without the promised plan file is not a reviewable
        # plan: nothing downstream could apply the exact reviewed bytes.
        plan_file = _saved_plan_record(workdir)
        if plan_file is None:
            return {
                "status": "plan_failed",
                "exit_codes": {
                    "init": init.returncode, "plan": plan.returncode
                },
                "output": (
                    f"{output}\n[ai-architect] terraform plan exited 0 "
                    f"but did not produce {SAVED_PLAN_FILENAME}; the plan "
                    "cannot be reviewed or applied."
                ).strip(),
                "summary": summary,
            }
        return {
            "status": "plan_ok",
            "exit_codes": {
                "init": init.returncode, "plan": plan.returncode
            },
            "output": output,
            "summary": summary,
            "plan_file": plan_file,
        }
    finally:
        if durable is None:
            shutil.rmtree(workdir, ignore_errors=True)


def run_terraform_destroy_plan(
    files: dict[str, str],
    *,
    extra_env: dict[str, str] | None = None,
    artifact_manifest=None,
    artifact_dir=None,
    workspace_dir=None,
) -> dict:
    """Save a reviewed destroy plan for an existing durable workspace.

    Destruction is never implied by editing or discarding a plan: it is
    its own reviewed operation. The plan is written to
    :data:`DESTROY_PLAN_FILENAME`, which is a different file from the
    apply plan, so neither can be replayed as the other. A durable
    workspace is required because destroy must see the state the
    reviewed apply created.
    """
    if not terraform_available():
        raise ExecutionRefused(
            "Terraform executable is not installed; destroy plan was not "
            "run."
        )
    if not files:
        raise ExecutionRefused(
            "Terraform destroy plan refused: the stored plan has no files."
        )
    if workspace_dir is None:
        raise ExecutionRefused(
            "Terraform destroy plan refused: a durable workspace is "
            "required so the existing state can be inspected."
        )
    staged = load_verified_artifacts(
        artifact_manifest, artifact_dir=artifact_dir
    )
    workdir = _write_workspace(
        files,
        staged,
        workdir=Path(workspace_dir),
        fingerprint=workspace_fingerprint(files, artifact_manifest),
    )
    env = _minimal_env(extra_env)
    init = subprocess.run(
        ["terraform", "init", "-no-color", "-input=false"],
        cwd=str(workdir), capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False, env=env,
    )
    if init.returncode != 0:
        return {
            "status": "destroy_plan_init_failed",
            "exit_codes": {"init": init.returncode},
            "output": _sanitize(f"{init.stdout}\n{init.stderr}"),
            "summary": None,
        }
    planned = subprocess.run(
        ["terraform", "plan", "-destroy", "-no-color", "-input=false",
         "-out", DESTROY_PLAN_FILENAME],
        cwd=str(workdir), capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False, env=env,
    )
    raw_output = f"{planned.stdout}\n{planned.stderr}"
    summary = _parse_plan_summary(raw_output)
    output = _sanitize(raw_output)
    if planned.returncode != 0:
        return {
            "status": "destroy_plan_failed",
            "exit_codes": {
                "init": init.returncode, "plan": planned.returncode,
            },
            "output": output,
            "summary": summary,
        }
    plan_file = _saved_plan_record(
        workdir, filename=DESTROY_PLAN_FILENAME
    )
    if plan_file is None:
        return {
            "status": "destroy_plan_failed",
            "exit_codes": {
                "init": init.returncode, "plan": planned.returncode,
            },
            "output": (
                f"{output}\n[ai-architect] terraform destroy plan exited "
                f"0 but did not produce {DESTROY_PLAN_FILENAME}; the plan "
                "cannot be reviewed or applied."
            ).strip(),
            "summary": summary,
        }
    return {
        "status": "destroy_plan_ok",
        "exit_codes": {
            "init": init.returncode, "plan": planned.returncode,
        },
        "output": output,
        "summary": summary,
        "plan_file": plan_file,
    }


def run_terraform_destroy(
    files: dict[str, str],
    *,
    confirm: str,
    extra_env: dict[str, str] | None = None,
    artifact_manifest=None,
    artifact_dir=None,
    workspace_dir=None,
    destroy_plan_sha256: str | None = None,
) -> dict:
    """Apply ONLY the exact reviewed destroy plan for a durable workspace.

    Requires ``confirm == "DESTROY"`` and a hash-bound
    :data:`DESTROY_PLAN_FILENAME` produced by
    :func:`run_terraform_destroy_plan`. A missing, mismatched, or
    unreadable destroy plan refuses before any subprocess; a fresh
    destroy plan is never generated here.
    """
    if confirm != _DESTROY_CONFIRMATION:
        raise ExecutionRefused(
            "Terraform destroy refused: explicit confirmation "
            f"{_DESTROY_CONFIRMATION!r} was not supplied."
        )
    if not terraform_available():
        raise ExecutionRefused(
            "Terraform executable is not installed; destroy was not run."
        )
    if not files:
        raise ExecutionRefused(
            "Terraform destroy refused: the stored plan has no files."
        )
    if workspace_dir is None:
        raise ExecutionRefused(
            "Terraform destroy refused: a durable workspace is required "
            "so the reviewed state can be destroyed."
        )
    staged = load_verified_artifacts(
        artifact_manifest, artifact_dir=artifact_dir
    )
    workdir = _write_workspace(
        files,
        staged,
        workdir=Path(workspace_dir),
        fingerprint=workspace_fingerprint(files, artifact_manifest),
    )
    env = _minimal_env(extra_env)
    _require_saved_plan(
        workdir,
        destroy_plan_sha256,
        filename=DESTROY_PLAN_FILENAME,
        operation="destroy",
    )
    init = subprocess.run(
        ["terraform", "init", "-no-color", "-input=false"],
        cwd=str(workdir), capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False, env=env,
    )
    if init.returncode != 0:
        result = {
            "status": "destroy_init_failed",
            "exit_codes": {"init": init.returncode},
            "output": _sanitize(f"{init.stdout}\n{init.stderr}"),
        }
    else:
        destroyed = subprocess.run(
            ["terraform", "apply", "-no-color", "-input=false",
             "-auto-approve", DESTROY_PLAN_FILENAME],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False, env=env,
        )
        output = _sanitize(f"{destroyed.stdout}\n{destroyed.stderr}")
        result = {
            "status": (
                "destroy_ok"
                if destroyed.returncode == 0
                else "destroy_failed"
            ),
            "exit_codes": {
                "init": init.returncode, "apply": destroyed.returncode,
            },
            "output": output,
        }
    _record_deployment(workdir, result, filename="destroy.json")
    return result


def run_terraform_drift_check(
    files: dict[str, str],
    *,
    extra_env: dict[str, str] | None = None,
    artifact_manifest=None,
    artifact_dir=None,
    workspace_dir=None,
) -> dict:
    """Refresh-only plan: report drift without changing state.

    Exit codes are Terraform's ``-detailed-exitcode`` contract: 0 means
    no drift, 2 means drift, anything else is an error. Nothing is
    applied, so the operator can inspect the refreshed plan before
    deciding whether to re-plan.
    """
    if not terraform_available():
        raise ExecutionRefused(
            "Terraform executable is not installed; drift check was not "
            "run."
        )
    if not files:
        raise ExecutionRefused(
            "Terraform drift check refused: the stored plan has no files."
        )
    if workspace_dir is None:
        raise ExecutionRefused(
            "Terraform drift check refused: a durable workspace is "
            "required so the existing state can be refreshed."
        )
    staged = load_verified_artifacts(
        artifact_manifest, artifact_dir=artifact_dir
    )
    workdir = _write_workspace(
        files,
        staged,
        workdir=Path(workspace_dir),
        fingerprint=workspace_fingerprint(files, artifact_manifest),
    )
    env = _minimal_env(extra_env)
    init = subprocess.run(
        ["terraform", "init", "-no-color", "-input=false"],
        cwd=str(workdir), capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False, env=env,
    )
    if init.returncode != 0:
        return {
            "status": "drift_error",
            "exit_codes": {"init": init.returncode},
            "output": _sanitize(f"{init.stdout}\n{init.stderr}"),
            "summary": None,
        }
    checked = subprocess.run(
        ["terraform", "plan", "-refresh-only", "-detailed-exitcode",
         "-no-color", "-input=false"],
        cwd=str(workdir), capture_output=True, text=True,
        timeout=_TIMEOUT_SECONDS, check=False, env=env,
    )
    raw_output = f"{checked.stdout}\n{checked.stderr}"
    summary = _parse_plan_summary(raw_output)
    status = {
        0: "no_drift",
        2: "drift",
    }.get(checked.returncode, "drift_error")
    return {
        "status": status,
        "exit_codes": {
            "init": init.returncode, "plan": checked.returncode,
        },
        "output": _sanitize(raw_output),
        "summary": summary,
    }


def run_terraform_apply(
    files: dict[str, str],
    *,
    confirm: str,
    extra_env: dict[str, str] | None = None,
    artifact_manifest=None,
    artifact_dir=None,
    workspace_dir=None,
    saved_plan_sha256: str | None = None,
) -> dict:
    """Run ``terraform init`` + ``terraform apply <saved plan>`` exactly.

    Requires ``confirm == "APPLY"`` in addition to the caller's
    approval record; anything else is refused before any subprocess
    starts. Apply executes ONLY the exact saved plan produced by the
    reviewed plan run: ``saved_plan_sha256`` is that review's binding,
    and the workspace's ``ai-architect.tfplan`` must exist and
    hash-match it — a missing, unreadable, or mismatched saved plan
    refuses before init, and a fresh plan is never generated or
    applied. ``artifact_manifest`` ([{name, sha256}], as bound in the
    approval) stages hash-verified local artifacts alongside the tree
    — without the approved bytes present, apply is refused before init;
    ``artifact_dir`` overrides the server-side artifact directory
    (tests only). ``workspace_dir`` switches from the throwaway temp
    workspace to a durable, inspectable one (marker protocol; nothing
    is removed afterward): terraform's state file and a deployment.json
    outcome record survive for inspection. Returns a plain-JSON result
    with sanitized output.
    """
    if confirm != _APPLY_CONFIRMATION:
        raise ExecutionRefused(
            "Terraform apply refused: explicit confirmation "
            f"{_APPLY_CONFIRMATION!r} was not supplied."
        )
    if not terraform_available():
        raise ExecutionRefused(
            "Terraform executable is not installed; apply was not run."
        )
    if not files:
        raise ExecutionRefused(
            "Terraform apply refused: the stored plan has no files."
        )
    staged = load_verified_artifacts(
        artifact_manifest, artifact_dir=artifact_dir
    )
    durable = None if workspace_dir is None else Path(workspace_dir)
    workdir = _write_workspace(
        files,
        staged,
        workdir=durable,
        fingerprint=workspace_fingerprint(files, artifact_manifest),
    )
    try:
        env = _minimal_env(extra_env)
        _require_saved_plan(workdir, saved_plan_sha256)
        init = subprocess.run(
            ["terraform", "init", "-no-color", "-input=false"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False, env=env,
        )
        if init.returncode != 0:
            result = {
                "status": "apply_init_failed",
                "exit_codes": {"init": init.returncode},
                "output": _sanitize(
                    f"{init.stdout}\n{init.stderr}"
                ),
            }
        else:
            applied = subprocess.run(
                ["terraform", "apply", "-no-color", "-input=false",
                 "-auto-approve", SAVED_PLAN_FILENAME],
                cwd=str(workdir), capture_output=True, text=True,
                timeout=_TIMEOUT_SECONDS, check=False, env=env,
            )
            output = _sanitize(f"{applied.stdout}\n{applied.stderr}")
            if applied.returncode != 0:
                result = {
                    "status": "apply_failed",
                    "exit_codes": {
                        "init": init.returncode,
                        "apply": applied.returncode,
                    },
                    "output": output,
                }
            else:
                result = {
                    "status": "apply_ok",
                    "exit_codes": {
                        "init": init.returncode,
                        "apply": applied.returncode,
                    },
                    "output": output,
                }
                result["outputs"] = _read_outputs(workdir, env)
                result["smoke"] = _smoke_http(result["outputs"])
        if durable is not None:
            _record_deployment(workdir, result)
        return result
    finally:
        if durable is None:
            shutil.rmtree(workdir, ignore_errors=True)
