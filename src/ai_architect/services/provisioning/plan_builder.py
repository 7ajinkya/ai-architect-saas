"""Deterministic provisioning-plan assembly from an approved design.

Chain (verified-first, LLM only as a bounded proposer):

approved ArchitectureResult
  -> build_infra_spec (existing deterministic derivation; raises on
     unregistered components exactly as before)
  -> on refusal: partition supported/unsupported (IAM skip preserved)
  -> apply_supplied_inputs (human decisions, contract-validated)
  -> select_modules (verified catalog only)
  -> render_module_composition (guarded verified module tree with
     pinned in-tree module sources; fails closed when the selection
     is not fully clean)
  -> controlled LLM fallback for unsupported nodes only
     (propose structured specs -> deterministic validate -> render
     fallback.tf; bounded: one proposal + one correction)
  -> guard merged tree (union vocabulary) -> content hash over merged
     files (existing approval binds this hash; no second mechanism)
  -> run_terraform_checks (fmt/init/validate when terraform exists)
  -> static inventory analysis (Plan Analyzer role: counts by kind,
     greenfield creates, zero destroys)

The result is a plain-JSON plan dict shaped for checkpoint storage
and API display. Blast analysis here is static inventory analysis,
explicitly labeled as such: it is NOT a live ``terraform plan``,
which requires credentials and runs only in the approval-gated
executor.
"""

from ai_architect.domain.provisioning.models import (
    ProvisioningErrorCode,
)
from ai_architect.domain.provisioning.render import (
    RenderedFile,
    RenderStatus,
)
from ai_architect.services.provisioning import iac_fallback as iac
from ai_architect.services.provisioning.demo_profile import (
    demo_profile_defaults,
)
from ai_architect.services.provisioning.deployment_naming import (
    deployment_namespace,
)
from ai_architect.services.provisioning.executor import (
    artifact_manifest_for_spec,
)
from ai_architect.services.provisioning.module_registry import (
    build_default_registry,
)
from ai_architect.services.provisioning.registry import (
    UnsupportedResourceError,
)
from ai_architect.services.provisioning.module_selection import (
    select_modules,
    validate_module_selection,
)
from ai_architect.services.provisioning.spec_builder import (
    ProvisioningError,
    apply_supplied_inputs,
    build_infra_spec,
    diagnose_spec,
    iam_exclusion_warning,
)
from ai_architect.services.provisioning.structured_inputs import (
    StructuredInputError,
    normalize_resource_inputs,
)
from ai_architect.services.provisioning.terraform_cli import (
    TerraformStatus,
    run_terraform_checks,
)
from ai_architect.services.provisioning.terraform_guard import (
    guard_terraform_source,
    resource_types_in_source,
)
from ai_architect.services.provisioning.terraform_renderer import (
    ALLOWED_TF_RESOURCE_TYPES,
    tree_hash,
    module_guard_policy,
    render_module_composition,
)


def _v(value) -> str:
    """Enum-safe string rendering for plan records."""
    return str(getattr(value, "value", value))


def _inventory(resources, instances_by_resource, generated=()) -> dict:
    """Static inventory over spec resources plus rendered fallback.

    ``resources`` are spec ResourceInstance rows; ``generated`` is a
    list of ``(resource_id, kind)`` pairs for fallback content actually
    included in the tree. Rendered fallback counts as creates (it is
    hash-bound in the tree); anything else uncovered is reported with
    explicit reasons elsewhere in the plan.
    """
    by_kind: dict[str, int] = {}
    covered = 0
    for resource in resources:
        kind = _v(resource.kind)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if resource.resource_id in instances_by_resource:
            covered += 1
    total = len(resources)
    for resource_id, kind in generated or ():
        by_kind[str(kind)] = by_kind.get(str(kind), 0) + 1
        covered += 1
        total += 1
    return {
        "analysis": "static_inventory",
        "creates": covered,
        "destroys": None,
        "uncovered": total - covered,
        "by_kind": by_kind,
        "note": (
            "Greenfield static inventory derived from the rendered "
            "tree, not a live terraform plan: every covered resource "
            "would be created. Destroys are confirmed only by the live "
            "plan review and are never assumed to be zero; uncovered "
            "resources are excluded with explicit reasons."
        ),
    }


