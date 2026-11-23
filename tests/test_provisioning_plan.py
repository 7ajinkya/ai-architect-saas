"""Provisioning plan assembly tests: approval gate, inputs, inventory.

No network, no terraform binary required (validation absence is an
honest status, never a failure). No LLM calls.

Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from ai_architect.domain.architecture.models import (
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.approvals import build_design_approval
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)
from ai_architect.services.provisioning.spec_builder import (
    ProvisioningError,
    apply_supplied_inputs,
    build_infra_spec,
)

GEN = datetime(2026, 10, 4, 12, 0, 0)
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


def _node(node_id, component_name, category):
    return ArchitectureNode(
        node_id=node_id,
        component_name=component_name,
        category=category,
        role_in_system="role",
    )


def _architecture():
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id="design-s1",
        version=1,
        nodes=[
            _node("s3-store", "Amazon Simple Storage Service", "storage"),
            _node("ddb-table", "Amazon DynamoDB", "database"),
        ],
        edges=[],
        entry_points=[],
        data_store_node_ids=["s3-store", "ddb-table"],
        boundaries=[],
        cost_links=[],
        cost_totals_snapshot={},
        decisions=[],
        assumptions=[],
        warnings=[],
        unresolved_tradeoffs=[],
        evidence_references=[],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id="req-s1",
            capacity_formula_version="v1",
            selection_generated_at=GEN,
            cost_generated_at=GEN,
            cost_pricing_as_of=GEN,
            region_code=REGION,
            upstream_inputs_hash="hash-1",
        ),
        generated_at=GEN,
    )


def _demo_architecture():
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id="design-s1",
        version=1,
        nodes=[
            _node(
                "api",
                "Amazon API Gateway (HTTP API)",
                "networking",
            ),
            _node("fn", "AWS Lambda", "compute"),
            _node("table", "Amazon DynamoDB", "database"),
            _node("logs", "Amazon CloudWatch Logs", "monitoring"),
        ],
        edges=[],
        entry_points=["api"],
        data_store_node_ids=["table"],
        boundaries=[],
        cost_links=[],
        cost_totals_snapshot={},
        decisions=[],
        assumptions=[],
        warnings=[],
        unresolved_tradeoffs=[],
        evidence_references=[],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id="req-s1",
            capacity_formula_version="v1",
            selection_generated_at=GEN,
            cost_generated_at=GEN,
            cost_pricing_as_of=GEN,
            region_code=REGION,
            upstream_inputs_hash="hash-1",
        ),
        generated_at=GEN,
    )


def _approval(artifact_id="artifact-1"):
    return build_design_approval(
        approved=True, note="ok", artifact_id=artifact_id,
        gate_outcome="approved_for_review",
    )


def _inputs():
    return {
        "s3-s3-store": dict(S3_VALUES),
        "dynamodb-ddb-table": dict(DDB_VALUES),
    }


class ApprovalGateTests(unittest.TestCase):
    def test_missing_approval_refuses_before_terraform(self):
        with self.assertRaises(ValueError) as ctx:
            build_provisioning_plan(
                architecture=_architecture(), provider=PROVIDER,
                approval=None,
            )
        self.assertIn("approved design", str(ctx.exception))

    def test_rejected_approval_refuses(self):
        record = build_design_approval(
            approved=False, note="no", artifact_id="artifact-1",
            gate_outcome="approved_for_review",
        )
        with self.assertRaises(ValueError):
            build_provisioning_plan(
                architecture=_architecture(), provider=PROVIDER,
                approval=record,
            )

    def test_artifact_mismatch_refuses(self):
        with self.assertRaises(ValueError) as ctx:
            build_provisioning_plan(
                architecture=_architecture(), provider=PROVIDER,
                approval=_approval(artifact_id="other"),
            )
        self.assertIn("different architecture", str(ctx.exception))


class SuppliedInputsTests(unittest.TestCase):
    def test_unknown_resource_fails_loudly(self):
        spec = build_infra_spec(_architecture(), provider=PROVIDER)
        with self.assertRaises(ValueError) as ctx:
            apply_supplied_inputs(spec, {"nope-x": {"a": "b"}})
        self.assertIn("unknown resource", str(ctx.exception))

    def test_inputs_change_spec_identity(self):
        spec = build_infra_spec(_architecture(), provider=PROVIDER)
        updated = apply_supplied_inputs(spec, _inputs())
        self.assertNotEqual(updated.spec_id, spec.spec_id)
        # Idempotent for identical inputs.
        again = apply_supplied_inputs(spec, _inputs())
        self.assertEqual(again.spec_id, updated.spec_id)


class PlanAssemblyTests(unittest.TestCase):
    def test_pending_inputs_surface_without_inputs(self):
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(),
        )
        pending = [
            (resource["resource_id"], item["name"])
            for resource in plan["resources"]
            for item in resource["pending_inputs"]
        ]
        self.assertIn(("s3-s3-store", "bucket_name"), pending)
        self.assertIn(("dynamodb-ddb-table", "billing_mode"), pending)
        self.assertEqual(plan["artifact_id"], "artifact-1")

    def test_valid_inputs_bind_verified_modules(self):
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=_inputs(),
        )
        by_resource = {
            resource["resource_id"]: resource
            for resource in plan["resources"]
        }
        self.assertEqual(
            by_resource["s3-s3-store"]["module"]["module_id"],
            "object_storage",
        )
        self.assertEqual(
            by_resource["dynamodb-ddb-table"]["module"]["module_id"],
            "nosql_table",
        )
        self.assertIsNone(plan["inventory"]["destroys"])
        self.assertGreater(plan["inventory"]["creates"], 0)
        self.assertIn("s3", plan["inventory"]["by_kind"])
        self.assertIn("dynamodb", plan["inventory"]["by_kind"])
        self.assertEqual(plan["inventory"]["analysis"], "static_inventory")
        self.assertTrue(plan["tree_hash"])
        self.assertIn("files", plan)
        # Terraform validation is honest about a missing binary.
        self.assertIn(
            plan["terraform_validation"]["status"],
            ("validate_ok", "not_installed", "validate_failed",
             "init_failed", "fmt_failed"),
        )

    def test_selection_is_deterministic(self):
        first = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=_inputs(),
        )
        second = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=_inputs(),
        )
        self.assertEqual(first["spec_id"], second["spec_id"])
        self.assertEqual(first["tree_hash"], second["tree_hash"])


class PriorInputsTests(unittest.TestCase):
    """Regeneration carries accepted human inputs without losing them."""

    def test_prior_inputs_carry_forward_to_regenerated_plan(self):
        first = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=_inputs(),
        )
        self.assertEqual(first["inputs"], _inputs())
        second = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), prior_inputs=first["inputs"],
        )
        self.assertEqual(second["inputs"], _inputs())
        self.assertEqual(second["input_warnings"], [])
        pending = [
            item["name"]
            for resource in second["resources"]
            for item in resource["pending_inputs"]
        ]
        self.assertEqual(pending, [])
        self.assertEqual(second["spec_id"], first["spec_id"])
        self.assertEqual(second["tree_hash"], first["tree_hash"])

    def test_current_inputs_override_carried_values(self):
        prior = {
            "s3-s3-store": dict(S3_VALUES, bucket_name="old-bucket"),
            "dynamodb-ddb-table": dict(DDB_VALUES),
        }
        current = {
            "s3-s3-store": dict(S3_VALUES, bucket_name="new-bucket"),
        }
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=current, prior_inputs=prior,
        )
        self.assertEqual(
            plan["inputs"]["s3-s3-store"]["bucket_name"], "new-bucket"
        )
        self.assertIn(
            "new-bucket", plan["files"]["main.tf"]
        )

    def test_stale_prior_inputs_are_dropped_with_warnings(self):
        prior = {
            "ghost-resource": {"bucket_name": "x"},
            "s3-s3-store": {
                **S3_VALUES,
                "retired_input": "y",
            },
            "dynamodb-ddb-table": dict(DDB_VALUES),
        }
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), prior_inputs=prior,
        )
        self.assertEqual(
            plan["inputs"],
            {
                "s3-s3-store": dict(S3_VALUES),
                "dynamodb-ddb-table": dict(DDB_VALUES),
            },
        )
        warnings = " ".join(plan["input_warnings"])
        self.assertIn("ghost-resource", warnings)
        self.assertIn("retired_input", warnings)

    def test_demo_lambda_artifact_is_not_persisted_as_user_input(self):
        architecture = _architecture()
        architecture.nodes.append(
            _node("fn", "AWS Lambda", "compute")
        )
        plan = build_provisioning_plan(
            architecture=architecture, provider=PROVIDER,
            approval=_approval(),
        )
        lambda_keys = plan["inputs"].get("lambda-fn", {})
        self.assertNotIn("code_artifact_path", lambda_keys)
        self.assertNotIn("code_artifact_sha256", lambda_keys)


class TerraformEndpointTests(unittest.TestCase):
    def _snapshot(self, architecture=None):
        return SimpleNamespace(
            session_id="design-s1",
            status="ready",
            error=None,
            requirements_state=SimpleNamespace(
                extraction=RequirementsExtraction(
                    application_description="store",
                    cloud_provider="aws",
                    region="us-east-1",
                ),
            ),
            architecture=architecture or _architecture(),
            design_approval=_approval(),
            terraform_artifact=None,
        )

    def test_terraform_endpoint_stores_and_serves_plan(self):
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["field"] = field
            saved["record"] = record

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "save_session_record", fake_save),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform",
                json={"inputs": _inputs()},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["artifact_id"], "artifact-1")
        self.assertIn("inventory", body)
        self.assertIsNone(body["inventory"]["destroys"])
        self.assertEqual(body["input_defaults"], [])
        self.assertEqual(saved["field"], "terraform_artifact")
        self.assertEqual(
            saved["record"]["artifact_id"], "artifact-1"
        )

    def test_terraform_endpoint_serves_deterministic_defaults(self):
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        with (
            patch.object(
                main_module,
                "load_design_snapshot",
                return_value=self._snapshot(_demo_architecture()),
            ),
            patch.object(
                main_module,
                "save_session_record",
                side_effect=lambda *a, **k: None,
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform",
                json={"inputs": {}},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "valid")
        self.assertEqual(len(body["input_defaults"]), 9)
        self.assertTrue(
            all(
                item["source"] == "deterministic_default"
                and item["requires_confirmation"] is True
                and item["rationale"]
                for item in body["input_defaults"]
            )
        )
        self.assertEqual(
            body["inputs"]["dynamodb-table"]["attribute_definitions"],
            "pk:S",
        )
        self.assertEqual(
            body["inputs"]["dynamodb-table"]["key_schema"],
            "HASH:pk",
        )

    def test_terraform_endpoint_serves_pending_diagnostics_verbatim(self):
        """Regression: an incomplete plan (no inputs supplied) must
        serialize its structured diagnostics, not crash with a 500.

        The UI's first Generate click sends {} and relies on the
        pending-input list in the response; every diagnostic is a
        {category, message} record, never a bare string.
        """
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(
                main_module, "save_session_record",
                side_effect=lambda *a, **k: None,
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform",
                json={"inputs": {}},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "incomplete")
        self.assertTrue(body["spec_diagnostics"])
        for item in (
            body["spec_diagnostics"] + body["render_diagnostics"]
        ):
            self.assertIn("category", item)
            self.assertIn("message", item)
            self.assertTrue(item["message"])
        pending = [
            (resource["resource_id"], item["name"])
            for resource in body["resources"]
            for item in resource["pending_inputs"]
        ]
        self.assertIn(("s3-s3-store", "bucket_name"), pending)

    def test_terraform_refused_without_approval(self):
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        snapshot = self._snapshot()
        snapshot.design_approval = None
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform", json={}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "terraform_not_allowed"
        )

    def test_terraform_read_serves_stored_plan(self):
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=_inputs(),
        )
        snapshot = self._snapshot()
        snapshot.terraform_artifact = plan
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = client.get("/design/sessions/design-s1/terraform")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["spec_id"], plan["spec_id"])

    def test_terraform_read_missing_is_404(self):
        from tests.api_auth import api_client

        from ai_architect.api import main as main_module
        from ai_architect.api.main import create_app

        client = api_client(create_app())
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=self._snapshot(),
        ):
            response = client.get("/design/sessions/design-s1/terraform")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"]["code"], "terraform_not_available"
        )


class StructuredInputPlanTests(unittest.TestCase):
    def test_json_dynamodb_inputs_render_valid_plan(self):
        inputs = _inputs()
        inputs["dynamodb-ddb-table"]["attribute_definitions"] = json.dumps(
            [
                {"attribute_name": "pk", "attribute_type": "S"},
                {"attribute_name": "sk", "attribute_type": "N"},
            ]
        )
        inputs["dynamodb-ddb-table"]["key_schema"] = json.dumps(
            [
                {"attribute_name": "pk", "key_type": "HASH"},
                {"attribute_name": "sk", "key_type": "RANGE"},
            ]
        )
        plan = build_provisioning_plan(
            architecture=_architecture(), provider=PROVIDER,
            approval=_approval(), inputs=inputs,
        )
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(
            plan["inputs"]["dynamodb-ddb-table"]["attribute_definitions"],
            "pk:S,sk:N",
        )
        self.assertEqual(
            plan["inputs"]["dynamodb-ddb-table"]["key_schema"],
            "HASH:pk,RANGE:sk",
        )

    def test_malformed_structured_input_is_refused(self):
        inputs = _inputs()
        inputs["dynamodb-ddb-table"]["attribute_definitions"] = (
            '[{"attribute_name": "pk", "attribute_type": "S", "extra": 1}]'
        )
        with self.assertRaises(ProvisioningError) as ctx:
            build_provisioning_plan(
                architecture=_architecture(), provider=PROVIDER,
                approval=_approval(), inputs=inputs,
            )
        self.assertEqual(ctx.exception.code, "malformed_parameter")
        self.assertIn("must contain exactly", str(ctx.exception))


class TerraformCliTests(unittest.TestCase):
    def test_parser_wires_terraform_subcommand(self):
        from ai_architect.cli.main import build_parser

        args = build_parser().parse_args(
            ["terraform", "--session-id", "design-s1",
             "--set", "s3-s3-store.bucket_name=my-design-bucket"]
        )
        self.assertEqual(args.command, "terraform")
        self.assertEqual(args.session_id, "design-s1")
        self.assertEqual(
            args.set, ["s3-s3-store.bucket_name=my-design-bucket"]
        )

    def test_set_parser_rejects_malformed_pairs(self):
        from ai_architect.cli.terraform import _parse_set

        with self.assertRaises(ValueError):
            _parse_set(["no-equals-here"])
        with self.assertRaises(ValueError):
            _parse_set(["noperiod=value"])
        self.assertEqual(
            _parse_set(["r1.bucket_name=my-design-bucket"]),
            {"r1": {"bucket_name": "my-design-bucket"}},
        )


if __name__ == "__main__":
    unittest.main()
