"""Primary CLI dispatcher for AI Architect.

Usage:
    ai-architect              -> interactive Design workflow (default)
    ai-architect design       -> same Design workflow (explicit subcommand)
    ai-architect approve      -> review a gated design, record approval
    ai-architect health       -> read-only environment health check (no LLM calls)
    ai-architect --version    -> package version

The Design workflow itself is NOT reimplemented here; both the bare
command and the `design` subcommand delegate to cli.design._run_async.
Approval never provisions: it only unlocks Terraform generation, and
provisioning keeps its own separate approval.
"""

import argparse
import asyncio
import sys
import urllib.request

VERSION = "0.1.0"


def run_health() -> int:
    """Read-only checks. Makes no LLM invocations."""
    from ai_architect.config.settings import settings

    ok = True

    print(f"ai-architect version: {VERSION}")
    print(f"ollama_base_url: {settings.ai_architect_ollama_base_url}")
    print(f"qdrant_url: {settings.qdrant_url}")

    def check_http(name: str, url: str) -> None:
        nonlocal ok
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                print(f"{name}: reachable (HTTP {response.status})")
        except Exception as e:
            ok = False
            print(f"{name}: UNREACHABLE ({e})")

    check_http("ollama", settings.ai_architect_ollama_base_url)
    check_http("qdrant", settings.qdrant_url.rstrip("/") + "/")

    print("health: OK" if ok else "health: DEGRADED")
    return 0 if ok else 1


def run_design(args) -> int:
    from ai_architect.cli import design as design_cli

    try:
        return asyncio.run(design_cli._run_async(args))
    except Exception as e:
        print(f"Error during design workflow: {e}")
        return 1


def run_approve(args) -> int:
    from ai_architect.cli import approve as approve_cli

    return approve_cli.main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-architect",
        description="AI Architect - agentic cloud architecture designer",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "--request",
        default=None,
        help="Natural-language request (else prompted interactively)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Design session id (default: new uuid)",
    )

    sub = parser.add_subparsers(dest="command")

    design_parser = sub.add_parser(
        "design",
        help="Run the interactive Design workflow (default command)",
    )
    design_parser.add_argument("--request", default=None)
    design_parser.add_argument("--session-id", default=None)

    sub.add_parser("health", help="Read-only health check (no LLM calls)")

    from ai_architect.cli import approve as approve_cli

    approve_cli.add_approve_parser(sub)
    approve_cli.add_approve_provisioning_parser(sub)

    from ai_architect.cli import terraform as terraform_cli

    terraform_cli.add_terraform_parser(sub)

    from ai_architect.cli import provision as provision_cli

    provision_cli.add_provision_parser(sub)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "health":
        return run_health()

    if args.command == "approve":
        return run_approve(args)

    if args.command == "approve-provisioning":
        from ai_architect.cli import approve as approve_cli

        return approve_cli.main_provisioning(args)

    if args.command == "terraform":
        from ai_architect.cli import terraform as terraform_cli

        return terraform_cli.main(args)

    if args.command == "provision":
        from ai_architect.cli import provision as provision_cli

        return provision_cli.main(args)

    # Bare command and `design` both invoke the same existing workflow.
    # (When using the `design` subcommand, pass its options after it.)
    passthrough = argparse.Namespace(
        request=getattr(args, "request", None),
        session_id=getattr(args, "session_id", None),
    )
    return run_design(passthrough)


if __name__ == "__main__":
    sys.exit(main())
