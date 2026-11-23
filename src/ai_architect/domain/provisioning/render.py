"""Slice 4B rendering contracts: deterministic Terraform render result.

Separate from the Slice 4A provisioning contract on purpose. Slice 4A owns
``InfraSpec``; this module owns only the *result* of rendering it, and never
mutates or extends ``InfraSpec``.

The render result makes four outcomes machine-readable and distinct:

- ``RENDERED``            - a guarded, deterministic source tree exists
- ``GUARD_FAILED``        - the safety guard refused the output
- ``UNREPRESENTABLE``     - a required parameter has no single safe
                            interpretation under the Slice 4A string contract
- ``MISSING_CODE_ARTIFACT`` - the resource cannot be rendered without
                            inventing a code artifact (Lambda in this slice)

Terraform CLI outcomes (unavailable / fmt / init / validate) live in
``services.provisioning.terraform_cli`` so that the renderer itself stays
usable and testable with no Terraform binary present.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RenderStatus(str, Enum):
    """Terminal state of one deterministic render."""

    RENDERED = "rendered"
    GUARD_FAILED = "guard_failed"
    UNREPRESENTABLE = "unrepresentable"
    MISSING_CODE_ARTIFACT = "missing_code_artifact"


class RenderDiagnosticCategory(str, Enum):
    """Why a render did not produce executable Terraform."""

    STRUCTURAL = "structural"
    MISSING_INPUT = "missing_input"
    UNREPRESENTABLE = "unrepresentable"
    GUARD = "guard"
    TOOLING = "tooling"


class RenderErrorCode(str, Enum):
    """Stable codes for deterministic render/guard failure reporting."""

    # A required parameter has no single safe interpretation.
    UNREPRESENTABLE_PARAMETER = "unrepresentable_parameter"
    # Value violates the closed grammar for its parameter.
    MALFORMED_PARAMETER = "malformed_parameter"
    # Value is outside the closed allowlist for its parameter.
    PARAMETER_OUT_OF_ALLOWLIST = "parameter_out_of_allowlist"
    # Parameter references another resource/attribute inconsistently.
    INCONSISTENT_REFERENCE = "inconsistent_resource_reference"
    # Slice 4A has no representation for the code artifact.
    MISSING_CODE_ARTIFACT = "missing_code_artifact"
    # Safety guard refused the rendered source.
    GUARD_VIOLATION = "guard_violation"
    # Guard could not establish safety and failed closed.
    GUARD_UNPARSEABLE = "guard_unparseable"
    # Required Slice 4A parameter absent from the spec.
    MISSING_REQUIRED_INPUT = "missing_required_input"
    # Tooling outcomes.
    TERRAFORM_UNAVAILABLE = "terraform_unavailable"
    TERRAFORM_INIT_FAILED = "terraform_init_failed"
    TERRAFORM_FMT_FAILED = "terraform_fmt_failed"
    TERRAFORM_VALIDATE_FAILED = "terraform_validate_failed"


class RenderDiagnostic(BaseModel):
    """One deterministic finding from the renderer or the guard."""

    category: RenderDiagnosticCategory
    code: RenderErrorCode
    message: str
    resource_id: Optional[str] = None
    parameter: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    def __str__(self) -> str:
        return self.message


class RenderedFile(BaseModel):
    """One file in the generated Terraform source tree."""

    path: str
    content: str

    model_config = ConfigDict(extra="forbid")


class RenderResult(BaseModel):
    """Deterministic Terraform rendering result.

    ``tree_hash`` is a sha256 over the canonical JSON of every
    ``(path, content)`` pair sorted by path, so it is stable across
    machines and independent of dict or insertion order.
    """

    status: RenderStatus
    provider: str
    region_code: str
    files: list[RenderedFile] = Field(default_factory=list)
    tree_hash: str = ""
    diagnostics: list[RenderDiagnostic] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def rendered(self) -> bool:
        return self.status is RenderStatus.RENDERED

    def file(self, path: str) -> Optional[RenderedFile]:
        for entry in self.files:
            if entry.path == path:
                return entry
        return None

    def source_tree(self) -> dict:
        """path -> content, for callers writing the tree to disk."""
        return {entry.path: entry.content for entry in self.files}
