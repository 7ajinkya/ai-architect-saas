"""Optional Terraform CLI integration for Slice 4B.

Terraform is treated as an **external executable**, never a Python
dependency. Everything here is optional: the renderer is fully usable and
testable with no ``terraform`` binary present.

Guarantees:

- ``terraform fmt -check`` is used for the non-mutating formatting
  assertion. ``terraform fmt`` is never used to *produce* the
  determinism guarantee; that guarantee is over renderer output.
- ``terraform init`` and ``terraform validate`` run only inside an
  isolated temporary directory that is removed afterwards.
- ``plan``, ``apply`` and ``destroy`` are hard-blocked by an allowlist;
  they cannot be invoked even by a caller mistake.
- No credentials are injected. No environment value is placed into a
  diagnostic. stdout/stderr are captured and sanitized.
- Absence of Terraform is reported as ``NOT_INSTALLED``, never as
  success.
"""

import os
import re
import shutil
import subprocess
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ai_architect.domain.provisioning.render import (
    RenderDiagnostic,
    RenderDiagnosticCategory,
    RenderErrorCode,
)

# Only these subcommands may ever be invoked.
PERMITTED_COMMANDS: frozenset = frozenset({"fmt", "init", "validate"})

# Explicitly never invoked, listed for auditability.
FORBIDDEN_COMMANDS: frozenset = frozenset(
    {"plan", "apply", "destroy", "import", "state", "output", "console",
     "taint", "untaint", "graph", "providers", "refresh", "login"}
)

_TIMEOUT_SECONDS = 300
_MAX_OUTPUT = 4000

_SECRET_PATTERN = re.compile(
    r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----|"
    r"(?i:aws_secret_access_key|aws_session_token|password|secret|token)"
    r"\s*[=:]\s*\S+)",
)


class TerraformStatus(str, Enum):
    """Machine-readable outcome of the CLI checks."""

    NOT_INSTALLED = "not_installed"
    FMT_OK = "fmt_ok"
    FMT_FAILED = "fmt_failed"
    INIT_OK = "init_ok"
    INIT_FAILED = "init_failed"
    VALIDATE_OK = "validate_ok"
    VALIDATE_FAILED = "validate_failed"
    NOT_RUN = "not_run"


class TerraformCheckResult(BaseModel):
    """Structured result of the optional Terraform CLI checks."""

    status: TerraformStatus
    installed: bool
    version: Optional[str] = None
    exit_codes: dict = Field(default_factory=dict)
    diagnostics: list[RenderDiagnostic] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def validated(self) -> bool:
        return self.status is TerraformStatus.VALIDATE_OK


def terraform_available() -> bool:
    return shutil.which("terraform") is not None


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


def _diagnostic(code, message) -> RenderDiagnostic:
    return RenderDiagnostic(
        category=RenderDiagnosticCategory.TOOLING,
        code=code,
        message=message,
    )


def _run(command: list, cwd: Path) -> tuple:
    """Run an allowed Terraform subcommand in an isolated directory.

    Returns (exit_code, sanitized combined output).
    """
    if len(command) < 2 or command[1] not in PERMITTED_COMMANDS:
        raise ValueError(
            f"Terraform subcommand {command[1:2]!r} is not permitted; "
            f"allowed: {', '.join(sorted(PERMITTED_COMMANDS))}."
        )
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
        env=_minimal_env(),
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    return completed.returncode, _sanitize(combined)


def _minimal_env() -> dict:
    """A deliberately bare environment.

    No AWS_* or other credential variable is forwarded, so the child
    process cannot inherit ambient cloud credentials.
    """
    keep = ("PATH", "SystemRoot", "TEMP", "TMP", "HOME", "PATHEXT",
            "ComSpec", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["CHECKPOINT_DISABLE"] = "1"
    return env


def terraform_version() -> Optional[str]:
    if not terraform_available():
        return None
    try:
        completed = subprocess.run(
            ["terraform", "version"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env=_minimal_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _sanitize(completed.stdout.splitlines()[0]) if completed.stdout else None


def run_terraform_checks(source_tree: dict) -> TerraformCheckResult:
    """Run fmt -check, init and validate against an isolated temp copy.

    Never mutates ``source_tree``: the tree is written to a temporary
    directory which is always removed.
    """
    if not terraform_available():
        return TerraformCheckResult(
            status=TerraformStatus.NOT_INSTALLED,
            installed=False,
            diagnostics=[
                _diagnostic(
                    RenderErrorCode.TERRAFORM_UNAVAILABLE,
                    "Terraform executable is not installed; Terraform "
                    "formatting and validation were not run and are not "
                    "claimed to have passed.",
                )
            ],
        )

    version = terraform_version()
    exit_codes: dict = {}
    diagnostics: list = []

    workdir = Path(tempfile.mkdtemp(prefix="infra-spec-terraform-"))
    try:
        for relative, content in sorted(source_tree.items()):
            target = workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

        code, output = _run(["terraform", "fmt", "-check", "-no-color"],
                            workdir)
        exit_codes["fmt"] = code
        if code != 0:
            diagnostics.append(
                _diagnostic(
                    RenderErrorCode.TERRAFORM_FMT_FAILED,
                    f"terraform fmt -check reported formatting drift: {output}",
                )
            )
            return TerraformCheckResult(
                status=TerraformStatus.FMT_FAILED,
                installed=True,
                version=version,
                exit_codes=exit_codes,
                diagnostics=diagnostics,
            )

        code, output = _run(
            ["terraform", "init", "-no-color", "-input=false"], workdir
        )
        exit_codes["init"] = code
        if code != 0:
            diagnostics.append(
                _diagnostic(
                    RenderErrorCode.TERRAFORM_INIT_FAILED,
                    f"terraform init failed; providers were not "
                    f"initialized, so validation was not attempted: {output}",
                )
            )
            return TerraformCheckResult(
                status=TerraformStatus.INIT_FAILED,
                installed=True,
                version=version,
                exit_codes=exit_codes,
                diagnostics=diagnostics,
            )

        code, output = _run(["terraform", "validate", "-no-color"], workdir)
        exit_codes["validate"] = code
        if code != 0:
            diagnostics.append(
                _diagnostic(
                    RenderErrorCode.TERRAFORM_VALIDATE_FAILED,
                    f"terraform validate failed: {output}",
                )
            )
            return TerraformCheckResult(
                status=TerraformStatus.VALIDATE_FAILED,
                installed=True,
                version=version,
                exit_codes=exit_codes,
                diagnostics=diagnostics,
            )

        return TerraformCheckResult(
            status=TerraformStatus.VALIDATE_OK,
            installed=True,
            version=version,
            exit_codes=exit_codes,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def assert_permitted(command: list) -> None:
    """Public guard so a caller mistake fails loudly rather than executing."""
    if len(command) < 2 or command[1] not in PERMITTED_COMMANDS:
        raise ValueError(
            f"Refusing to run Terraform subcommand {command[1:2]!r}; "
            f"only {', '.join(sorted(PERMITTED_COMMANDS))} are permitted."
        )
