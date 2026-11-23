"""Evidence for the http_api module contract (Slice 4D).

The managed HTTP entry point is an API Gateway HTTP API with a default
stage (auto-deploy, access logging), one AWS_PROXY Lambda integration,
one $default route, and the invoke permission scoped to the same tree's
API execution ARN. No custom domains, authorizers, or usage plans.

Topology is fail-closed: an API with zero or several Lambdas, or with
zero or several log groups, is refused rather than guessed.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_http_api.py
"""
import unittest
import uuid
from datetime import datetime

from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ParameterSource,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
)
from ai_architect.domain.provisioning.module import ModuleId
from ai_architect.domain.provisioning.render import RenderStatus
from ai_architect.services.provisioning.module_catalog import HTTP_API
from ai_architect.services.provisioning.terraform_guard import (
    guard_terraform_source,
)
from ai_architect.services.provisioning.terraform_renderer import (
    ALLOWED_TF_RESOURCE_TYPES,
    render_infra_spec,
)

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"

LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "index.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
    "code_artifact_path": "demo-function.zip",
    "code_artifact_sha256": "ab" * 32,
}

DDB_VALUES = {
    "billing_mode": "PAY_PER_REQUEST",
    "attribute_definitions": "pk:S",
    "key_schema": "HASH:pk",
    "point_in_time_recovery": "true",
}

LOG_GROUP_VALUES = {"retention_days": "30"}


def _resource(kind, resource_id, values) -> ResourceInstance:
    return ResourceInstance(
        resource_id=resource_id,
        node_id=resource_id.split("-", 1)[-1],
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


def _spec(resources) -> InfraSpec:
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
        resources=resources,
    )


def _golden_resources():
    return [
        _resource(ResourceKind.APIGATEWAY_HTTP, "apigateway-api", {}),
        _resource(ResourceKind.LAMBDA, "lambda-fn", LAMBDA_VALUES),
        _resource(ResourceKind.DYNAMODB, "dynamodb-table", DDB_VALUES),
        _resource(
            ResourceKind.CLOUDWATCH_LOG_GROUP, "log-group-fn",
            LOG_GROUP_VALUES,
        ),
    ]


class HttpApiContractTests(unittest.TestCase):
    """The entry point needs no human inputs and declares its outputs."""

    def test_module_identity_and_provider(self):
        self.assertEqual(HTTP_API.module_id, ModuleId.HTTP_API)
        self.assertEqual(HTTP_API.provider, "aws")

    def test_required_inputs_are_all_derived(self):
        """The entry point takes no human decision: every module variable
        binds from the resource identity, a sibling, or a named policy."""
        self.assertEqual(
            sorted(i.name for i in HTTP_API.inputs if i.required),
            ["access_log_format", "api_name", "lambda_arn",
             "lambda_invoke_arn", "log_group_arn"],
        )
        for definition in HTTP_API.inputs:
            self.assertIsNone(
                definition.binding.parameter_name, msg=definition.name
            )

    def test_declares_api_id_endpoint_and_execution_arn(self):
        outputs = {o.name for o in HTTP_API.outputs}
        self.assertIn("api_id", outputs)
        self.assertIn("api_endpoint", outputs)
        self.assertIn("api_execution_arn", outputs)


class HttpApiRenderTests(unittest.TestCase):
    """The golden stack renders the proxy integration, wired shut."""

    def test_golden_stack_renders(self):
        result = render_infra_spec(_spec(_golden_resources()))
        self.assertEqual(result.status, RenderStatus.RENDERED)

    def test_proxy_integration_and_route_are_present(self):
        result = render_infra_spec(_spec(_golden_resources()))
        combined = "\n".join(result.source_tree().values())
        self.assertIn('resource "aws_apigatewayv2_api"', combined)
        self.assertRegex(combined, r'integration_type\s+= "AWS_PROXY"')
        self.assertRegex(combined, r'route_key\s+= "\$default"')
        self.assertIn("access_log_settings {", combined)

    def test_invoke_permission_is_scoped(self):
        result = render_infra_spec(_spec(_golden_resources()))
        combined = "\n".join(result.source_tree().values())
        self.assertRegex(combined, r'principal\s+= "apigateway\.amazonaws\.com"')
        self.assertIn("execution_arn", combined)

    def test_rendered_tree_passes_the_guard(self):
        result = render_infra_spec(_spec(_golden_resources()))
        self.assertEqual(result.status, RenderStatus.RENDERED)
        self.assertEqual(
            guard_terraform_source(
                result.source_tree(),
                allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            ),
            [],
        )


class HttpApiTopologyTests(unittest.TestCase):
    """Ambiguous wiring is refused, never guessed."""

    def test_api_without_lambda_is_refused(self):
        resources = [r for r in _golden_resources()
                     if r.kind is not ResourceKind.LAMBDA]
        result = render_infra_spec(_spec(resources))
        self.assertNotEqual(result.status, RenderStatus.RENDERED)

    def test_api_with_two_lambdas_is_refused(self):
        resources = _golden_resources() + [
            _resource(ResourceKind.LAMBDA, "lambda-second", LAMBDA_VALUES)
        ]
        result = render_infra_spec(_spec(resources))
        self.assertNotEqual(result.status, RenderStatus.RENDERED)

    def test_api_without_log_group_is_refused(self):
        resources = [r for r in _golden_resources()
                     if r.kind is not ResourceKind.CLOUDWATCH_LOG_GROUP]
        result = render_infra_spec(_spec(resources))
        self.assertNotEqual(result.status, RenderStatus.RENDERED)


if __name__ == "__main__":
    unittest.main()
