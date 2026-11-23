"""Regression tests: demo Lambda handler <-> DynamoDB ``pk`` key contract.

Production evidence (verified, not re-derivable offline): the deployed
table's schema is a string partition key ``pk`` with no sort key. The
canonical handler source is ``infra/lambda/demo_handler.py``; the
deterministic builder ``infra/lambda/build_demo_artifact.py`` packages
it as ``handler.py`` inside ``infra/lambda/dist/demo-handler.zip``.

These tests pin, without any AWS call, credentials, or Terraform run:

- the generated handler reads with ``Key={"pk": <identifier>}``;
- PUT/POST stores the identifier under ``pk``, never ``id``;
- HTTP behavior: 400 missing identifier, 404 missing item, 400 invalid
  JSON, 400 non-object body, 405 unsupported method;
- TABLE_NAME comes from the environment only (never hard-coded);
- the built artifact contains the corrected code and only the
  ``handler.py`` entry;
- the demo profile's configured entry point matches the artifact
  (``handler.handler`` <-> ``handler.py``) and the module wires the
  table name into the Lambda TABLE_NAME environment variable.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_demo_handler_artifact.py
"""
import importlib.util
import json
import os
import pathlib
import re
import unittest
import uuid
import zipfile
from datetime import datetime
from unittest.mock import patch

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ParameterSource,
    PendingInput,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
)
from ai_architect.services.approvals import build_design_approval
from ai_architect.services.provisioning.demo_profile import (
    demo_profile_defaults,
)
from ai_architect.services.provisioning.deployment_naming import (
    deployment_namespace,
)
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)
from ai_architect.services.provisioning.spec_builder import (
    demo_lambda_artifact,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LAMBDA_DIR = REPO_ROOT / "infra" / "lambda"
SOURCE = LAMBDA_DIR / "demo_handler.py"
BUILDER_PATH = LAMBDA_DIR / "build_demo_artifact.py"
DIST_ARTIFACT = LAMBDA_DIR / "dist" / "demo-handler.zip"
SERVERLESS_MAIN_TF = (
    REPO_ROOT / "infra" / "modules" / "aws-serverless-function" / "main.tf"
)

ENTRY_NAME = "handler.py"
DEPLOYED_TABLE_NAME = "dynamodb-amazon-dynamodb-on-demand-pitr-enabled"
TEST_TABLE_NAME = "demo-table-under-test"

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        f"_build_demo_artifact_{uuid.uuid4().hex}", BUILDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_to(directory: pathlib.Path):
    return _load_builder().build_artifact(SOURCE, directory)


class _FakeTable:
    """Records the exact DynamoDB calls the handler makes."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.get_item_keys = []
        self.put_items = []

    def get_item(self, Key):
        self.get_item_keys.append(Key)
        item = self.items.get(Key.get("pk")) if isinstance(Key, dict) else None
        return {"Item": item} if item is not None else {}

    def put_item(self, Item):
        self.put_items.append(Item)
        self.items[Item.get("pk")] = dict(Item)
        return {}


class _FakeResource:
    def __init__(self, table):
        self.table = table
        self.requested_names = []

    def Table(self, name):
        self.requested_names.append(name)
        return self.table


def _generated_handler_module(directory: pathlib.Path):
    """Extract handler.py from a fresh artifact build and import it."""
    target, _ = _build_to(directory)
    with zipfile.ZipFile(target) as bundle:
        content = bundle.read(ENTRY_NAME)
    extracted = directory / ENTRY_NAME
    extracted.write_bytes(content)
    spec = importlib.util.spec_from_file_location(
        f"_generated_handler_{uuid.uuid4().hex}", extracted
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(method, *, path_id=None, query_id=None, body=None):
    event = {
        "requestContext": {"http": {"method": method}},
        "pathParameters": {} if path_id is None else {"id": path_id},
        "queryStringParameters": None if query_id is None else {"id": query_id},
    }
    if body is not None:
        event["body"] = body
    return event


class GeneratedHandlerExecutionTests(unittest.TestCase):
    """Execute the generated handler with boto3 stubbed; no AWS calls."""

    def setUp(self):
        import shutil
        import tempfile

        self.directory = pathlib.Path(
            tempfile.mkdtemp(prefix="demo-handler-test-")
        )
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.module = _generated_handler_module(self.directory)
        self.table = _FakeTable()
        self.resource = _FakeResource(self.table)
        boto_patcher = patch("boto3.resource", return_value=self.resource)
        boto_patcher.start()
        self.addCleanup(boto_patcher.stop)
        env_patcher = patch.dict(os.environ, {"TABLE_NAME": TEST_TABLE_NAME})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_get_reads_by_pk(self):
        self.table.items = {"abc": {"pk": "abc", "note": "stored"}}
        response = self.module.handler(
            _event("GET", path_id="abc"), None
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            json.loads(response["body"]), {"pk": "abc", "note": "stored"}
        )
        self.assertEqual(self.table.get_item_keys, [{"pk": "abc"}])

    def test_get_accepts_query_string_identifier_with_pk_key(self):
        self.table.items = {"q-1": {"pk": "q-1"}}
        response = self.module.handler(
            _event("GET", query_id="q-1"), None
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.table.get_item_keys, [{"pk": "q-1"}])

    def test_get_missing_item_is_404(self):
        response = self.module.handler(
            _event("GET", path_id="absent"), None
        )
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(json.loads(response["body"]), {"error": "not found"})
        self.assertEqual(self.table.get_item_keys, [{"pk": "absent"}])

    def test_put_stores_identifier_under_pk(self):
        response = self.module.handler(
            _event("PUT", path_id="xyz", body=json.dumps({"note": "hello"})),
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.table.put_items, [{"note": "hello", "pk": "xyz"}])
        self.assertEqual(self.table.items["xyz"]["pk"], "xyz")
        self.assertEqual(json.loads(response["body"])["pk"], "xyz")

    def test_post_stores_identifier_under_pk(self):
        response = self.module.handler(
            _event("POST", path_id="xyz", body=json.dumps({"note": "hello"})),
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.table.put_items, [{"note": "hello", "pk": "xyz"}])

    def test_put_overwrites_client_supplied_pk_with_path_identifier(self):
        response = self.module.handler(
            _event(
                "PUT", path_id="real-id",
                body=json.dumps({"pk": "spoofed", "note": "x"}),
            ),
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.table.put_items[0]["pk"], "real-id")

    def test_missing_identifier_is_400(self):
        for event in (
            _event("GET"),
            _event("GET", path_id=""),
            _event("PUT", path_id="", body="{}"),
        ):
            with self.subTest(event=event):
                response = self.module.handler(event, None)
                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(
                    json.loads(response["body"]), {"error": "missing id"}
                )
        self.assertEqual(self.resource.requested_names, [])
        self.assertEqual(self.table.get_item_keys, [])
        self.assertEqual(self.table.put_items, [])

    def test_invalid_json_is_400(self):
        response = self.module.handler(
            _event("PUT", path_id="abc", body="{not json"), None
        )
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(
            json.loads(response["body"]), {"error": "invalid JSON body"}
        )
        self.assertEqual(self.table.put_items, [])

    def test_non_object_body_is_400(self):
        response = self.module.handler(
            _event("PUT", path_id="abc", body="[1, 2]"), None
        )
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(
            json.loads(response["body"]), {"error": "object body required"}
        )
        self.assertEqual(self.table.put_items, [])

    def test_unsupported_method_is_405(self):
        response = self.module.handler(
            _event("DELETE", path_id="abc"), None
        )
        self.assertEqual(response["statusCode"], 405)
        self.assertEqual(
            json.loads(response["body"]), {"error": "method not allowed"}
        )
        self.assertEqual(self.table.get_item_keys, [])
        self.assertEqual(self.table.put_items, [])

    def test_table_name_comes_only_from_environment(self):
        self.module.handler(_event("GET", path_id="abc"), None)
        self.assertEqual(self.resource.requested_names, [TEST_TABLE_NAME])


class GeneratedArtifactContentTests(unittest.TestCase):
    """The generated package carries the corrected handler."""

    def setUp(self):
        import shutil
        import tempfile

        self.directory = pathlib.Path(
            tempfile.mkdtemp(prefix="demo-artifact-test-")
        )
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_artifact_contains_only_handler_entry(self):
        target, digest = _build_to(self.directory)
        self.assertEqual(target.name, "demo-handler.zip")
        self.assertEqual(len(digest), 64)
        with zipfile.ZipFile(target) as bundle:
            self.assertEqual(bundle.namelist(), [ENTRY_NAME])
            self.assertEqual(bundle.read(ENTRY_NAME), SOURCE.read_bytes())

    def test_artifact_handler_uses_pk_for_reads_and_writes(self):
        target, _ = _build_to(self.directory)
        with zipfile.ZipFile(target) as bundle:
            content = bundle.read(ENTRY_NAME).decode("utf-8")
        self.assertIn('Key={"pk": item_id}', content)
        self.assertIn('body["pk"] = item_id', content)
        self.assertNotIn('Key={"id"', content)
        self.assertNotIn('body["id"] = item_id', content)
        self.assertNotIn(DEPLOYED_TABLE_NAME, content)

    def test_artifact_build_is_deterministic(self):
        first, first_digest = _build_to(self.directory / "a")
        second, second_digest = _build_to(self.directory / "b")
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_canonical_source_uses_pk_and_never_hardcodes_the_table(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('Key={"pk": item_id}', source)
        self.assertIn('body["pk"] = item_id', source)
        self.assertIn('os.environ["TABLE_NAME"]', source)
        self.assertNotIn(DEPLOYED_TABLE_NAME, source)

    def test_pipeline_artifact_matches_fresh_build_and_corrected_code(self):
        result = demo_lambda_artifact()
        self.assertIsNotNone(result)
        basename, digest = result
        self.assertEqual(basename, "demo-handler.zip")
        self.assertTrue(DIST_ARTIFACT.exists())
        fresh, fresh_digest = _build_to(self.directory)
        self.assertEqual(DIST_ARTIFACT.read_bytes(), fresh.read_bytes())
        self.assertEqual(digest, fresh_digest)
        with zipfile.ZipFile(DIST_ARTIFACT) as bundle:
            content = bundle.read(ENTRY_NAME).decode("utf-8")
        self.assertIn('Key={"pk": item_id}', content)
        self.assertIn('body["pk"] = item_id', content)


class DemoProfileWiringConsistencyTests(unittest.TestCase):
    """The configured entry point, artifact, and table schema agree."""

    @staticmethod
    def _demo_spec() -> InfraSpec:
        def resource(resource_id, kind, pending):
            return ResourceInstance(
                resource_id=resource_id,
                node_id=resource_id.split("-", 1)[-1],
                kind=kind,
                component_name=f"{kind.value} component",
                pending_inputs=[
                    PendingInput(name=name, reason="test")
                    for name in pending
                ],
            )

        return InfraSpec(
            spec_id="spec-1",
            provider=PROVIDER,
            region_code=REGION,
            source_architecture=SourceArchitectureRef(
                artifact_id=str(uuid.uuid4()),
                version=1,
                session_id="design-1",
                upstream_inputs_hash="upstream",
                architecture_hash="arch-hash",
            ),
            resources=[
                resource("apigateway_http-api", ResourceKind.APIGATEWAY_HTTP, []),
                resource(
                    "lambda-fn",
                    ResourceKind.LAMBDA,
                    ("runtime", "handler", "memory_mb", "timeout_seconds"),
                ),
                resource(
                    "dynamodb-table",
                    ResourceKind.DYNAMODB,
                    (
                        "billing_mode",
                        "attribute_definitions",
                        "key_schema",
                        "point_in_time_recovery",
                    ),
                ),
                resource(
                    "cloudwatch_log_group-logs",
                    ResourceKind.CLOUDWATCH_LOG_GROUP,
                    ("retention_days",),
                ),
            ],
        )

    def test_entry_point_matches_the_artifact_module_name(self):
        defaults = {
            (default.resource_id, default.name): default
            for default in demo_profile_defaults(self._demo_spec())
        }
        handler_default = defaults[("lambda-fn", "handler")]
        module_name, function_name = handler_default.value.split(".", 1)
        self.assertEqual(
            module_name, ENTRY_NAME.removesuffix(".py"),
            "configured entry point module must match the artifact entry",
        )
        self.assertEqual(function_name, "handler")
        content = SOURCE.read_text(encoding="utf-8")
        self.assertRegex(content, r"(?m)^def handler\(event, context\):")

    def test_dynamodb_defaults_keep_the_verified_pk_contract(self):
        defaults = {
            (default.resource_id, default.name): default.value
            for default in demo_profile_defaults(self._demo_spec())
        }
        self.assertEqual(
            defaults[("dynamodb-table", "attribute_definitions")], "pk:S"
        )
        self.assertEqual(
            defaults[("dynamodb-table", "key_schema")], "HASH:pk"
        )
        self.assertNotIn(("dynamodb-table", "id"), defaults)

    def test_lambda_module_maps_table_name_into_environment(self):
        module_main = SERVERLESS_MAIN_TF.read_text(encoding="utf-8")
        self.assertIn("TABLE_NAME = var.table_name", module_main)

    def test_rendered_demo_plan_wires_handler_and_table_name(self):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)", "networking"),
            ("fn", "AWS Lambda", "compute"),
            ("table", "Amazon DynamoDB", "database"),
            ("logs", "Amazon CloudWatch Logs", "monitoring"),
        ]
        architecture = ArchitectureResult(
            artifact_id="artifact-1",
            session_id="design-s1",
            version=1,
            nodes=[
                ArchitectureNode(
                    node_id=node_id,
                    component_name=name,
                    category=category,
                    role_in_system="role",
                )
                for node_id, name, category in nodes
            ],
            edges=[],
            entry_points=["api"],
            data_store_node_ids=["table"],
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
        plan = build_provisioning_plan(
            architecture=architecture,
            provider=PROVIDER,
            approval=build_design_approval(
                approved=True,
                note="ok",
                artifact_id="artifact-1",
                gate_outcome="approved_for_review",
            ),
            inputs={},
        )
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(
            plan["inputs"]["lambda-fn"]["handler"], "handler.handler"
        )
        main = plan["files"]["main.tf"]
        namespace = deployment_namespace(architecture.session_id)
        self.assertRegex(main, r'handler\s*=\s*"handler\.handler"')
        self.assertRegex(
            main,
            rf'table_name\s*=\s*"dynamodb-table-{namespace}"',
        )


if __name__ == "__main__":
    unittest.main()
