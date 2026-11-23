"""Slice 4B tests: deterministic Terraform renderer + safety guard.

Covers render determinism, the closed Terraform vocabulary, fail-closed
parameter handling, guard rejections, and the optional Terraform CLI path.

Terraform is NOT required to run this suite. When the binary is absent
the CLI tests assert an explicit unavailable state, never a pass.

Run: poetry run python -m unittest discover tests
"""
import pathlib
import sys
import unittest
import uuid
from datetime import datetime
from unittest import mock

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ParameterSource,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
)
from ai_architect.domain.provisioning.render import (
    RenderErrorCode,
    RenderStatus,
)
from ai_architect.services.provisioning.spec_builder import build_infra_spec
from ai_architect.services.provisioning.terraform_cli import (
    PERMITTED_COMMANDS,
    TerraformStatus,
    assert_permitted,
    run_terraform_checks,
    terraform_available,
)
from ai_architect.services.provisioning.terraform_guard import (
    ALLOWED_TOP_LEVEL,
    FORBIDDEN_RESOURCE_TYPES,
    find_policy_surface,
    guard_terraform_source,
    resource_types_in_source,
)
from ai_architect.services.provisioning.terraform_renderer import (
    ALLOWED_TF_RESOURCE_TYPES,
    REGISTERED_TF_RESOURCE_TYPES,
    render_infra_spec,
)

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


def _spec(resources: list, region=REGION, provider=PROVIDER) -> InfraSpec:
    return InfraSpec(
        spec_id="spec-1",
        provider=provider,
        region_code=region,
        source_architecture=SourceArchitectureRef(
            artifact_id=str(uuid.uuid4()),
            version=1,
            session_id="design-1",
            upstream_inputs_hash="upstream",
            architecture_hash="arch-hash",
        ),
        resources=resources,
    )


def _resource(kind, resource_id, values, node_id=None) -> ResourceInstance:
    return ResourceInstance(
        resource_id=resource_id,
        node_id=node_id if node_id is not None else resource_id.split("-", 1)[-1],
        kind=kind,
        component_name=f"{kind.value} component",
        parameters=[
            ResolvedParameter(
                name=name,
                value=value,
                source=ParameterSource.REQUIRED_INPUT,
            )
            for name, value in values.items()
        ],
    )


def _s3(resource_id="s3-my-bucket", values=None) -> ResourceInstance:
    return _resource(ResourceKind.S3, resource_id, values or S3_VALUES)


def _ddb(resource_id="dynamodb-table", values=None) -> ResourceInstance:
    return _resource(ResourceKind.DYNAMODB, resource_id, values or DDB_VALUES)


def _lambda(resource_id="lambda-fn", values=None) -> ResourceInstance:
    return _resource(ResourceKind.LAMBDA, resource_id, values or LAMBDA_VALUES)


def _log_group(resource_id="cloudwatch-log-group", values=None) -> ResourceInstance:
    return _resource(
        ResourceKind.CLOUDWATCH_LOG_GROUP,
        resource_id,
        values or {"retention_days": "30"},
    )


class DeterminismTests(unittest.TestCase):
    """1-4, 14. Byte-identical output; stable ordering and identities."""

    def test_same_spec_is_byte_identical(self):
        spec = _spec([_s3(), _ddb()])
        first = render_infra_spec(spec)
        second = render_infra_spec(spec)
        self.assertEqual(first.status, RenderStatus.RENDERED)
        self.assertEqual(first.source_tree(), second.source_tree())
        self.assertEqual(first.tree_hash, second.tree_hash)

    def test_resource_order_does_not_affect_output(self):
        forward = render_infra_spec(_spec([_s3(), _ddb()]))
        reverse = render_infra_spec(_spec([_ddb(), _s3()]))
        self.assertEqual(forward.source_tree(), reverse.source_tree())
        self.assertEqual(forward.tree_hash, reverse.tree_hash)

    def test_parameter_order_does_not_affect_output(self):
        s3 = _resource(
            ResourceKind.S3,
            "s3-my-bucket",
            {name: S3_VALUES[name] for name in reversed(list(S3_VALUES))},
        )
        forward = render_infra_spec(_spec([s3]))
        reverse = render_infra_spec(_spec([_s3()]))
        self.assertEqual(forward.source_tree(), reverse.source_tree())

    def test_file_order_is_sorted(self):
        result = render_infra_spec(_spec([_s3()]))
        paths = [entry.path for entry in result.files]
        self.assertEqual(paths, sorted(paths))

    def test_stable_resource_ids_are_preserved(self):
        result = render_infra_spec(
            _spec([_s3(resource_id="s3-my-bucket"), _ddb("dynamodb-orders")])
        )
        main = result.file("main.tf").content
        self.assertIn('resource "aws_s3_bucket" "s3-my-bucket"', main)
        self.assertIn('resource "aws_dynamodb_table" "dynamodb-orders"', main)

    def test_tree_hash_is_a_content_hash(self):
        result = render_infra_spec(_spec([_s3()]))
        self.assertEqual(len(result.tree_hash), 64)

    def test_no_timestamps_uuids_or_paths_in_output(self):
        result = render_infra_spec(_spec([_s3(), _ddb()]))
        combined = "\n".join(result.source_tree().values())
        self.assertNotIn(str(uuid.uuid4()), combined)
        self.assertNotIn(NOW.isoformat(), combined)
        self.assertNotIn("2026-01-01", combined)
        self.assertNotIn("C:\\", combined)
        self.assertNotIn("/tmp/", combined)
        self.assertNotIn("/home/", combined)

    def test_attribute_order_is_explicit_not_dict_order(self):
        result = render_infra_spec(_spec([_ddb()]))
        main = result.file("main.tf").content
        order = [
            main.index("billing_mode"),
            main.index("hash_key"),
            main.index("name"),
            main.index("range_key"),
            main.index("attribute {"),
            main.index("point_in_time_recovery"),
        ]
        self.assertEqual(order, sorted(order))


