"""Evidence for the four serverless full-stack module contracts.

Covers the full-stack profile end to end (spec -> defaults -> selection ->
render -> Terraform validate) and the implementation-level safety
properties of the CloudFront, Cognito, SQS and SNS modules. The catalog
contracts name this file as their ``evidence_ref``.

Run: .venv/Scripts/python -m unittest tests.test_provisioning_serverless_modules
"""
import pathlib
import re
import unittest
from datetime import date, datetime

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.module import (
    ModuleId,
    SourceKind,
    VerificationMethod,
    VerificationStatus,
)
from ai_architect.services.approvals import build_design_approval
from ai_architect.services.provisioning.demo_profile import (
    demo_profile_defaults,
)
from ai_architect.services.provisioning.module_catalog import CATALOG
from ai_architect.services.provisioning.module_registry import (
    build_default_registry,
)
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)
from ai_architect.services.provisioning.spec_builder import build_infra_spec

NOW = datetime(2026, 1, 1)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVIDER = "aws"
REGION = "us-east-1"

NEW_MODULES = {
    ModuleId.CLOUDFRONT_DISTRIBUTION: "infra/modules/aws-cloudfront-distribution",
    ModuleId.COGNITO_USER_POOL: "infra/modules/aws-cognito-user-pool",
    ModuleId.SQS_QUEUE: "infra/modules/aws-sqs-queue",
    ModuleId.SNS_TOPIC: "infra/modules/aws-sns-topic",
}

FULL_STACK_NODES = [
    ("api", "Amazon API Gateway (HTTP API)", "networking"),
    ("fn", "AWS Lambda", "compute"),
    ("table", "Amazon DynamoDB", "database"),
    ("bucket", "Amazon S3", "storage"),
    ("logs", "Amazon CloudWatch Logs", "monitoring"),
    ("cdn", "Amazon CloudFront", "networking"),
    ("auth", "Amazon Cognito", "security"),
    ("queue", "Amazon Simple Queue Service (SQS) Standard", "integration"),
    ("topic", "Amazon Simple Notification Service (SNS)", "integration"),
    ("alarm", "Amazon CloudWatch Alarms", "monitoring"),
]


def _node(nid, name, cat):
    return ArchitectureNode(
        node_id=nid, component_name=name, category=cat,
        role_in_system="role",
    )


def _architecture(nodes=None):
    nodes = nodes or FULL_STACK_NODES
    return ArchitectureResult(
        artifact_id="artifact-full",
        session_id="design-full",
        version=1,
        nodes=[_node(nid, name, cat) for nid, name, cat in nodes],
        edges=[],
        entry_points=["cdn"],
        data_store_node_ids=["table", "bucket"],
        decisions=[ArchitectureDecision(decision="d", rationale="r")],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id="req-1",
            capacity_formula_version="v1",
            selection_generated_at=NOW,
            cost_generated_at=NOW,
            cost_pricing_as_of=NOW,
            region_code=REGION,
            upstream_inputs_hash="hash-1",
        ),
        generated_at=NOW,
    )


def _module_file(relative: str, name: str) -> str:
    return (REPO_ROOT / relative / name).read_text(encoding="utf-8")


class FullStackProfileTests(unittest.TestCase):
    def setUp(self):
        self.spec = build_infra_spec(_architecture(), provider=PROVIDER)

    def test_profile_maps_all_ten_capabilities(self):
        self.assertEqual(
            {resource.kind.value for resource in self.spec.resources},
            {
                "apigateway_http",
                "lambda",
                "dynamodb",
                "s3",
                "cloudwatch_log_group",
                "cloudfront_distribution",
                "cognito_user_pool",
                "sqs_queue",
                "sns_topic",
                "cloudwatch_alarm",
            },
        )

    def test_defaults_cover_every_input_except_bucket_name(self):
        defaults = demo_profile_defaults(self.spec)
        values = {(item.resource_id, item.name) for item in defaults}
        self.assertEqual(len(defaults), 28)
        self.assertNotIn(
            ("s3-bucket", "bucket_name"), values,
            "a globally unique bucket name is never invented",
        )
        by_name = {
            (item.resource_id, item.name): item.value for item in defaults
        }
        self.assertEqual(
            by_name[("cognito_user_pool-auth", "policy_minimum_length")], "12"
        )
        self.assertEqual(
            by_name[("cloudfront_distribution-cdn", "price_class")],
            "PriceClass_100",
        )
        self.assertEqual(
            by_name[("sqs_queue-queue", "message_retention_seconds")], "86400"
        )
        self.assertEqual(
            by_name[("cloudwatch_alarm-alarm", "notification_target")], "none"
        )

    def test_a_different_shape_gets_no_defaults(self):
        nodes = FULL_STACK_NODES + [
            ("bucket2", "Amazon S3", "storage"),
        ]
        spec = build_infra_spec(_architecture(nodes), provider=PROVIDER)
        self.assertEqual(demo_profile_defaults(spec), ())


