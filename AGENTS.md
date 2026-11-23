# AGENTS.md — Project-level AWS skill workflow

This repository (`ai-architect-saas`) is an agentic cloud-architecture design
system. The backend proposes infrastructure; deterministic code validates it.
The frontend renders served artifacts only.

## AWS skill workflow (mandatory)

Before doing AWS-specific implementation or design work — service selection,
cloud architecture, compute, storage, databases, networking, IAM, security,
serverless, messaging, AI/ML, deployment, Terraform, provisioning,
resilience, observability, pricing/cost management, troubleshooting — the
agent MUST discover and load the relevant AWS skill(s) through the existing
global skill mechanism (for example the session `skill` tool listing skills
under the user's global skills installation).

Rules:

- Discover skills; do NOT invent skill names.
- Do NOT copy skills into this repository.
- Do NOT duplicate the global installation.
- Do NOT create another skill system.
- Consulted skills inform proposals only; they never override:
  explicit project requirements, deterministic validation, closed-world
  rules, fail-closed behavior, safety constraints, or task scope.

## Authority hierarchy

1. Project requirements (this task + existing contracts).
2. Deterministic validators/contracts (capacity, selection, cost, composer,
   checkpoint persistence).
3. Relevant AWS skill guidance.
4. LLM implementation/design proposal.

## Deterministic principles (never weaken)

- LLM proposes; deterministic code validates and decides acceptability.
- Do NOT weaken validators to make LLM output pass.
- Do NOT add aliases to hide model mistakes.
- Do NOT accept arbitrary component names or evidence references.
- Do NOT fabricate prices or infrastructure facts.
- Recovery stays bounded; failure is explicit and diagnosable.

## Reporting

When an AWS skill materially influences a decision, the final report MUST
state: skill consulted, relevant guidance, resulting decision, and any
project-specific deviation.
