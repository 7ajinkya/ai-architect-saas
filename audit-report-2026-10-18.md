# AI Architect SaaS — Requirements-to-Implementation Audit

**Repository:** `ai-architect-saas` (workspace root)
**Audit date:** 2026-10-18
**Branch / HEAD:** `master` / `584ac7d91fc5d9a34e6aeeec731c1925c58c6412` ("add composer failure diagnostics channel and failure-pair regression tests")
**Mode:** READ-ONLY investigation and planning. No feature implemented, no file modified, no dependency installed, no AWS API called, no `terraform init/validate/plan/apply/destroy` run against the repo. One new report file (this document) was created; that addition is the only change to the working tree caused by this audit (see Deliverable H).

---

## 0. Method and evidence discipline

1. **Source of truth.** The original requirements are the supplied educosys specification *Designing an AI Architect: An Agentic SaaS That Designs, Provisions and Operates Cloud Systems* (`.zwork/attachments/.../AI Architect Project Requirements.txt`, 17 sections). Repository-level constraints come from `AGENTS.md`. The audit brief is the task's statement of work; it is not a requirements document.
2. **Inspection, not inference.** Every status below was determined by reading code and, where available, executing tests or read-only checks. Statements taken from documentation, UI labels, route names, or prior agent claims were treated as hypotheses until confirmed in code.
3. **Tests actually executed.** The backend suite and frontend suite were run offline in the existing environments; results are recorded verbatim in §10 and reflected per-requirement in Deliverable B. Failures are reported, not smoothed over. No test that needs AWS, Qdrant, or network was required to pass; one network egress side effect (LangSmith) is disclosed.
4. **Read-only Terraform evidence.** `terraform fmt -check -recursive` was run against a **temporary copy** of `infra/modules` using the locally installed Terraform v1.16.5. Nothing in the repo was written. `terraform init/validate` and any plan/apply were **not** run (validation remains UNCERTAIN; the code path that would run it is audited instead).
5. **Evidence notation.** `path:line` references point at the repository as of the HEAD above. "Tests" means test files that exercise the behavior; "executed" distinguishes tests that actually ran in this audit from tests that merely exist.

---

## 1. Document classification (audit brief §2)

| Document | Class | Why |
|---|---|---|
| `AI Architect Project Requirements.txt` (workspace attachment) | **A — Original requirements** | The 17-section product specification; authoritative for scope, architecture, safety, and roadmap. |
| Audit brief (`pasted_text_...b5c7761c.txt`) | **Task statement of work** (not A–F) | Defines this audit's boundaries and deliverables; it does not redefine product requirements. |
| `AGENTS.md` (repo root, untracked) | **A — Project-level requirements/constraints** | AWS-skill workflow, authority hierarchy, deterministic principles, reporting duty. |
| `docs/demo-provisioning.md` | **B + C — Design decision + implementation instruction** | Documents the bounded local demo provisioning path (workspace protocol, plan/authorize/apply, single-use approvals, no destroy surface). It is a subsequent design decision, not a replacement for the original requirements. |
| `audit_compare.json` (repo root) | **D — Inspection artifact** | Side-by-side comparison of four real session outputs (two complete pipelines, two requirements-only). |
| `checkpoint_test.py`, `checkpoint_requirements_test.py` (repo root) | **E — Verification scripts** | Manual checkpoint persistence round-trip checks. |
| `backend{,2,3}.out|err.log` | **F — Supporting evidence** | Uvicorn access logs from three local server runs; no timestamps, so chronology rests on file mtimes (2026-10-17). |
| `ai_architect_checkpoints.sqlite` (45.4 MB) | **F — Runtime data** | LangGraph `SqliteSaver`: 3,346 checkpoint rows, 8,069 writes, 333 threads. |
| `.env.example` | **C — Implementation instruction** | Environment contract template. |
| `data/sources/`, `audit_sources/` | **F — Supporting material** | RAG corpora (system-design references; AWS/Azure/GCP architecture pattern collections). |
| No document in the repository **authorizes** replacing or narrowing an original requirement. | — | Demo-scope reductions proposed in Deliverable E therefore require explicit approval; none is claimed as already authorized. |

---

# DELIVERABLE A — Executive summary

**What the project currently does.** `ai-architect-saas` is a local, single-tenant FastAPI + Vue application implementing the Design phase and a guarded slice of the Provision phase of the spec. A user starts a design session; a LangGraph pipeline extracts a typed requirements spec (with human clarification interrupts), computes capacity with a deterministic calculator, retrieves grounded component candidates from a Qdrant-backed corpus, prices them against the AWS Price List API, composes a typed architecture, runs an adversarial critic loop (≤2 revisions), and passes a deterministic quality gate. An explicit human design approval then unlocks Terraform generation: an `InfraSpec` (closed five-kind registry) → module selection against a verified catalog → a deterministic rendered Terraform tree → formatting/validation checks → a live `terraform plan` review → a single-use, tree-binding provisioning approval → `terraform apply` in an isolated workspace, with an `apply_running` marker and an explicit `APPLY` confirmation.

**What is genuinely working (verified).** The entire Design pipeline runs end to end and is evidenced by real checkpoint data (`ef769931…`, `09745641…`) and two complete recorded outputs in `audit_compare.json`; 1,100 backend tests and all 171 frontend tests pass. Deterministic validation is real throughout the design path: closed-world schemas, fail-closed stage guards, deterministic capacity arithmetic, evidence-reference existence checks, unpriced-cost honesty (`fully_priced=false`), region gates, a bounded critique loop, and a quality gate that alone can write the READY state. The provisioning safety protocol is also real: design approval is artifact-bound (`services/approvals.py`), provisioning approval is bound to `tree_hash` + `spec_id` + `artifact_id` + artifact manifest and is single-use (`executor.py:331-386`), apply demands a prior `plan_ok` review of the same tree and the literal `APPLY` confirmation (`executor.py:389-427, 699-703`), the workspace path is validated against traversal (`executor.py:92-119`), the child receives a bare environment plus an explicit `AWS_*` allowlist, and there is no destroy surface. The 30-day CloudWatch log-retention requirement now propagates correctly through a closed allowlist with explicit/ambiguous/missing semantics (`spec_builder.py:283-311`) — historical observation G is fixed.

**What remains incomplete (verified).** Three things dominate:
1. **The verified-module library is selected but never rendered.** `ModuleSelection` is computed, yet the renderer emits raw `resource` blocks that duplicate the modules, and `terraform_guard.py:48` explicitly bans `module` blocks. The catalog has drifted from the real modules (`HTTP_API` declares `inputs=()` vs five module variables; `package_type` has no module variable; the log-group contract omits `log_group_name`; `billing_mode` is `pay_per_request` in the catalog but `PAY_PER_REQUEST` in the provider). Two module files still fail `terraform fmt -check`.
2. **The reviewed plan is not the applied artifact.** Apply runs `terraform apply -auto-approve` (`executor.py:738-743`), which re-plans; the plan file written during review is never consumed, and the plan summary is parsed from human-readable output by regex rather than from `terraform show -json`.
3. **The provisioning path has never been run end to end.** No `var/terraform/...` workspace, `terraform.tfstate`, or `deployment.json` exists; provisioning is verified only by offline tests with a faked Terraform CLI. There are also two real defects on the design path: a `ValidationError` on the region-default ("you decide") path (`Assumption.confidence` is forbidden by the model but constructed at `services/requirements/assumptions.py:270`), and non-hermetic API token handling in tests that fails whenever `.env` defines the real token.

**Is an end-to-end demo currently possible?** **Not as a verified, honest end-to-end AWS demo.** The Design half is demo-ready. The Provision half is structurally close — gates, approvals, rendering, and execution all exist — but it cannot yet claim "verified Terraform plan and controlled AWS deployment": the plan/apply path has never been exercised, the verified-module claim is not true of what is actually deployed, the applied plan can diverge from the reviewed one, and there is no post-apply verification. With the six slices in Deliverable F (roughly 5–9 engineering days, only the last needing AWS access), a convincing demo is achievable.