class ProviderRegionTests(unittest.TestCase):
    """5. Provider and region come from the InfraSpec."""

    def test_provider_block_comes_from_spec(self):
        result = render_infra_spec(_spec([_s3()], provider=PROVIDER))
        self.assertIn('provider "aws"', result.file("provider.tf").content)

    def test_region_comes_from_spec(self):
        result = render_infra_spec(_spec([_s3()], region="eu-west-1"))
        self.assertIn('region = "eu-west-1"', result.file("provider.tf").content)

    def test_region_change_changes_output(self):
        east = render_infra_spec(_spec([_s3()], region="us-east-1"))
        west = render_infra_spec(_spec([_s3()], region="us-west-2"))
        self.assertNotEqual(east.tree_hash, west.tree_hash)

    def test_no_credential_block_is_emitted(self):
        result = render_infra_spec(_spec([_s3()]))
        provider = result.file("provider.tf").content
        self.assertNotIn("access_key", provider)
        self.assertNotIn("secret_key", provider)
        self.assertNotIn("token", provider)
        self.assertNotIn("profile", provider)


class RenderabilityTests(unittest.TestCase):
    """6, 7, and the Slice 4B resource decisions."""

    def test_s3_renders_deterministically(self):
        result = render_infra_spec(_spec([_s3()]))
        self.assertEqual(result.status, RenderStatus.RENDERED)
        main = result.file("main.tf").content
        for expected in (
            'resource "aws_s3_bucket" "s3-my-bucket"',
            'resource "aws_s3_bucket_versioning" "s3-my-bucket_versioning"',
            "resource \"aws_s3_bucket_server_side_encryption_configuration\"",
            "resource \"aws_s3_bucket_public_access_block\"",
            'bucket = "my-design-bucket"',
            'status = "Enabled"',
            'sse_algorithm = "AES256"',
            "restrict_public_buckets = true",
        ):
            self.assertIn(expected, main)

    def test_dynamodb_renders_deterministically(self):
        """Semantic structure is asserted; alignment is fmt's job, not ours."""
        result = render_infra_spec(_spec([_ddb()]))
        self.assertEqual(result.status, RenderStatus.RENDERED)
        main = result.file("main.tf").content
        self.assertIn('resource "aws_dynamodb_table" "dynamodb-table"', main)
        self.assertIn('billing_mode = "PAY_PER_REQUEST"', main)
        self.assertRegex(main, r'hash_key\s+= "pk"')
        self.assertRegex(main, r'range_key\s+= "sk"')
        self.assertIn("point_in_time_recovery {", main)
        self.assertIn("enabled = true", main)

    def test_dynamodb_attribute_blocks_come_from_grammar(self):
        result = render_infra_spec(_spec([_ddb()]))
        main = result.file("main.tf").content
        self.assertIn('name = "pk"', main)
        self.assertIn('type = "S"', main)
        self.assertIn('name = "sk"', main)
        self.assertIn('type = "N"', main)

    def test_lambda_renders_explicit_logging_config(self):
        values = {
            **LAMBDA_VALUES,
            "code_artifact_path": "demo-handler.zip",
            "code_artifact_sha256": "a" * 64,
        }
        result = render_infra_spec(
            _spec([_lambda(values=values), _ddb(), _log_group()])
        )
        self.assertEqual(result.status, RenderStatus.RENDERED)
        main = result.file("main.tf").content
        self.assertIn("logging_config {", main)
        self.assertIn('log_format = "Text"', main)
        self.assertRegex(
            main,
            r"log_group\s*=\s*"
            r"aws_cloudwatch_log_group\.cloudwatch-log-group\.name",
        )

    def test_lambda_fails_closed_with_missing_code_artifact(self):
        """All Slice 4A Lambda inputs present, still no code artifact."""
        result = render_infra_spec(_spec([_lambda()]))
        self.assertEqual(result.status, RenderStatus.MISSING_CODE_ARTIFACT)
        codes = {d.code for d in result.diagnostics}
        self.assertIn(RenderErrorCode.MISSING_CODE_ARTIFACT, codes)

    def test_lambda_diagnostic_names_the_gap(self):
        result = render_infra_spec(_spec([_lambda()]))
        message = " ".join(str(d) for d in result.diagnostics)
        self.assertIn("code-artifact", message)
        self.assertIn("filename", message)

    def test_lambda_produces_no_files_and_no_invention(self):
        result = render_infra_spec(_spec([_lambda()]))
        self.assertEqual(result.files, [])
        self.assertEqual(result.tree_hash, "")
        combined = str(result.source_tree())
        for invented in ("aws_lambda_function", "filename", "s3_key",
                         "image_uri", ".zip"):
            self.assertNotIn(invented, combined)

    def test_lambda_stays_in_the_registry(self):
        from ai_architect.services.provisioning.registry import (
            RESOURCE_CONTRACTS,
        )
        self.assertIn(ResourceKind.LAMBDA, RESOURCE_CONTRACTS)

    def test_lambda_blocks_the_whole_render(self):
        result = render_infra_spec(_spec([_s3(), _lambda()]))
        self.assertEqual(result.status, RenderStatus.MISSING_CODE_ARTIFACT)
        self.assertEqual(result.files, [])

    def test_unsupported_kind_fails_closed(self):
        bogus = ResourceInstance(
            resource_id="sqs-queue",
            node_id="queue",
            kind=ResourceKind.S3,
            component_name="Amazon SQS",
            parameters=[],
        )
        result = render_infra_spec(_spec([bogus]))
        self.assertNotEqual(result.status, RenderStatus.RENDERED)

    def test_missing_required_parameter_fails_closed(self):
        partial = _resource(
            ResourceKind.S3, "s3-my-bucket",
            {"bucket_name": "my-design-bucket", "versioning": "enabled"},
        )
        result = render_infra_spec(_spec([partial]))
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)
        codes = {d.code for d in result.diagnostics}
        self.assertIn(RenderErrorCode.MISSING_REQUIRED_INPUT, codes)
        self.assertEqual(result.files, [])


