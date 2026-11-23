# Local demo: plan review, authorization, apply, inspection, destroy

This is the bounded local-demo path for provisioning: generate a
Terraform plan for an approved design, review the live plan, authorize
that exact reviewed tree, apply it, inspect the result afterward, check
for drift, and destroy it through a separately reviewed operation.
Nothing here weakens existing gates: the LLM still proposes and
deterministic code still decides; apply still requires an explicit,
matching approval and the `APPLY` confirmation, and destroy requires its
own reviewed plan and the `DESTROY` confirmation.

## Verified serverless full-stack scope

The verified module catalog covers ten resource kinds:

- S3 object storage, Lambda function, HTTP API, DynamoDB table,
  CloudWatch log group and alarms
- CloudFront distribution in front of the S3 bucket (origin access
  control plus a bucket policy scoped to the distribution)
- Cognito user pool with a standalone app client
- SQS standard queue
- SNS topic with an SQS subscription

A design using exactly these kinds renders as a whole architecture with
no fallback proposals. Inputs that are not derivable remain explicit
human inputs: in particular S3 `bucket_name` is never invented by a
profile.

That list is also the closed world given to the Component Selection
stage for AWS: the selection prompt names exactly these ten components
and tells the model to map a requirement that needs anything else (a
CloudWatch dashboard, for example) onto the closest listed component and
state the gap in its assumptions. A component outside the list has no
module, so it fails closed at spec-build time instead of silently
appearing in a plan.

Known limitations of the verified modules:

- CloudFront serves the S3 origin only; no custom domain, ACM
  certificate, Lambda@Edge, or CloudFront Functions.
- Cognito is standalone: the HTTP API does not attach a JWT authorizer,
  and identity pools/federation are out of scope.
- SQS is a standard queue with no Lambda event source mapping, FIFO, or
  DLQ configuration.
- SNS fan-out targets SQS only (no email/SMS/push/HTTP subscriptions).
- SQS and SNS are provisioned as topology components but are not
  exercised by the demo handler: Lambda does not publish to SNS or send
  to SQS, and no consumer is attached to the queue.

## Where things live

Durable workspaces (one per session per plan tree):

```
var/terraform/<session-id>/<tree_hash[:16]>/
    .ai-architect-workspace.json   # marker binding the tree fingerprint
    *.tf                           # the rendered plan files
    demo-handler.zip               # staged, hash-verified artifacts
    ai-architect.tfplan            # saved apply plan
    ai-architect-destroy.tfplan    # saved destroy plan (separate file)
    terraform.tfstate              # written by terraform apply
    deployment.json                # executor outcome after an apply
    destroy.json                   # executor outcome after a destroy
```

`var/` is gitignored. Terraform's `terraform.tfstate` in this workspace
is the authoritative machine record of what exists; `deployment.json`
and `destroy.json` are the executor's own outcomes for human reading.
Nothing deletes the workspace: an interrupted apply or destroy leaves
everything inspectable.

## UI flow (frontend)

0. **Suggest values** (optional) - in the Terraform plan panel, the
   "Pending human decisions" block has a **Suggest values** button. It
   calls `POST /design/sessions/{id}/provisioning/suggest-inputs` and
   fills the input boxes with a starting point. The values are
   advisory: nothing is written to the plan, the checkpoint, or AWS, and
   the operator still edits them and presses **Submit inputs &
   regenerate**, which re-validates every value server-side.

   The three value classes are decided by code, not by the model:

   - `platform_default` - facts the project owns: the packaged demo
     artifact's `runtime` and `handler`, and the key schema its handler
     requires. The model is never asked about them.
   - `ai_suggestion` - policy values a model proposes for the design. It
     uses the user's request when it constrains a value and falls back to
     the default a careful AWS engineer would deploy when the request is
     silent: a detailed request is context, not a prerequisite. Every
     proposal is decoded by the same module-contract decoder that later
     accepts or refuses a human-typed value; a value outside the closed
     allowlist is dropped with its reason and the input stays for the
     operator. One bounded correction round may repair a refused
     proposal. A scalar spelling (`1024`, `true`) is normalized to the
     plan's string form instead of being refused, a partly malformed
     answer is salvaged item by item, and an unreachable model degrades
     to the platform defaults plus a note - suggestions never turn the
     panel into an error, because the operator can always type values.
   - skipped - values only the operator can decide. A bucket name is
     globally unique in S3, so it is never proposed; the panel reports
     it under "Left for you".

   The model never sees or proposes account identifiers, ARNs, URLs,
   IAM policies, secrets, prices, code artifacts or file paths.

1. **Run plan review** - calls `POST /design/sessions/{id}/provision`
   with `{"mode": "plan"}`. The live `terraform plan` runs in the
   workspace above; the summary (`+add ~change -destroy`) is shown. The
   destroy count comes from this live review, never from the static
   inventory (the static inventory reports destroys as unknown).
