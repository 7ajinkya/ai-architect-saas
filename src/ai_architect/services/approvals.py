"""Human approval rules shared by the API and CLI surfaces.

Approval is the explicit human boundary between design and any
downstream use (Terraform generation, provisioning). Both surfaces
enforce the identical rule through the helpers here, so neither can
drift into accepting an ungated or blocked design.

Approving never provisions anything: it only records the verdict.
Downstream steps re-validate the stored record (approval flag, gate
outcome, artifact identity) before proceeding.
"""

from datetime import datetime

# Gate verdicts under which a human verdict may be recorded.
# approved_for_review: clean design, review and accept.
# requires_user_decision: remaining criticals must be explicitly
# accepted as trade-offs (or rejected) in the approval note.
APPROVABLE_GATE_OUTCOMES = (
    "approved_for_review",
    "requires_user_decision",
)


def design_approval_allowed(snapshot) -> tuple[bool, str]:
    """Whether a design verdict may be recorded for this snapshot.

    Returns (allowed, gate_outcome_or_reason). Never raises for
    content reasons: unreadable state is simply not approvable.
    """
    try:
        gate = snapshot.quality_gate
        outcome = str(getattr(gate, "outcome", "") or "")
    except Exception:  # noqa: BLE001 - unreadable means not approvable
        return False, "none"
    if not outcome or outcome not in APPROVABLE_GATE_OUTCOMES:
        return False, outcome or "none"
    try:
        has_architecture = snapshot.architecture is not None
    except Exception:  # noqa: BLE001 - unreadable means not approvable
        return False, outcome
    if not has_architecture:
        return False, outcome
    return True, outcome


def build_design_approval(
    *,
    approved: bool,
    note: str,
    artifact_id: str,
    gate_outcome: str,
) -> dict:
    """Build the persisted approval record. Plain JSON only."""
    return {
        "approved": bool(approved),
        "note": note or "",
        "artifact_id": artifact_id,
        "gate_outcome": gate_outcome,
        "recorded_at": datetime.utcnow().isoformat(),
    }


def require_approved_design(approval, architecture) -> dict:
    """Return the approval record if it authorizes this exact artifact.

    Raises ValueError naming the mismatch otherwise: missing record,
    explicit rejection, gate outcome outside the approvable set, or an
    artifact id that does not match the architecture being used. Every
    downstream step (Terraform generation, provisioning) calls this
    before doing anything, so a stale or negative approval can never
    silently authorize work.
    """
    if not isinstance(approval, dict) or not approval.get("approved"):
        raise ValueError(
            "Terraform generation requires an approved design: no "
            "positive design approval is recorded."
        )
    gate_outcome = str(approval.get("gate_outcome") or "")
    if gate_outcome not in APPROVABLE_GATE_OUTCOMES:
        raise ValueError(
            "Terraform generation requires an approved design: the "
            f"recorded gate outcome {gate_outcome!r} is not approvable."
        )
    artifact_id = str(getattr(architecture, "artifact_id", "") or "")
    if not artifact_id or approval.get("artifact_id") != artifact_id:
        raise ValueError(
            "Terraform generation requires an approved design: the "
            "recorded approval targets a different architecture "
            "artifact."
        )
    return approval
