"""Provision command: execute a stored plan, or show refusal.

Usage:
    ai-architect provision --session-id <id> --mode plan
    ai-architect provision --session-id <id> --mode apply
        --confirm APPLY [--pass-aws-env]
    ai-architect provision --session-id <id> --mode destroy-plan
    ai-architect provision --session-id <id> --mode destroy
        --confirm DESTROY [--pass-aws-env]
    ai-architect provision --session-id <id> --mode drift
        [--pass-aws-env]

Plan mode is a read-only preview and needs no provisioning approval:
it runs ``terraform init`` + ``plan`` in a durable workspace under
``var/terraform/<session>/<tree>`` and prints the sanitized result; the
workspace (terraform state, plan file) is kept for inspection. Apply
mode additionally requires ``--confirm APPLY``, a stored plan review
for the same tree (run plan mode first), and a provisioning approval
bound to that review's exact saved plan file and the workspace
fingerprint; it executes the reviewed saved plan file — never a freshly
computed plan — consumes the approval (single use) and records an
apply_running marker before anything runs, so a duplicate or
interrupted apply is an explicit state, never a silent retry.

Destroy is its own reviewed operation: ``destroy-plan`` saves a
distinct ``terraform plan -destroy`` file, and ``destroy`` requires
``--confirm DESTROY`` plus that stored review bound to the destroy
plan's exact sha256. An apply review can never authorize a destroy.
Drift mode runs a refresh-only plan and reports ``no_drift``,
``drift``, or ``drift_error`` without mutating state. All modes refuse
when the stored plan does not match the session's current architecture
artifact; anything refused stops before any subprocess starts.

``--pass-aws-env`` forwards an explicit allowlist of AWS_* entries
from the operator's own environment into the child process. Without
it, the child receives no credentials at all.
"""

import argparse
import asyncio
import os

from rich.console import Console

from ai_architect.cli.text import sanitize_console_text
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
    save_session_record,
)
from ai_architect.services.provisioning.executor import (
    FORWARDABLE_AWS_ENV,
    ExecutionRefused,
    build_pending_apply_record,
    build_provisioning_result,
    consume_provisioning_approval,
    require_destroy_plan_review,
    require_provisioning_approval,
    require_provisioning_review,
    run_terraform_apply,
    run_terraform_destroy,
    run_terraform_destroy_plan,
    run_terraform_drift_check,
    run_terraform_plan,
    terraform_available,
    workspace_path,
)

console = Console()


def _safe(text: object) -> str:
    return sanitize_console_text(text)


def _forwarded_env(pass_flag: bool) -> dict:
    """Explicitly forwarded AWS entries, or nothing at all."""
    if not pass_flag:
        return {}
    return {
        key: os.environ[key]
        for key in FORWARDABLE_AWS_ENV
        if os.environ.get(key)
    }


def render_result(result: dict, mode: str) -> None:
    """Print an executor outcome without inventing success."""
    console.print(
        f"[bold blue]Provision {mode}: "
        f"{_safe(result.get('status'))}[/bold blue]"
    )
    for key, value in sorted((result.get("exit_codes") or {}).items()):
        console.print(f"  exit {key}: {value}")
    summary = result.get("summary")
    if isinstance(summary, dict):
        console.print(
            f"  Plan summary: +{summary.get('add')} "
            f"~{summary.get('change')} -{summary.get('destroy')}"
        )
    output = result.get("output") or ""
    if output:
        console.print("  --- output (sanitized) ---")
        console.print(_safe(output))