class ParameterSafetyTests(unittest.TestCase):
    """Unsafe / ambiguous parameter values fail closed."""

    def _render_with(self, values):
        return render_infra_spec(
            _spec([_resource(ResourceKind.S3, "s3-my-bucket", values)])
        )

    def test_unknown_enum_value_fails_closed(self):
        result = self._render_with({**S3_VALUES, "versioning": "on"})
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)
        self.assertIn(
            RenderErrorCode.PARAMETER_OUT_OF_ALLOWLIST,
            {d.code for d in result.diagnostics},
        )

    def test_provisioned_dynamodb_billing_is_refused(self):
        """PROVISIONED would need read/write capacity we must not invent."""
        result = render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES, "billing_mode": "provisioned"})])
        )
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_invalid_bucket_name_fails_closed(self):
        for bad in ("My_Bucket", "a", "bucket with space", "-leading"):
            with self.subTest(bad=bad):
                result = self._render_with({**S3_VALUES, "bucket_name": bad})
                self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_boolean_forms_normalize_deterministically(self):
        """Every accepted spelling yields the same rendered Terraform."""
        outputs = set()
        for value in ("true", "enabled", "TRUE", " Enabled "):
            with self.subTest(value=value):
                result = render_infra_spec(
                    _spec([_ddb(values={**DDB_VALUES,
                                       "point_in_time_recovery": value})])
                )
                self.assertEqual(result.status, RenderStatus.RENDERED)
                outputs.add(result.file("main.tf").content)
        self.assertEqual(len(outputs), 1, "boolean forms did not normalize")
        self.assertIn("enabled = true", outputs.pop())

    def test_boolean_false_normalizes(self):
        result = render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES,
                               "point_in_time_recovery": "false"})])
        )
        self.assertIn("enabled = false", result.file("main.tf").content)

    def test_unrecognized_boolean_fails_closed(self):
        result = render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES, "point_in_time_recovery": "1"})])
        )
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_key_attribute_must_be_declared(self):
        result = render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES, "key_schema": "HASH:missing"})])
        )
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)
        self.assertIn(
            RenderErrorCode.INCONSISTENT_REFERENCE,
            {d.code for d in result.diagnostics},
        )

    def test_hash_key_is_mandatory(self):
        result = render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES, "key_schema": "RANGE:sk"})])
        )
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)


