"""Slice 2 tests: Module Selection and Binding Reliability.

Covers the demo Lambda artifact auto-supply in the provisioning plan,
per-resource diagnostics, and the all-or-nothing contract (documented
as intentional).

Run: .venv/Scripts/python -m unittest tests.test_provisioning_slice2
"""
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
    ParameterSource,
    ResourceKind,
)
from ai_architect.services.approvals import build_design_approval
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)
from ai_architect.services.provisioning.spec_builder import (
    demo_lambda_artifact,
    merge_demo_lambda_artifact_inputs,
)

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"


def _node(nid, name, cat="compute"):
    return ArchitectureNode(
        node_id=nid, component_name=name, category=cat,
        role_in_system="role",
    )


def _architecture(nodes=None):
    nodes = nodes or [
        ("api", "Amazon API Gateway (HTTP API)", "networking"),
        ("fn", "AWS Lambda", "compute"),
        ("table", "Amazon DynamoDB", "database"),
        ("bucket", "Amazon S3", "storage"),
        ("logs", "Amazon CloudWatch Logs", "monitoring"),
    ]
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id="design-s1",
        version=1,
        nodes=[_node(nid, name, cat) for nid, name, cat in nodes],
        edges=[],
        entry_points=["api"],
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


def _approval():
    return build_design_approval(
        approved=True, note="ok", artifact_id="artifact-1",
        gate_outcome="approved_for_review",
    )


class DemoArtifactAutoSupplyTests(unittest.TestCase):
    """Demo Lambda artifact is auto-supplied when inputs are absent."""

    def test_demo_lambda_artifact_returns_tuple(self):
        result = demo_lambda_artifact()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "demo-handler.zip")
        self.assertEqual(len(result[1]), 64)

    def test_plan_auto_supplies_code_artifact_for_lambda(self):
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=_approval(),
            inputs={},
        )
        lambda_res = next(
            r for r in plan["resources"]
            if r["kind"] == "lambda"
        )
        pending = {p["name"] for p in lambda_res["pending_inputs"]}
        self.assertNotIn("code_artifact_path", pending)
        self.assertNotIn("code_artifact_sha256", pending)
        # Other Lambda inputs remain pending
        self.assertIn("runtime", pending)
        self.assertIn("handler", pending)

    def test_plan_does_not_override_user_code_artifact(self):
        user_inputs = {
            "lambda-fn": {
                "code_artifact_path": "custom.zip",
                "code_artifact_sha256": "a" * 64,
            }
        }
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=_approval(),
            inputs=user_inputs,
        )
        lambda_res = next(
            r for r in plan["resources"]
            if r["kind"] == "lambda"
        )
        # User values are applied; code_artifact_path should not be pending
        pending = {p["name"] for p in lambda_res["pending_inputs"]}
        self.assertNotIn("code_artifact_path", pending)
        self.assertNotIn("code_artifact_sha256", pending)

    def test_plan_preserves_missing_inputs_for_other_resources(self):
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=_approval(),
            inputs={},
        )
        s3_res = next(
            r for r in plan["resources"] if r["kind"] == "s3"
        )
        self.assertEqual(
            {p["name"] for p in s3_res["pending_inputs"]},
            {"bucket_name", "versioning", "encryption", "public_access_block"},
        )
        ddb_res = next(
            r for r in plan["resources"] if r["kind"] == "dynamodb"
        )
        self.assertEqual(
            {p["name"] for p in ddb_res["pending_inputs"]},
            {"billing_mode", "attribute_definitions", "key_schema",
             "point_in_time_recovery"},
        )

    def test_plan_status_incomplete_without_all_inputs(self):
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=_approval(),
            inputs={},
        )
        self.assertEqual(plan["status"], "incomplete")
        self.assertTrue(plan["selection_errors"])
        self.assertTrue(
            any(
                "Required module input" in error
                for error in plan["selection_errors"]
            )
        )
        self.assertNotIn(
            "Selection has no selection_id.", plan["selection_errors"]
        )

    def test_plan_with_all_inputs_supplied_succeeds(self):
        S3_VALUES = {
            "bucket_name": "my-bucket",
            "versioning": "enabled",
            "encryption": "aes256",
            "public_access_block": "all_blocked",
        }
        DDB_VALUES = {
            "billing_mode": "PAY_PER_REQUEST",
            "attribute_definitions": "pk:S",
            "key_schema": "HASH:pk",
            "point_in_time_recovery": "true",
        }
        LAMBDA_VALUES = {
            "runtime": "python3.12",
            "handler": "index.handler",
                    "memory_mb": "512",
            "timeout_seconds": "30",
        }
        inputs = {
            "s3-bucket": S3_VALUES,
            "dynamodb-table": DDB_VALUES,
            "lambda-fn": LAMBDA_VALUES,
            "cloudwatch_log_group-logs": {"retention_days": "30"},
        }
        plan = build_provisioning_plan(
            architecture=_architecture(),
            provider=PROVIDER,
            approval=_approval(),
            inputs=inputs,
        )
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(plan["selection_errors"], [])
        self.assertTrue(plan["modules"])