class FullStackPlanTests(unittest.TestCase):
    def test_full_stack_plan_renders_and_validates(self):
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=build_design_approval(
                approved=True, note="ok", artifact_id="artifact-full",
                gate_outcome="approved_for_review",
            ),
            inputs={
                "s3-bucket": {"bucket_name": "full-stack-test-bucket-1234"},
            },
        )

        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(plan["render_status"], "rendered")
        self.assertEqual(plan["render_diagnostics"], [])
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(len(plan["modules"]), 10)
        self.assertEqual(
            plan["terraform_validation"]["status"], "validate_ok"
        )
        main = plan["files"]["main.tf"]
        self.assertEqual(main.count('module "'), 10)

        self.assertIn(
            "origin_bucket_arn         = "
            "module.object_storage-s3-bucket.bucket_arn",
            main,
        )
        self.assertIn(
            "origin_bucket_domain_name = "
            "module.object_storage-s3-bucket.regional_domain_name",
            main,
        )
        self.assertIn(
            "subscriber_queue_arn = "
            "module.sqs_queue-sqs_queue-queue.queue_arn",
            main,
        )
        self.assertIn(
            "subscriber_queue_url = "
            "module.sqs_queue-sqs_queue-queue.queue_url",
            main,
        )
        self.assertIn("policy_minimum_length = 12", main)

        lambda_module = plan["files"][
            "infra/modules/aws-serverless-function/main.tf"
        ]
        self.assertIn("logging_config {", lambda_module)
        self.assertRegex(
            lambda_module, r"log_group\s*=\s*local\.log_group_name"
        )

        outputs = plan["files"]["outputs.tf"]
        for label, reference in (
            (
                "object_storage-s3-bucket__bucket_id",
                "module.object_storage-s3-bucket.bucket_id",
            ),
            (
                "cloudfront_distribution-cloudfront_distribution-cdn"
                "__distribution_domain_name",
                "module.cloudfront_distribution-"
                "cloudfront_distribution-cdn.distribution_domain_name",
            ),
            (
                "cognito_user_pool-cognito_user_pool-auth__user_pool_id",
                "module.cognito_user_pool-cognito_user_pool-auth.user_pool_id",
            ),
            (
                "sqs_queue-sqs_queue-queue__queue_url",
                "module.sqs_queue-sqs_queue-queue.queue_url",
            ),
            (
                "sns_topic-sns_topic-topic__topic_arn",
                "module.sns_topic-sns_topic-topic.topic_arn",
            ),
        ):
            self.assertIn(f'output "{label}"', outputs)
            self.assertIn(f"value = {reference}", outputs)

    def test_plan_contains_only_module_blocks_for_new_services(self):
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=build_design_approval(
                approved=True, note="ok", artifact_id="artifact-full",
                gate_outcome="approved_for_review",
            ),
            inputs={
                "s3-bucket": {"bucket_name": "full-stack-test-bucket-1234"},
            },
        )
        main = plan["files"]["main.tf"]
        for raw_type in (
            'resource "aws_cloudfront_distribution"',
            'resource "aws_cognito_user_pool"',
            'resource "aws_sqs_queue"',
            'resource "aws_sns_topic"',
        ):
            self.assertNotIn(raw_type, main)
        for relative in NEW_MODULES.values():
            self.assertIn(f"{relative}/main.tf", plan["files"])