class InjectionTests(unittest.TestCase):
    """Structured blocks cannot be injected through parameter strings."""

    def _ddb_with(self, **overrides):
        return render_infra_spec(
            _spec([_ddb(values={**DDB_VALUES, **overrides})])
        )

    def test_hcl_brace_injection_is_refused(self):
        for payload in (
            'pk:S,sk:N\n  }\n  provisioner "local-exec" {',
            'pk:S} evil {',
        ):
            with self.subTest(payload=payload):
                result = self._ddb_with(attribute_definitions=payload)
                self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)
                self.assertEqual(result.files, [])

    def test_interpolation_injection_is_refused(self):
        for payload in ('${var.secret}', 'pk:S,sk:${aws_secrets}N'):
            with self.subTest(payload=payload):
                result = self._ddb_with(attribute_definitions=payload)
                self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_quote_injection_is_refused(self):
        result = self._ddb_with(key_schema='HASH:pk", "x')
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_bucket_name_cannot_smuggle_hcl(self):
        result = render_infra_spec(
            _spec([
                _s3(values={
                    **S3_VALUES,
                    "bucket_name": 'x"\n  lifecycle {',
                })
            ])
        )
        self.assertEqual(result.status, RenderStatus.UNREPRESENTABLE)

    def test_no_arbitrary_block_type_can_enter_output(self):
        result = render_infra_spec(_spec([_s3(), _ddb()]))
        declared = {
            resource_type
            for resource_type, _ in resource_types_in_source(result.source_tree())
        }
        self.assertTrue(declared)
        self.assertTrue(declared <= ALLOWED_TF_RESOURCE_TYPES)


class GuardTests(unittest.TestCase):
    """8-13. Structural guard rejections."""

    def _guard(self, main):
        return guard_terraform_source(
            {"main.tf": main}, allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES
        )

    def test_guard_accepts_renderer_output(self):
        result = render_infra_spec(_spec([_s3(), _ddb()]))
        self.assertEqual(
            guard_terraform_source(
                result.source_tree(),
                allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            ),
            [],
        )

    def test_local_exec_rejected(self):
        main = (
            'resource "aws_s3_bucket" "b" {\n'
            "  bucket = \"b\"\n"
            "  provisioner \"local-exec\" {\n"
            "    command = \"rm -rf /\"\n"
            "  }\n"
            "}\n"
        )
        codes = {d.code for d in self._guard(main)}
        self.assertIn(RenderErrorCode.GUARD_VIOLATION, codes)

    def test_remote_exec_rejected(self):
        main = (
            'resource "aws_s3_bucket" "b" {\n'
            "  bucket = \"b\"\n"
            "  provisioner \"remote-exec\" {\n"
            "    inline = [\"curl evil\"]\n"
            "  }\n"
            "}\n"
        )
        self.assertTrue(self._guard(main))

    def test_external_data_source_rejected(self):
        main = 'data "external" "script" {\n}\n'
        codes = {d.code for d in self._guard(main)}
        self.assertIn(RenderErrorCode.GUARD_VIOLATION, codes)

    def test_unsupported_resource_type_rejected(self):
        main = 'resource "aws_sqs_queue" "q" {\n  name = "q"\n}\n'
        messages = " ".join(str(d) for d in self._guard(main))
        self.assertIn("not represented by the provisioning registry", messages)

    def test_null_resource_rejected(self):
        main = 'resource "null_resource" "n" {\n}\n'
        self.assertTrue(self._guard(main))

    def test_module_block_rejected(self):
        main = 'module "m" {\n  source = "./x"\n}\n'
        self.assertTrue(self._guard(main))

    def test_unknown_reference_rejected(self):
        main = (
            'resource "aws_s3_bucket_versioning" "b_versioning" {\n'
            "  bucket = aws_s3_bucket.ghost.id\n"
            "  versioning_configuration {\n"
            '    status = "Enabled"\n'
            "  }\n"
            "}\n"
        )
        codes = {d.code for d in self._guard(main)}
        self.assertIn(RenderErrorCode.INCONSISTENT_REFERENCE, codes)

    def test_iam_policy_attachment_rejected(self):
        main = (
            'resource "aws_iam_role_policy_attachment" "a" {\n'
            "  role = \"r\"\n"
            "}\n"
        )
        self.assertTrue(self._guard(main))

    def test_credential_material_rejected(self):
        main = (
            'resource "aws_s3_bucket" "b" {\n'
            "  bucket = \"b\"\n"
            "  access_key = \"AKIAIOSFODNN7EXAMPLE\"\n"
            "}\n"
        )
        self.assertTrue(self._guard(main))

    def test_interpolation_rejected(self):
        main = 'resource "aws_s3_bucket" "b" {\n  bucket = "${var.x}"\n}\n'
        self.assertTrue(self._guard(main))

    def test_unparseable_structure_fails_closed(self):
        main = 'resource "aws_s3_bucket" "b" {\n  bucket = "b"\n'
        codes = {d.code for d in self._guard(main)}
        self.assertIn(RenderErrorCode.GUARD_UNPARSEABLE, codes)

    def test_empty_tree_rejected(self):
        codes = {d.code for d in guard_terraform_source(
            {}, allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES)}
        self.assertIn(RenderErrorCode.GUARD_VIOLATION, codes)

    def test_guard_does_not_depend_on_renderer_patterns(self):
        """Guard owns its credential detection independently."""
        from ai_architect.services.provisioning import terraform_guard
        from ai_architect.services.provisioning import terraform_renderer
        self.assertIsNotNone(terraform_guard._SECRET_VALUE)
        self.assertIsNone(getattr(terraform_renderer, "_SECRET_VALUE", None))