2. **Authorize this plan** - calls
   `POST /design/sessions/{id}/approve-provisioning`, binding the
   approval to the plan's `tree_hash`/`spec_id`/`artifact_id` and
   artifact manifest. Re-running the review invalidates the previous
   authorization in the UI.
3. **Apply approved plan** - calls the provision endpoint with
   `{"mode": "apply", "confirm": "APPLY"}`. The backend re-validates
   the approval against the stored plan, requires the stored review to
   match the current tree, consumes the approval (single use), and
   records an `apply_running` marker before anything runs.

The API also supports `destroy-plan`, `destroy`, and `drift` modes (see
below); the current UI exposes plan/approve/apply only.

## CLI flow

```powershell
.venv\Scripts\python.exe -m ai_architect.cli.main provision `
    --session-id <id> --mode plan
.venv\Scripts\python.exe -m ai_architect.cli.main provision `
    --session-id <id> --mode apply --confirm APPLY [--pass-aws-env]
.venv\Scripts\python.exe -m ai_architect.cli.main provision `
    --session-id <id> --mode destroy-plan [--pass-aws-env]
.venv\Scripts\python.exe -m ai_architect.cli.main provision `
    --session-id <id> --mode destroy --confirm DESTROY [--pass-aws-env]
.venv\Scripts\python.exe -m ai_architect.cli.main provision `
    --session-id <id> --mode drift [--pass-aws-env]
```

All modes share the executor gates with the API. Apply from the CLI
requires a stored plan review for the same tree (run plan mode first)
and consumes the approval. `destroy-plan` saves a distinct
`terraform plan -destroy` file; `destroy` executes only that exact saved
destroy plan, bound by its sha256. `drift` runs a refresh-only plan and
reports `no_drift`, `drift`, or `drift_error` without changing state.
`--pass-aws-env` forwards an explicit AWS_* allowlist from your own
environment; without it the child process receives no credentials.

## Post-apply inspection

After a successful apply the stored result includes:

- `outputs`: `terraform output -json` (`status` plus `values`), served
  verbatim for inspection.
- `smoke`: a bounded HTTP check against any output named
  `<instance_id>__api_endpoint`. `ok` means the endpoint answered with a
  status below 500; `not_applicable` means the design has no HTTP
  endpoint; `not_checked` means outputs could not be read. A failed
  smoke check is recorded but never turns a successful apply into a
  failure.

## Safety properties

- **Single use.** An apply consumes its approval (`consumed_at`); a
  spent approval can never authorize another run. Re-approve a reviewed
  plan to get a fresh one.
- **Duplicate/interrupted applies.** An `apply_running` marker is
  stored before the executor starts and cleared when a result is
  stored. A second apply while it is present is refused with
  `apply_interrupted`; inspect the workspace, then run plan mode again.
  A pre-flight refusal (e.g. terraform not installed) restores the
  review and unconsumed approval so a retry stays possible.
- **Destroy is separate.** An apply review can never authorize a
  destroy. `destroy-plan` stores mode `destroy_plan` with the saved
  destroy plan's `{name, sha256}`; `destroy` requires
  `--confirm DESTROY` and that exact binding. A `destroy_running` marker
  prevents a silent retry after an interruption; run `destroy-plan`
  again to produce a fresh review.
- **Never discarded state.** The workspace is only ever written by the
  marker protocol: a directory without a marker for this exact tree
  fingerprint is refused, never touched.

## CloudWatch alarms

The verified `cloudwatch_alarms` module renders four upper-bound metric
alarms when the design contains exactly one HTTP API and one Lambda:

- API Gateway `5XXError` count and `Latency` p99 on the `$default` stage.
- Lambda `Errors` count and `Duration` p99.

Every operational choice is an explicit human input: four thresholds,
period, evaluation periods, comparison operator, missing-data treatment,
and notification target. There are no defaults. If the architecture does
not supply these values, rendering stops with missing-input diagnostics
rather than inventing them.

`notification_target` is either `none` or an existing same-region SNS
topic ARN. The platform never creates a topic for alarms, and it does not
configure OK or insufficient-data actions. Generic raw
`aws_cloudwatch_metric_alarm` resources are still refused by the fallback
renderer; only this verified module path is supported. Error-rate,
composite, anomaly-detection, metric-math, throttling, and additional
metric alarms are out of scope.

## Inspect and clean up

```powershell
# Show what terraform believes exists:
terraform -chdir=var\terraform\<session-id>\<tree> show

# Read the executor's own outcomes:
Get-Content var\terraform\<session-id>\<tree>\deployment.json
Get-Content var\terraform\<session-id>\<tree>\destroy.json
```

Prefer the reviewed path for teardown:

```powershell
ai-architect provision --session-id <id> --mode destroy-plan --pass-aws-env
# Review the printed destroy summary, then:
ai-architect provision --session-id <id> --mode destroy --confirm DESTROY --pass-aws-env
```

A manual `terraform destroy` in your own shell is still possible for
operator recovery, but it bypasses the reviewed destroy-plan binding and
is not an application action.