**The three biggest blockers.** (1) Module-composition bypass + catalog drift (root cause of "uncovered resources despite modules existing" and of the unverifiable "verified module" claim). (2) Plan-artifact integrity: regex-parsed plan summary and a re-planning `apply -auto-approve` that never consumes the reviewed plan. (3) No recorded end-to-end provisioning run, plus the two design-path defects above.

# DELIVERABLE B — Requirements traceability matrix

Status vocabulary is exactly as required by the brief. "Demo" = whether the requirement must work for the AWS demo. "Authorized omission" is `none` unless a document explicitly authorizes it.

Legend for Demo: **M** must work · **D** can be deferred with approval · **P** production-only improvement.

### B.1 Phase 1 — Design (§§1–5 of the original requirements)

| ID | Requirement (preserved meaning) | Status | Evidence | Tests (executed?) | Gaps / deviations | Demo | Authorized omission |
|---|---|---|---|---|---|---|---|
| R-01 | Chat-style entry: free-form requirement input starts a design session; artifact-oriented output, not chat prose. | IMPLEMENTED_AND_VERIFIED | `api/main.py` (POST `/design/sessions`, `/messages`, `/run`); `cli/design.py`; real sessions in `backend2.out.log` (200s) and SQLite | `test_api_design_run.py` (executed: 15 failures, root cause A — see §10); `test_api_sessions.py` | API tests fail non-hermetically when `.env` holds a real token (test defect, not product defect). Read endpoints return full session incl. Terraform files (over-exposure). | M | none |
| R-02 | Requirements Agent extracts a typed spec; asks materially-architectural questions (voice/video, residency), defaults sizing assumptions and flags them as editable. | IMPLEMENTED_AND_VERIFIED | `agents/requirements/*`; `domain/requirements/models.py` (closed-world, `extra="forbid"`); `services/requirements/validator.py` (provider/region/regulated-impact blocks); clarification interrupts recorded in checkpoints (`826d108a…`, `b9ae1d61…`) | `test_requirements_boundary.py`, `test_api_design_run.py` (clarification gating), `test_architecture_pipeline.py` — executed | CLI lacks the API's deferral policy (dead-end on bad answers); retry prompt asks for a `confidence` field the schema forbids; capacity sizing assumptions bypass the assumptions ledger. | M | none |
| R-03 | Deterministic capacity calculator: peak/read QPS, connections, storage growth, hot working set, egress; numbers never LLM-generated; independently unit-tested. | IMPLEMENTED_AND_VERIFIED | `services/capacity/calculator.py` (v1 formulas incl. `storage_growth_bytes_per_day × storage_retention_days`, `services/capacity/calculator.py:47-49`); `services/capacity/consistency.py` (3× consistency gate); real `capacity_result` channel in checkpoints | `test_capacity_chain.py`, `test_capacity_consistency.py`, `test_capacity_fallback.py` — executed (all pass) | Formula version is present in code; the spec's "store inputs + formula version with the model" is only partially represented in persisted state (checkpoint blobs; no dedicated CapacityModel entity). | M | none |
| R-04 | Component selection grounded in hybrid retrieval with hard metadata filters; every choice carries a citation; validator strips ungrounded claims; retrieval failure halts rather than degrading silently. | PARTIALLY_IMPLEMENTED | `agents/component_selection/*` (category taxonomy, provider match); `services/knowledge/retrieval.py`; evidence refs must exist in the retrieval bundle (`test_component_selection_evidence_refs.py`) | `test_component_selection_boundary.py`, `test_component_selection_evidence_refs.py`, `test_component_selection_recovery.py` — executed (pass) | **No closed-world component-name registry** (a decided service name is validated by category/provider, not by registry membership). Empty retrieval bundle degrades to `insufficient-evidence` warnings instead of halting (deviation from §10's failure rule). Model-asserted `provider_specific=False` bypasses evidence checks. | M | none |
| R-05 | Knowledge base: four source tiers with different refresh cadences; chunk/tag/version; embed/index; freshness and compliance as hard filters. | PARTIALLY_IMPLEMENTED | `services/knowledge/{ingestion,chunking,retrieval,contracts}.py`; `infrastructure/qdrant/*`; `providers/{qdrant_provider,aws_provider,aws_mcp_adapter}.py`; corpora in `data/sources` (11,762 files) and `audit_sources` (5,455 files) | **None of the knowledge/RAG stack is imported by any test** (26 untested modules — §10.4) | Refresh cadences (weekly/daily) are not implemented as scheduled jobs; ingestion path untested end to end; whether the registered providers are exercised in production runs is UNCERTAIN. | D (prebuilt corpus suffices for demo) | none (proposed) |
| R-06 | Cost estimation from live pricing (never remembered rates), 24h freshness, on-demand vs 1y/3y reserved scenarios, variable-cost flags, honest unpriced handling. | PARTIALLY_IMPLEMENTED | `services/cost/pricing_service.py` (AWS Price List API; freshness gate); `services/cost/calculator.py`; `domain/cost/models.py`; recorded totals in `audit_compare.json` | `test_cost_calculator.py`, `test_cost_pricing.py`, `test_cost_accounting_pipeline.py`, `test_cost_url_shortener_audit.py` — executed (pass; fakes) | **No reserved/RI scenario comparison found** — on-demand only. Live API path not exercised in this audit (offline tests fake the client). Unpriced honesty is real (`fully_priced=false`). | M (on-demand path) / D (RI scenarios) | none (proposed deferral) |
| R-07 | Architecture composer produces a typed topology (nodes/edges/data flow), not prose. | IMPLEMENTED_AND_VERIFIED | `agents/architecture/run.py` (50 KB); `services/architecture/core.py`; `domain/architecture/models.py`; `/architecture` served 200 for `ef769931…` | `test_architecture_composer.py`, `test_architecture_contract.py`, `test_architecture_whitespace_regression.py`, `test_architecture_component_refs.py` — executed (pass) | Closed-world enforcement is at the service-set (ABC) level, not per-component name registry (see R-04). | M | none |
| R-08 | Design critic runs a fixed checklist: SPOF, cost ceiling, headroom >70%, observability, AZ-failure durability, security posture, scaling cliffs; findings loop back; bounded revisions. | PARTIALLY_IMPLEMENTED | `agents/critic/*`; `agents/design/graph.py` (critique loop, `MAX_CRITIQUE_CYCLES=2`, `domain/architecture/critique.py:31`); `services/architecture/quality_gate.py` | `test_design_critic.py`, `test_design_critique_loop.py`, `test_design_quality_gate.py` — executed (pass) | Deterministic gate enforces existence, artifact-ID match, criticals, ceiling, pricing completeness — **but SPOF/security/observability/durability enter only through LLM critic findings**; no deterministic redundancy/observability checks. | M (critic loop) / D (deterministic checks) | none (proposed deferral) |
| R-09 | Output is a versioned artifact (editable assumptions, cost toggles, trade-offs); changing an assumption re-runs the pipeline and produces a diff against the previous version. | PARTIALLY_IMPLEMENTED | `artifact_id` binding (`approvals.py`); checkpoints per session; `audit_compare.json` compares four sessions | `test_checkpoint_design_backfill.py`, `test_architecture_pipeline.py` — executed | The **diff view** is a manual comparison artifact; no in-app version diff and no evidence of a re-run-on-edit UX. Artifact immutability/versioning is partial (artifact_id, not a versioned store). | M (artifact) / D (diff view) | none (proposed deferral) |
| R-10 | Region handling: regions explicit; residency blockers; no silent default; availability used as hard filter. | PARTIALLY_IMPLEMENTED | `services/region/normalizer.py`; region-required gate; botocore-based validity; `AI_ARCHITECT_DEFAULT_REGION` policy | `test_region_*.py` — executed; **3 failures + 5 subtests** from the `Assumption.confidence` bug (root cause B) | The "you decide" region-default path **crashes** with `ValidationError` (`assumptions.py:270` constructs `confidence="medium"`; `Assumption` forbids it). Live region availability as a retrieval hard filter is UNCERTAIN. | M (fix is tiny) | none |

### B.2 Phase 2 — Provisioning (§6 of the original requirements + audit §5 observations)

| ID | Requirement | Status | Evidence | Tests (executed?) | Gaps / deviations | Demo | Authorized omission |
|---|---|---|---|---|---|---|---|
| R-11 | IaC generation composes from a **verified, parameterized module library**; the agent selects modules and parameters, never authors raw HCL. | PARTIALLY_IMPLEMENTED | Catalog: `services/provisioning/module_catalog.py` (5 `VERIFIED` contracts with evidence refs); selection: `module_selection.py` (all-or-nothing); **render**: `terraform_renderer.py` emits raw `resource` blocks; `terraform_guard.py:48` bans `module` | `test_provisioning_module_selection.py`, `test_provisioning_module_contract.py`, `test_provisioning_terraform_render.py` — executed (pass; 1 skip because Terraform is installed) | **Modules are never composed.** Selection output is discarded by the renderer; rendered HCL duplicates module content. Catalog↔module drift: `HTTP_API` `inputs=()` vs 5 real variables; `package_type` declared with no module variable; log-group module needs `log_group_name` not supplied by selection; `billing_mode` catalog value `pay_per_request` vs provider `PAY_PER_REQUEST`. | M (this is the demo's central claim) | none |
| R-12 | Uncovered components are flagged explicitly; drafts go to human review; nothing is silently improvised or auto-applied. | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | `services/provisioning/iac_fallback.py` (partition supported vs fallback nodes); `spec_builder.py` leaves required inputs pending rather than inventing them; generation status reports incomplete + pending decisions | `test_iac_fallback.py`, `test_provisioning_infra_spec.py` — executed (pass) | Honest reporting exists; no evidence a real generation run surfaced pending decisions to a human UI. | M | none |
| R-13 | Gate 1 — static validation: `terraform validate`, `fmt`, provider schema, tfsec/checkov. | PARTIALLY_IMPLEMENTED | `services/provisioning/terraform_cli.py` (fmt→init→validate in isolated temp dir, `PERMITTED_COMMANDS={fmt,init,validate}`, sanitized output, `NOT_INSTALLED` never success) | `test_provisioning_terraform_render.py` (fmt assertions), executed | **No tfsec/checkov.** `terraform validate` never executed in this audit and no recorded validation result exists → whether a rendered tree passes init/validate is UNCERTAIN. Module `fmt -check` fails today on 2 files (my run, §10.5). | M (fmt/validate) / D (security scanners) | none (proposed deferral for scanners) |
| R-14 | Gate 2 — policy-as-code (OPA/Sentinel): allowed regions, instance caps, tags, encryption, no public S3. | PARTIALLY_IMPLEMENTED | No OPA/Sentinel. Substitute deterministic policy: closed allowlists in `spec_builder.py` (retention), catalog enums, renderer value allowlists (e.g. `billing_mode` mapping `terraform_renderer.py:280`), S3 module bakes public-access-block/encryption, guard bans dangerous blocks | Contract/renderer tests executed | No versioned policy engine, no tag schema enforcement, no region allow-list at render time (region gate is earlier). Deterministic enough for demo **if disclosed**. | D (disclose) | none (proposed) |
| R-15 | Gate 3 — blast radius / risk tiering (Tier 1–4) with destroy/stateful/cost-delta scoring; Tier 3 typed confirmation; Tier 4 blocked. | PARTIALLY_IMPLEMENTED | Live plan summary (adds/changes/destroys) is produced and shown; `plan_builder.py`; approvals are binary + `APPLY` string | `test_provisioning_plan.py`, `test_provisioning_http_api.py` — executed (pass) | **No Tier 1–4 machinery, no reversibility score, no cost-delta-vs-spend.** Destroy count comes from the live plan; static inventory reports destroys unknown (documented). | D (disclose) | none (proposed) |
| R-16 | Gate 4 — mandatory human approval with plain-English summary, diff, cost delta, risk tier, rollback plan; explicit confirmation for destructive/stateful/spend changes. | PARTIALLY_IMPLEMENTED | Design approval (`services/approvals.py`); provisioning approval bound to tree/spec/artifact+manifest, single-use, `consumed_at`, restored on pre-flight refusal (`executor.py:299-386`); `confirm="APPLY"`; plan-review requirement (`executor.py:389-427`) | `test_design_approval.py`, `test_provisioning_execute.py`, `test_provisioning_http_api.py` — executed (pass) | No risk tier and **no rollback plan shown** at approval (see R-18). Design approval is reusable with no expiry (artifact-bound but not single-use). | M (binding/single-use) / D (risk tier, rollback display) | none (proposed) |
| R-17 | Terraform apply in an isolated runner with state lock, short-lived scoped credentials, immutable audit log. | PARTIALLY_IMPLEMENTED | Isolated workspace per session/tree under `var/terraform/<session>/<tree16>` with marker protocol and path validation (`executor.py:92-119`); bare env + explicit `AWS_*` allowlist; `apply_running` marker; no destroy surface; sanitized output | `test_provisioning_execute.py` (marker/refusal semantics), executed | **No STS credential broker** (operator forwards env). **No remote state/state lock** — local state in the workspace; concurrency protection is a single-process marker, not a lock. **No immutable/WORM audit log** (uvicorn logs only). Apply re-plans (see R-20). | M (isolation/marker) / D (broker, WORM) / P (remote state) | none (proposed) |
| R-18 | Rollback plan pre-staged before apply; explicit "no safe rollback" disclosure where impossible. | NOT_IMPLEMENTED | No rollback planner anywhere in `src/`; docs state there is no destroy surface and teardown is manual | None | Rollback is manual `terraform destroy` in the operator shell (`docs/demo-provisioning.md`). Must be disclosed at approval. | D (disclose) | none (proposed) |
| R-19 | 30-day CloudWatch Logs retention requirement reaches the provisioning spec and rendered configuration. | IMPLEMENTED_AND_VERIFIED | `spec_builder.py:283-311` (closed allowlist `("7","30","90","365")`, explicit/ambiguous/missing, refuses values outside the set rather than coercing); `plan_builder.py:130-132` (requirements contract is the only source); catalog enum (`module_catalog.py:94-96,494-499`); module `retention_in_days = var.retention_days` | `test_provisioning_retention.py`, `test_provisioning_log_group.py` executed (pass) | None found. Historical observation G is **fixed**. | M | none |
| R-20 | Terraform plan is a real machine-readable dry run that the human reviews, and apply executes exactly the reviewed plan. | PARTIALLY_IMPLEMENTED | Plan mode runs a live `terraform plan` (documented in `docs/demo-provisioning.md`; executor plan path); **summary parsed from human output by regex**; **apply = `terraform apply -auto-approve`** (`executor.py:738-743`), plan file never consumed | `test_provisioning_execute.py` (faked CLI) | The reviewed plan can diverge from the applied one; no `plan -json`/`show -json` parsing. This is a genuine integrity gap, not a cosmetic one. | M | none |
| R-21 | Post-deployment verification / smoke tests; operations telemetry auto-attaches. | NOT_IMPLEMENTED | `deployment.json` records exit codes/output only (`_record_deployment`); nothing calls the deployed endpoint; no telemetry attachment | None | No smoke test, no health verification, no teardown verification. | M (minimal smoke) / D (full telemetry) | none (proposed) |
| R-22 | Cleanup/teardown is safe and explicit. | INTENTIONALLY_DEFERRED (documented decision) | No destroy surface by design (`executor.py:34` `_APPROVED_SUBCOMMANDS={plan,apply}`; `docs/demo-provisioning.md` "there is no destroy command") | `test_provisioning_execute.py` (destroy absent) | Manual operator `terraform destroy`; workspace retained for inspection (good). No in-app cleanup. | D (manual teardown accepted) | Documented in `docs/demo-provisioning.md` (design decision B) |

### B.3 Phase 3 — Operate, and platform-wide requirements (§§7–12, 14)

| ID | Requirement | Status | Evidence | Tests | Gaps | Demo | Authorized omission |
|---|---|---|---|---|---|---|---|
| R-23 | Telemetry ingestion: infra metrics, traces/logs, billing, LLM tokens, deploy events; stream processing. | NOT_IMPLEMENTED | No telemetry modules in `src/` | — | Entire Phase 3 absent. | D (proposed, roadmap V3) | none (proposed) |
| R-24 | Detection agents: anomaly, cost watchdog, failure precursor, drift detector. | NOT_IMPLEMENTED | — | — | — | D (proposed) | none (proposed) |
| R-25 | RCA agent: structured hypothesis + evidence + confidence + alternatives; confidence drives routing. | NOT_IMPLEMENTED | — | — | — | D (proposed) | none (proposed) |
| R-26 | Remediation planner routes through the same four provisioning gates; tiered auto/propose/escalate; progressive trust. | NOT_IMPLEMENTED | — | — | — | D (proposed) | none (proposed) |
| R-27 | LLM gateway proxy with per-request attribution; token-cost forecasting; platform self-monitoring. | NOT_IMPLEMENTED | `infrastructure/llm/*` provides provider abstraction and low/medium/high tier routing; LangSmith tracing configured in `.env` (key names only inspected) | `test_llm_tier_routing.py`, `test_llm_provider_independence.py` executed (pass) | No gateway proxy, no token attribution, no cost watchdog. LangSmith tracing is partial self-observability. | D / P | none (proposed) |
| R-28 | Signal correlation and deduplication before notification. | NOT_IMPLEMENTED | — | — | — | D | none (proposed) |
| R-29 | Multi-tenancy isolation at every layer; bring-your-own-cloud-account; per-tenant state/keys. | NOT_IMPLEMENTED | Single-user local app; no tenant model in API or storage; one checkpoint DB | — | Deferred wholesale; must not be claimed as done. | D (proposed) | none (proposed) |
| R-30 | Six planes with hard boundaries; execution plane contains no LLM; agents hold no cloud credentials. | PARTIALLY_IMPLEMENTED | Agentic vs deterministic separation is real (renderer/executor are pure code); no credentials in agent code; operator env forwarding is explicit | Executor/guard tests executed | No separate governance plane or credential broker (see R-17). | M (deterministic execution) / D (broker) | none (proposed) |
| R-31 | Fail loud, not silent: degradation tiers (stale pricing flagged; retrieval failure halts; telemetry gap reported; state-backend failure halts applies; LLM failover rules). | PARTIALLY_IMPLEMENTED | Design path fails closed (`graph.py:584-591`; stage guards); unpriced costs honest; `NOT_INSTALLED` never success (`terraform_cli.py`) | Fail-closed and gate tests executed | Retrieval failure degrades to warnings (not halt — deviation); no circuit breakers; no telemetry-gap concept (Phase 3 absent). | M (fail-closed behavior) / D (breakers) | none (proposed) |
| R-32 | Agent-level failure handling: strict schema validation, one retry, fallback verdicts. | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | Typed schemas `extra="forbid"`; bounded recovery (`test_component_selection_recovery.py`, `test_composer_recovery_r2.py`); retry prompts | Executed (pass) | Retry prompt demands a `confidence` field the schema forbids (contradiction); failure diagnostics channel exists (HEAD commit). | M | none |
| R-33 | Self-observability: full agent traces, continuous eval suite, self token monitoring. | PARTIALLY_IMPLEMENTED | LangSmith tracing enabled via env; structured checkpoint channels; `audit_compare.json` as evidence | — | No eval suite, no token-cost monitoring, no trace UI. | D / P | none (proposed) |
| R-34 | Data model: versioned DesignArtifact, CapacityModel w/ formula version, per-tenant encrypted Terraform state, ChangeProposal, Incident, AgentTrace, WORM AuditLog. | PARTIALLY_IMPLEMENTED | `artifact_id` + checkpointed state; no dedicated entities for the rest | Checkpoint tests executed | No WORM audit, no ChangeProposal entity (approval dicts instead), no per-tenant keys (single tenant), no Incident/AgentTrace store. | D / P | none (proposed) |
| R-35 | Security beyond credentials: structural prompt-injection defense; secrets never through agent context; runner network isolation; data classification. | PARTIALLY_IMPLEMENTED | Untrusted text cannot set risk tier or credential scope (there is no such authority to seize); secrets are not generated by agents; Terraform output sanitized (`_SECRET_PATTERN`, `terraform_cli.py:44-52`, `executor.py:_sanitize`); no secrets in rendered infra (Lambda env carries only `TABLE_NAME`) | Sanitizer exercised indirectly | Runner is a local subprocess, not a network-isolated worker; no data-classification/retention policy; `.env` holds live provider keys (normal for local dev, must not ship). | M (structural defense) / P (isolation, classification) | none (proposed) |
| R-36 | API gateway: authn/authz, tenant isolation, rate limiting, audit interceptor. | PARTIALLY_IMPLEMENTED | `api/security.py`: bearer token on mutating POSTs, fail-closed 503 when unset, 401 on mismatch, `secrets.compare_digest`; reads/health open by design; CORS default `localhost:5173` | `test_api_security.py` executed (pass) | **No rate limiting.** Defect: unauthenticated `GET /optimization-recommendations` writes to the checkpoint. Read endpoints expose full sessions incl. Terraform files. No tenant concept. | M (token) / P (rate limit) | none (proposed) |
| R-37 | Frontend renders served artifacts only; no client-side business logic. | IMPLEMENTED_AND_VERIFIED | `frontend/src/api/client.ts`; pure adapters (`adapters/graph.ts`, `adapters/stages.ts`); components consume served state | Frontend: 14 files / 171 tests, all passed (executed) | Read endpoints over-expose session data (R-36); no streaming (polling GETs observed in run 2 logs). | M | none |
| R-38 | Deterministic validators are closed-world and fail closed; LLM proposes, code validates; validators never weakened to pass model output. | IMPLEMENTED_AND_VERIFIED | `extra="forbid"` schemas; category/provider/evidence checks; retention allowlist refuses coercion; guard bans dangerous blocks; `NOT_INSTALLED` ≠ success | 1,100 passing tests incl. boundary/regression suites (executed) | Exceptions listed under R-04/R-08 (registry-level naming, deterministic SPOF checks). | M | none |
| R-39 | Roadmap staging: MVP design-only → V2 guarded provisioning (single cloud, all changes human-approved) → V3 observe → V4 autonomy. | IMPLEMENTED (as staging) | Current state spans MVP + V2; `docs/demo-provisioning.md` documents a bounded V2 demo | — | No auto-apply tier exists — **consistent** with V2 ("no auto-apply at all initially"). | M | The roadmap is part of the original spec (A), so V3/V4 sequencing is roadmap-consistent — but this audit does **not** treat that as feature-level authorization. |
| R-40 | Model-tier routing for cost/latency (small/mid/large per agent). | IMPLEMENTED_BUT_NOT_FULLY_VERIFIED | `infrastructure/llm/{profiles,capabilities,factory}.py`; low/medium/high routing | `test_llm_tier_routing.py`, `test_llm_capabilities.py` executed (pass) | No cost telemetry (R-27); provider pinning (Ollama `gpt-oss:120b-cloud`) is environment-specific. | D / P | none (proposed) |

### B.4 Audit §5 historical observations — present-state verification

| Observation | Present state | Evidence |
|---|---|---|
| A. Cost subtotal with explicit unpriced resources | **Still true (honest).** `fully_priced=false` semantics; unpriced ≠ zero | `domain/cost/models.py`; `test_api_design_state.py` unpriced-cost cases (executed); `test_cost_accounting_pipeline.py` (executed) |
| B. Review flags regional SPOF, missing alarms, security assumptions | **True via the LLM critic**, not deterministic gate checks | `agents/critic/*`; `quality_gate.py` checks existence/criticals/ceiling/pricing only |
| C. Generation reports incomplete status, uncovered resources, pending decisions | **True.** Pending required inputs are preserved, not invented | `spec_builder.py` (retention missing → pending); `iac_fallback.py`; generation diagnostics |
| D. Five verified modules under `infra/modules/` | **True.** HTTP API, Lambda, DynamoDB, S3, Log Group; catalog marks all `VERIFIED` with evidence refs and dates | `infra/modules/*`; `module_catalog.py` |
| E. Resources reported uncovered despite modules existing | **Root cause found:** selection is computed all-or-nothing, but the renderer never consumes it; the tree is raw resources. Catalog↔module drift compounds it | `module_selection.py`; `terraform_renderer.py`; `terraform_guard.py:48` |
| F. Lambda requires a real artifact path + SHA256 | **True and enforced.** Module vars `code_artifact_path`/`code_artifact_sha256` are required (no defaults); renderer embeds the human-verified sha256; staging verifies bytes | `infra/modules/aws-serverless-function/variables.tf:14-23`; `main.tf` (`filebase64sha256`); `build_demo_artifact.py`; `test_provisioning_artifact_staging.py` (executed) |
| G. 30-day retention not propagated; Slice 1 fix | **Fixed.** Closed allowlist, explicit/ambiguous/missing, refuse-not-coerce, requirements-only source | `spec_builder.py:283-311`; `plan_builder.py:130-132`; `module_catalog.py:94-96`; `test_provisioning_retention.py` (executed) |
| H. `terraform fmt` flags `aws-http-api/main.tf`, `aws-serverless-function/main.tf` | **Still true today.** Verified with Terraform v1.16.5 on a temp copy; exit 3 | §10.5 (exact drift shown) |
| I. Terraform validation not completed | **Still not verified.** `terraform_cli.validate` path exists but no recorded run; no `var/` workspaces exist | `terraform_cli.py`; absence of `var/terraform` |

# DELIVERABLE C — Architecture and call-path map

## C.1 Runtime shape (as built)

```
Vue frontend (frontend/src)                 CLI (src/ai_architect/cli/main.py)
   │  served state only, pure adapters          │
   ▼                                            ▼
FastAPI (api/main.py, 16 routes)  ◄──────►  Design engine (agents/design/run.py:52-83)
   │                                            │ LangGraph StateGraph
   │  LangGraph checkpoints                     ├─ validate_requirements → (ask_clarification | capacity)
   ▼                                            ├─ capacity → component_selection → cost_estimation
SQLite checkpoints (45.4 MB,                   ├─ architecture_composer → design_critic
3,346 rows / 333 threads)                      │     └─ (revision ≤2 | component_reselection → cost → composer)
   │                                            ├─ quality_gate → READY | REQUIRES_REVISION | REQUIRES_USER_DECISION | BLOCKED
   │                                            └─ any stage exception → FAILED → END (fail-closed)
   ▼
Design approval (services/approvals.py; gate outcome + artifact_id)
   ▼
Provisioning (services/provisioning/*)
   plan_builder → InfraSpec (closed 5-kind registry) → ModuleSelection (catalog, all-or-nothing)
   → terraform_renderer (raw resources; module blocks banned) → terraform_cli (fmt→init→validate, temp dir)
   → executor.plan (live terraform plan in var/terraform/<session>/<tree16>)
   → provisioning approval (tree_hash+spec_id+artifact_id+artifacts; single-use)
   → executor.apply (init + apply -auto-approve, AWS env allowlist, apply_running marker)
   → deployment.json + terraform.tfstate (local, no destroy surface)
```

## C.2 The twenty stages (brief §4)

| # | Stage | Entry point / main files | Inputs → outputs | Error handling | Tests | Actual behavior / limitation |
|---|---|---|---|---|---|---|
| 1 | Requirements input | `api/main.py` POST `/design/sessions`, `/messages`; `cli/design.py` | free text → session | 4xx/5xx; logs show one 502 | `test_api_design_run.py` (fails: token env) | Works; 502 seen once in run 1 (cause unrecoverable) |
| 2 | Deterministic requirements validation | `services/requirements/validator.py` | typed spec → validated/blocks | fail-closed stage guard | `test_requirements_boundary.py` | Closed-world; provider/region/regulated blocks |
| 3 | Requirements-agent processing | `agents/requirements/{agent,graph,prompt,run}.py` | text → `RequirementsSpec` | retry + interrupt | `test_api_sessions.py` | Interrupts recorded; CLI deferral policy missing |
| 4 | Requirements pipeline | `agents/design/graph.py` | state graph | FAILED→END | `test_architecture_pipeline.py` | Clarification branch works; 5 interrupts recorded in one session |
| 5 | Capacity modeling | `agents/capacity/*`; `services/capacity/{calculator,consistency}.py` | spec → `capacity_result` | fallback + consistency gate | `test_capacity_*` (all pass) | Deterministic v1 formulas; 3× gate |
| 6 | Component discovery/retrieval | `services/knowledge/retrieval.py`; Qdrant | query → candidate bundle | empty bundle → warnings (not halt) | none directly (untested stack) | Deviation from §10 halt rule |
| 7 | Component selection/binding | `agents/component_selection/*` | capacity + bundle → selection w/ evidence refs | bounded recovery | selection suite (pass) | Evidence existence enforced; no name registry |
| 8 | Cost estimation | `services/cost/*` | selection + region → cost result | unpriced flagged | cost suite (pass) | On-demand only; live API path unexercised |
| 9 | Architecture composition | `agents/architecture/run.py` (50 KB) | all → `ArchitectureResult` | diagnostics channel | composer suite (pass) | Deterministic contract tests pass |
| 10 | Validation / quality gates | `services/architecture/{validator,quality_gate}.py` | arch + critique → gate outcome | READY only here | quality-gate suite (pass) | SPOF/alarms not deterministic |
| 11 | Design approval | `services/approvals.py`; `cli/approve.py`; API | gate+artifact → approval record | refuses non-approvable | `test_design_approval.py` (pass) | Reusable, no expiry; artifact-bound |
| 12 | Terraform spec generation | `services/provisioning/plan_builder.py`, `spec_builder.py` | approved arch + requirements → InfraSpec | pending inputs preserved | provisioning spec tests (pass) | Retention requirement now propagates |
| 13 | Verified module selection | `module_selection.py`, `module_registry.py`, `module_catalog.py` | InfraSpec → ModuleSelection | all-or-nothing | selection/contract tests (pass) | Selection is **discarded by render** |
| 14 | Terraform rendering | `terraform_renderer.py` (41 KB), `domain/provisioning/render.py` | spec → tree (main/provider/versions) | guard + fail-closed names | render tests (pass; 1 skip) | Raw resources; `module` banned |
| 15 | Formatting & validation | `terraform_cli.py` | tree → fmt/init/validate result | `NOT_INSTALLED` ≠ success | render tests assert formatting; **never run on a real tree** | Module files fail fmt (2) |
| 16 | Plan generation & review | `executor.plan` + docs | tree → live plan + summary | refuses on missing review | execute tests (faked CLI) | Summary regex-parsed; plan file stored but unused |
| 17 | Explicit human approval | `executor.py:299-427`; API | approval + review → authorized tree | mismatch refused, restored | execute tests (pass) | Binding and single-use verified |
| 18 | Apply & provision | `executor.py:684-765` | tree + approval + `APPLY` → result | `apply_running` marker; preflight restore | execute tests (faked) | `apply -auto-approve` re-plans; **never run for real** |
| 19 | Post-deploy verification | — | — | — | — | **Not implemented** |
| 20 | Cleanup/teardown | — (manual) | — | — | — | No destroy surface; manual only |

## C.3 Disconnected or unreachable functionality

- **ModuleSelection → renderer**: computed, validated, then unused for rendering (the core disconnect).
- **`terraform_cli.run_terraform_checks`** (fmt/init/validate) is wired into generation paths but no evidence it ran on a real tree; it is not the same code path as executor plan/apply.
- **CLI commands `ingest`, `retrieve`, `component_selection`, `provision`**: not imported by any test (provision tested via HTTP).
- **`knowledge/registry.py`** and the whole ingestion/Qdrant stack: not imported by any test.
- **`GET /optimization-recommendations`**: unauthenticated yet mutates session state.

# DELIVERABLE D — Provisioning gap analysis

**Question:** what precisely prevents "valid architecture → verified Terraform plan → controlled AWS deployment"?

## D.1 Root cause 1 (primary): the verified-module layer is bypassed at render time

`ModuleSelection` is the product's central safety claim: generation is a bounded selection-and-parameterization problem over tested modules, not free-form HCL. The code builds that selection (`module_selection.py`, all-or-nothing), maintains a catalog with `VERIFIED` metadata and evidence refs (`module_catalog.py`), and contract-tests the modules (`test_provisioning_module_contract.py`). But the renderer emits **raw `resource` blocks** that hand-replicate each module (`terraform_renderer.py:61-78` kinds; `:436` DynamoDB; `:702-774` API Gateway), and `terraform_guard.py:48` lists `module` as a forbidden block. Consequences:

- The artifact that is fmt-checked, planned, and applied is **not** the module that was verified. Module tests and renderer tests assert different implementations.
- Drift is already present and provable: `HTTP_API` contract declares `inputs=()` while the module needs `api_name`, `lambda_invoke_arn`, `lambda_arn`, `log_group_arn`, `access_log_format` (`infra/modules/aws-http-api/variables.tf`); the Lambda contract declares a `package_type` parameter with **no** corresponding module variable (`module_catalog.py:332`); the log-group module requires `log_group_name` (`variables.tf:1`) which selection never supplies (registry requires only `retention_days`, `registry.py:116`); `billing_mode` allowlist is `pay_per_request` (`module_catalog.py:79`) while the provider requires `PAY_PER_REQUEST` (the raw renderer compensates at `terraform_renderer.py:280`, the module path would not).
- Two module files still fail `terraform fmt -check` (verified, §10.5), so the "verified" library itself is not clean.

This is the root cause behind historical observation E ("resources uncovered despite modules existing"): binding diagnostics are not the only issue — the pipeline can select modules correctly and still emit something else.

## D.2 Root cause 2: the reviewed plan is not the applied artifact

Plan mode produces a real `terraform plan` and a stored plan file, and approval is bound to `tree_hash`/`spec_id`/`artifact_id`/manifest with single-use consumption. But:

- The human-facing summary is parsed from human-readable plan output by regex, not from `terraform show -json` — brittle and not a machine-verifiable resource diff.
- Apply executes `terraform apply -no-color -input=false -auto-approve` (`executor.py:738-743`), i.e., **re-plans and applies**, rather than `terraform apply tfplan`. Between review and apply, provider behavior, state, or environment can change; the approved artifact is not provably the executed one. Tree-hash binding proves "same configuration", not "same plan".

## D.3 Root cause 3: the deployment path is unexercised

No `var/terraform/...` workspace, `terraform.tfstate`, or `deployment.json` exists anywhere in the repo; all provisioning evidence is offline tests with a faked CLI. Therefore: whether a rendered tree passes `terraform init`/`validate` against real providers, whether the five-kind demo architecture actually applies in a real account, and whether the plan/approval/apply protocol behaves under interruption are all **UNCERTAIN**. This is not a code defect; it is missing evidence, and it must be closed before any demo claim.

## D.4 Contributing defects

1. **`Assumption.confidence` contract drift** (`domain/requirements/models.py:17` forbids extras; `services/requirements/assumptions.py:270` constructs `confidence="medium"`): the "you decide" region path raises `ValidationError` — a reachable production bug; 3 test failures + 5 failing subtests.
2. **Non-hermetic API tests** (`tests/api_auth.py` uses `os.environ.setdefault`; `.env` loads a real token at import via `config/settings.py:5`): 42 API-test failures on developer machines; order-sensitive suite (49 failures in isolation).
3. **Unauthenticated state mutation**: `GET /optimization-recommendations` writes to the checkpoint; read endpoints expose Terraform files.
4. **No post-apply smoke test**: `deployment.json` records exit codes only; a green apply is not evidence the API works.
5. **No rollback plan / risk tier** at approval; no `tfsec/checkov`, no OPA. These are demo-scope disclosure items, not hidden gaps.
6. **Local state, no lock**: state lives in the workspace; concurrency is guarded by a single-process marker, not a state lock. Acceptable only under the documented single-operator demo constraint.

## D.5 What is already strong (do not rebuild)

Design approval binding; provisioning approval binding and single-use consumption; restoration on pre-flight refusal; `APPLY` confirmation; workspace path validation; bare child environment with explicit `AWS_*` allowlist; sanitized output; `apply_running` marker and refusal of duplicate applies; no destroy surface; deterministic renderer with `_align_equals`; closed five-kind resource registry; retention allowlist with explicit/ambiguous/missing semantics; artifact hash staging.

# DELIVERABLE E — Demo scope

## E.1 Must work for the demo

1. Design run E2E: requirements (with at least one clarification) → capacity → selection → cost → architecture → critic → quality gate → design approval.
2. Terraform generation for the demo architecture: InfraSpec → module selection → deterministic render → `fmt`/`init`/`validate` **actually passing on the rendered tree**.
3. Live plan review showing adds/changes/destroys, with a machine-readable summary.
4. Provisioning approval bound to the exact reviewed tree and consumed exactly once; `APPLY` typed confirmation.
5. Apply in the isolated workspace with operator-supplied AWS credentials; stored `terraform.tfstate` + `deployment.json`.
6. A minimal post-apply smoke check (invoke the HTTP API endpoint if present) with honest pass/fail reporting.
7. Manual teardown documented and verified at least once.
8. Honest display of known limitations at approval time: single region/AZ demo, no alarms, public HTTP API without an authorizer, no automated rollback, local state, no destroy surface.

## E.2 Can be intentionally deferred **with approval** (proposed, not authorized)

- Phase 3 (telemetry, detection, RCA, remediation, token monitoring) — roadmap-consistent, but approval required for this demo.
- Multi-tenancy and bring-your-own-cloud-account isolation.
- Reserved/RI cost scenarios (on-demand only for the demo).
- OPA/Sentinel and tfsec/checkov (substituted by closed-world allowlists and module contracts — must be disclosed).
- Risk tiers 1–4 and rollback planner (disclose "no automated rollback").
- STS credential broker (operator env forwarding for the demo).
- WORM/immutable audit store, rate limiting, eval suites, circuit breakers.
- In-app diff view for design revisions.

## E.3 Must never be bypassed (even for the demo)

- Design approval gate and its artifact-ID binding.
- Provisioning approval binding (`tree_hash`+`spec_id`+`artifact_id`+artifacts) and single-use consumption.
- `plan_ok` review requirement and `APPLY` confirmation.
- Fail-closed validators; closed-world allowlists (component categories, retention values, resource kinds, guard bans).
- No fabricated prices, hashes, module capabilities, or deployment results.
- No weakening of `api/security.py` to make tests pass.
- Honest `NOT_INSTALLED`/`UNCERTAIN` reporting (never claim validation passed when it did not run).

## E.4 Production-only improvements

Remote state backend with locking and per-tenant encryption; credential broker; network-isolated runner; authorizers on the HTTP API; alarm resources (Errors/Throttles/Duration P99 — see §9); WORM audit; rate limiting; multi-tenant isolation; eval suite; telemetry/drift detection; rollback automation.

# DELIVERABLE F — Dependency-ordered implementation plan

Each slice is independently verifiable. Effort is a rough engineering estimate (one engineer). "AWS?" = whether real AWS access/authorization is required.

### Slice 1 — Fix design-path contract defects
- **Objective:** remove reachable crashes and test non-hermeticity without weakening validators.
- **Confirmed root cause:** `Assumption.confidence` constructed but forbidden; `api_auth.py` `setdefault` no-op with a real `.env` token; retry prompt asks for a forbidden field; duplicate `aws_provider` methods.
- **Files:** `domain/requirements/models.py` or `services/requirements/assumptions.py` (one decision applied consistently), `agents/*/prompt.py` (confidence references), `tests/api_auth.py`, `services/knowledge/providers/aws_provider.py`.
- **Depends on:** nothing.
- **Acceptance:** full backend suite green in a clean and in a `.env`-with-token environment; region "you decide" path records an assumption and proceeds; no validator relaxed.
- **Regression tests:** `test_region_default_policy.py`, `test_api_*`, `test_requirements_boundary.py`.
- **Verify:** `.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q` (expect 0 failed).
- **Risks:** schema change must be migrated consistently (prompts + recorded data).
- **Effort:** 0.5–1 day. **AWS?** No.

### Slice 2 — Reconcile the module catalog with the real modules; align formatting
- **Objective:** make the catalog truthful before it becomes the render source.
- **Confirmed root cause:** `HTTP_API` `inputs=()` vs 5 variables; `package_type` without a variable; log-group `log_group_name` unbound; `billing_mode` casing; fmt drift.
- **Files:** `services/provisioning/module_catalog.py`, `module_registry.py`, `registry.py`, `module_parameters.py`, `infra/modules/aws-http-api/main.tf`, `infra/modules/aws-serverless-function/main.tf`, contract tests.
- **Depends on:** nothing (parallel to Slice 1).
- **Acceptance:** every declared input binds to a real variable of the same type; every required module variable is supplied or explicitly derived; `terraform fmt -check -recursive` exits 0; catalog tests assert module↔contract equality.
- **Regression tests:** `test_provisioning_module_contract.py`, `test_provisioning_module_parameters.py`, `test_provisioning_log_group.py`, `test_provisioning_slice2.py`; plus a new fmt assertion test.
- **Verify:** `terraform fmt -check -recursive infra/modules`; pytest subset.
- **Risks:** touching a "verified" catalog requires updating verification metadata honestly.
- **Effort:** 0.5–1 day. **AWS?** No.

### Slice 3 — Render `module` blocks from `ModuleSelection`
- **Objective:** make the deployed artifact the verified module artifact.
- **Confirmed root cause:** renderer emits raw resources; guard bans `module`; selection discarded.
- **Files:** `terraform_renderer.py`, `terraform_guard.py` (allow `module` blocks only for registry sources with pinned local paths and no `source` overrides), `module_selection.py` (emit bindings), renderer tests.
- **Depends on:** Slice 2.
- **Acceptance:** rendered tree contains one `module` block per selected resource, with parameters bound from the spec and names derived deterministically; identical inputs → byte-identical tree (hash test); guard still rejects `remote-exec`, `external`, `lifecycle`, `import`, etc.; `terraform init/validate` passes in the temp-dir check.
- **Regression tests:** `test_provisioning_terraform_render.py`, `test_provisioning_plan.py`, module contract tests; determinism hash test.
- **Verify:** pytest subset; `terraform init -backend=false && terraform validate` in a temp copy (requires provider download — network).
- **Risks:** provider schema surprises; mitigated by the temp-dir validate gate.
- **Effort:** 2–4 days. **AWS?** No (network for provider download).
- **Decision note:** the alternative — keep raw-resource rendering and withdraw the "verified module" claim — is a scope change that requires explicit project-owner approval; this plan recommends module rendering because R-11 is a load-bearing product claim.

### Slice 4 — Plan-artifact integrity
- **Objective:** apply exactly what was reviewed.
- **Confirmed root cause:** regex summary; `apply -auto-approve` re-plans; plan file unused.
- **Files:** `executor.py`, `plan_builder.py`, API provisioning surface, tests.
- **Depends on:** Slice 3 (tree must be final).
- **Acceptance:** plan mode writes `ai_architect.tfplan` and derives the summary from `terraform show -json` (resource addresses, actions); apply runs `terraform apply ai_architect.tfplan` and refuses if the plan file is missing or its tree hash changed; `APPLY` confirmation, single-use, and marker semantics unchanged.
- **Regression tests:** `test_provisioning_execute.py`, `test_provisioning_plan.py`, `test_provisioning_http_api.py` (fake CLI asserts the exact argv).
- **Verify:** pytest subset; inspect argv assertions.
- **Risks:** plan files embed state references; keep them inside the gitignored workspace and never in the repo.
- **Effort:** 1–2 days. **AWS?** No.

### Slice 5 — Post-apply smoke verification
- **Objective:** a green apply becomes evidence the demo works.
- **Confirmed root cause:** `deployment.json` records exit codes only.
- **Files:** `executor.py` (post-apply hook), `deployment.json` schema, tests.
- **Depends on:** Slice 4.
- **Acceptance:** when the spec contains an HTTP API + Lambda, the executor performs a bounded GET against the deployed endpoint and records status/latency/body hash; failure is reported as `smoke_failed`, never retried silently; absent endpoint → `smoke_skipped` with reason.
- **Regression tests:** new unit tests with a fake HTTP client.
- **Verify:** pytest subset.
- **Risks:** network flakiness — bounded timeout, explicit result.
- **Effort:** 0.5–1 day. **AWS?** Only to verify for real (Slice 6).

### Slice 6 — First controlled real run (plan → review → authorize → apply → smoke → manual destroy)
- **Objective:** close the "never exercised" evidence gap.
- **Confirmed root cause:** no `var/terraform` artifacts exist.
- **Files:** none expected (unless defects surface).
- **Depends on:** Slices 1–5.
- **Acceptance:** one real session produces a plan, an approval, an apply, a smoke result, and a clean manual destroy; `deployment.json` + `tfstate` retained; findings recorded.
- **Verify:** run the documented UI/CLI flow; inspect `var/terraform/<session>/<tree>/`.
- **Risks:** real spend (small demo stack), IAM scoping, region availability. **Requires explicit user authorization and AWS credentials.**
- **Effort:** 0.5–1 day. **AWS?** Yes.

### Backlog (post-demo, not blocking)
Risk tiers + rollback planner + approval display (R-15/R-16/R-18); tfsec/checkov + OPA consolidation (R-13/R-14); remote state/locking (R-17); STS broker; WORM audit; Phase 3 operate suite; multi-tenancy; eval suite. Each is independently sliceable; none should be smuggled into the demo.

**Single highest-priority next slice: Slice 2.** Evidence: the module composition is the demo's central claim and the root cause is confirmed (catalog drift + banned `module` blocks + fmt failures). Slice 3 — the actual fix — is blocked by Slice 2's drift; Slice 2 is small, entirely offline-verifiable (`fmt -check` + contract tests), and does not risk the working design pipeline. Slice 1 should run in parallel by a second engineer because it is independent and removes two live defects.

# DELIVERABLE G — Demo readiness checklist

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Start a design session and extract requirements | PASS | Real sessions + checkpoints |
| 2 | Clarification interrupt works and records answers | PASS | `__interrupt__` writes in `826d108a…`, `b9ae1d61…` |
| 3 | Capacity values are deterministic | PASS | `calculator.py`, passing tests, recorded `capacity_result` |
| 4 | Component selection grounded with evidence refs | PARTIAL | Evidence-existence tests pass; no name registry; empty-bundle degrades |
| 5 | Cost estimate with honest unpriced handling | PASS (on-demand) | `fully_priced=false`; recorded totals |
| 6 | Architecture artifact served to UI | PASS | `/architecture` 200 for `ef769931…`; frontend tests 171/171 |
| 7 | Critic loop bounded and gate deterministic | PASS | ≤2 cycles; gate tests |
| 8 | Design approval blocks unapproved generation | PASS | `approvals.py`; tests |
| 9 | Terraform generation reaches a rendered tree | PASS (offline) | Renderer tests; deterministic `_align_equals` |
| 10 | `terraform fmt` passes on generated tree | NOT VERIFIED | Renderer asserts formatting; no real tree run recorded |
| 11 | `terraform validate` passes on generated tree | NOT VERIFIED | Code path exists; never executed; no recorded result |
| 12 | Generated tree composes verified modules | **FAIL** | Raw resources; `module` banned (`terraform_guard.py:48`) |
| 13 | Module library itself passes `fmt -check` | **FAIL** | 2 files fail (verified, Terraform v1.16.5) |
| 14 | Live plan review shows real add/change/destroy | NOT VERIFIED | Implemented; never run; summary regex-parsed |
| 15 | Provisioning approval binds to exact tree | PASS | `executor.py:299-386`; tests |
| 16 | Approval is single-use | PASS | `consumed_at`; tests |
| 17 | Apply requires prior review + `APPLY` | PASS | `executor.py:389-427, 699-703`; tests (faked CLI) |
| 18 | Apply executes the reviewed plan | **FAIL** | `apply -auto-approve` re-plans; plan file unused |
| 19 | Apply ran successfully against AWS | NOT VERIFIED | No `var/`, `tfstate`, `deployment.json` |
| 20 | Post-apply smoke test | **FAIL (not implemented)** | `deployment.json` exit codes only |
| 21 | Interrupted apply cannot silently retry | PASS | `apply_running` marker; tests |
| 22 | Cleanup path documented and safe | PARTIAL | Manual `terraform destroy` only; not exercised |
| 23 | Rollback plan shown before apply | **FAIL (not implemented)** | No rollback planner |
| 24 | API auth on mutating routes | PASS | Bearer, fail-closed 503, constant-time compare |
| 25 | No unauthenticated state mutation | **FAIL** | `GET /optimization-recommendations` writes |
| 26 | Frontend renders served artifacts only | PASS | Adapters; 171 vitest tests |
| 27 | Backend test suite green | **FAIL (46/1140)** | Two root causes (§10) |
| 28 | Requirements traceability documented | PASS | This report, Deliverable B |

# DELIVERABLE H — Repository preservation report

**Repository state at audit start (unchanged at end):**

- Branch `master`; HEAD `584ac7d91fc5d9a34e6aeeec731c1925c58c6412`; recent commits `584ac7d`, `cdbbfae`, `f6cfb67`, `79d746d`, `50edd71`.
- 29 modified tracked files, including `.env.example`, `.gitignore`, `poetry.lock`, `pyproject.toml`, `src/ai_architect/agents/{architecture,capacity,design,requirements}/**`, `src/ai_architect/config/settings.py`, `src/ai_architect/domain/{capacity,requirements}/**`, `src/ai_architect/infrastructure/{checkpoint,embeddings,llm,qdrant}/**`, `src/ai_architect/services/{knowledge,requirements}/**`, `tests/test_architecture_composer_regression.py`.
- Untracked: whole trees `frontend/`, `infra/`, `docs/`, `audit_sources/`, `data/` (aggregated), plus `AGENTS.md`, `audit_compare.json`, six backend logs, `package-lock.json`, and ~90 untracked `src/ai_architect/**` and `tests/**` paths (API layer, architecture agent, component selection, critic, cost, provisioning, approvals, region services, 62 test files). All were preserved untouched.

**What this audit did (and did not do):**

- DID: read files; run `git status/log/show` (read-only); run the offline backend suite with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`; run frontend vitest with `CI=1`; run `terraform fmt -check -recursive` and `terraform fmt` **on a temporary copy** in `%TEMP%` (deleted afterward); query the checkpoint SQLite read-only; read `.env` for **key names only**.
- DID NOT: modify/delete/stage any file; reset/clean/stash; install dependencies; call AWS APIs; run `terraform init/validate/plan/apply/destroy` against the repo; print secret values.

**Files created by this audit:** `audit-report-2026-10-18.md` (this report — the only new file in the repo tree). Agent scratch notes and run transcripts live under `.zwork/` (gitignored runtime state: `.zwork/audit-notes/tests-docs.md`, `.zwork/runs/28b8706c/*`).

**Side effects disclosed:** pytest wrote nothing to the repo (cache disabled, bytecode disabled); vitest may have refreshed `frontend/node_modules/.vite` (pre-existing dependency cache); LangSmith tracing caused outbound HTTP 429 noise during the backend run (no test failure); Terraform temp copies were removed.

**Git status at audit end:** identical entry set to audit start, plus the untracked report file above.

## 9. AWS skill consultation record (required by `AGENTS.md`)

Before the AWS-specific design assessments in this audit (Terraform provisioning, module quality, demo-scope judgments), the relevant AWS skills were discovered through the global skill mechanism and loaded:

| Skill consulted | Relevant guidance | Resulting decision in this audit | Project-specific deviation |
|---|---|---|---|
| `aws-serverless` (+ `references/production.md`) | Production readiness: set log retention explicitly (default is never expire); minimum alarm set (Errors/Throttles/Duration P99/…); one IAM role per function scoped to exact ARNs; explicit timeout, never the 3s default; post-deploy smoke tests; no secrets in env vars | Confirmed the 30-day retention requirement is legitimate and its propagation fix (R-19) is the right shape; treated the missing alarm resources as a real production gap to disclose, not a demo blocker; confirmed the Lambda module's least-privilege inline policy and required (no-default) `timeout_seconds` are sound; promoted post-apply smoke verification to a demo slice (S5) | Alarms are not provisioned by the log-group module (`module_catalog.py` limitation: "No metric filters, alarms, or subscription filters exposed") — accepted only as a disclosed demo limitation, not as production-ready |
| `aws-security` | Least privilege, encryption at rest, no suppression of findings, no secret values in reports | Kept all evidence read-only; reported key **names** only; treated "no authorizer on the HTTP API" and "no rate limiting" as explicit disclosures | Demo runs in the operator's own account with operator-supplied credentials; the spec's credential-broker model is deferred (R-17), not silently claimed |
| `aws-observability` | CloudWatch log groups, retention, alarms as first-class configuration | Grounded the retention checks and the "observability gap" finding in the design critic | No CloudWatch telemetry is consumed by the app (Phase 3 absent) |
| `aws-deployment` | CI/CD pipeline guidance (CodePipeline/CodeBuild/CodeDeploy) | Determined it is **not applicable** to this Terraform-based local demo; no pipeline changes recommended | None — skill explicitly out of scope for Terraform provisioning |

Skills informed judgments only; they did not override project requirements, deterministic validators, closed-world rules, fail-closed behavior, or task scope.

## 10. Test and verification record (executed)

1. **Backend (executed):** Python 3.12.0 (`.venv`), pytest 9.1.1, `pytest tests -p no:cacheprovider -q`, `PYTHONDONTWRITEBYTECODE=1` → **1140 collected; 1100 passed; 46 failed (39 tests + 7 subtests); 1 skipped; 295 subtests passed; 742 warnings; 209 s.**
2. **Failure root causes:** (A) non-hermetic API token — `tests/api_auth.py` uses `os.environ.setdefault`, while `config/settings.py:5` loads `.env` (which defines the real token) at import → 401s: 15 in `test_api_design_run.py`, 21+2 in `test_api_design_state.py`. (B) `Assumption.confidence` contract drift — `ValidationError: Extra inputs are not permitted` at `services/requirements/assumptions.py:270`: 3+5 in `test_region_default_policy.py`. The suite is order-sensitive (49 failures when the three files run in isolation).
3. **Frontend (executed):** vitest 2.x, 14 files, **171 tests, all passed** (16.4 s).
4. **Coverage:** static import analysis over 125 source modules → **26 never imported by any test**, including the entire knowledge/RAG stack, Qdrant infrastructure, all agent wrappers/prompts, `cli/{ingest,retrieve,provision,component_selection,progress}.py`, `api/models.py`, `llm/test_connection.py`. Line coverage not measurable (pytest-cov not installed; install prohibited).
5. **Terraform (executed, temp copy):** Terraform v1.16.5; `terraform fmt -check -recursive` exits **3**, flagging `aws-http-api/main.tf` (integration block `=` alignment) and `aws-serverless-function/main.tf` (`name` attribute over-aligned). `init/validate` not run.
6. **End-to-end evidence:** full design pipeline persisted (`ef769931…`, `09745641…`; two complete outputs with on-demand totals 5.805905 and 5713.969104 USD/month in `audit_compare.json`). No Terraform plan/apply artifacts exist (`var/` absent) — provisioning is **not** end-to-end verified.

## 11. Compliance with the final rules

Investigation and planning only; no slices implemented; no AWS provisioning; no `terraform apply/destroy`; no missing evidence invented; unverifiable items marked UNCERTAIN; repository preserved. Per the brief, work stops here.

**Highest-priority next slice:** Slice 2 (catalog↔module reconciliation + fmt alignment), immediately followed by Slice 3 (render `module` blocks from `ModuleSelection`). The evidence is direct and reproducible: `module_catalog.py` `HTTP_API inputs=()`, `terraform_guard.py:48` banning `module`, `terraform_renderer.py:436/702-774` emitting raw resources, and `terraform fmt -check` failing today on two of the five "verified" modules.
