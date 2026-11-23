"""Deterministic verified-module selection: the terminal Slice 4C artifact.

    InfraSpec [4A, frozen]
        -> capability mapping          capability_mapping.py
        -> verified registry           module_registry.py
        -> typed parameter binding     module_parameters.py
        -> ModuleSelection             this module

Selection is a **pure function** of
``(InfraSpec, provider, registry, namespace)``. The same inputs always
produce a byte-identical ``ModuleSelection``: collection order is explicit,
identifiers are content-derived, and nothing consults a clock, a random
source, the environment, the network or a model. The optional namespace
affects provider-visible names only; resource identities stay stable.

Trust properties:

- ``source`` and ``version`` are copied from the catalog contract. The
  ``proposal`` argument, when supplied, may only *nominate* a ``ModuleId``
  and suggest parameter values; it can never widen the vocabulary, and every
  nomination is re-validated against the catalog contract. Passing a
  fabricated source or version is not merely ignored, it is impossible to
  express.
- Only ``VerificationStatus.VERIFIED`` contracts are selectable.
- **All or nothing.** A single rejected resource yields zero instances, so a
  partially populated selection can never reach a future renderer.

Failure wording for an unmapped capability is fixed by the requirements:
"This design requires a component I don't have a verified module for. I can
generate a draft for engineering review, but I won't apply it automatically."
"""

import hashlib
import json

from ai_architect.domain.provisioning.models import InfraSpec, ResourceInstance
from ai_architect.domain.provisioning.module import (
    ModuleDiagnostic,
    ModuleDiagnosticCategory,
    ModuleErrorCode,
    ModuleId,
    ModuleInstance,
    ModuleSelection,
    ParameterType,
    SourceSpecRef,
)
from ai_architect.services.provisioning.capability_mapping import (
    describe_capability,
    module_for_kind,
)
from ai_architect.services.provisioning.module_parameters import (
    bind_resource_inputs,
    unmapped_parameters,
)
from ai_architect.services.provisioning.module_registry import (
    VerifiedModuleRegistry,
)

#: Fixed, requirements-mandated wording for a capability the catalog cannot
#: satisfy. Not improvised at the call site so every occurrence is identical.
NO_VERIFIED_MODULE_MESSAGE = (
    "This design requires a component I don't have a verified module for. "
    "I can generate a draft for engineering review, but I won't apply it "
    "automatically."
)


