"""Slice 4A tests: Infrastructure Specification + Resource Registry.

Covers the deterministic ArchitectureResult -> InfraSpec mapping, the
bounded resource registry, and the validate_spec boundary.

No LLM, no network, no Terraform binary, no Terraform dependency.

Run: poetry run python -m unittest discover tests
"""
import pathlib
import sys
import unittest
import uuid
from datetime import datetime

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
    EdgeKind,
)
from ai_architect.domain.provisioning.models import (
    DiagnosticCategory,
    InfraSpec,
    ParameterSource,
    PendingInput,
    ProvisioningErrorCode,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
    SupportingResource,
)
from ai_architect.services.provisioning.registry import (
    RESOURCE_CONTRACTS,
    UnsupportedResourceError,
    match_contract,
)
from ai_architect.services.provisioning.spec_builder import (
    ProvisioningError,
    apply_supplied_inputs,
    architecture_hash,
    build_infra_spec,
    diagnose_spec,
    validate_spec,
)

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"


def _inputs_ref(region=REGION) -> ArchitectureInputsRef:
    return ArchitectureInputsRef(
        requirements_session_id="req-1",
        capacity_formula_version="capacity/v1",
        selection_generated_at=NOW,
        cost_generated_at=NOW,
        cost_pricing_as_of=NOW,
        region_code=region,
        upstream_inputs_hash="upstream-hash-abc",
    )


def _node(node_id, name, category="compute") -> ArchitectureNode:
    return ArchitectureNode(
        node_id=node_id,
        component_name=name,
        category=category,
        role_in_system=f"role for {name}",
    )


def _architecture(
    nodes=None,
    edges=None,
    entry_points=None,
    data_stores=None,
    region=REGION,
) -> ArchitectureResult:
    nodes = nodes if nodes is not None else [
        _node("api", "Amazon API Gateway", "networking"),
        _node("fn", "AWS Lambda"),
        _node("store", "Amazon DynamoDB", "database"),
    ]
    edges = edges or [
        ArchitectureEdge(
            edge_id="api->fn",
            from_node="api",
            to_node="fn",
            kind=EdgeKind.REQUEST,
            protocol="https",
            description="invoke",
        )
    ]
    return ArchitectureResult(
        artifact_id=str(uuid.uuid4()),
        session_id="design-1",
        version=1,
        nodes=nodes,
        edges=edges,
        entry_points=entry_points if entry_points is not None else ["api"],
        data_store_node_ids=data_stores if data_stores is not None else ["store"],
        decisions=[
            ArchitectureDecision(decision="d", rationale="r"),
        ],
        inputs_ref=_inputs_ref(region),
        generated_at=NOW,
    )


def _supported_only() -> ArchitectureResult:
    return _architecture(
        nodes=[
            _node("bucket", "Amazon S3", "storage"),
            _node("fn", "AWS Lambda"),
            _node("table", "Amazon DynamoDB (On-Demand)", "database"),
        ],
        entry_points=["fn"],
        data_stores=["bucket", "table"],
    )