class ImplicitIamTests(unittest.TestCase):
    """5. Implicit IAM has zero policy attachments."""

    def test_policy_resource_types_are_not_in_the_vocabulary(self):
        # Only the two deliberately admitted IAM surfaces may appear:
        # an inline least-privilege role policy scoped to the spec's own
        # log group/tables, and the API Gateway invoke permission scoped
        # to the same tree's execution ARN. Everything broader stays out.
        for admitted in ("aws_iam_role_policy", "aws_lambda_permission"):
            self.assertIn(admitted, ALLOWED_TF_RESOURCE_TYPES)
        for forbidden in (
            "aws_iam_role_policy_attachment",
            "aws_iam_policy",
            "aws_iam_policy_attachment",
        ):
            self.assertNotIn(forbidden, ALLOWED_TF_RESOURCE_TYPES)
            self.assertIn(forbidden, FORBIDDEN_RESOURCE_TYPES)

    def test_rendered_output_has_no_policy_surface(self):
        result = render_infra_spec(_spec([_s3(), _ddb()]))
        self.assertEqual(find_policy_surface(result.source_tree()), [])

    def test_basic_execution_role_policy_is_never_attached(self):
        result = render_infra_spec(_spec([_s3(), _ddb()]))
        combined = "\n".join(result.source_tree().values())
        self.assertNotIn("AWSLambdaBasicExecutionRole", combined)

    def test_bare_iam_role_passes_the_guard_with_no_policy(self):
        main = 'resource "aws_iam_role" "lambda_execution_role" {\n  name = "r"\n}\n'
        self.assertEqual(
            guard_terraform_source(
                {"main.tf": main},
                allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            ),
            [],
        )

    def test_iam_role_with_policy_attachment_is_rejected(self):
        main = (
            'resource "aws_iam_role" "lambda_execution_role" {\n'
            '  name = "r"\n'
            "}\n"
            'resource "aws_iam_role_policy_attachment" "a" {\n'
            '  role       = aws_iam_role.lambda_execution_role.name\n'
            '  policy_arn = "arn:aws:iam::aws:policy/X"\n'
            "}\n"
        )
        self.assertTrue(
            guard_terraform_source(
                {"main.tf": main},
                allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            )
        )

    def test_lambda_render_emits_no_iam_at_all(self):
        result = render_infra_spec(_spec([_lambda()]))
        self.assertEqual(result.files, [])
        self.assertNotIn("aws_iam_role", str(result.source_tree()))