async def _provision_async(args) -> int:
    if args.mode not in (
        "plan", "apply", "destroy-plan", "destroy", "drift",
    ):
        console.print(
            f"[red]Unknown mode {args.mode!r}: use plan, apply, "
            "destroy-plan, destroy, or drift.[/red]"
        )
        return 1
    try:
        snapshot = await load_design_snapshot(args.session_id)
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Could not load session: {_safe(e)}[/red]")
        return 1
    if snapshot is None or snapshot.requirements_state is None:
        console.print(
            f"[red]Unknown session: {_safe(args.session_id)}[/red]"
        )
        return 1

    plan = snapshot.terraform_artifact
    if not isinstance(plan, dict) or not plan.get("files"):
        console.print(
            "[red]Provisioning is not available: the session has no "
            "stored Terraform plan with files.[/red]"
        )
        return 1
    architecture = getattr(snapshot, "architecture", None)
    architecture_id = str(getattr(architecture, "artifact_id", "") or "")
    if not architecture_id:
        console.print(
            "[red]Provisioning is not available: the session has no "
            "current architecture artifact.[/red]"
        )
        return 1
    if plan.get("artifact_id") != architecture_id:
        console.print(
            "[red]Provisioning is not available: the stored Terraform "
            "plan does not match the session's current architecture "
            "artifact. Regenerate the plan first.[/red]"
        )
        return 1

    if args.mode == "apply" and args.confirm != "APPLY":
        console.print(
            '[red]Apply refused: pass --confirm APPLY to confirm '
            "infrastructure mutation explicitly.[/red]"
        )
        return 1
    if args.mode == "destroy" and args.confirm != "DESTROY":
        console.print(
            '[red]Destroy refused: pass --confirm DESTROY to confirm '
            "infrastructure destruction explicitly.[/red]"
        )
        return 1

    review = snapshot.provisioning_result
    if args.mode == "apply":
        try:
            require_provisioning_approval(
                snapshot.provisioning_approval, plan
            )
        except ExecutionRefused as e:
            console.print(f"[red]{_safe(e)}[/red]")
            return 1
        if (
            isinstance(review, dict)
            and review.get("mode") == "apply"
            and review.get("status") == "apply_running"
        ):
            console.print(
                "[red]A previous apply did not finish. Inspect the "
                f"terraform workspace (the state is preserved there), "
                "then run plan mode again before applying.[/red]"
            )
            return 1
        try:
            review = require_provisioning_review(review, plan)
        except ExecutionRefused as e:
            console.print(f"[red]{_safe(e)}[/red]")
            return 1
        try:
            require_provisioning_approval(
                snapshot.provisioning_approval,
                plan,
                session_id=args.session_id,
                review=review,
            )
        except ExecutionRefused as e:
            console.print(f"[red]{_safe(e)}[/red]")
            return 1
    elif args.mode == "destroy":
        if (
            isinstance(review, dict)
            and review.get("mode") == "destroy"
            and review.get("status") == "destroy_running"
        ):
            console.print(
                "[red]A previous destroy did not finish. Inspect the "
                "terraform workspace (the state is preserved there), "
                "run destroy-plan mode again, and review its output "
                "before destroying.[/red]"
            )
            return 1
        try:
            review = require_destroy_plan_review(review, plan)
        except ExecutionRefused as e:
            console.print(f"[red]{_safe(e)}[/red]")
            return 1

    try:
        workspace = workspace_path(args.session_id, plan.get("tree_hash", ""))
    except ExecutionRefused as e:
        console.print(f"[red]{_safe(e)}[/red]")
        return 1

    marker_written = False
    if not terraform_available():
        console.print(
            "[yellow]Terraform executable is not installed; nothing "
            "ran and nothing is claimed.[/yellow]"
        )
        return 1

    extra = _forwarded_env(bool(args.pass_aws_env))
    if args.mode in ("apply", "destroy", "drift") and not extra:
        console.print(
            "[yellow]Warning: no AWS credentials forwarded "
            "(--pass-aws-env not given); the operation will fail "
            "against real AWS without them.[/yellow]"
        )

    if args.mode == "apply":
        # Single-use authorization plus an explicit apply_running
        # marker: a duplicate or interrupted apply can never run
        # silently again.
        try:
            await save_session_record(
                args.session_id,
                field="provisioning_result",
                record=build_pending_apply_record(plan),
            )
            await save_session_record(
                args.session_id,
                field="provisioning_approval",
                record=consume_provisioning_approval(
                    snapshot.provisioning_approval
                ),
            )
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            console.print(
                f"[red]Could not record pending apply: {_safe(e)}[/red]"
            )
            return 1
        marker_written = True
    elif args.mode == "destroy":
        # A destroy review authorizes exactly one run: replacing it with
        # this marker means an interrupted destroy is an explicit state
        # and a fresh destroy plan is required before retrying.
        try:
            await save_session_record(
                args.session_id,
                field="provisioning_result",
                record=build_provisioning_result(
                    plan, "destroy", {"status": "destroy_running"}
                ),
            )
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            console.print(
                f"[red]Could not record pending destroy: {_safe(e)}[/red]"
            )
            return 1
        marker_written = True

    try:
        manifest = plan.get("artifacts")
        if args.mode == "apply":
            # The exact reviewed saved plan: its hash is the binding
            # recorded by the plan review and is re-verified against
            # the workspace file by the executor before anything runs.
            outcome = run_terraform_apply(
                dict(plan["files"]), confirm=args.confirm, extra_env=extra,
                artifact_manifest=manifest, workspace_dir=workspace,
                saved_plan_sha256=review["plan_file"]["sha256"],
            )
        elif args.mode == "destroy-plan":
            outcome = run_terraform_destroy_plan(
                dict(plan["files"]), extra_env=extra,
                artifact_manifest=manifest, workspace_dir=workspace,
            )
        elif args.mode == "destroy":
            outcome = run_terraform_destroy(
                dict(plan["files"]), confirm=args.confirm, extra_env=extra,
                artifact_manifest=manifest, workspace_dir=workspace,
                destroy_plan_sha256=review["plan_file"]["sha256"],
            )
        elif args.mode == "drift":
            outcome = run_terraform_drift_check(
                dict(plan["files"]), extra_env=extra,
                artifact_manifest=manifest, workspace_dir=workspace,
            )
        else:
            outcome = run_terraform_plan(
                dict(plan["files"]), extra_env=extra,
                artifact_manifest=manifest, workspace_dir=workspace,
            )
    except ExecutionRefused as e:
        if marker_written:
            # Nothing ran: restore the reviewed plan (and, for apply,
            # the unconsumed approval) so a retry stays possible.
            try:
                await save_session_record(
                    args.session_id,
                    field="provisioning_result",
                    record=dict(snapshot.provisioning_result),
                )
                if args.mode == "apply":
                    await save_session_record(
                        args.session_id,
                        field="provisioning_approval",
                        record=dict(snapshot.provisioning_approval),
                    )
            except Exception as restore_error:  # noqa: BLE001
                console.print(
                    "[red]Could not restore pre-run records: "
                    f"{_safe(restore_error)}[/red]"
                )
                return 1
        console.print(f"[red]{_safe(e)}[/red]")
        return 1

    record_mode = "destroy_plan" if args.mode == "destroy-plan" else args.mode
    result = build_provisioning_result(plan, record_mode, outcome)
    try:
        await save_session_record(
            args.session_id, field="provisioning_result", record=result
        )
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Could not record result: {_safe(e)}[/red]")
        return 1

    render_result(result, args.mode)
    ok = outcome["status"] in (
        "plan_ok", "apply_ok", "destroy_plan_ok", "destroy_ok",
        "no_drift", "drift",
    )
    return 0 if ok else 1


def add_provision_parser(sub) -> None:
    provision_parser = sub.add_parser(
        "provision",
        help="Execute a stored, approved Terraform plan",
    )
    provision_parser.add_argument(
        "--session-id", required=True, help="Design session id"
    )
    provision_parser.add_argument(
        "--mode", required=True,
        help="plan | apply | destroy-plan | destroy | drift",
    )
    provision_parser.add_argument(
        "--confirm", default="",
        help='Required value "APPLY" or "DESTROY" for those modes',
    )
    provision_parser.add_argument(
        "--pass-aws-env",
        action="store_true",
        help="Forward an explicit AWS_* allowlist from your environment",
    )


def main(args) -> int:
    try:
        return asyncio.run(_provision_async(args))
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Error during provisioning: {e}[/red]")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_provision_parser(sub)
    main(parser.parse_args())