def _canonical(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _instance_id(module_id: ModuleId, resource_id: str) -> str:
    """Stable instance identity derived from capability and resource.

    Deterministic by construction: the same InfraSpec resource always yields
    the same module instance name, on any machine.
    """
    return f"{module_id.value}-{resource_id}"


def _provenance(spec: InfraSpec) -> SourceSpecRef:
    source = spec.source_architecture
    return SourceSpecRef(
        spec_id=spec.spec_id,
        spec_schema_version=spec.schema_version,
        artifact_id=source.artifact_id,
        architecture_hash=source.architecture_hash,
        upstream_inputs_hash=source.upstream_inputs_hash,
    )


def _selection_id(selection: ModuleSelection) -> str:
    payload = selection.model_dump(mode="json")
    payload.pop("selection_id", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _unavailable(resource: ResourceInstance, capability: str) -> ModuleDiagnostic:
    return ModuleDiagnostic(
        category=ModuleDiagnosticCategory.UNAVAILABLE,
        code=ModuleErrorCode.MODULE_NOT_AVAILABLE,
        message=(
            f"{NO_VERIFIED_MODULE_MESSAGE} "
            f"(component {resource.component_name!r}, capability {capability!r})"
        ),
        resource_id=resource.resource_id,
    )


def _bind_one(
    resource: ResourceInstance,
    contract,
    diagnostics: list,
    siblings=(),
    namespace="",
):
    """Bind one resource, appending only its own diagnostics.

    Tracks its own diagnostic slice so a failure on an earlier resource can
    never suppress a later one. ``siblings`` are the spec's other resources,
    consumed by the cross-resource wiring derivations.
    """
    start = len(diagnostics)
    bindings, binding_diagnostics = bind_resource_inputs(
        resource, contract, siblings, namespace
    )
    diagnostics.extend(binding_diagnostics)

    for name in unmapped_parameters(resource, contract):
        diagnostics.append(
            ModuleDiagnostic(
                category=ModuleDiagnosticCategory.UNMAPPED,
                code=ModuleErrorCode.MODULE_PARAMETER_UNMAPPED,
                message=(
                    f"Provisioning parameter {name!r} on resource "
                    f"{resource.resource_id!r} is not consumed by module "
                    f"{contract.module_id.value!r}; refusing to silently drop it."
                ),
                resource_id=resource.resource_id,
                input_name=name,
            )
        )

    bound = {binding.input_name for binding in bindings}
    for required in contract.required_input_names:
        if required not in bound:
            diagnostics.append(
                ModuleDiagnostic(
                    category=ModuleDiagnosticCategory.INVALID_PARAMETER,
                    code=ModuleErrorCode.MODULE_INPUT_REQUIRED,
                    message=(
                        f"Required module input {required!r} is unbound for "
                        f"resource {resource.resource_id!r}."
                    ),
                    resource_id=resource.resource_id,
                    input_name=required,
                )
            )

    if len(diagnostics) > start:
        return None

    return ModuleInstance(
        instance_id=_instance_id(contract.module_id, resource.resource_id),
        resource_id=resource.resource_id,
        module_id=contract.module_id,
        source=contract.source,
        source_kind=contract.source_kind,
        source_digest=contract.source_digest,
        version=contract.version,
        bindings=tuple(
            sorted(bindings, key=lambda binding: binding.input_name)
        ),
    )


def select_modules(
    spec: InfraSpec,
    *,
    provider: str,
    registry: VerifiedModuleRegistry,
    proposal: dict = None,
    namespace: str = "",
) -> ModuleSelection:
    """Derive a deterministic, fully-provenanced ``ModuleSelection``.

    ``proposal`` is optional advisory input from a future IaC Generator
    agent. It is re-validated from scratch against the catalog; it cannot
    introduce a module, a source, a version or an undeclared input.

    ``namespace`` is a validated name suffix applied to provider-visible
    names. Resource identities and module instance IDs are unchanged.

    Never raises for content reasons: every rejection is reported through
    ``ModuleSelection.diagnostics``.
    """
    diagnostics = []
    instances = []

    for resource in spec.resources:
        module_id = module_for_kind(resource.kind)
        if module_id is None:
            diagnostics.append(
                _unavailable(resource, resource.kind.value)
            )
            continue

        # An advisory nomination is honoured only if it names a capability
        # the deterministic mapping already implies; disagreement is a
        # rejection, never a redirection.
        if proposal is not None:
            nominated = proposal.get(resource.resource_id)
            if nominated is not None and nominated != module_id:
                diagnostics.append(
                    ModuleDiagnostic(
                        category=ModuleDiagnosticCategory.STRUCTURAL,
                        code=ModuleErrorCode.MODULE_NOT_AVAILABLE,
                        message=(
                            f"Proposal nominated module {nominated.value!r} for "
                            f"resource {resource.resource_id!r}, but the "
                            f"capability mapping resolves it to "
                            f"{module_id.value!r}; refusing to redirect."
                        ),
                        resource_id=resource.resource_id,
                    )
                )
                continue

        try:
            contract = registry.get(module_id)
        except KeyError:
            diagnostics.append(
                _unavailable(resource, describe_capability(module_id))
            )
            continue

        # Provider compatibility is checked explicitly so it is reported as
        # its own cause rather than being conflated with verification.
        if contract.provider != provider:
            diagnostics.append(
                ModuleDiagnostic(
                    category=ModuleDiagnosticCategory.STRUCTURAL,
                    code=ModuleErrorCode.MODULE_PROVIDER_INCOMPATIBLE,
                    message=(
                        f"Module {module_id.value!r} targets provider "
                        f"{contract.provider!r}, but this design declares "
                        f"{provider!r}."
                    ),
                    resource_id=resource.resource_id,
                )
            )
            continue

        try:
            contract = registry.require_selectable(
                module_id, provider=provider
            )
        except LookupError as error:
            diagnostics.append(
                ModuleDiagnostic(
                    category=ModuleDiagnosticCategory.NOT_VERIFIED,
                    code=ModuleErrorCode.MODULE_NOT_VERIFIED,
                    message=str(error),
                    resource_id=resource.resource_id,
                )
            )
            continue

        instance = _bind_one(
            resource,
            contract,
            diagnostics,
            siblings=tuple(spec.resources),
            namespace=namespace,
        )
        if instance is not None:
            instances.append(instance)

    if diagnostics:
        # All or nothing: a partial selection must never reach Terraform.
        diagnostics = tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    item.resource_id or "", item.code.value, item.message
                ),
            )
        )
        return ModuleSelection(
            selection_id="",
            provider=provider,
            region_code=spec.region_code,
            catalog_version=registry.catalog_version,
            catalog_digest=registry.catalog_digest,
            source_spec=_provenance(spec),
            instances=(),
            diagnostics=diagnostics,
        )

    ordered = tuple(
        sorted(instances, key=lambda item: item.instance_id)
    )
    selection = ModuleSelection(
        selection_id="",
        provider=provider,
        region_code=spec.region_code,
        catalog_version=registry.catalog_version,
        catalog_digest=registry.catalog_digest,
        source_spec=_provenance(spec),
        instances=ordered,
        diagnostics=(),
    )
    return selection.model_copy(
        update={"selection_id": _selection_id(selection)}
    )