class SupportedResourceTests(unittest.TestCase):
    """1. Supported-only architecture derives a well-formed InfraSpec."""

    def test_supported_only_architecture_builds_a_structurally_valid_spec(self):
        """Build succeeds and there are zero STRUCTURAL diagnostics.

        The only findings are unresolved required inputs, which is the
        designed behaviour: the artifact carries real names, identities
        and provenance, and states what a human must still supply rather
        than inventing it.
        """
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual(len(spec.resources), 3)

        diagnostics = diagnose_spec(spec)
        structural = [
            d for d in diagnostics
            if d.category is DiagnosticCategory.STRUCTURAL
        ]
        self.assertEqual(
            [str(d) for d in structural], [], msg="unexpected structural errors"
        )

        # Registry membership alone must not imply provisionable.
        missing = [
            d for d in diagnostics
            if d.category is DiagnosticCategory.MISSING_INPUT
        ]
        self.assertTrue(missing, "expected unresolved-input diagnostics")

    def test_registry_contains_exactly_ten_kinds(self):
        self.assertEqual(
            set(RESOURCE_CONTRACTS),
            {
                ResourceKind.S3,
                ResourceKind.LAMBDA,
                ResourceKind.DYNAMODB,
                ResourceKind.APIGATEWAY_HTTP,
                ResourceKind.CLOUDFRONT_DISTRIBUTION,
                ResourceKind.COGNITO_USER_POOL,
                ResourceKind.SQS_QUEUE,
                ResourceKind.SNS_TOPIC,
                ResourceKind.CLOUDWATCH_LOG_GROUP,
                ResourceKind.CLOUDWATCH_ALARM,
            },
        )

    def test_vendor_prefix_and_parenthetical_normalization(self):
        for name in (
            "Amazon S3",
            "amazon simple storage service",
            "Amazon S3 (Standard + Glacier Deep Archive)",
            "AWS Lambda",
            "Amazon DynamoDB (On‑Demand)",
            "amazon dynamodb",
            "Amazon CloudWatch",
            "Amazon CloudWatch Logs",
            "CloudWatch Logs",
            "Amazon CloudWatch Alarm",
            "Amazon CloudWatch Alarms",
            "CloudWatch Alarms",
            "Amazon CloudWatch Alarms (for API Gateway and Lambda metrics)",
            "Amazon API Gateway (HTTP API)",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(match_contract(name))

    def test_capability_qualifiers_fail_closed(self):
        """A qualifier naming a different capability is never mapped.

        Parenthetical aliases are stripped during normalization, so
        these must be rejected from the raw name before the contract
        match; silently provisioning an object bucket or an HTTP API
        would be the wrong resource.
        """
        for name in (
            "Amazon S3 static website hosting",
            "S3 website hosting",
            "Amazon S3 (Static Website)",
            "Amazon API Gateway REST API",
            "API Gateway (REST)",
            "Amazon API Gateway WebSocket API",
            "API Gateway (WebSocket)",
            "Amazon CloudWatch Metric Alarm (Lambda error rate)",
            "Amazon CloudWatch Composite Alarms",
            "Amazon CloudWatch Alarms (error rate)",
            "Amazon CloudWatch Alarms (throttles)",
            "Amazon CloudWatch Alarms (anomaly detection)",
            "Amazon CloudWatch Alarms (metric math)",
        ):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedResourceError):
                    match_contract(name)

    def test_generic_api_gateway_still_maps_to_http_api(self):
        self.assertEqual(
            match_contract("Amazon API Gateway").kind,
            ResourceKind.APIGATEWAY_HTTP,
        )

    def test_iam_is_only_a_supporting_resource(self):
        self.assertNotIn("iam", {k.value for k in RESOURCE_CONTRACTS})
        self.assertEqual(
            RESOURCE_CONTRACTS[ResourceKind.LAMBDA].supporting[0][0], "iam_role"
        )

    def test_entry_point_and_data_store_flags_come_from_architecture(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        by_id = {r.node_id: r for r in spec.resources}
        self.assertTrue(by_id["fn"].is_entry_point)
        self.assertTrue(by_id["bucket"].is_data_store)
        self.assertFalse(by_id["fn"].is_data_store)


class BareIamExclusionTests(unittest.TestCase):
    """The bare IAM capability is excluded loudly, never provisioned."""

    def _golden_plus_iam(self):
        return _architecture(
            nodes=[
                _node("api", "Amazon API Gateway (HTTP API)", "networking"),
                _node("fn", "AWS Lambda"),
                _node("table", "Amazon DynamoDB (On-Demand)", "database"),
                _node("logs", "Amazon CloudWatch Logs", "monitoring"),
                _node(
                    "role",
                    "AWS IAM (service role for Lambda and API Gateway)",
                    "security",
                ),
            ],
            entry_points=["api"],
            data_stores=["table"],
        )

    def test_bare_iam_node_is_excluded_with_a_warning(self):
        spec = build_infra_spec(self._golden_plus_iam(), provider=PROVIDER)
        kinds = {resource.kind for resource in spec.resources}
        self.assertEqual(
            kinds,
            {
                ResourceKind.APIGATEWAY_HTTP,
                ResourceKind.LAMBDA,
                ResourceKind.DYNAMODB,
                ResourceKind.CLOUDWATCH_LOG_GROUP,
            },
        )
        self.assertEqual(len(spec.warnings), 1)
        self.assertIn("role", spec.warnings[0])
        self.assertIn("never", spec.warnings[0])

    def test_specific_iam_names_still_fail_closed(self):
        for name in ("AWS IAM User", "AWS IAM Policy", "IAM role for CI"):
            with self.subTest(name=name):
                architecture = _architecture(nodes=[_node("x", name)])
                with self.assertRaises(UnsupportedResourceError):
                    build_infra_spec(architecture, provider=PROVIDER)

    def test_iam_only_architecture_yields_an_empty_warned_spec(self):
        architecture = _architecture(nodes=[_node("role", "AWS IAM")])
        spec = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(spec.resources, [])
        self.assertTrue(
            any("role" in warning and "never" in warning
                for warning in spec.warnings),
            msg=f"expected an IAM exclusion warning, got {spec.warnings}",
        )


class UnsupportedResourceTests(unittest.TestCase):
    """2/3. Unsupported components fail closed, never dropped."""

    def test_unsupported_component_raises(self):
        architecture = _architecture(
            nodes=[_node("db", "Amazon RDS", "database")]
        )
        with self.assertRaises(UnsupportedResourceError) as ctx:
            build_infra_spec(architecture, provider=PROVIDER)
        self.assertIn("RDS", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "unsupported_resource")

    def test_mixed_architecture_fails_closed(self):
        architecture = _architecture(
            nodes=[
                _node("bucket", "Amazon S3", "storage"),
                _node("db", "Amazon RDS", "database"),
            ]
        )
        with self.assertRaises(UnsupportedResourceError):
            build_infra_spec(architecture, provider=PROVIDER)

    def test_category_is_never_a_fallback(self):
        """A 'storage' category must not imply S3."""
        architecture = _architecture(
            nodes=[
                ArchitectureNode(
                    node_id="weird",
                    component_name="Totally Unknown Thing",
                    category="storage",
                    role_in_system="r",
                )
            ]
        )
        with self.assertRaises(UnsupportedResourceError):
            build_infra_spec(architecture, provider=PROVIDER)

    def test_component_names_are_not_rewritten(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        names = {r.node_id: r.component_name for r in spec.resources}
        self.assertEqual(names["table"], "Amazon DynamoDB (On-Demand)")

    def test_unsupported_error_lists_supported_components(self):
        with self.assertRaises(UnsupportedResourceError) as ctx:
            match_contract("Amazon SageMaker Real-Time Inference")
        self.assertIn("amazon s3", str(ctx.exception))


class DeterminismTests(unittest.TestCase):
    """4/5. Same architecture -> same spec; stable identity from node_id."""

    def test_identical_architecture_yields_identical_spec(self):
        architecture = _supported_only()
        first = build_infra_spec(architecture, provider=PROVIDER)
        second = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))
        self.assertEqual(first.spec_id, second.spec_id)

    def test_spec_id_is_a_stable_content_hash(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual(len(spec.spec_id), 64)
        self.assertTrue(
            all(character in "0123456789abcdef" for character in spec.spec_id)
        )

    def test_resource_identity_derives_from_node_id(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual(
            [r.resource_id for r in spec.resources],
            ["s3-bucket", "lambda-fn", "dynamodb-table"],
        )

    def test_resource_identity_tracks_node_id(self):
        architecture = _architecture(
            nodes=[_node("my-bucket", "Amazon S3", "storage")]
        )
        spec = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(spec.resources[0].resource_id, "s3-my-bucket")
        self.assertEqual(spec.resources[0].node_id, "my-bucket")

    def test_no_timestamps_or_random_ids_in_spec(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        rendered = spec.model_dump(mode="json")
        self.assertNotIn("generated_at", rendered)
        self.assertNotIn("created_at", rendered)

    def test_source_architecture_identity_is_preserved(self):
        architecture = _supported_only()
        spec = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(
            spec.source_architecture.artifact_id, architecture.artifact_id
        )
        self.assertEqual(
            spec.source_architecture.upstream_inputs_hash,
            architecture.inputs_ref.upstream_inputs_hash,
        )
        self.assertEqual(
            spec.source_architecture.architecture_hash,
            architecture_hash(architecture),
        )

    def test_resource_order_is_architecture_order(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual([r.node_id for r in spec.resources],
                         ["bucket", "fn", "table"])


class MissingInputTests(unittest.TestCase):
    """6. Required inputs are represented, never defaulted."""

    def test_all_required_inputs_are_pending_not_defaulted(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        lambda_resource = next(r for r in spec.resources if r.kind is ResourceKind.LAMBDA)
        self.assertEqual(
            {p.name for p in lambda_resource.pending_inputs},
            set(RESOURCE_CONTRACTS[ResourceKind.LAMBDA].required_inputs),
        )
        self.assertEqual(lambda_resource.parameters, [])

    def test_validate_spec_reports_missing_inputs(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        errors = validate_spec(spec)
        self.assertTrue(errors)
        joined = " ".join(errors)
        for name in RESOURCE_CONTRACTS[ResourceKind.LAMBDA].required_inputs:
            self.assertIn(name, joined)

    def test_no_parameter_is_invented_and_every_requirement_is_pending(self):
        """Nothing is defaulted: no parameter is derived, none supplied.

        This is the direct guard on the no-invention rule. It asserts
        positively rather than looping over an empty collection.
        """
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual(len(spec.resources), 3)

        total_required = 0
        for resource in spec.resources:
            contract = RESOURCE_CONTRACTS[resource.kind]
            self.assertEqual(
                resource.parameters, [],
                msg=f"{resource.resource_id} invented parameters",
            )
            self.assertEqual(
                {p.name for p in resource.pending_inputs},
                set(contract.required_inputs),
                msg=f"{resource.resource_id} did not declare all requirements",
            )
            for pending in resource.pending_inputs:
                self.assertTrue(
                    pending.reason.strip(),
                    msg=f"{resource.resource_id}:{pending.name} has no reason",
                )
            total_required += len(contract.required_inputs)

        self.assertEqual(total_required, 14)
        self.assertEqual(len(spec.unresolved_inputs()), total_required)

    def test_specific_provisioning_parameters_are_never_defaulted(self):
        """The named values the contract forbids inventing."""
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        rendered = str(spec.model_dump(mode="json"))
        for invented in (
            "python3",
            "nodejs",
            "index.handler",
            "PAY_PER_REQUEST",
            "PROVISIONED",
            "HASH",
        ):
            self.assertNotIn(invented, rendered)

    def test_missing_inputs_are_categorised_not_conflated(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        diagnostics = diagnose_spec(spec)
        for diagnostic in diagnostics:
            if diagnostic.category is DiagnosticCategory.MISSING_INPUT:
                self.assertEqual(
                    diagnostic.code, ProvisioningErrorCode.MISSING_INPUT
                )
                self.assertIsNotNone(diagnostic.resource_id)
                self.assertIsNotNone(diagnostic.input_name)

    def test_validate_spec_output_matches_diagnose_spec_messages(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertEqual(
            validate_spec(spec), [str(d) for d in diagnose_spec(spec)]
        )

    def test_diagnostics_are_deterministic(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        first = [d.model_dump(mode="json") for d in diagnose_spec(spec)]
        second = [d.model_dump(mode="json") for d in diagnose_spec(spec)]
        self.assertEqual(first, second)

    def test_structural_and_missing_input_are_separable(self):
        """Supplying every input clears MISSING_INPUT, leaves STRUCTURAL."""
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        resources = []
        for resource in spec.resources:
            required = RESOURCE_CONTRACTS[resource.kind].required_inputs
            resources.append(
                {
                    **resource.model_dump(mode="json"),
                    "parameters": [
                        {
                            "name": name,
                            "value": "supplied",
                            "source": ParameterSource.REQUIRED_INPUT.value,
                        }
                        for name in required
                    ],
                    "pending_inputs": [],
                }
            )
        supplied = InfraSpec.model_validate(
            {**spec.model_dump(mode="json"), "resources": resources}
        )
        diagnostics = diagnose_spec(supplied)
        self.assertEqual(
            [str(d) for d in diagnostics if d.category is DiagnosticCategory.MISSING_INPUT],
            [],
        )
        self.assertEqual(
            [str(d) for d in diagnostics if d.category is DiagnosticCategory.STRUCTURAL],
            [],
        )

    def test_structural_defect_is_categorised_structural(self):
        """Provider mismatch is a structural defect, not a missing input.

        The spec also still lists its outstanding inputs; the point is
        that the mismatch itself is never filed as a human decision.
        """
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        broken = spec.model_copy(update={"provider": "gcp"})
        diagnostics = diagnose_spec(broken)

        mismatches = [
            d for d in diagnostics
            if d.code is ProvisioningErrorCode.PROVIDER_MISMATCH
        ]
        self.assertTrue(mismatches)
        for diagnostic in mismatches:
            self.assertEqual(
                diagnostic.category, DiagnosticCategory.STRUCTURAL
            )

    def test_undeclared_requirement_is_structural_not_missing_input(self):
        """A requirement neither supplied nor declared is inconsistent."""
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        stripped = spec.model_copy(
            update={
                "resources": [
                    r.model_copy(update={"pending_inputs": []})
                    for r in spec.resources
                ]
            }
        )
        diagnostics = diagnose_spec(stripped)
        structural = [
            d for d in diagnostics
            if "neither supplies nor declares" in d.message
        ]
        self.assertTrue(structural)
        for diagnostic in structural:
            self.assertEqual(
                diagnostic.category, DiagnosticCategory.STRUCTURAL
            )

    def test_supplying_inputs_clears_errors(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        resources = []
        for resource in spec.resources:
            required = RESOURCE_CONTRACTS[resource.kind].required_inputs
            resources.append(
                {
                    **resource.model_dump(mode="json"),
                    "parameters": [
                        {
                            "name": name,
                            "value": "supplied",
                            "source": ParameterSource.REQUIRED_INPUT.value,
                        }
                        for name in required
                    ],
                    "pending_inputs": [],
                }
            )
        supplied = InfraSpec.model_validate(
            {**spec.model_dump(mode="json"), "resources": resources}
        )
        self.assertEqual(validate_spec(supplied), [])
        self.assertEqual(diagnose_spec(supplied), [])

    def test_supporting_resource_is_recorded_for_lambda(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        fn = next(r for r in spec.resources if r.kind is ResourceKind.LAMBDA)
        self.assertEqual([s.kind for s in fn.supporting], ["iam_role"])
        self.assertEqual(fn.supporting[0].reference, "lambda_execution_role")


class SuppliedInputTests(unittest.TestCase):
    """A supplied requirement is resolved, not merely recorded."""

    def test_supplied_input_clears_pending_and_reidentifies(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        bucket = next(
            r for r in spec.resources if r.kind is ResourceKind.S3
        )
        updated = apply_supplied_inputs(
            spec, {bucket.resource_id: {"bucket_name": "my-design-bucket"}}
        )
        resource = next(
            r for r in updated.resources if r.kind is ResourceKind.S3
        )
        self.assertEqual(
            {p.name for p in resource.parameters}, {"bucket_name"}
        )
        self.assertNotIn(
            "bucket_name", {p.name for p in resource.pending_inputs}
        )
        self.assertEqual(
            {p.name for p in resource.pending_inputs},
            set(RESOURCE_CONTRACTS[ResourceKind.S3].required_inputs)
            - {"bucket_name"},
        )
        self.assertNotEqual(updated.spec_id, spec.spec_id)
        self.assertEqual(
            len(updated.unresolved_inputs()),
            len(spec.unresolved_inputs()) - 1,
        )

    def test_unknown_resource_id_fails_loudly(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        with self.assertRaises(ProvisioningError):
            apply_supplied_inputs(spec, {"no-such": {"x": "y"}})


class ProviderRegionTests(unittest.TestCase):
    """7/8. Provider mismatch and region failures."""

    def test_unsupported_provider_is_refused(self):
        for provider in ("gcp", "azure", "", "unknown"):
            with self.subTest(provider=provider):
                with self.assertRaises(ProvisioningError) as ctx:
                    build_infra_spec(_supported_only(), provider=provider)
                self.assertEqual(ctx.exception.code, "provider_mismatch")

    def test_provider_mismatch_in_validate_spec(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        mismatched = spec.model_copy(update={"provider": "gcp"})
        errors = validate_spec(mismatched)
        self.assertTrue(any("not supported" in e for e in errors))

    def test_missing_region_refused(self):
        architecture = _architecture(region="")
        with self.assertRaises(ProvisioningError) as ctx:
            build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(ctx.exception.code, "missing_region")

    def test_invalid_region_refused(self):
        architecture = _architecture(region="Atlantis")
        with self.assertRaises(ProvisioningError) as ctx:
            build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(ctx.exception.code, "invalid_region")

    def test_region_is_canonicalized_through_shared_normalizer(self):
        architecture = _supported_only()
        architecture = architecture.model_copy(
            update={
                "inputs_ref": _inputs_ref(region="US East (N. Virginia)"),
            }
        )
        spec = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(spec.region_code, "us-east-1")

    def test_validate_spec_flags_non_canonical_region(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        bad = spec.model_copy(update={"region_code": "US East (N. Virginia)"})
        self.assertTrue(
            any("canonical" in e for e in validate_spec(bad))
        )


class IdentityValidationTests(unittest.TestCase):
    """9. Duplicate and malformed identities."""

    def _spec(self):
        return build_infra_spec(_supported_only(), provider=PROVIDER)

    def test_duplicate_resource_identity_detected(self):
        spec = self._spec()
        first = spec.resources[0]
        duplicate = first.model_copy(update={"node_id": "other"})
        broken = spec.model_copy(update={"resources": [first, duplicate]})
        errors = validate_spec(broken)
        self.assertTrue(any("Duplicate resource identity" in e for e in errors))

    def test_empty_resource_identity_detected(self):
        spec = self._spec()
        broken = spec.model_copy(
            update={
                "resources": [
                    spec.resources[0].model_copy(update={"resource_id": ""})
                ]
            }
        )
        errors = validate_spec(broken)
        self.assertTrue(any("empty resource_id" in e for e in errors))

    def test_identity_kind_mismatch_detected(self):
        spec = self._spec()
        broken = spec.model_copy(
            update={
                "resources": [
                    spec.resources[0].model_copy(update={"resource_id": "lambda-bucket"})
                ]
            }
        )
        errors = validate_spec(broken)
        self.assertTrue(any("kind prefix" in e for e in errors))

    def test_empty_component_name_detected(self):
        spec = self._spec()
        broken = spec.model_copy(
            update={
                "resources": [
                    spec.resources[0].model_copy(update={"component_name": ""})
                ]
            }
        )
        self.assertTrue(any("empty component_name" in e for e in validate_spec(broken)))

    def test_empty_supporting_reference_detected(self):
        spec = self._spec()
        fn = next(r for r in spec.resources if r.kind is ResourceKind.LAMBDA)
        broken_fn = fn.model_copy(
            update={
                "supporting": [SupportingResource(kind="iam_role", reference="", reason="r")]
            }
        )
        broken = spec.model_copy(
            update={"resources": [broken_fn]}
        )
        self.assertTrue(any("empty reference" in e for e in validate_spec(broken)))

    def test_builder_refuses_empty_node_id(self):
        architecture = _architecture(
            nodes=[ArchitectureNode(
                node_id="  ",
                component_name="Amazon S3",
                category="storage",
                role_in_system="r",
            )]
        )
        with self.assertRaises(UnsupportedResourceError) as ctx:
            build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(ctx.exception.code, "invalid_resource_identity")


class SecretTests(unittest.TestCase):
    """13. Credentials must never enter a spec."""

    def _with_parameter(self, name, value):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        resource = spec.resources[0].model_copy(
            update={
                "parameters": [
                    ResolvedParameter(
                        name=name, value=value, source=ParameterSource.REQUIRED_INPUT
                    )
                ],
                "pending_inputs": [],
            }
        )
        return spec.model_copy(update={"resources": [resource]})

    def test_credential_shaped_parameter_name_rejected(self):
        for name in ("aws_secret_access_key", "password", "api_token", "private_key"):
            with self.subTest(name=name):
                errors = validate_spec(self._with_parameter(name, "x"))
                self.assertTrue(
                    any("credential" in e for e in errors),
                    msg=f"{name} was not rejected: {errors}",
                )

    def test_credential_shaped_parameter_value_rejected(self):
        for value in (
            "AKIAIOSFODNN7EXAMPLE",
            "-----BEGIN RSA PRIVATE KEY-----",
            "aws_secret_access_key=abc123",
        ):
            with self.subTest(value=value):
                errors = validate_spec(self._with_parameter("note", value))
                self.assertTrue(any("credential" in e for e in errors))

    def test_benign_parameter_allowed(self):
        errors = validate_spec(self._with_parameter("bucket_name", "my-bucket"))
        self.assertFalse([e for e in errors if "credential" in e])


class SliceBoundaryTests(unittest.TestCase):
    """10/11/12. Slice 4A boundaries: no LLM, no Terraform, no mutation."""

    def test_no_llm_invocation_during_spec_construction(self):
        from unittest.mock import patch

        with patch(
            "ai_architect.infrastructure.llm.factory.create_llm",
            side_effect=AssertionError("LLM must not be constructed"),
        ):
            spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertTrue(spec.resources)

    def test_provisioning_modules_do_not_reference_llm(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ai_architect"
        targets = [
            root / "domain" / "provisioning" / "models.py",
            root / "services" / "provisioning" / "registry.py",
            root / "services" / "provisioning" / "spec_builder.py",
        ]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "langchain",
                "create_llm",
                "infrastructure.llm",
                "aws_mcp",
                "retrieval",
            ):
                self.assertNotIn(
                    forbidden, text, f"{path.name} references {forbidden}"
                )

    def test_no_terraform_imported_or_required(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertNotIn("terraform", sys.modules)
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8").lower()
        self.assertNotIn("terraform", pyproject)
        self.assertNotIn("hcl", pyproject)

    def test_existing_architecture_artifact_is_unchanged(self):
        architecture = _supported_only()
        before = architecture.model_dump(mode="json")
        build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(architecture.model_dump(mode="json"), before)

    def test_spec_is_typed_not_a_dict(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        self.assertIsInstance(spec, InfraSpec)
        for resource in spec.resources:
            self.assertIsInstance(resource, ResourceInstance)
            self.assertIsInstance(resource.kind, ResourceKind)
        self.assertIsInstance(spec.source_architecture, SourceArchitectureRef)

    def test_empty_architecture_warns_but_does_not_fail(self):
        architecture = _architecture(nodes=[], edges=[], entry_points=[], data_stores=[])
        spec = build_infra_spec(architecture, provider=PROVIDER)
        self.assertEqual(spec.resources, [])
        self.assertTrue(any("no nodes" in w for w in spec.warnings))
        self.assertEqual(validate_spec(spec), [])

    def test_pending_input_typed(self):
        spec = build_infra_spec(_supported_only(), provider=PROVIDER)
        pending = spec.unresolved_inputs()
        self.assertTrue(pending)
        for resource in spec.resources:
            for item in resource.pending_inputs:
                self.assertIsInstance(item, PendingInput)


if __name__ == "__main__":
    unittest.main()
