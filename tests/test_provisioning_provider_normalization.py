"""Regression: provider normalization ("AWS" -> "aws") in plan assembly.

The requirements pipeline may surface the cloud provider in provider
spelling ("AWS") while the verified module catalog declares the
normalized spelling ("aws"). ``build_infra_spec`` has always normalized
the provider, but plan assembly must feed the same normalized value into
module selection: passing the raw value made every contract look
provider-incompatible, refused the whole (all-or-nothing) selection,
emptied ``selection_id``, and stopped the Terraform tree from rendering.

These tests pin the fixed behavior at the plan level and through the
HTTP Terraform endpoint.

Run: .venv\\Scripts\\python.exe -m pytest tests/test_provisioning_provider_normalization.py -q
"""
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

GEN = datetime(2026, 10, 4, 12, 0, 0)
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


def _plan(provider):
    return build_provisioning_plan(
        architecture=_architecture(),
        provider=provider,
        approval=_approval(),
        inputs=_inputs(),
    )


class UppercaseProviderSelectionTests(unittest.TestCase):
    def test_uppercase_provider_binds_verified_modules(self):
        """Regression S1: "AWS" must select modules, not refuse them all."""
        plan = _plan("AWS")
        self.assertEqual(plan["provider"], "aws")
        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(
            sorted(module["module_id"] for module in plan["modules"]),
            ["nosql_table", "object_storage"],
        )
        modules_by_resource = {
            resource["resource_id"]: resource["module"]
            for resource in plan["resources"]
        }
        self.assertEqual(
            modules_by_resource["s3-s3-store"]["module_id"],
            "object_storage",
        )
        self.assertEqual(
            modules_by_resource["dynamodb-ddb-table"]["module_id"],
            "nosql_table",
        )

    def test_uppercase_provider_renders_plan(self):
        """The selection must reach the renderer, not stop cold."""
        plan = _plan("AWS")
        self.assertEqual(plan["render_status"], "rendered")
        self.assertEqual(plan["render_diagnostics"], [])
        self.assertTrue(plan["tree_hash"])
        self.assertIn("provider.tf", plan["files"])
        self.assertGreater(plan["inventory"]["creates"], 0)

    def test_provider_spelling_does_not_change_the_plan(self):
        """ "AWS" and "aws" are the same design: identical artifacts."""
        upper = _plan("AWS")
        lower = _plan("aws")
        self.assertEqual(upper["spec_id"], lower["spec_id"])
        self.assertEqual(upper["tree_hash"], lower["tree_hash"])
        self.assertEqual(upper["provider"], lower["provider"])
        self.assertEqual(upper["modules"], lower["modules"])
        self.assertEqual(upper["files"], lower["files"])
        self.assertEqual(upper["resources"], lower["resources"])
        self.assertEqual(
            upper["render_status"], lower["render_status"],
        )


class TerraformEndpointProviderCaseTests(unittest.TestCase):
    def _snapshot(self):
        return SimpleNamespace(
            session_id="design-s1",
            status="ready",
            error=None,
            requirements_state=SimpleNamespace(
                extraction=RequirementsExtraction(
                    application_description="store",
                    cloud_provider="AWS",
                    region="us-east-1",
                ),
            ),
            architecture=_architecture(),
            design_approval=_approval(),
            terraform_artifact=None,
        )

    def test_endpoint_renders_with_uppercase_cloud_provider(self):
        """Regression S1 over HTTP: extraction reports "AWS"."""
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
        self.assertEqual(body["provider"], "aws")
        self.assertEqual(body["selection_errors"], [])
        self.assertEqual(len(body["modules"]), 2)
        self.assertEqual(body["render_status"], "rendered")
        self.assertEqual(body["render_diagnostics"], [])
        self.assertTrue(body["files"])
        self.assertTrue(body["tree_hash"])
        self.assertEqual(saved["field"], "terraform_artifact")
        self.assertEqual(saved["record"]["artifact_id"], "artifact-1")


if __name__ == "__main__":
    unittest.main()