class VocabularyTests(unittest.TestCase):
    """Vocabulary is derived from the Slice 4A registry."""

    def test_raw_vocabulary_is_a_closed_subset_of_registry_kinds(self):
        """CloudWatch alarms are module-only; the raw path stays closed."""
        from ai_architect.services.provisioning.registry import (
            RESOURCE_CONTRACTS,
        )
        self.assertLessEqual(
            set(REGISTERED_TF_RESOURCE_TYPES), set(RESOURCE_CONTRACTS)
        )
        self.assertNotIn(
            "aws_cloudwatch_metric_alarm", REGISTERED_TF_RESOURCE_TYPES
        )

    def test_no_unsupported_service_types_present(self):
        # Alarm resources live only inside the pinned module. Keeping
        # them out of the raw/fallback vocabulary prevents the generic
        # LLM path from emitting them.
        for absent in (
            "aws_sqs_queue", "aws_sns_topic", "aws_instance",
            "aws_db_instance", "aws_cloudwatch_metric_alarm",
            "aws_ecr_repository",
        ):
            self.assertNotIn(absent, ALLOWED_TF_RESOURCE_TYPES)
        # The HTTP API entry point is a verified capability, so its
        # managed resource family is present and nothing else crept in.
        for present in (
            "aws_apigatewayv2_api", "aws_apigatewayv2_integration",
            "aws_apigatewayv2_route", "aws_apigatewayv2_stage",
        ):
            self.assertIn(present, ALLOWED_TF_RESOURCE_TYPES)

    def test_allowed_top_level_is_closed(self):
        self.assertEqual(
            set(ALLOWED_TOP_LEVEL),
            {"terraform", "provider", "resource", "variable", "output",
             "locals"},
        )

    def test_rendered_tree_uses_only_expected_files(self):
        result = render_infra_spec(_spec([_s3()]))
        self.assertEqual(
            sorted(entry.path for entry in result.files),
            ["main.tf", "provider.tf", "versions.tf"],
        )

    def test_no_files_created_for_appearance(self):
        result = render_infra_spec(_spec([_s3()]))
        names = {entry.path for entry in result.files}
        self.assertNotIn("variables.tf", names)
        self.assertNotIn("outputs.tf", names)