class ServerlessModuleSourceTests(unittest.TestCase):
    def test_lambda_writes_to_the_managed_log_group(self):
        main = _module_file(
            "infra/modules/aws-serverless-function", "main.tf"
        )
        self.assertIn("logging_config {", main)
        self.assertIn('log_format = "Text"', main)
        self.assertRegex(main, r"log_group\s*=\s*local\.log_group_name")
        self.assertIn(
            'element(split(":", var.log_group_arn), 6)', main
        )
        self.assertIn('"${var.log_group_arn}:*"', main)

    def test_cloudfront_uses_oac_and_a_scoped_bucket_policy(self):
        main = _module_file(
            "infra/modules/aws-cloudfront-distribution", "main.tf"
        )
        self.assertIn('resource "aws_cloudfront_origin_access_control"', main)
        self.assertIn("origin_access_control_id", main)
        self.assertIn('resource "aws_s3_bucket_policy"', main)
        self.assertIn("AWS:SourceArn", main)
        self.assertIn("Managed-CachingOptimized", main)
        self.assertIn('viewer_protocol_policy = "redirect-to-https"', main)
        self.assertIn("bucket = var.origin_bucket_id", main)

    def test_cognito_is_a_standalone_user_pool(self):
        main = _module_file(
            "infra/modules/aws-cognito-user-pool", "main.tf"
        )
        self.assertIn('resource "aws_cognito_user_pool"', main)
        self.assertIn("allow_admin_create_user_only", main)
        self.assertRegex(main, r"generate_secret\s*=\s*false")
        self.assertIn("ALLOW_USER_SRP_AUTH", main)
        self.assertIn("software_token_mfa_configuration {", main)
        self.assertIn(
            'enabled = var.mfa_configuration != "OFF"', main
        )
        self.assertNotIn("aws_cognito_identity_pool", main)
        self.assertNotIn("aws_cognito_user_pool_domain", main)

    def test_sqs_is_a_standard_encrypted_queue_without_a_trigger(self):
        main = _module_file("infra/modules/aws-sqs-queue", "main.tf")
        self.assertIn('resource "aws_sqs_queue"', main)
        self.assertRegex(main, r"sqs_managed_sse_enabled\s*=\s*true")
        self.assertNotIn("fifo_queue", main)
        self.assertNotIn("aws_lambda_event_source_mapping", main)

    def test_sns_fans_out_to_the_queue_with_a_source_arn_condition(self):
        main = _module_file("infra/modules/aws-sns-topic", "main.tf")
        variables = _module_file(
            "infra/modules/aws-sns-topic", "variables.tf"
        )
        self.assertIn('resource "aws_sns_topic"', main)
        self.assertIn('resource "aws_sns_topic_subscription"', main)
        self.assertRegex(main, r"raw_message_delivery\s*=\s*true")
        self.assertRegex(main, r'protocol\s*=\s*"sqs"')
        self.assertIn('resource "aws_sqs_queue_policy"', main)
        self.assertIn("sqs:SendMessage", main)
        self.assertIn("ArnEquals", main)
        self.assertIn("aws:SourceArn", main)
        self.assertIn('variable "subscriber_queue_url"', variables)
        self.assertNotRegex(main, r'protocol\s*=\s*"email"')


class ServerlessContractTests(unittest.TestCase):
    def test_new_contracts_are_verified_with_recorded_evidence(self):
        registry = build_default_registry()
        for module_id, relative in NEW_MODULES.items():
            with self.subTest(module=module_id.value):
                contract = registry.get(module_id)
                self.assertEqual(contract.provider, PROVIDER)
                self.assertEqual(contract.source_kind, SourceKind.LOCAL_PATH)
                self.assertEqual(contract.source, f"../../{relative}")
                self.assertEqual(contract.version, "1.0.0")
                self.assertEqual(contract.contract_version, "1.0.0")
                self.assertEqual(
                    contract.verification.status,
                    VerificationStatus.VERIFIED,
                )
                self.assertEqual(
                    contract.verification.verified_on, date(2026, 10, 20)
                )
                self.assertEqual(
                    contract.verification.evidence_ref,
                    "tests/test_provisioning_serverless_modules.py",
                )
                self.assertIn(
                    VerificationMethod.TESTED_CONTRACT,
                    contract.verification.methods,
                )
                self.assertTrue(contract.verification.limitations)
                self.assertTrue(contract.inputs)
                self.assertTrue(contract.outputs)

    def test_s3_exposes_the_regional_domain_cloudfront_consumes(self):
        registry = build_default_registry()
        contract = registry.get(ModuleId.OBJECT_STORAGE)
        self.assertEqual(contract.contract_version, "1.1.0")
        self.assertIn(
            "regional_domain_name",
            {output.name for output in contract.outputs},
        )

    def test_catalog_contains_exactly_the_ten_verified_modules(self):
        self.assertEqual({contract.module_id for contract in CATALOG}, set(
            ModuleId
        ))


if __name__ == "__main__":
    unittest.main()