def validate_module_selection(
    selection: ModuleSelection, *, registry: VerifiedModuleRegistry
) -> list:
    """Validate a selection against the catalog allowlists.

    Same convention as the frozen 4A validator: returns error strings,
    empty means valid, raises nothing. This is the pure check a future
    renderer and guard will run immediately before emitting ``module``
    blocks, so the trust decision is testable without any Terraform.
    """
    errors = []

    if not (selection.selection_id or "").strip():
        errors.append("Selection has no selection_id.")
    if len(selection.catalog_digest or "") != 64:
        errors.append("Selection has no catalog_digest.")
    if selection.catalog_digest != registry.catalog_digest:
        errors.append(
            "Selection was produced against a different catalog trust state."
        )

    allowed_sources = registry.source_allowlist()
    allowed_versions = registry.version_allowlist()
    allowed_digests = registry.source_digest_allowlist()

    seen_instances = set()
    seen_resources = set()

    for instance in selection.instances:
        identity = instance.instance_id
        if identity in seen_instances:
            errors.append(f"Duplicate module instance identity {identity!r}.")
        seen_instances.add(identity)

        if instance.resource_id in seen_resources:
            errors.append(
                f"Resource {instance.resource_id!r} is realised by more than one "
                f"module instance."
            )
        seen_resources.add(instance.resource_id)

        if not identity.startswith(f"{instance.module_id.value}-"):
            errors.append(
                f"Module instance {identity!r} does not carry its capability "
                f"prefix."
            )

        if instance.source not in allowed_sources:
            errors.append(
                f"Module instance {identity!r} uses source {instance.source!r} "
                f"which is not an exact trusted registry entry."
            )
        if instance.version not in allowed_versions:
            errors.append(
                f"Module instance {identity!r} uses version {instance.version!r} "
                f"which is not an exact trusted registry entry."
            )
        if instance.source_digest not in allowed_digests:
            errors.append(
                f"Module instance {identity!r} source digest is not the pinned "
                f"catalog digest."
            )

        try:
            contract = registry.get(instance.module_id)
        except KeyError:
            errors.append(
                f"Module instance {identity!r} names a capability that is not "
                f"in the verified catalog."
            )
            continue

        declared = {definition.name: definition for definition in contract.inputs}
        bound = set()
        for binding in instance.bindings:
            if binding.input_name in bound:
                errors.append(
                    f"Module instance {identity!r} binds input "
                    f"{binding.input_name!r} more than once."
                )
            bound.add(binding.input_name)

            definition = declared.get(binding.input_name)
            if definition is None:
                errors.append(
                    f"Module instance {identity!r} binds undeclared input "
                    f"{binding.input_name!r}; smuggling is refused."
                )
                continue
            if binding.value.type is not definition.type:
                errors.append(
                    f"Module instance {identity!r} binds input "
                    f"{binding.input_name!r} as {binding.value.type.value!r} but "
                    f"the contract declares {definition.type.value!r}."
                )
            if definition.is_secret and binding.value.type is not ParameterType.SECRET_REF:
                errors.append(
                    f"Module instance {identity!r} input {binding.input_name!r} is "
                    f"declared secret and must be a reference."
                )

        for required in contract.required_input_names:
            if required not in bound:
                errors.append(
                    f"Module instance {identity!r} omits required module input "
                    f"{required!r}."
                )

    return errors