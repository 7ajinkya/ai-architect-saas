"""Slice 4C tests: deterministic ModuleSelection, provenance and failure modes.

Exercises the 4A -> 4C seam: real ``build_infra_spec`` output fed into
``select_modules``, plus determinism, trust, and the explicit failure paths.

Run: poetry run python -m unittest tests.test_provisioning_module_selection
"""
import pathlib
import unittest
import uuid
from datetime import datetime

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ParameterSource,
    ResolvedParameter,
    ResourceKind,
)
from ai_architect.domain.provisioning.module import (
    BindingOrigin,
    ModuleDiagnosticCategory,
    ModuleErrorCode,
    ModuleId,
    ModuleInstance,
    ParameterType,
    TypedBool,
    TypedEnum,
    TypedString,
)
from ai_architect.services.provisioning.module_catalog import CATALOG_VERSION
from ai_architect.services.provisioning.module_registry import (
    VerifiedModuleRegistry,
    build_default_registry,
)
from ai_architect.services.provisioning.module_selection import (
    NO_VERIFIED_MODULE_MESSAGE,
    select_modules,
    validate_module_selection,
)
from ai_architect.services.provisioning.spec_builder import build_infra_spec

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"

S3_VALUES = {
    "bucket_name": "my-design-bucket",
    "versioning": "enabled",
    "encryption": "aes256",
    "public_access_block": "all_blocked",
}
DDB_VALUES = {
    "billing_mode": "PAY_PER_REQUEST",
    "attribute_definitions": "pk:S,sk:N",
    "key_schema": "HASH:pk,RANGE:sk",
    "point_in_time_recovery": "true",
}
LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "index.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
}


def _architecture(nodes):
    return ArchitectureResult(
        artifact_id=str(uuid.uuid4()),
        session_id="design-1",
        version=1,
        nodes=[
            ArchitectureNode(
                node_id=node_id, component_name=name,
                category="storage", role_in_system=f"role {name}",
            )
            for node_id, name in nodes
        ],
        edges=[],
        entry_points=[],
        data_store_node_ids=[node_id for node_id, _ in nodes],
        decisions=[ArchitectureDecision(decision="d", rationale="r")],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id="req-1",
            capacity_formula_version="capacity/v1",
            selection_generated_at=NOW,
            cost_generated_at=NOW,
            cost_pricing_as_of=NOW,
            region_code=REGION,
            upstream_inputs_hash="upstream-hash",
        ),
        generated_at=NOW,
    )


def _supply(spec, overrides=None):
    """Supply every pending input, mimicking a human completing 4A."""
    overrides = overrides or {}
    defaults = {
        ResourceKind.S3: S3_VALUES,
        ResourceKind.DYNAMODB: DDB_VALUES,
        ResourceKind.LAMBDA: LAMBDA_VALUES,
        ResourceKind.APIGATEWAY_HTTP: {},
        ResourceKind.CLOUDWATCH_LOG_GROUP: {"retention_days": "30"},
    }
    resources = []
    for resource in spec.resources:
        values = dict(defaults[resource.kind])
        values.update(overrides.get(resource.resource_id, {}))
        resources.append({
            **resource.model_dump(mode="json"),
            "parameters": [
                {"name": n, "value": v, "source": "required_input"}
                for n, v in values.items()
            ],
            "pending_inputs": [],
        })
    return InfraSpec.model_validate(
        {**spec.model_dump(mode="json"), "resources": resources}
    )


def _spec(nodes, overrides=None):
    built = build_infra_spec(_architecture(nodes), provider=PROVIDER)
    return _supply(built, overrides)


def _selection(
    nodes, overrides=None, registry=None, proposal=None, namespace=""
):
    registry = registry or build_default_registry()
    spec = _spec(nodes, overrides)
    selection = select_modules(
        spec,
        provider=PROVIDER,
        registry=registry,
        proposal=proposal,
        namespace=namespace,
    )
    return selection, spec, registry


