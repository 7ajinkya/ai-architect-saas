"""Slice 3 tests: verified Terraform module-block rendering.

Covers the 4C -> Terraform seam: a verified ``ModuleSelection`` rendered
into one pinned ``module`` block per selected resource with sibling
references, the digest-pinned guard policy, fail-closed refusal paths,
and the provisioning-plan integration (terraform init/validate when the
binary is present).

Run: .venv/Scripts/python -m unittest tests.test_provisioning_module_render
"""
import json
import unittest
from datetime import datetime

from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ResourceKind,
)
from ai_architect.domain.provisioning.render import (
    RenderStatus,
)
from ai_architect.services.approvals import build_design_approval
from ai_architect.services.provisioning.deployment_naming import (
    deployment_namespace,
)
from ai_architect.services.provisioning.module_registry import (
    build_default_registry,
    local_source_tree_path,
)
from ai_architect.services.provisioning.module_selection import (
    select_modules,
)
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)
from ai_architect.services.provisioning.spec_builder import build_infra_spec
from ai_architect.services.provisioning.terraform_guard import (
    _module_content_digest,
    guard_terraform_source,
)
from ai_architect.services.provisioning.terraform_renderer import (
    ALLOWED_TF_RESOURCE_TYPES,
    module_guard_policy,
    render_infra_spec,
    render_module_composition,
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
    "attribute_definitions": "pk:S",
    "key_schema": "HASH:pk",
    "point_in_time_recovery": "true",
}
LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "index.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
    "code_artifact_path": "demo-handler.zip",
    "code_artifact_sha256": "a" * 64,
}

ACCESS_LOG_LITERAL = (
    '"{\\"requestId\\":\\"$context.requestId\\",'
    '\\"ip\\":\\"$context.identity.sourceIp\\",'
    '\\"routeKey\\":\\"$context.routeKey\\",'
    '\\"status\\":\\"$context.status\\"}"'
)


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


def _render(nodes, overrides=None, registry=None):
    registry = registry or build_default_registry()
    spec = _spec(nodes, overrides)
    selection = select_modules(spec, provider=PROVIDER, registry=registry)
    rendered = render_module_composition(
        spec, selection, registry=registry
    )
    return rendered, spec, selection, registry


class ModuleCompositionRenderTests(unittest.TestCase):
    """One pinned module block per selected resource, deterministic."""

    def test_full_architecture_renders_and_passes_guard(self):
        rendered, _spec_, selection, _registry = _render(None)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)
        self.assertEqual(rendered.diagnostics, [])
        self.assertEqual(
            len(selection.instances), 5,
            "expected api/lambda/table/bucket/log-group instances",
        )
        main = rendered.file("main.tf").content
        self.assertEqual(main.count('module "'), 5)
        self.assertTrue(rendered.tree_hash)

    def test_sibling_references_use_module_outputs(self):
        rendered, _spec_, _selection, _registry = _render(None)
        main = rendered.file("main.tf").content
        self.assertIn(
            "lambda_arn        = "
            "module.serverless_function-lambda-fn.function_arn",
            main,
        )
        self.assertIn(
            "log_group_arn     = "
            "module.log_group-cloudwatch_log_group-logs.log_group_arn",
            main,
        )

    def test_lambda_invoke_arn_reference(self):
        rendered, _spec_, _selection, _registry = _render(None)
        main = rendered.file("main.tf").content
        self.assertIn(
            "module.serverless_function-lambda-fn.function_invoke_arn",
            main,
        )

    def test_multiple_table_arns_render_in_spec_order(self):
        nodes = [
            ("fn", "AWS Lambda", "compute"),
            ("logs", "Amazon CloudWatch Logs", "monitoring"),
            ("table-a", "Amazon DynamoDB", "database"),
            ("table-b", "Amazon DynamoDB", "database"),
        ]
        rendered, _spec_, _selection, _registry = _render(nodes)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)
        main = rendered.file("main.tf").content
        self.assertIn(
            "table_arns           = "
            "[module.nosql_table-dynamodb-table-a.table_arn, "
            "module.nosql_table-dynamodb-table-b.table_arn]",
            main,
        )

    def test_access_log_format_is_the_raw_renderer_literal(self):
        rendered, _spec_, _selection, _registry = _render(None)
        main = rendered.file("main.tf").content
        self.assertIn(
            f"access_log_format = {ACCESS_LOG_LITERAL}", main
        )

    def test_module_sources_are_in_tree_paths_and_copied(self):
        rendered, _spec_, selection, registry = _render(None)
        main = rendered.file("main.tf").content
        for instance in selection.instances:
            contract = registry.get(instance.module_id)
            tree_dir = local_source_tree_path(contract.source)
            self.assertIn(f'source', main)
            self.assertIn(f'"./{tree_dir}"', main)
            for name in sorted(
                {"main.tf", "variables.tf", "outputs.tf", "versions.tf"}
            ):
                self.assertIsNotNone(
                    rendered.file(f"{tree_dir}/{name}"),
                    f"missing in-tree copy {tree_dir}/{name}",
                )

    def test_empty_spec_renders_scaffolding_only(self):
        built = build_infra_spec(
            _architecture([("role", "AWS IAM (service role)", "security")]),
            provider=PROVIDER,
        )
        spec = _supply(built)
        registry = build_default_registry()
        selection = select_modules(
            spec, provider=PROVIDER, registry=registry
        )
        rendered = render_module_composition(
            spec, selection, registry=registry
        )
        self.assertEqual(rendered.status, RenderStatus.RENDERED)
        self.assertEqual(rendered.file("main.tf").content, "\n")
        self.assertEqual(
            sorted(entry.path for entry in rendered.files),
            ["main.tf", "outputs.tf", "provider.tf", "versions.tf"],
        )

    def test_render_is_byte_identical_across_runs(self):
        first, _spec_, _sel_a, _registry = _render(None)
        second, _spec_, _sel_b, _registry = _render(None)
        self.assertEqual(
            [entry.model_dump(mode="json") for entry in first.files],
            [entry.model_dump(mode="json") for entry in second.files],
        )
        self.assertEqual(first.tree_hash, second.tree_hash)