def build_provisioning_plan(
    *,
    architecture,
    provider: str,
    approval: dict,
    inputs: dict[str, dict[str, str]] | None = None,
    prior_inputs: dict[str, dict[str, str]] | None = None,
    requirements=None,
    registry=None,
    iac_generator=None,
) -> dict:
    """Assemble the full provisioning plan for an approved architecture.

    ``approval`` is the stored design-approval record; it must accept
    this exact artifact or a ValueError is raised before any Terraform
    is produced. ``inputs`` maps resource_id to human-decided required
    parameters (e.g. bucket names); unknown resources, collisions, and
    contract violations fail loudly via the existing machinery.
    ``prior_inputs`` is the explicit input map stored on the previous
    plan: values are carried forward only while they still name a
    pending input of the same resource; anything stale is dropped with
    an ``input_warnings`` entry rather than silently breaking the next
    plan. Current ``inputs`` win over carried-forward values.
    ``requirements`` is the session's requirements contract and is the
    only source for values derived from explicit user requirements
    (today: CloudWatch log-group retention).
    ``registry`` defaults to the project-owned catalog.
    ``iac_generator`` overrides the controlled LLM fallback used for
    components outside the verified catalog (tests only — production
    always uses the default generator); it is called as
    ``iac_generator(component=..., context=...)`` and must return
    ``(specs, proposal_sha256)``.
    """
    from ai_architect.services.approvals import require_approved_design

    require_approved_design(approval, architecture)

    try:
        spec = build_infra_spec(
            architecture, provider=provider, requirements=requirements
        )
        fallback_nodes: list = []
        iam_skipped: list = []
    except UnsupportedResourceError as original:
        # Verified-first: only components the closed registry refuses
        # take the LLM fallback path. build_infra_spec itself is
        # untouched (still fail-closed for direct callers).
        supported, fallback_nodes, iam_skipped = iac.partition_architecture(
            architecture
        )
        if not fallback_nodes:
            raise
        filtered = architecture.model_copy(
            update={"nodes": list(supported)}
        )
        try:
            spec = build_infra_spec(
                filtered, provider=provider, requirements=requirements
            )
        except (UnsupportedResourceError, ProvisioningError):
            raise original
        if iam_skipped:
            spec = spec.model_copy(
                update={
                    "warnings": [
                        *spec.warnings,
                        *(
                            iam_exclusion_warning(node)
                            for node in iam_skipped
                        ),
                    ]
                }
            )

    # Fallback resource identities are deterministic and validated up
    # front; anything unusable fails that component closed here.
    fallback_ids: dict[str, object] = {}
    fallback_refused: list[dict] = []
    for node in fallback_nodes:
        refusal = iac.fallback_refusal_reason(node.component_name)
        if refusal is not None:
            fallback_refused.append({
                "resource_id": "",
                "node_id": node.node_id,
                "component_name": node.component_name,
                "message": refusal,
            })
            continue
        try:
            resource_id = iac.fallback_resource_id(node.node_id)
        except iac.FallbackError as exc:
            fallback_refused.append({
                "resource_id": "",
                "node_id": node.node_id,
                "component_name": node.component_name,
                "message": str(exc),
            })
            continue
        fallback_ids[resource_id] = node

    # Carry forward only explicit human inputs that still apply. A value
    # for a resource/input that no longer exists is discarded with a
    # visible warning; it is never silently sent to the contract
    # validator as if the plan still needed it.
    input_warnings: list[str] = []
    supplied: dict[str, dict[str, str]] = {}
    pending_by_resource = {
        resource.resource_id: {
            item.name for item in resource.pending_inputs
        }
        for resource in spec.resources
    }
    derived_by_resource = {
        resource.resource_id: {
            parameter.name for parameter in resource.parameters
        }
        for resource in spec.resources
    }
    resources_by_id = {
        resource.resource_id: resource for resource in spec.resources
    }
    if prior_inputs is not None and not isinstance(prior_inputs, dict):
        input_warnings.append(
            "Stored plan inputs were malformed and were ignored."
        )
        prior_inputs = None
    for resource_id in sorted(prior_inputs or {}):
        values = prior_inputs[resource_id]
        if resource_id not in pending_by_resource:
            input_warnings.append(
                f"Stored input for {resource_id!r} no longer matches a "
                "resource in this plan and was discarded."
            )
            continue
        if not isinstance(values, dict):
            input_warnings.append(
                f"Stored inputs for {resource_id!r} were malformed and "
                "were discarded."
            )
            continue
        kept: dict[str, str] = {}
        for name in sorted(values):
            value = values[name]
            if name in derived_by_resource[resource_id]:
                input_warnings.append(
                    f"Stored input {name!r} for {resource_id!r} is no "
                    "longer a required input and was discarded."
                )
                continue
            if name not in pending_by_resource[resource_id]:
                input_warnings.append(
                    f"Stored input {name!r} for {resource_id!r} is not a "
                    "required input for this plan and was discarded."
                )
                continue
            if not isinstance(value, str) or not value:
                input_warnings.append(
                    f"Stored input {name!r} for {resource_id!r} is not a "
                    "usable string and was discarded."
                )
                continue
            kept[name] = value
        if kept:
            try:
                kept = normalize_resource_inputs(
                    resources_by_id[resource_id], kept
                )
            except StructuredInputError as exc:
                input_warnings.append(
                    f"Stored inputs for {resource_id!r} were discarded: "
                    f"{exc}"
                )
                kept = {}
        if kept:
            supplied[resource_id] = kept
    for resource_id in sorted(inputs or {}):
        merged = dict(supplied.get(resource_id, {}))
        merged.update(dict(inputs[resource_id]))
        resource = resources_by_id.get(resource_id)
        if resource is not None:
            try:
                merged = normalize_resource_inputs(resource, merged)
            except StructuredInputError as exc:
                raise ProvisioningError(
                    str(exc),
                    code=ProvisioningErrorCode.MALFORMED_PARAMETER.value,
                ) from exc
        supplied[resource_id] = merged
    applied_defaults: list[dict] = []
    for default in demo_profile_defaults(spec):
        values = supplied.setdefault(default.resource_id, {})
        if default.name in values:
            continue
        values[default.name] = default.value
        applied_defaults.append({
            "resource_id": default.resource_id,
            "name": default.name,
            "value": default.value,
            "source": default.source,
            "rationale": default.rationale,
            "requires_confirmation": default.requires_confirmation,
        })
    user_supplied = {
        resource_id: dict(values)
        for resource_id, values in supplied.items()
        if values
    }

    # Auto-supply the project's demo Lambda artifact (path + computed
    # hash) for Lambda resources when the operator has not explicitly
    # provided code_artifact inputs. Uses the existing deterministic
    # builder; never fabricates values. User-supplied values win.
    from ai_architect.services.provisioning.spec_builder import (
        merge_demo_lambda_artifact_inputs,
    )

    supplied = merge_demo_lambda_artifact_inputs(user_supplied, spec)
    spec_ids = {resource.resource_id for resource in spec.resources}
    unknown = sorted(
        key for key in supplied
        if key not in spec_ids and key not in fallback_ids
    )
    if unknown:
        raise ProvisioningError(
            f"Supplied inputs name unknown resource "
            f"{unknown[0]!r}; expected one of "
            f"{sorted(spec_ids | set(fallback_ids))}.",
            code=ProvisioningErrorCode.INCONSISTENT_REFERENCE.value,
        )
    targeted = sorted(key for key in supplied if key in fallback_ids)
    if targeted:
        # Fallback v1 has no human-input channel: values can only come
        # from the validated proposal, so naming a fallback resource
        # is refused loudly rather than silently dropped.
        raise ProvisioningError(
            f"Supplied inputs name fallback resource "
            f"{targeted[0]!r}, which takes no operator inputs; remove it.",
            code=ProvisioningErrorCode.MALFORMED_PARAMETER.value,
        )
    if supplied:
        spec = apply_supplied_inputs(
            spec, {key: supplied[key] for key in supplied}
        )

    spec_diagnostics = [
        {"category": str(item.category), "message": item.message}
        for item in diagnose_spec(spec)
    ]

    active_registry = registry or build_default_registry()
    # spec.provider, not the raw argument: build_infra_spec normalizes
    # provider casing (e.g. "AWS" -> "aws"), and module contracts declare
    # the normalized spelling. Passing the raw value here would make
    # every contract look provider-incompatible and refuse the whole
    # selection.
    selection = select_modules(
        spec,
        provider=spec.provider,
        registry=active_registry,
        namespace=deployment_namespace(architecture.session_id),
    )
    selection_errors = validate_module_selection(
        selection, registry=active_registry
    )
    if selection.diagnostics:
        selection_errors = [
            (
                f"{item.resource_id}: {item.message}"
                if item.resource_id else item.message
            )
            for item in selection.diagnostics
        ] + [
            error for error in selection_errors
            if error != "Selection has no selection_id."
        ]

    rendered = render_module_composition(
        spec, selection, registry=active_registry
    )
    module_policy = module_guard_policy(
        selection, registry=active_registry
    )
    rendered_ok = rendered.status == RenderStatus.RENDERED
    render_diagnostics = [
        {"category": str(item.category), "message": item.message}
        for item in rendered.diagnostics
    ]

    # Controlled LLM fallback for components outside the verified
    # catalog. Always attempted when such components exist: the plan is
    # all-or-nothing, so fallback diagnostics belong next to verified
    # diagnostics even when the verified tree itself failed. Bounded
    # cost (human-gated generations only; at most two rounds, each at
    # most one proposal + one correction per component). Every proposal
    # is validated deterministically; failures are recorded per
    # component, never raised (a non-FallbackError from the generator
    # is also converted: the endpoint must never 500 on generator
    # bugs).
    generate = iac_generator or iac.generate_fallback_content
    context = {}
    if fallback_ids:
        context = {
            "provider": spec.provider,
            "region_code": spec.region_code,
            "design_artifact_id": architecture.artifact_id,
            "evidence_refs": list(
                getattr(architecture, "evidence_references", None) or []
            )[:24],
        }

    def _component_of(node):
        return {
            "component_name": node.component_name,
            "node_id": node.node_id,
            "category": getattr(node, "category", ""),
            "role_in_system": getattr(node, "role_in_system", ""),
        }

    def _failed_record(resource_id, node, messages):
        return {
            "resource_id": resource_id,
            "node_id": getattr(node, "node_id", ""),
            "component_name": getattr(node, "component_name", ""),
            "resource_types": [],
            "proposal_sha256": "",
            "status": "failed",
            "notes": list(messages),
        }

    def _generate_round(jobs):
        """Run one generation round over {rid: (node, prior|None)}.

        Returns ``(valid, failed)`` where valid holds
        ``(resource_id, node, specs, sha)`` and failed holds
        generated-style records. Never raises.
        """
        valid = []
        failed = []
        for resource_id, (node, prior) in sorted(jobs.items()):
            try:
                specs, sha = generate(
                    component=_component_of(node),
                    context=context,
                    prior_errors=prior,
                )
            except iac.FallbackError as exc:
                failed.append(_failed_record(
                    resource_id, node, [str(exc)]
                ))
            except Exception as exc:  # noqa: BLE001 - fail closed
                failed.append(_failed_record(
                    resource_id, node,
                    [f"Fallback generator failed: {exc}."],
                ))
            else:
                valid.append((resource_id, node, specs, sha))
        return valid, failed

    def _render_all(valid):
        """Render valid entries; returns (content|None, error|None)."""
        if not valid:
            return None, None
        try:
            return iac.render_fallback_file([
                (resource_id, specs)
                for resource_id, _, specs, _ in valid
            ]), None
        except iac.FallbackError as exc:
            return None, str(exc)

    generated: list[dict] = []
    fallback_errors: list[str] = []
    for refused in fallback_refused:
        message = refused["message"]
        fallback_errors.append(
            f"{refused['node_id'] or '(no node id)'}: {message}"
        )
        render_diagnostics.append({
            "category": "fallback_refused",
            "message": (
                f"{refused['component_name'] or refused['node_id']}: "
                f"{message}"
            ),
        })
        generated.append({
            "resource_id": refused["resource_id"],
            "node_id": refused["node_id"],
            "component_name": refused["component_name"],
            "resource_types": [],
            "proposal_sha256": "",
            "status": "failed",
            "notes": [message],
        })

    valid_entries, failed_records = _generate_round({
        resource_id: (node, None)
        for resource_id, node in fallback_ids.items()
    })
    generated.extend(failed_records)
    fallback_errors.extend(
        f"{record['resource_id']}: {record['notes'][0]}"
        for record in failed_records
        if record["notes"]
    )
    fallback_content, render_error = _render_all(valid_entries)
    if render_error is not None:
        fallback_errors.append(render_error)
        for resource_id, node, _specs, _sha in valid_entries:
            generated.append(_failed_record(
                resource_id, node, [render_error]
            ))
        valid_entries = []

    base_files = (
        {item.path: item.content for item in rendered.files}
        if rendered_ok
        else {}
    )
    fallback_attempted = bool(fallback_ids) or any(
        True for _ in fallback_refused
    )

    def _mapped(diags):
        return [
            {"category": str(item.category), "message": item.message}
            for item in diags
        ]

    def _merge_and_guard(content):
        """Merge fallback content; returns (files, guard_diags, included).

        Anything but a fully clean merge leaves files empty: partial
        trees are never appliable.
        """
        if not base_files or content is None or fallback_errors:
            return {}, [], False
        merged = dict(base_files)
        merged["fallback.tf"] = content
        diags = guard_terraform_source(
            merged,
            allowed_resource_types=(
                ALLOWED_TF_RESOURCE_TYPES | iac.FALLBACK_POLICY_TYPES
            ),
            module_policy=module_policy,
        )
        if diags:
            return {}, diags, False
        return merged, [], True

    def _run_checks(tree_files):
        """fmt/init/validate on a tree; returns (validation, types, ok)."""
        if not tree_files:
            return (
                {
                    "status": "not_run",
                    "installed": False,
                    "version": None,
                    "exit_codes": {},
                    "diagnostics": [],
                },
                [],
                rendered_ok,
            )
        checks = run_terraform_checks(tree_files)
        validation = {
            "status": _v(checks.status),
            "installed": bool(checks.installed),
            "version": checks.version,
            "exit_codes": dict(checks.exit_codes),
            "diagnostics": [
                {"category": str(item.category), "message": item.message}
                for item in checks.diagnostics
            ],
        }
        terraform_ok = (
            checks.status == TerraformStatus.VALIDATE_OK
            or not checks.installed
        )
        resource_types = sorted(
            {
                resource_type
                for resource_type, _name in resource_types_in_source(
                    tree_files
                )
            }
        )
        return validation, resource_types, terraform_ok

    def _implicated_rids(messages, entries):
        """Map validate messages to fallback resource ids by label."""
        labels = {}
        for resource_id, _node, specs, _sha in entries:
            for index in range(len(specs)):
                labels[f"{resource_id}-{index + 1}"] = resource_id
        found: dict[str, list[str]] = {}
        for message in messages:
            for label, resource_id in sorted(labels.items()):
                if f'"{label}"' in message:
                    found.setdefault(resource_id, []).append(message)
        return found

    # Round-1 merge. With no fallback attempted this is the legacy
    # path exactly (verified tree only, renderer-guarded).
    guard_failed = False
    if not fallback_attempted:
        files = dict(base_files)
        fallback_included = False
    else:
        files, guard_added, fallback_included = _merge_and_guard(
            fallback_content
        )
        render_diagnostics = render_diagnostics + _mapped(guard_added)
        guard_failed = bool(guard_added)
    validation, resource_types, terraform_ok = _run_checks(files)

    # Bounded validate-driven retry (at most one extra round): only
    # when validate ran on a merged tree, failed, and names
    # fallback.tf. Only implicated components regenerate; the rest
    # carry over untouched.
    if (
        validation.get("status") == TerraformStatus.VALIDATE_FAILED.value
        and fallback_included
        and valid_entries
    ):
        validate_messages = [
            item.get("message", "")
            for item in validation.get("diagnostics") or []
            if "fallback.tf" in item.get("message", "")
        ]
        implicated = _implicated_rids(validate_messages, valid_entries)
        targets = [rid for rid in implicated if rid in fallback_ids]
        if targets and validate_messages:
            kept = [
                entry for entry in valid_entries if entry[0] not in set(targets)
            ]
            priors = {rid: implicated[rid] for rid in targets}
            new_valid, new_failed = _generate_round({
                rid: (fallback_ids[rid], priors[rid]) for rid in targets
            })
            for record in new_failed:
                generated.append(record)
                if record["notes"]:
                    fallback_errors.append(
                        f"{record['resource_id']}: {record['notes'][0]}"
                    )
            valid_entries = kept + new_valid
            fallback_content, render_error = _render_all(valid_entries)
            if render_error is not None:
                fallback_errors.append(render_error)
                for rid, node, _s, _h in valid_entries:
                    generated.append(_failed_record(
                        rid, node, [render_error]
                    ))
                valid_entries = []
                fallback_content = None
            files, guard_added, fallback_included = _merge_and_guard(
                fallback_content
            )
            render_diagnostics = render_diagnostics + _mapped(guard_added)
            guard_failed = bool(guard_added)
            validation, resource_types, terraform_ok = _run_checks(files)
    # Final generated records: rendered only for content present in
    # the bound tree; guard-rejected content failed (never bound);
    # validated-but-excluded content discarded (never bound). Only
    # "rendered" entries count toward inventory and hashing.
    for resource_id, node, specs, sha in valid_entries:
        if fallback_included:
            generated.append({
                "resource_id": resource_id,
                "node_id": node.node_id,
                "component_name": node.component_name,
                "resource_types": sorted({item["type"] for item in specs}),
                "proposal_sha256": sha,
                "status": "rendered",
                "notes": [],
            })
        elif guard_failed:
            generated.append({
                "resource_id": resource_id,
                "node_id": node.node_id,
                "component_name": node.component_name,
                "resource_types": [],
                "proposal_sha256": "",
                "status": "failed",
                "notes": [
                    "Merged tree failed safety guard; content excluded."
                ] + [
                    item["message"]
                    for item in render_diagnostics
                    if "fallback.tf" in item["message"]
                ],
            })
        else:
            generated.append({
                "resource_id": resource_id,
                "node_id": node.node_id,
                "component_name": node.component_name,
                "resource_types": sorted({item["type"] for item in specs}),
                "proposal_sha256": sha,
                "status": "discarded",
                "notes": [
                    "Fallback content validated but excluded: the "
                    "overall tree was not renderable."
                ],
            })
    fallback_failed = bool(fallback_errors) or any(
        entry["status"] != "rendered" for entry in generated
    )

    instances_by_resource = {
        instance.resource_id: {
            "instance_id": instance.instance_id,
            "module_id": _v(instance.module_id),
            "version": instance.version,
        }
        for instance in selection.instances
    }
    # Local deployment artifacts referenced by the approved spec
    # (basename + sha256, no bytes): persisted here so the provisioning
    # approval can bind them and the executor can stage exactly them.
    artifacts = artifact_manifest_for_spec(spec)
    generated_covered = [
        (entry["resource_id"], "iac_fallback")
        for entry in generated
        if entry["status"] == "rendered" and fallback_included
    ]
    resources = [
        {
            "resource_id": resource.resource_id,
            "node_id": resource.node_id,
            "kind": _v(resource.kind),
            "component_name": resource.component_name,
            "module": instances_by_resource.get(resource.resource_id),
            "pending_inputs": [
                {"name": item.name, "reason": item.reason}
                for item in resource.pending_inputs
            ],
        }
        for resource in spec.resources
    ]
    for entry in generated:
        resources.append({
            "resource_id": entry["resource_id"],
            "node_id": entry["node_id"],
            "kind": "iac_fallback",
            "component_name": entry["component_name"],
            "module": None,
            "pending_inputs": [],
        })

    terraform_ok = terraform_ok and rendered_ok
    ok = (
        not spec_diagnostics
        and not selection_errors
        and not render_diagnostics
        and terraform_ok
        and not fallback_failed
    )
    tree_hash_value = rendered.tree_hash
    if fallback_included:
        # The approval binds this merged hash: any post-approval change
        # to fallback content invalidates the approval like any other
        # tree change. No second approval mechanism exists.
        tree_hash_value = tree_hash([
            RenderedFile(path=path, content=content)
            for path, content in sorted(files.items())
        ])
    elif fallback_attempted:
        # Attempted but not included: nothing is appliable, so no hash
        # may authorize anything. require_provisioning_approval refuses
        # empty tree hashes; the executor refuses empty file sets.
        tree_hash_value = ""
    return {
        "status": "valid" if ok else "incomplete",
        "artifact_id": architecture.artifact_id,
        "spec_id": spec.spec_id,
        "tree_hash": tree_hash_value,
        "provider": spec.provider,
        "region_code": spec.region_code,
        "resources": resources,
        "modules": sorted(
            instances_by_resource.values(),
            key=lambda item: item["instance_id"],
        ),
        "generated": generated,
        "selection_errors": [str(item) for item in selection_errors],
        "spec_diagnostics": spec_diagnostics,
        "render_status": _v(rendered.status),
        "render_diagnostics": render_diagnostics,
        "files": files,
        "resource_types": resource_types,
        "terraform_validation": validation,
        "inventory": _inventory(
            spec.resources, instances_by_resource,
            generated=generated_covered,
        ),
        "spec_warnings": list(spec.warnings),
        "artifacts": artifacts,
        "inputs": user_supplied,
        "input_defaults": applied_defaults,
        "input_warnings": input_warnings,
    }