class DeterminismTests(unittest.TestCase):
    """1-4. Identical output, stable ordering, content-derived identity."""

    def test_same_spec_is_byte_identical(self):
        spec = _spec([("bucket", "Amazon S3"), ("table", "Amazon DynamoDB")])
        registry = build_default_registry()
        first = select_modules(spec, provider=PROVIDER, registry=registry)
        second = select_modules(spec, provider=PROVIDER, registry=registry)
        self.assertEqual(
            first.model_dump(mode="json"), second.model_dump(mode="json")
        )
        self.assertEqual(first.selection_id, second.selection_id)

    def test_resource_order_does_not_affect_output(self):
        """Same artifact, resources listed in the other order."""
        registry = build_default_registry()
        spec = _spec([("bucket", "Amazon S3"), ("table", "Amazon DynamoDB")])
        forward = select_modules(spec, provider=PROVIDER, registry=registry)
        reversed_spec = spec.model_copy(
            update={"resources": tuple(reversed(spec.resources))}
        )
        reverse = select_modules(
            reversed_spec, provider=PROVIDER, registry=registry
        )
        self.assertTrue(forward.selected and reverse.selected)
        self.assertEqual(
            forward.model_dump(mode="json"), reverse.model_dump(mode="json")
        )

    def test_instances_are_sorted_by_instance_id(self):
        selection, _, _ = _selection(
            [("table", "Amazon DynamoDB"), ("bucket", "Amazon S3")]
        )
        identifiers = [i.instance_id for i in selection.instances]
        self.assertEqual(identifiers, sorted(identifiers))

    def test_instance_id_derives_from_capability_and_resource(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        self.assertEqual(
            selection.instances[0].instance_id,
            "object_storage-s3-bucket",
        )

    def test_selection_id_is_a_content_hash(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        self.assertEqual(len(selection.selection_id), 64)

    def test_no_timestamps_uuids_or_paths_in_selection(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3"), ("table", "Amazon DynamoDB")]
        )
        rendered = str(selection.model_dump(mode="json"))
        self.assertNotIn(str(uuid.uuid4()), rendered)
        self.assertNotIn("2026-01-01", rendered)
        self.assertNotIn("C:\\", rendered)
        self.assertNotIn("/home/", rendered)

    def test_region_and_provider_come_from_spec(self):
        selection, spec, _ = _selection([("bucket", "Amazon S3")])
        self.assertEqual(selection.region_code, spec.region_code)
        self.assertEqual(selection.provider, PROVIDER)

    def test_catalog_trust_state_is_bound_into_selection(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        self.assertEqual(selection.catalog_version, CATALOG_VERSION)
        self.assertEqual(selection.catalog_digest, registry.catalog_digest)

    def test_infra_spec_is_not_mutated(self):
        spec = _spec([("bucket", "Amazon S3")])
        before = spec.model_dump(mode="json")
        select_modules(
            spec, provider=PROVIDER, registry=build_default_registry()
        )
        self.assertEqual(spec.model_dump(mode="json"), before)


class ProvenanceTests(unittest.TestCase):
    """13. Full chain back to the approved architecture."""

    def test_provenance_preserves_architecture_identity(self):
        selection, spec, _ = _selection([("bucket", "Amazon S3")])
        source = spec.source_architecture
        self.assertEqual(selection.source_spec.spec_id, spec.spec_id)
        self.assertEqual(selection.source_spec.artifact_id, source.artifact_id)
        self.assertEqual(
            selection.source_spec.architecture_hash, source.architecture_hash
        )
        self.assertEqual(
            selection.source_spec.upstream_inputs_hash,
            source.upstream_inputs_hash,
        )

    def test_instance_traces_back_to_resource_identity(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        instance = selection.instances[0]
        self.assertEqual(instance.resource_id, "s3-bucket")
        self.assertIsNotNone(selection.instance_for("s3-bucket"))
        self.assertIsNone(selection.instance_for("s3-absent"))

    def test_selection_schema_version_is_declared(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        self.assertEqual(selection.schema_version, "module-selection/v1")


class TrustPropagationTests(unittest.TestCase):
    """14. Source and version come only from the catalog."""

    def test_source_and_version_match_catalog_exactly(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        contract = registry.get(ModuleId.OBJECT_STORAGE)
        instance = selection.instances[0]
        self.assertEqual(instance.source, contract.source)
        self.assertEqual(instance.version, contract.version)
        self.assertEqual(instance.source_digest, contract.source_digest)

    def test_adversarial_proposal_cannot_change_source_or_version(self):
        """A proposal naming another capability is refused, not honoured."""
        selection, _, registry = _selection(
            [("bucket", "Amazon S3")],
            registry=build_default_registry(),
            proposal={"s3-bucket": ModuleId.NOSQL_TABLE},
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_NOT_AVAILABLE, codes)

    def test_matching_proposal_is_accepted_and_ignored(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3")],
            proposal={"s3-bucket": ModuleId.OBJECT_STORAGE},
        )
        self.assertTrue(selection.selected)

    def test_proposal_cannot_introduce_an_unmapped_module(self):
        """There is no way to name a module outside the capability mapping."""
        selection, _, _ = _selection(
            [("bucket", "Amazon S3")],
            proposal={"s3-bucket": ModuleId.SERVERLESS_FUNCTION},
        )
        self.assertFalse(selection.selected)

    def test_selection_validates_clean_against_registry(self):
        selection, _, registry = _selection(
            [("bucket", "Amazon S3"), ("table", "Amazon DynamoDB")]
        )
        self.assertEqual(
            validate_module_selection(selection, registry=registry), []
        )

    def test_tampered_source_is_detected(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        tampered = selection.model_copy(update={
            "instances": (
                selection.instances[0].model_copy(
                    update={"source": "../../infra/modules/evil"}
                ),
            ),
        })
        errors = validate_module_selection(tampered, registry=registry)
        self.assertTrue(any("exact trusted registry entry" in e for e in errors))

    def test_tampered_version_is_detected(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        tampered = selection.model_copy(update={
            "instances": (
                selection.instances[0].model_copy(update={"version": "9.9.9"}),
            ),
        })
        errors = validate_module_selection(tampered, registry=registry)
        self.assertTrue(any("exact trusted registry entry" in e for e in errors))

    def test_stale_catalog_digest_is_detected(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        tampered = selection.model_copy(update={"catalog_digest": "f" * 64})
        errors = validate_module_selection(tampered, registry=registry)
        self.assertTrue(any("different catalog trust state" in e for e in errors))

    def test_surplus_input_is_detected(self):
        """Smuggling an input the contract never declared is refused."""
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        instance = selection.instances[0]
        smuggled = instance.model_copy(update={
            "bindings": instance.bindings + (
                instance.bindings[0].model_copy(
                    update={"input_name": "acl"}
                ),
            ),
        })
        tampered = selection.model_copy(update={"instances": (smuggled,)})
        errors = validate_module_selection(tampered, registry=registry)
        self.assertTrue(any("smuggling is refused" in e for e in errors))

    def test_missing_required_input_is_detected(self):
        selection, _, registry = _selection([("bucket", "Amazon S3")])
        instance = selection.instances[0]
        stripped = instance.model_copy(
            update={"bindings": instance.bindings[:-1]}
        )
        tampered = selection.model_copy(update={"instances": (stripped,)})
        errors = validate_module_selection(tampered, registry=registry)
        self.assertTrue(any("omits required module input" in e for e in errors))


class FailureModeTests(unittest.TestCase):
    """15-17. Explicit failures, all-or-nothing."""

    def test_pending_module_fails_closed(self):
        from ai_architect.domain.provisioning.module import VerificationStatus
        from ai_architect.services.provisioning.module_catalog import (
            CATALOG,
            SERVERLESS_FUNCTION,
        )
        pending_fn = SERVERLESS_FUNCTION.model_copy(
            update={
                "verification": SERVERLESS_FUNCTION.verification.model_copy(
                    update={"status": VerificationStatus.PENDING}
                )
            }
        )
        registry = VerifiedModuleRegistry(
            tuple(
                pending_fn if c.module_id is ModuleId.SERVERLESS_FUNCTION else c
                for c in CATALOG
            ),
            catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        selection, _, _ = _selection(
            [("fn", "AWS Lambda")], registry=registry
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_NOT_VERIFIED, codes)
        categories = {d.category for d in selection.diagnostics}
        self.assertIn(ModuleDiagnosticCategory.NOT_VERIFIED, categories)

    def test_pending_failure_does_not_invent_a_code_artifact(self):
        selection, _, _ = _selection([("fn", "AWS Lambda")])
        rendered = str(selection.model_dump(mode="json"))
        for invented in ("s3_key", "image_uri", ".zip", "filename ="):
            self.assertNotIn(invented, rendered)

    def test_unavailable_capability_uses_mandated_wording(self):
        """The requirements fix this sentence."""
        self.assertEqual(
            NO_VERIFIED_MODULE_MESSAGE,
            "This design requires a component I don't have a verified module "
            "for. I can generate a draft for engineering review, but I won't "
            "apply it automatically.",
        )

    def test_unavailable_message_reaches_the_selection(self):
        from ai_architect.services.provisioning.module_catalog import CATALOG
        registry = VerifiedModuleRegistry(
            tuple(
                c for c in CATALOG
                if c.module_id is not ModuleId.OBJECT_STORAGE
            ),
            catalog_version=CATALOG_VERSION,
        )
        selection, _, _ = _selection([("bucket", "Amazon S3")], registry=registry)
        self.assertFalse(selection.selected)
        self.assertIn(
            NO_VERIFIED_MODULE_MESSAGE,
            " ".join(str(d) for d in selection.diagnostics),
        )

    def test_unresolved_input_fails_selection(self):
        """An empty pending set means the human has not decided yet."""
        built = build_infra_spec(
            _architecture([("bucket", "Amazon S3")]), provider=PROVIDER
        )
        selection = select_modules(
            built, provider=PROVIDER, registry=build_default_registry()
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_REQUIRED, codes)

    def test_all_or_nothing_one_bad_resource_suppresses_all(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3"), ("fn", "AWS Lambda")]
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        self.assertEqual(selection.selection_id, "")

    def test_provider_mismatch_is_reported(self):
        spec = _spec([("bucket", "Amazon S3")])
        selection = select_modules(
            spec, provider="gcp", registry=build_default_registry()
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_PROVIDER_INCOMPATIBLE, codes)

    def test_invalid_enum_value_fails_closed(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3")],
            overrides={"s3-bucket": {"versioning": "on"}},
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST, codes)

    def test_invalid_bucket_name_fails_closed(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3")],
            overrides={"s3-bucket": {"bucket_name": "Bad_Name!"}},
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_TYPE_INVALID, codes)

    def test_undeclared_key_attribute_fails_closed(self):
        selection, _, _ = _selection(
            [("table", "Amazon DynamoDB")],
            overrides={"dynamodb-table": {"key_schema": "HASH:ghost"}},
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_INCONSISTENT, codes)

    def test_diagnostics_are_deterministically_ordered(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3"), ("fn", "AWS Lambda")]
        )
        keys = [(d.resource_id or "", d.code.value) for d in selection.diagnostics]
        self.assertEqual(keys, sorted(keys))

    def test_no_diagnostic_leaks_a_secret_shaped_value(self):
        selection, _, _ = _selection(
            [("bucket", "Amazon S3")],
            overrides={"s3-bucket": {"bucket_name": "AKIAIOSFODNN7EXAMPLE"}},
        )
        self.assertFalse(selection.selected)


class TypedBindingTests(unittest.TestCase):
    """21. Values are typed by the contract, not by name."""

    def test_scalars_are_typed(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        values = {
            b.input_name: b.value
            for b in selection.instances[0].bindings
        }
        self.assertIsInstance(values["bucket_name"], TypedString)
        self.assertIsInstance(values["sse_algorithm"], TypedEnum)
        self.assertIsInstance(values["access_posture"], TypedEnum)

    def test_bool_parameters_are_typed(self):
        selection, _, _ = _selection([("table", "Amazon DynamoDB")])
        values = {
            b.input_name: b.value
            for b in selection.instances[0].bindings
        }
        self.assertIsInstance(
            values["point_in_time_recovery"], TypedBool
        )
        self.assertTrue(values["point_in_time_recovery"].value)

    def test_structured_4a_params_project_to_scalars(self):
        """The whole point: no caller expresses Terraform structure."""
        selection, _, _ = _selection([("table", "Amazon DynamoDB")])
        values = {
            b.input_name: b.value
            for b in selection.instances[0].bindings
        }
        self.assertEqual(values["hash_key_name"].value, "pk")
        self.assertEqual(values["range_key_name"].value, "sk")
        self.assertEqual(values["hash_key_type"].value, "S")
        self.assertEqual(values["range_key_type"].value, "N")
        self.assertNotIn("attribute_definitions", values)
        self.assertNotIn("key_schema", values)

    def test_derived_and_supplied_inputs_record_distinct_origins(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        origins = {
            b.input_name: b.origin for b in selection.instances[0].bindings
        }
        self.assertEqual(origins["bucket_name"], BindingOrigin.REQUIRED_INPUT)
        self.assertEqual(origins["bucket_name"], BindingOrigin.REQUIRED_INPUT)
        for name, origin in origins.items():
            self.assertIn(
                origin,
                {BindingOrigin.REQUIRED_INPUT, BindingOrigin.DERIVED},
                msg=name,
            )

    def test_resource_id_derived_input(self):
        selection, _, _ = _selection([("table", "Amazon DynamoDB")])
        table_name = next(
            b for b in selection.instances[0].bindings
            if b.input_name == "table_name"
        )
        self.assertEqual(table_name.value.value, "dynamodb-table")
        self.assertEqual(table_name.origin, BindingOrigin.DERIVED)
        self.assertIsNone(table_name.from_parameter)

    def test_optional_range_key_absent_is_simply_unbound(self):
        selection, _, _ = _selection(
            [("table", "Amazon DynamoDB")],
            overrides={"dynamodb-table": {"key_schema": "HASH:pk"}},
        )
        self.assertTrue(selection.selected)
        names = {b.input_name for b in selection.instances[0].bindings}
        self.assertIn("hash_key_name", names)
        self.assertNotIn("range_key_name", names)

    def test_bindings_are_sorted(self):
        selection, _, _ = _selection([("bucket", "Amazon S3")])
        names = [b.input_name for b in selection.instances[0].bindings]
        self.assertEqual(names, sorted(names))


class CrossResourceBindingTests(unittest.TestCase):
    """Sibling wiring derivations bind from the spec, never from text.

    Every value asserted here is a sibling resource identity (or the one
    named access-log policy), resolved to a Terraform reference only at
    invocation time. Zero or multiple siblings is a diagnostic.
    """

    LAMBDA_FULL = {
        "runtime": "python3.12",
        "handler": "index.handler",
        "memory_mb": "512",
        "timeout_seconds": "30",
        "code_artifact_path": "demo-handler.zip",
        "code_artifact_sha256": "ab" * 32,
    }

    def _stack(self, extra_nodes=()):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)"),
            ("fn", "AWS Lambda"),
            ("table", "Amazon DynamoDB"),
            ("logs", "Amazon CloudWatch Logs"),
        ]
        nodes.extend(extra_nodes)
        return _selection(
            nodes, overrides={"lambda-fn": dict(self.LAMBDA_FULL)}
        )

    @staticmethod
    def _bindings(selection, resource_id):
        instance = selection.instance_for(resource_id)
        return {b.input_name: b for b in instance.bindings}

    def test_descriptive_live_names_select_and_bind(self):
        selection, spec, _ = _selection(
            [
                ("api", "Amazon API Gateway HTTP API"),
                ("fn", "AWS Lambda Function (Python\u202f3.12)"),
                (
                    "table",
                    "Amazon DynamoDB Table (On\u2011Demand billing)",
                ),
                (
                    "logs",
                    "Amazon CloudWatch Log Group "
                    "(for Lambda & API Gateway)",
                ),
                ("role", "IAM Role for Lambda Execution"),
            ],
            overrides={"lambda-fn": dict(self.LAMBDA_FULL)},
        )
        self.assertTrue(selection.selected)
        self.assertEqual(
            {resource.kind for resource in spec.resources},
            {
                ResourceKind.APIGATEWAY_HTTP,
                ResourceKind.LAMBDA,
                ResourceKind.DYNAMODB,
                ResourceKind.CLOUDWATCH_LOG_GROUP,
            },
        )
        self.assertEqual(len(spec.resources), 4)
        self.assertTrue(
            any("role" in warning and "never" in warning
                for warning in spec.warnings),
            msg=spec.warnings,
        )

        fn = self._bindings(selection, "lambda-fn")
        self.assertEqual(
            fn["log_group_arn"].value.value,
            "cloudwatch_log_group-logs",
        )
        self.assertEqual(
            fn["table_arns"].value.value, ("dynamodb-table",)
        )

    def test_full_stack_binds_sibling_identities(self):
        selection, _, _ = self._stack()
        self.assertTrue(selection.selected)

        fn = self._bindings(selection, "lambda-fn")
        self.assertEqual(
            fn["log_group_arn"].value.value, "cloudwatch_log_group-logs"
        )
        self.assertEqual(fn["log_group_arn"].origin, BindingOrigin.DERIVED)
        self.assertIsNone(fn["log_group_arn"].from_parameter)
        self.assertEqual(fn["table_arns"].value.value, ("dynamodb-table",))
        self.assertEqual(fn["table_name"].value.value, "dynamodb-table")

        api = self._bindings(selection, "apigateway_http-api")
        self.assertEqual(api["api_name"].value.value, "apigateway_http-api")
        self.assertEqual(api["lambda_arn"].value.value, "lambda-fn")
        self.assertEqual(api["lambda_invoke_arn"].value.value, "lambda-fn")
        self.assertEqual(
            api["log_group_arn"].value.value, "cloudwatch_log_group-logs"
        )
        self.assertEqual(
            api["access_log_format"].value.value, "json_request_context_v1"
        )

        logs = self._bindings(selection, "cloudwatch_log_group-logs")
        self.assertEqual(
            logs["log_group_name"].value.value,
            "/aws/ai-architect/cloudwatch_log_group-logs",
        )
        self.assertEqual(logs["retention_days"].value.value, "30")

    def test_full_stack_validates_clean_against_the_catalog(self):
        selection, _, registry = self._stack()
        self.assertEqual(
            validate_module_selection(selection, registry=registry), []
        )

    def test_two_log_groups_are_ambiguous(self):
        selection, _, _ = self._stack(
            extra_nodes=[("logs2", "Amazon CloudWatch Logs")]
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_INCONSISTENT, codes)

    def test_lambda_without_log_group_is_diagnosed(self):
        selection, _, _ = _selection(
            [("fn", "AWS Lambda"), ("table", "Amazon DynamoDB")],
            overrides={"lambda-fn": dict(self.LAMBDA_FULL)},
        )
        self.assertFalse(selection.selected)
        codes = {d.code for d in selection.diagnostics}
        self.assertIn(ModuleErrorCode.MODULE_INPUT_INCONSISTENT, codes)

    def test_table_wiring_follows_spec_order(self):
        selection, _, _ = self._stack(
            extra_nodes=[("table2", "Amazon DynamoDB")]
        )
        self.assertTrue(selection.selected)
        fn = self._bindings(selection, "lambda-fn")
        self.assertEqual(
            fn["table_arns"].value.value,
            ("dynamodb-table", "dynamodb-table2"),
        )
        self.assertEqual(fn["table_name"].value.value, "dynamodb-table")


class ProviderNameNamespaceTests(unittest.TestCase):
    """A deployment namespace changes AWS-visible names, not identities."""

    NAMESPACE = "a1b2c3d4"

    def _stack(self):
        return _selection(
            [
                ("api", "Amazon API Gateway (HTTP API)"),
                ("fn", "AWS Lambda"),
                ("table", "Amazon DynamoDB"),
                ("logs", "Amazon CloudWatch Logs"),
            ],
            overrides={
                "lambda-fn": {
                    "runtime": "python3.12",
                    "handler": "index.handler",
                    "memory_mb": "512",
                    "timeout_seconds": "30",
                    "code_artifact_path": "demo-handler.zip",
                    "code_artifact_sha256": "ab" * 32,
                }
            },
            namespace=self.NAMESPACE,
        )

    @staticmethod
    def _bindings(selection, resource_id):
        instance = selection.instance_for(resource_id)
        return {item.input_name: item for item in instance.bindings}

    def test_provider_visible_names_are_namespaced(self):
        selection, _, _ = self._stack()
        fn = self._bindings(selection, "lambda-fn")
        table = self._bindings(selection, "dynamodb-table")
        api = self._bindings(selection, "apigateway_http-api")
        logs = self._bindings(selection, "cloudwatch_log_group-logs")

        self.assertEqual(
            fn["function_name"].value.value,
            f"lambda-fn-{self.NAMESPACE}",
        )
        self.assertEqual(
            fn["table_name"].value.value,
            f"dynamodb-table-{self.NAMESPACE}",
        )
        self.assertEqual(
            table["table_name"].value.value,
            f"dynamodb-table-{self.NAMESPACE}",
        )
        self.assertEqual(
            api["api_name"].value.value,
            f"apigateway_http-api-{self.NAMESPACE}",
        )
        self.assertEqual(
            logs["log_group_name"].value.value,
            "/aws/ai-architect/"
            f"cloudwatch_log_group-logs-{self.NAMESPACE}",
        )

    def test_sibling_identities_and_instance_ids_are_unchanged(self):
        selection, _, _ = self._stack()
        fn = self._bindings(selection, "lambda-fn")
        api = self._bindings(selection, "apigateway_http-api")

        self.assertEqual(
            fn["log_group_arn"].value.value, "cloudwatch_log_group-logs"
        )
        self.assertEqual(
            fn["table_arns"].value.value, ("dynamodb-table",)
        )
        self.assertEqual(api["lambda_arn"].value.value, "lambda-fn")
        self.assertEqual(
            api["log_group_arn"].value.value,
            "cloudwatch_log_group-logs",
        )
        self.assertEqual(
            selection.instance_for("lambda-fn").instance_id,
            "serverless_function-lambda-fn",
        )

    def test_namespace_is_deterministic(self):
        _, spec, registry = self._stack()
        first = select_modules(
            spec,
            provider=PROVIDER,
            registry=registry,
            namespace=self.NAMESPACE,
        )
        second = select_modules(
            spec,
            provider=PROVIDER,
            registry=registry,
            namespace=self.NAMESPACE,
        )
        self.assertEqual(
            first.model_dump(mode="json"), second.model_dump(mode="json")
        )
        self.assertEqual(first.selection_id, second.selection_id)


class WorkloadNeutralityTests(unittest.TestCase):
    """3. The registry is a capability boundary, not a workload boundary."""

    def test_no_workload_terms_in_4c_sources(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src/ai_architect"
        targets = [
            root / "domain" / "provisioning" / "module.py",
            root / "services" / "provisioning" / "module_catalog.py",
            root / "services" / "provisioning" / "capability_mapping.py",
            root / "services" / "provisioning" / "module_selection.py",
        ]
        for path in targets:
            text = path.read_text(encoding="utf-8").lower()
            for forbidden in (
                "whatsapp", "url-shorten", "url short", "healthcare",
                "payment", "scheduler", "invoice", "e-commerce", "medical",
                "patient", "banking",
            ):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")

    def test_capability_mapping_is_explicit_and_closed(self):
        from ai_architect.services.provisioning.capability_mapping import (
            CAPABILITY_MAPPING,
            module_for_kind,
        )
        self.assertEqual(
            CAPABILITY_MAPPING,
            {
                ResourceKind.S3: ModuleId.OBJECT_STORAGE,
                ResourceKind.DYNAMODB: ModuleId.NOSQL_TABLE,
                ResourceKind.LAMBDA: ModuleId.SERVERLESS_FUNCTION,
                ResourceKind.APIGATEWAY_HTTP: ModuleId.HTTP_API,
                ResourceKind.CLOUDFRONT_DISTRIBUTION:
                    ModuleId.CLOUDFRONT_DISTRIBUTION,
                ResourceKind.COGNITO_USER_POOL: ModuleId.COGNITO_USER_POOL,
                ResourceKind.SQS_QUEUE: ModuleId.SQS_QUEUE,
                ResourceKind.SNS_TOPIC: ModuleId.SNS_TOPIC,
                ResourceKind.CLOUDWATCH_LOG_GROUP: ModuleId.LOG_GROUP,
                ResourceKind.CLOUDWATCH_ALARM: ModuleId.CLOUDWATCH_ALARMS,
            },
        )
        self.assertIs(
            module_for_kind(ResourceKind.S3), ModuleId.OBJECT_STORAGE
        )

    def test_capability_mapping_is_pure(self):
        from ai_architect.services.provisioning.capability_mapping import (
            module_for_kind,
        )
        first = [module_for_kind(k) for k in ResourceKind]
        second = [module_for_kind(k) for k in ResourceKind]
        self.assertEqual(first, second)

    def test_no_string_or_category_fallback_in_mapping(self):
        """module_for_kind resolves only through the explicit table."""
        import ast

        from ai_architect.services.provisioning import capability_mapping

        source = pathlib.Path(
            capability_mapping.__file__
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)

        target = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "module_for_kind"
        )
        referenced = {
            node.attr for node in ast.walk(target)
            if isinstance(node, ast.Attribute)
        }
        self.assertEqual(referenced, {"get"})
        self.assertNotIn("normalize_component_name", source)
        self.assertNotIn("startswith", source)


class FrozenContractTests(unittest.TestCase):
    """20. Slice 4A and 4B remain untouched."""

    def test_4a_domain_models_still_declared(self):
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/ai_architect/domain/provisioning/models.py"
        )
        text = path.read_text(encoding="utf-8")
        for declaration in (
            "class InfraSpec", "class ResourceInstance",
            "class ResolvedParameter", "class PendingInput",
            "class ParameterSource", "class ResourceKind",
            "SUPPORTED_PROVIDERS", "SCHEMA_VERSION",
        ):
            self.assertIn(declaration, text)

    def test_resource_kind_vocabulary_is_closed(self):
        self.assertEqual(
            [k.value for k in ResourceKind],
            ["s3", "lambda", "dynamodb", "apigateway_http",
             "cloudfront_distribution", "cognito_user_pool", "sqs_queue",
             "sns_topic", "cloudwatch_log_group", "cloudwatch_alarm"],
        )

    def test_module_vocabulary_is_closed(self):
        self.assertEqual(
            set(ModuleId), {
                ModuleId.OBJECT_STORAGE,
                ModuleId.NOSQL_TABLE,
                ModuleId.SERVERLESS_FUNCTION,
                ModuleId.HTTP_API,
                ModuleId.CLOUDFRONT_DISTRIBUTION,
                ModuleId.COGNITO_USER_POOL,
                ModuleId.SQS_QUEUE,
                ModuleId.SNS_TOPIC,
                ModuleId.LOG_GROUP,
                ModuleId.CLOUDWATCH_ALARMS,
            },
        )

    def test_registry_declares_ten_contracts(self):
        from ai_architect.services.provisioning.registry import (
            RESOURCE_CONTRACTS,
        )
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

    def test_4b_guard_still_forbids_module_blocks(self):
        """4C must not have loosened the existing 4B safety boundary."""
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/ai_architect/services/provisioning/terraform_guard.py"
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn('"module",', text)

    def test_4b_raw_renderer_survives_module_composition_slice(self):
        """The raw 4B renderer stays intact; module blocks arrive only
        through the separate verified composition path, and the guard
        still denies them by default (see the test above)."""
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/ai_architect/services/provisioning/terraform_renderer.py"
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("aws_s3_bucket", text)
        self.assertIn("aws_dynamodb_table", text)
        self.assertIn("def render_infra_spec", text)
        self.assertIn("def render_module_composition", text)

    def test_4a_spec_builder_still_works(self):
        built = build_infra_spec(
            _architecture([("bucket", "Amazon S3")]), provider=PROVIDER
        )
        self.assertEqual(len(built.resources), 1)
        self.assertEqual(built.resources[0].kind, ResourceKind.S3)


if __name__ == "__main__":
    unittest.main()