class GuardPolicyTests(unittest.TestCase):
    """The pinned-module guard policy: default deny, digest trust."""

    def _rendered_tree_and_policy(self):
        rendered, _spec_, selection, registry = _render(None)
        policy = module_guard_policy(selection, registry=registry)
        return rendered, selection, registry, policy

    def test_guard_digest_helper_matches_registry_digest(self):
        registry = build_default_registry()
        for contract in registry.all_contracts():
            files = registry.module_tree(contract.module_id)
            digest = _module_content_digest(sorted(files.items()))
            self.assertEqual(
                digest, contract.source_digest,
                f"guard digest divergence for {contract.module_id.value}",
            )

    def test_rendered_tree_passes_guard_with_policy(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        diagnostics = guard_terraform_source(
            rendered.source_tree(),
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertEqual(
            [str(item) for item in diagnostics], []
        )

    def test_module_blocks_are_denied_without_policy(self):
        rendered, _selection, _registry, _policy = (
            self._rendered_tree_and_policy()
        )
        diagnostics = guard_terraform_source(
            rendered.source_tree(),
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
        )
        self.assertTrue(diagnostics)
        self.assertTrue(
            any(
                item.code.value
                in {"guard_violation", "guard_unparseable"}
                for item in diagnostics
            )
        )

    def test_tampered_module_content_is_refused(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        tree = rendered.source_tree()
        target = "infra/modules/aws-object-storage/main.tf"
        tree[target] = tree[target].replace(
            'resource "aws_s3_bucket" "this"',
            'resource "aws_s3_bucket" "this_evil"',
        )
        diagnostics = guard_terraform_source(
            tree,
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertTrue(diagnostics)

    def test_unpinned_source_is_refused(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        tree = rendered.source_tree()
        tree["main.tf"] = tree["main.tf"].replace(
            '"./infra/modules/aws-object-storage"',
            '"./infra/modules/aws-object-storage-evil"',
        )
        diagnostics = guard_terraform_source(
            tree,
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertTrue(
            any(
                "not an exact pinned registry entry" in item.message
                for item in diagnostics
            )
        )

    def test_meta_argument_on_module_block_is_refused(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        tree = rendered.source_tree()
        tree["main.tf"] = tree["main.tf"].replace(
            'module "object_storage-s3-bucket" {\n',
            'module "object_storage-s3-bucket" {\n'
            '  version = "1.0.0"\n',
        )
        diagnostics = guard_terraform_source(
            tree,
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertTrue(
            any(
                "not a declared input" in item.message
                for item in diagnostics
            )
        )

    def test_undeclared_module_output_reference_is_refused(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        tree = rendered.source_tree()
        tree["main.tf"] = tree["main.tf"].replace(
            "module.log_group-cloudwatch_log_group-logs.log_group_arn",
            "module.log_group-cloudwatch_log_group-logs.no_such_output",
        )
        diagnostics = guard_terraform_source(
            tree,
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertTrue(
            any(
                "undeclared output" in item.message
                for item in diagnostics
            )
        )

    def test_undeclared_module_instance_reference_is_refused(self):
        rendered, _selection, _registry, policy = (
            self._rendered_tree_and_policy()
        )
        tree = rendered.source_tree()
        tree["main.tf"] = tree["main.tf"].replace(
            "module.nosql_table-dynamodb-table.table_arn",
            "module.ghost-table.table_arn",
        )
        diagnostics = guard_terraform_source(
            tree,
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertTrue(
            any(
                "undeclared module instance" in item.message
                for item in diagnostics
            )
        )


class SelectionRefusalTests(unittest.TestCase):
    """Unclean or tampered selections never render."""

    def test_missing_log_group_refuses_render(self):
        nodes = [
            ("fn", "AWS Lambda", "compute"),
            ("table", "Amazon DynamoDB", "database"),
        ]
        rendered, _spec_, selection, _registry = _render(nodes)
        self.assertFalse(selection.selected)
        self.assertEqual(rendered.status, RenderStatus.UNREPRESENTABLE)
        self.assertEqual(rendered.files, [])
        self.assertEqual(rendered.tree_hash, "")
        self.assertTrue(rendered.diagnostics)

    def test_missing_sibling_identity_refuses_render(self):
        rendered, spec, selection, registry = _render(None)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)

        from ai_architect.domain.provisioning.module import TypedString

        api_instance = next(
            instance for instance in selection.instances
            if instance.resource_id == "apigateway_http-api"
        )
        tampered_bindings = tuple(
            binding.model_copy(
                update={"value": TypedString(value="ghost-log-group")}
            )
            if binding.input_name == "log_group_arn"
            else binding
            for binding in api_instance.bindings
        )
        tampered_instance = api_instance.model_copy(
            update={"bindings": tampered_bindings}
        )
        tampered_selection = selection.model_copy(update={
            "instances": tuple(
                tampered_instance
                if instance is api_instance
                else instance
                for instance in selection.instances
            ),
        })
        refused = render_module_composition(
            spec, tampered_selection, registry=registry
        )
        self.assertEqual(refused.status, RenderStatus.UNREPRESENTABLE)
        self.assertTrue(
            any(
                "ghost-log-group" in item.message
                for item in refused.diagnostics
            )
        )

    def test_dotted_module_label_refuses_render(self):
        rendered, spec, selection, registry = _render(None)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)

        s3_instance = next(
            instance for instance in selection.instances
            if instance.resource_id == "s3-bucket"
        )
        renamed = s3_instance.model_copy(update={
            "instance_id": s3_instance.instance_id.replace(
                "object_storage-s3-bucket", "object_storage-s3.bucket"
            ),
        })
        tampered_selection = selection.model_copy(update={
            "instances": tuple(
                renamed if instance is s3_instance else instance
                for instance in selection.instances
            ),
        })
        refused = render_module_composition(
            spec, tampered_selection, registry=registry
        )
        self.assertEqual(refused.status, RenderStatus.UNREPRESENTABLE)
        self.assertTrue(
            any(
                "s3.bucket" in item.message
                for item in refused.diagnostics
            )
        )

    def test_duplicate_resource_realisation_is_refused(self):
        rendered, spec, selection, registry = _render(None)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)

        api_instance = next(
            instance for instance in selection.instances
            if instance.resource_id == "apigateway_http-api"
        )
        duplicated = api_instance.model_copy(update={
            "instance_id": api_instance.instance_id + "-copy",
        })
        tampered_selection = selection.model_copy(update={
            "instances": selection.instances + (duplicated,),
        })
        refused = render_module_composition(
            spec, tampered_selection, registry=registry
        )
        self.assertNotEqual(refused.status, RenderStatus.RENDERED)
        self.assertTrue(refused.diagnostics)


class PlanIntegrationTests(unittest.TestCase):
    """The provisioning plan consumes the module tree end to end."""

    def _plan(self, nodes=None, inputs=None):
        architecture = _architecture(nodes)
        default_inputs = {
            "s3-bucket": S3_VALUES,
            "dynamodb-table": DDB_VALUES,
            "lambda-fn": LAMBDA_VALUES,
            "cloudwatch_log_group-logs": {"retention_days": "30"},
        }
        node_for_input = {
            "s3-bucket": "bucket",
            "dynamodb-table": "table",
            "lambda-fn": "fn",
            "cloudwatch_log_group-logs": "logs",
        }
        if nodes is None:
            present = {"api", "fn", "table", "bucket", "logs"}
        else:
            present = {node_id for node_id, _name, _cat in nodes}
        merged = {
            key: values
            for key, values in default_inputs.items()
            if node_for_input[key] in present
        }
        merged.update(inputs or {})
        return build_provisioning_plan(
            architecture=architecture,
            provider=PROVIDER,
            approval=build_design_approval(
                approved=True, note="ok", artifact_id="artifact-1",
                gate_outcome="approved_for_review",
            ),
            inputs=merged,
        )

    def test_plan_is_valid_with_module_tree(self):
        plan = self._plan()
        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(plan["render_status"], "rendered")
        self.assertEqual(plan["status"], "valid")
        self.assertTrue(plan["tree_hash"])
        self.assertIn(
            "infra/modules/aws-http-api/main.tf", plan["files"]
        )
        self.assertIn("main.tf", plan["files"])

    def test_plan_namespaces_provider_visible_names(self):
        plan = self._plan()
        namespace = deployment_namespace("design-s1")
        main = plan["files"]["main.tf"]

        self.assertRegex(
            main, rf'function_name\s*=\s*"lambda-fn-{namespace}"'
        )
        self.assertRegex(
            main,
            rf'api_name\s*=\s*"apigateway_http-api-{namespace}"',
        )
        self.assertGreaterEqual(
            main.count(f'"dynamodb-table-{namespace}"'), 2
        )
        self.assertIn(
            f"/aws/ai-architect/cloudwatch_log_group-logs-{namespace}",
            main,
        )

    def test_four_module_demo_uses_deterministic_defaults(self):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)", "networking"),
            ("fn", "AWS Lambda", "compute"),
            ("table", "Amazon DynamoDB", "database"),
            ("logs", "Amazon CloudWatch Logs", "monitoring"),
        ]
        plan = build_provisioning_plan(
            architecture=_architecture(nodes),
            provider=PROVIDER,
            approval=build_design_approval(
                approved=True, note="ok", artifact_id="artifact-1",
                gate_outcome="approved_for_review",
            ),
            inputs={},
        )
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(plan["render_status"], "rendered")
        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(len(plan["modules"]), 4)
        self.assertEqual(len(plan["input_defaults"]), 9)
        self.assertTrue(
            all(
                item["source"] == "deterministic_default"
                and item["requires_confirmation"] is True
                and item["rationale"]
                for item in plan["input_defaults"]
            )
        )
        self.assertEqual(
            plan["inputs"]["dynamodb-table"]["attribute_definitions"], "pk:S"
        )
        self.assertEqual(
            plan["inputs"]["dynamodb-table"]["key_schema"], "HASH:pk"
        )
        self.assertEqual(
            plan["inputs"]["lambda-fn"]["runtime"], "python3.12"
        )
        self.assertEqual(
            plan["inputs"]["cloudwatch_log_group-logs"]["retention_days"],
            "30",
        )

    def test_four_module_demo_accepts_json_dynamodb_inputs(self):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)", "networking"),
            ("fn", "AWS Lambda", "compute"),
            ("table", "Amazon DynamoDB", "database"),
            ("logs", "Amazon CloudWatch Logs", "monitoring"),
        ]
        inputs = {
            "dynamodb-table": {
                **DDB_VALUES,
                "attribute_definitions": json.dumps(
                    [{"attribute_name": "pk", "attribute_type": "S"}]
                ),
                "key_schema": json.dumps(
                    [{"attribute_name": "pk", "key_type": "HASH"}]
                ),
            }
        }
        plan = self._plan(nodes=nodes, inputs=inputs)
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(plan["render_status"], "rendered")
        self.assertEqual(plan["selection_errors"], [])
        self.assertEqual(len(plan["modules"]), 4)
        self.assertEqual(
            plan["inputs"]["dynamodb-table"]["attribute_definitions"], "pk:S"
        )
        self.assertEqual(
            plan["inputs"]["dynamodb-table"]["key_schema"], "HASH:pk"
        )

    def test_plan_fails_closed_without_log_group(self):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)", "networking"),
            ("fn", "AWS Lambda", "compute"),
            ("table", "Amazon DynamoDB", "database"),
            ("bucket", "Amazon S3", "storage"),
        ]
        plan = self._plan(nodes=nodes)
        self.assertEqual(plan["status"], "incomplete")
        self.assertEqual(plan["files"], {})
        self.assertEqual(plan["tree_hash"], "")
        self.assertNotEqual(plan["render_status"], "rendered")


class RawRendererPreservationTests(unittest.TestCase):
    """The raw 4B renderer stays available and unchanged in behavior."""

    def test_raw_renderer_still_emits_resource_blocks(self):
        spec = _spec([("bucket", "Amazon S3", "storage")])
        result = render_infra_spec(spec)
        self.assertEqual(result.status, RenderStatus.RENDERED)
        self.assertIn(
            'resource "aws_s3_bucket"',
            result.file("main.tf").content,
        )


if __name__ == "__main__":
    unittest.main()