class AllOrNothingContractTests(unittest.TestCase):
    """Document the intentional all-or-nothing contract."""

    def test_all_or_nothing_is_intentional_contract(self):
        from ai_architect.services.provisioning.module_selection import (
            select_modules,
        )
        from ai_architect.services.provisioning.spec_builder import (
            build_infra_spec,
        )
        from ai_architect.services.provisioning.module_registry import (
            build_default_registry,
        )

        arch = _architecture()
        spec = build_infra_spec(arch, provider=PROVIDER)
        selection = select_modules(
            spec, provider=PROVIDER, registry=build_default_registry()
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        self.assertEqual(selection.selection_id, "")
        codes = {d.code.value for d in selection.diagnostics}
        self.assertIn("module_input_required", codes)


class MergeDemoArtifactTests(unittest.TestCase):
    """Unit tests for merge_demo_lambda_artifact_inputs."""

    def test_merge_empty_supplied(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="lambda-fn", node_id="fn",
                kind=ResourceKind.LAMBDA, component_name="AWS Lambda",
            )],
        )
        merged = merge_demo_lambda_artifact_inputs({}, spec)
        self.assertIn("lambda-fn", merged)
        self.assertEqual(
            merged["lambda-fn"]["code_artifact_path"], "demo-handler.zip"
        )

    def test_merge_does_not_mutate_caller(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="lambda-fn", node_id="fn",
                kind=ResourceKind.LAMBDA, component_name="AWS Lambda",
            )],
        )
        original = {}
        merged = merge_demo_lambda_artifact_inputs(original, spec)
        self.assertEqual(original, {})
        self.assertNotEqual(merged, original)

    def test_merge_user_values_win(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="lambda-fn", node_id="fn",
                kind=ResourceKind.LAMBDA, component_name="AWS Lambda",
            )],
        )
        user = {"lambda-fn": {
            "code_artifact_path": "custom.zip",
            "code_artifact_sha256": "a" * 64,
        }}
        merged = merge_demo_lambda_artifact_inputs(user, spec)
        self.assertEqual(merged["lambda-fn"]["code_artifact_path"], "custom.zip")

    def test_merge_partial_auto_supplies_missing(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="lambda-fn", node_id="fn",
                kind=ResourceKind.LAMBDA, component_name="AWS Lambda",
            )],
        )
        partial = {"lambda-fn": {"runtime": "python3.12"}}
        merged = merge_demo_lambda_artifact_inputs(partial, spec)
        self.assertEqual(merged["lambda-fn"]["runtime"], "python3.12")
        self.assertEqual(
            merged["lambda-fn"]["code_artifact_path"], "demo-handler.zip"
        )

    def test_merge_no_auto_supply_when_path_present(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="lambda-fn", node_id="fn",
                kind=ResourceKind.LAMBDA, component_name="AWS Lambda",
            )],
        )
        partial = {"lambda-fn": {"code_artifact_path": "custom.zip"}}
        merged = merge_demo_lambda_artifact_inputs(partial, spec)
        self.assertNotIn("code_artifact_sha256", merged["lambda-fn"])

    def test_merge_skips_non_lambda(self):
        from ai_architect.domain.provisioning.models import (
            InfraSpec, ResourceInstance, ResourceKind, SourceArchitectureRef,
        )
        spec = InfraSpec(
            spec_id="t", provider="aws", region_code="us-east-1",
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()), version=1, session_id="s",
                upstream_inputs_hash="h", architecture_hash="a",
            ),
            resources=[ResourceInstance(
                resource_id="s3-bucket", node_id="bucket",
                kind=ResourceKind.S3, component_name="Amazon S3",
            )],
        )
        merged = merge_demo_lambda_artifact_inputs({}, spec)
        self.assertEqual(merged, {})


if __name__ == "__main__":
    unittest.main()
