"""Evidence for the log_group module contract (Slice 4D).

One CloudWatch log group per function node: the group name derives from
the stable resource identity, retention is a human cost decision from a
closed allowlist. No metric filters, alarms, or subscription filters.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_log_group.py
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
from ai_architect.domain.provisioning.module import (
    DerivationKind,
    ModuleId,
)
from ai_architect.domain.provisioning.render import RenderStatus
from ai_architect.services.provisioning.module_catalog import LOG_GROUP
from ai_architect.services.provisioning.terraform_renderer import (
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


def _golden_spec(retention_days="30") -> InfraSpec:
    return _spec([
        _resource(ResourceKind.APIGATEWAY_HTTP, "apigateway-api", {}),
        _resource(ResourceKind.LAMBDA, "lambda-fn", LAMBDA_VALUES),
        _resource(ResourceKind.DYNAMODB, "dynamodb-table", DDB_VALUES),
        _resource(
            ResourceKind.CLOUDWATCH_LOG_GROUP, "log-group-fn",
            {"retention_days": retention_days},
        ),
    ])


class LogGroupContractTests(unittest.TestCase):
    """Retention is the only human decision, from a closed allowlist."""

    def test_module_identity(self):
        self.assertEqual(LOG_GROUP.module_id, ModuleId.LOG_GROUP)

    def test_retention_is_the_only_human_input(self):
        """The group name is derived by policy; retention is the human
        decision. Both are required module variables."""
        self.assertEqual(
            {i.name for i in LOG_GROUP.inputs if i.required},
            {"log_group_name", "retention_days"},
        )
        by_name = {i.name: i for i in LOG_GROUP.inputs}
        self.assertEqual(
            by_name["log_group_name"].binding.derivation,
            DerivationKind.LOG_GROUP_NAME,
        )
        self.assertIsNone(by_name["log_group_name"].binding.parameter_name)
        self.assertEqual(
            by_name["retention_days"].binding.parameter_name,
            "retention_days",
        )

    def test_retention_allowlist(self):
        definition = next(
            i for i in LOG_GROUP.inputs if i.name == "retention_days"
        )
        self.assertEqual(
            set(definition.constraints.allowed_values),
            {"7", "30", "90", "365"},
        )


class LogGroupRenderTests(unittest.TestCase):
    def test_golden_stack_renders_with_retention(self):
        result = render_infra_spec(_golden_spec("30"))
        self.assertEqual(result.status, RenderStatus.RENDERED)
        combined = "\n".join(result.source_tree().values())
        self.assertIn('resource "aws_cloudwatch_log_group"', combined)
        self.assertRegex(combined, r"retention_in_days\s+= 30")

    def test_group_name_is_derived_not_supplied(self):
        result = render_infra_spec(_golden_spec("90"))
        combined = "\n".join(result.source_tree().values())
        self.assertRegex(combined, r'name\s+= "/aws/ai-architect/log-group-fn"')

    def test_unlisted_retention_is_refused(self):
        for bad in ("14", "0", "never", ""):
            with self.subTest(retention=bad):
                result = render_infra_spec(_golden_spec(bad))
                self.assertNotEqual(result.status, RenderStatus.RENDERED)


if __name__ == "__main__":
    unittest.main()