class TerraformCliTests(unittest.TestCase):
    """18-19 and CLI-specific behaviour."""

    def test_unavailable_state_is_explicit_when_absent(self):
        if terraform_available():
            self.skipTest("Terraform is installed on this machine")
        result = run_terraform_checks({"main.tf": ""})
        self.assertEqual(result.status, TerraformStatus.NOT_INSTALLED)
        self.assertFalse(result.installed)
        self.assertFalse(result.validated)
        codes = {d.code for d in result.diagnostics}
        self.assertIn(RenderErrorCode.TERRAFORM_UNAVAILABLE, codes)
        self.assertIn("not claimed", " ".join(str(d) for d in result.diagnostics))

    def test_init_failure_is_reported_explicitly(self):
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=True,
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_version",
            return_value="Terraform v1.9.0",
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli._run",
            side_effect=[(0, ""), (1, "provider registry unreachable")],
        ):
            result = run_terraform_checks({"main.tf": ""})
        self.assertEqual(result.status, TerraformStatus.INIT_FAILED)
        self.assertFalse(result.validated)
        codes = {d.code for d in result.diagnostics}
        self.assertIn(RenderErrorCode.TERRAFORM_INIT_FAILED, codes)

    def test_validate_failure_is_reported_explicitly(self):
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=True,
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_version",
            return_value="Terraform v1.9.0",
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli._run",
            side_effect=[(0, ""), (0, ""), (1, "Invalid reference")],
        ):
            result = run_terraform_checks({"main.tf": ""})
        self.assertEqual(result.status, TerraformStatus.VALIDATE_FAILED)
        self.assertFalse(result.validated)

    def test_success_path(self):
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=True,
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_version",
            return_value="Terraform v1.9.0",
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli._run",
            side_effect=[(0, ""), (0, ""), (0, "")],
        ):
            result = run_terraform_checks({"main.tf": ""})
        self.assertEqual(result.status, TerraformStatus.VALIDATE_OK)
        self.assertTrue(result.validated)
        self.assertEqual(
            result.exit_codes, {"fmt": 0, "init": 0, "validate": 0}
        )

    def test_fmt_check_is_used_and_non_mutating(self):
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=True,
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_version",
            return_value="Terraform v1.9.0",
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli._run",
            side_effect=[(0, ""), (0, ""), (0, "")],
        ) as runner:
            run_terraform_checks({"main.tf": "x"})
        first = runner.call_args_list[0][0][0]
        self.assertEqual(first[1], "fmt")
        self.assertIn("-check", first)

    def test_fmt_failure_short_circuits(self):
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=True,
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_version",
            return_value="Terraform v1.9.0",
        ), mock.patch(
            "ai_architect.services.provisioning.terraform_cli._run",
            side_effect=[(3, "drift")],
        ) as runner:
            result = run_terraform_checks({"main.tf": "x"})
        self.assertEqual(result.status, TerraformStatus.FMT_FAILED)
        self.assertEqual(runner.call_count, 1)

    def test_source_tree_is_not_mutated(self):
        tree = {"main.tf": 'resource "aws_s3_bucket" "b" {\n  bucket = "b"\n}\n'}
        before = dict(tree)
        with mock.patch(
            "ai_architect.services.provisioning.terraform_cli.terraform_available",
            return_value=False,
        ):
            run_terraform_checks(tree)
        self.assertEqual(tree, before)

    def test_plan_apply_destroy_are_never_permitted(self):
        for command in ("plan", "apply", "destroy"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    assert_permitted(["terraform", command])

    def test_permitted_commands_are_the_documented_set(self):
        self.assertEqual(PERMITTED_COMMANDS, {"fmt", "init", "validate"})

    def test_run_refuses_forbidden_subcommand(self):
        import tempfile
        from ai_architect.services.provisioning.terraform_cli import _run
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _run(["terraform", "apply", "-auto-approve"], tmp)

    def test_environment_is_not_forwarded(self):
        import os
        from ai_architect.services.provisioning.terraform_cli import _minimal_env
        env = _minimal_env()
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertEqual(env["TF_INPUT"], "0")

    def test_output_is_sanitized(self):
        from ai_architect.services.provisioning.terraform_cli import _sanitize
        raw = 'error: AWS_SECRET_ACCESS_KEY=supersecretvalue'
        cleaned = _sanitize(raw)
        self.assertNotIn("supersecretvalue", cleaned)
        self.assertIn("[redacted-secret]", cleaned)

    def test_output_truncation_keeps_the_tail(self):
        from ai_architect.services.provisioning.terraform_cli import _sanitize
        padding = "x" * 9000
        tail = "Error: invalid value for variable"
        cleaned = _sanitize(f"{padding}\n{tail}")
        self.assertIn("[truncated]", cleaned)
        self.assertTrue(cleaned.endswith(tail))


class Slice4aSeamTests(unittest.TestCase):
    """Real Slice 4A builder output flows into the renderer."""

    def _architecture(self, names):
        return ArchitectureResult(
            artifact_id=str(uuid.uuid4()),
            session_id="design-1",
            version=1,
            nodes=[
                ArchitectureNode(
                    node_id=node_id,
                    component_name=name,
                    category="storage" if "S3" in name else "database",
                    role_in_system=f"role for {name}",
                )
                for node_id, name in names
            ],
            edges=[],
            entry_points=[],
            data_store_node_ids=[node_id for node_id, _ in names],
            decisions=[ArchitectureDecision(decision="d", rationale="r")],
            inputs_ref=ArchitectureInputsRef(
                requirements_session_id="req-1",
                capacity_formula_version="capacity/v1",
                selection_generated_at=NOW,
                cost_generated_at=NOW,
                cost_pricing_as_of=NOW,
                region_code="eu-west-1",
                upstream_inputs_hash="upstream",
            ),
            generated_at=NOW,
        )

    def _fill_inputs(self, spec, values_by_kind):
        resources = []
        for resource in spec.resources:
            resources.append(
                {
                    **resource.model_dump(mode="json"),
                    "parameters": [
                        {
                            "name": name,
                            "value": values_by_kind[resource.kind][name],
                            "source": "required_input",
                        }
                        for name in values_by_kind[resource.kind]
                    ],
                    "pending_inputs": [],
                }
            )
        return InfraSpec.model_validate(
            {**spec.model_dump(mode="json"), "resources": resources}
        )

    def test_builder_output_renders_end_to_end(self):
        architecture = self._architecture(
            [("bucket", "Amazon S3"), ("table", "Amazon DynamoDB")]
        )
        built = build_infra_spec(architecture, provider=PROVIDER)
        filled = self._fill_inputs(
            built, {ResourceKind.S3: S3_VALUES, ResourceKind.DYNAMODB: DDB_VALUES}
        )
        result = render_infra_spec(filled)
        self.assertEqual(result.status, RenderStatus.RENDERED)
        self.assertIn('region = "eu-west-1"', result.file("provider.tf").content)
        self.assertIn("s3-bucket", result.file("main.tf").content)
        self.assertIn("dynamodb-table", result.file("main.tf").content)

    def test_builder_lambda_architecture_fails_closed(self):
        architecture = self._architecture([("fn", "AWS Lambda")])
        built = build_infra_spec(architecture, provider=PROVIDER)
        filled = self._fill_inputs(
            built, {ResourceKind.LAMBDA: LAMBDA_VALUES}
        )
        result = render_infra_spec(filled)
        self.assertEqual(result.status, RenderStatus.MISSING_CODE_ARTIFACT)

    def test_builder_output_is_not_mutated_by_rendering(self):
        architecture = self._architecture([("bucket", "Amazon S3")])
        built = build_infra_spec(architecture, provider=PROVIDER)
        before = built.model_dump(mode="json")
        render_infra_spec(built)
        self.assertEqual(built.model_dump(mode="json"), before)


class BoundaryTests(unittest.TestCase):
    """15-17, 20. Renderer boundaries and frozen contracts."""

    def _renderer_sources(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ai_architect"
        return {
            "render.py": root / "domain" / "provisioning" / "render.py",
            "terraform_renderer.py": root / "services" / "provisioning" / "terraform_renderer.py",
            "terraform_guard.py": root / "services" / "provisioning" / "terraform_guard.py",
            "terraform_cli.py": root / "services" / "provisioning" / "terraform_cli.py",
        }

    def test_renderer_contains_no_llm_dependency(self):
        for name, path in self._renderer_sources().items():
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "langchain", "create_llm", "infrastructure.llm",
                "chat_model", "ainvoke", "astream",
            ):
                self.assertNotIn(forbidden, text, f"{name} references {forbidden}")

    def test_renderer_does_not_call_mcp(self):
        for name, path in self._renderer_sources().items():
            text = path.read_text(encoding="utf-8")
            for forbidden in ("aws_mcp", "retrieval", "mcp", "qdrant"):
                self.assertNotIn(forbidden, text.lower(), f"{name} references {forbidden}")

    def test_renderer_does_not_call_aws(self):
        for name, path in self._renderer_sources().items():
            text = path.read_text(encoding="utf-8")
            for forbidden in ("import boto3", "boto3.client", "botocore"):
                self.assertNotIn(forbidden, text, f"{name} references {forbidden}")

    def test_renderer_reads_no_environment(self):
        for name in ("terraform_renderer.py", "terraform_guard.py"):
            text = self._renderer_sources()[name].read_text(encoding="utf-8")
            self.assertNotIn("os.environ", text, f"{name} reads the environment")
            self.assertNotIn("getenv", text, f"{name} reads the environment")

    def test_renderer_does_not_import_subprocess(self):
        """Only the optional CLI module may shell out."""
        for name in ("terraform_renderer.py", "terraform_guard.py", "render.py"):
            text = self._renderer_sources()[name].read_text(encoding="utf-8")
            self.assertNotIn("subprocess", text, f"{name} imports subprocess")

    def test_terraform_not_a_python_dependency(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8").lower()
        self.assertNotIn("terraform", pyproject)
        self.assertNotIn("hcl", pyproject)

    def test_spec_4a_contracts_unchanged(self):
        """Slice 4A models and registry are untouched by 4B."""
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ai_architect"
        models = (root / "domain" / "provisioning" / "models.py").read_text(
            encoding="utf-8"
        )
        for field in (
            "class InfraSpec", "class ResourceInstance", "class ResolvedParameter",
            "class PendingInput", "SCHEMA_VERSION",
        ):
            self.assertIn(field, models)
        registry = (root / "services" / "provisioning" / "registry.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("RESOURCE_CONTRACTS", registry)

    def test_renderer_does_not_mutate_infra_spec(self):
        spec = _spec([_s3(), _ddb()])
        before = spec.model_dump(mode="json")
        render_infra_spec(spec)
        self.assertEqual(spec.model_dump(mode="json"), before)

    def test_no_hcl_library_imported(self):
        for module in ("hcl", "hcl2", "python_hcl2"):
            self.assertNotIn(module, sys.modules)

    def test_render_result_is_typed(self):
        result = render_infra_spec(_spec([_s3()]))
        self.assertIsInstance(result.status, RenderStatus)
        for entry in result.files:
            self.assertIsInstance(entry.path, str)
            self.assertIsInstance(entry.content, str)


if __name__ == "__main__":
    unittest.main()
