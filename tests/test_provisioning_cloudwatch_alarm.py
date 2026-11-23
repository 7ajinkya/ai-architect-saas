"""Evidence for the cloudwatch_alarms module contract.

The capability is exactly four upper-bound alarms for the single HTTP
API and single Lambda in the same spec. Thresholds, period, evaluation
policy, comparison, missing-data treatment and notification are human
decisions; the platform derives only names and sibling dimensions.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_cloudwatch_alarm.py
"""
import re
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
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
)
from ai_architect.domain.provisioning.module import (
    DerivationKind,
    ModuleId,
    ParameterType,
    VerificationStatus,
)
from ai_architect.domain.provisioning.render import RenderStatus
from ai_architect.services.provisioning.module_catalog import CLOUDWATCH_ALARMS
from ai_architect.services.provisioning.module_registry import (
    build_default_registry,
    local_source_tree_path,
)
from ai_architect.services.provisioning.module_selection import select_modules
from ai_architect.services.provisioning.spec_builder import build_infra_spec
from ai_architect.services.provisioning.terraform_guard import (
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

LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "index.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
    "code_artifact_path": "demo-function.zip",
    "code_artifact_sha256": "a" * 64,
}

ALARM_VALUES = {
    "api_5xx_error_threshold": "10",
    "api_latency_p99_threshold_ms": "750",
    "lambda_error_threshold": "5",
    "lambda_duration_p99_threshold_ms": "2500",
    "period_seconds": "60",
    "evaluation_periods": "2",
    "comparison_operator": "GreaterThanOrEqualToThreshold",
    "missing_data_treatment": "notBreaching",
    "notification_target": "none",
}

ALARM_REQUIRED_INPUTS = {
    "api_5xx_error_threshold",
    "api_latency_p99_threshold_ms",
    "lambda_error_threshold",
    "lambda_duration_p99_threshold_ms",
    "period_seconds",
    "evaluation_periods",
    "comparison_operator",
    "missing_data_treatment",
    "notification_target",
}


def _node(nid, name, cat):
    return ArchitectureNode(
        node_id=nid,
        component_name=name,
        category=cat,
        role_in_system="role",
    )


def _architecture(nodes=None):
    nodes = nodes or [
        ("api", "Amazon API Gateway (HTTP API)", "networking"),
        ("fn", "AWS Lambda", "compute"),
        ("logs", "Amazon CloudWatch Logs", "monitoring"),
        (
            "alarm",
            "Amazon CloudWatch Alarms "
            "(for API Gateway and Lambda metrics)",
            "monitoring",
        ),
    ]
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id="design-alarm",
        version=1,
        nodes=[_node(nid, name, cat) for nid, name, cat in nodes],
        edges=[],
        entry_points=["api"],
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
            session_id="design-alarm",
            upstream_inputs_hash="upstream",
            architecture_hash="arch-hash",
        ),
        resources=resources,
    )


def _build(nodes=None):
    return build_infra_spec(_architecture(nodes), provider=PROVIDER)


def _supply(spec, alarm_overrides=None):
    values = dict(ALARM_VALUES)
    values.update(alarm_overrides or {})
    resources = []
    for resource in spec.resources:
        if resource.kind is ResourceKind.LAMBDA:
            supplied = LAMBDA_VALUES
        elif resource.kind is ResourceKind.CLOUDWATCH_ALARM:
            supplied = values
        elif resource.kind is ResourceKind.CLOUDWATCH_LOG_GROUP:
            supplied = {"retention_days": "30"}
        else:
            supplied = {}
        resources.append({
            **resource.model_dump(mode="json"),
            "parameters": [
                {"name": n, "value": v, "source": "required_input"}
                for n, v in supplied.items()
            ],
            "pending_inputs": [],
        })
    return InfraSpec.model_validate(
        {**spec.model_dump(mode="json"), "resources": resources}
    )


def _selected(nodes=None, alarm_overrides=None):
    spec = _supply(_build(nodes), alarm_overrides)
    registry = build_default_registry()
    return spec, registry, select_modules(
        spec, provider=PROVIDER, registry=registry
    )


def _alarm_instance(selection):
    return next(
        instance for instance in selection.instances
        if instance.module_id is ModuleId.CLOUDWATCH_ALARMS
    )


class CloudWatchAlarmContractTests(unittest.TestCase):
    """The contract is explicit, closed and free of hidden defaults."""

    def test_module_identity_and_verification(self):
        self.assertEqual(
            CLOUDWATCH_ALARMS.module_id, ModuleId.CLOUDWATCH_ALARMS
        )
        self.assertEqual(CLOUDWATCH_ALARMS.provider, PROVIDER)
        self.assertEqual(
            CLOUDWATCH_ALARMS.verification.status,
            VerificationStatus.VERIFIED,
        )

    def test_every_human_decision_is_required(self):
        self.assertEqual(
            set(CLOUDWATCH_ALARMS.required_input_names),
            ALARM_REQUIRED_INPUTS
            | {"alarm_name_prefix", "api_id", "function_name"},
        )
        for definition in CLOUDWATCH_ALARMS.inputs:
            self.assertIsNone(
                definition.default, msg=definition.name
            )

    def test_sibling_identity_is_derived_not_asked_for(self):
        by_name = {item.name: item for item in CLOUDWATCH_ALARMS.inputs}
        self.assertIs(
            by_name["api_id"].binding.derivation,
            DerivationKind.API_GATEWAY_API_ID,
        )
        self.assertIs(
            by_name["function_name"].binding.derivation,
            DerivationKind.LAMBDA_FUNCTION_NAME,
        )
        self.assertTrue(by_name["alarm_name_prefix"].binding.resource_id)
        for name in ALARM_REQUIRED_INPUTS:
            self.assertEqual(
                by_name[name].binding.parameter_name, name
            )

    def test_threshold_and_policy_ranges_are_closed(self):
        by_name = {item.name: item for item in CLOUDWATCH_ALARMS.inputs}
        self.assertEqual(
            (by_name["api_5xx_error_threshold"].constraints.minimum,
             by_name["api_5xx_error_threshold"].constraints.maximum),
            (0, 1_000_000),
        )
        self.assertEqual(
            (by_name["lambda_error_threshold"].constraints.minimum,
             by_name["lambda_error_threshold"].constraints.maximum),
            (0, 1_000_000),
        )
        self.assertEqual(
            by_name["api_latency_p99_threshold_ms"].constraints.maximum,
            3_600_000,
        )
        self.assertEqual(
            by_name["lambda_duration_p99_threshold_ms"].constraints.maximum,
            900_000,
        )
        self.assertEqual(
            by_name["period_seconds"].constraints.allowed_values,
            frozenset({"60", "300", "900"}),
        )
        self.assertEqual(
            by_name["comparison_operator"].constraints.allowed_values,
            frozenset(
                {"GreaterThanThreshold", "GreaterThanOrEqualToThreshold"}
            ),
        )
        self.assertEqual(
            by_name["missing_data_treatment"].constraints.allowed_values,
            frozenset({"breaching", "notBreaching", "ignore", "missing"}),
        )

    def test_notification_is_none_or_an_sns_topic_arn(self):
        definition = next(
            item for item in CLOUDWATCH_ALARMS.inputs
            if item.name == "notification_target"
        )
        pattern = definition.constraints.pattern
        self.assertTrue(re.fullmatch(pattern, "none"))
        self.assertTrue(re.fullmatch(
            pattern, "arn:aws:sns:us-east-1:123456789012:ops-alerts"
        ))
        self.assertFalse(re.fullmatch(pattern, "ops@example.com"))
        self.assertFalse(re.fullmatch(pattern, "arn:aws:sns:us-east-1:x:ops"))

    def test_declares_alarm_arns_output(self):
        outputs = {item.name: item for item in CLOUDWATCH_ALARMS.outputs}
        self.assertIs(
            outputs["alarm_arns"].type, ParameterType.STRING_LIST
        )


class CloudWatchAlarmSelectionTests(unittest.TestCase):
    """Bindings derive only identity and dimensions."""

    def test_alarm_binds_sibling_identities_and_human_values(self):
        _spec_, _registry, selection = _selected()
        self.assertTrue(selection.selected, selection.diagnostics)
        bindings = {
            binding.input_name: binding.value.value
            for binding in _alarm_instance(selection).bindings
        }
        self.assertEqual(bindings["api_id"], "apigateway_http-api")
        self.assertEqual(bindings["function_name"], "lambda-fn")
        self.assertEqual(
            bindings["alarm_name_prefix"], "cloudwatch_alarm-alarm"
        )
        self.assertEqual(bindings["api_5xx_error_threshold"], 10)
        self.assertEqual(bindings["api_latency_p99_threshold_ms"], 750)
        self.assertEqual(bindings["lambda_error_threshold"], 5)
        self.assertEqual(bindings["lambda_duration_p99_threshold_ms"], 2500)
        self.assertEqual(bindings["period_seconds"], "60")
        self.assertEqual(bindings["evaluation_periods"], 2)
        self.assertEqual(
            bindings["comparison_operator"],
            "GreaterThanOrEqualToThreshold",
        )
        self.assertEqual(bindings["missing_data_treatment"], "notBreaching")
        self.assertEqual(bindings["notification_target"], "none")

    def test_missing_inputs_are_reported_not_defaulted(self):
        spec = _build()
        selection = select_modules(
            spec, provider=PROVIDER, registry=build_default_registry()
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        missing = {
            item.input_name for item in selection.diagnostics
            if item.resource_id == "cloudwatch_alarm-alarm"
        }
        self.assertEqual(missing, ALARM_REQUIRED_INPUTS)

    def test_sns_topic_arn_is_accepted_verbatim(self):
        arn = "arn:aws:sns:us-east-1:123456789012:ops-alerts"
        _spec_, _registry, selection = _selected(
            alarm_overrides={"notification_target": arn}
        )
        self.assertTrue(selection.selected, selection.diagnostics)
        bindings = {
            binding.input_name: binding.value.value
            for binding in _alarm_instance(selection).bindings
        }
        self.assertEqual(bindings["notification_target"], arn)

    def test_invalid_threshold_fails_closed(self):
        for bad in ("-1", "1000001", "1.5"):
            with self.subTest(value=bad):
                _spec_, _registry, selection = _selected(
                    alarm_overrides={"api_5xx_error_threshold": bad}
                )
                self.assertFalse(selection.selected)
                self.assertEqual(selection.instances, ())

    def test_invalid_period_fails_closed(self):
        _spec_, _registry, selection = _selected(
            alarm_overrides={"period_seconds": "120"}
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())

    def test_invalid_comparison_fails_closed(self):
        _spec_, _registry, selection = _selected(
            alarm_overrides={"comparison_operator": "LessThanThreshold"}
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())

    def test_invalid_missing_data_fails_closed(self):
        _spec_, _registry, selection = _selected(
            alarm_overrides={"missing_data_treatment": "alarm"}
        )
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())

    def test_ambiguous_lambda_topology_is_refused(self):
        nodes = [
            ("api", "Amazon API Gateway (HTTP API)", "networking"),
            ("fn-a", "AWS Lambda", "compute"),
            ("fn-b", "AWS Lambda", "compute"),
            ("logs", "Amazon CloudWatch Logs", "monitoring"),
            ("alarm", "Amazon CloudWatch Alarms", "monitoring"),
        ]
        _spec_, _registry, selection = _selected(nodes=nodes)
        self.assertFalse(selection.selected)
        self.assertEqual(selection.instances, ())
        self.assertTrue(any(
            "requires exactly one lambda resource" in item.message
            for item in selection.diagnostics
        ))


class CloudWatchAlarmRenderTests(unittest.TestCase):
    """The pinned module is copied, wired and guarded."""

    def _render(self, alarm_overrides=None):
        spec, registry, selection = _selected(
            alarm_overrides=alarm_overrides
        )
        rendered = render_module_composition(
            spec, selection, registry=registry
        )
        return rendered, selection, registry

    def test_alarm_module_renders_and_passes_guard(self):
        rendered, selection, registry = self._render()
        self.assertTrue(selection.selected, selection.diagnostics)
        self.assertEqual(rendered.status, RenderStatus.RENDERED)
        self.assertEqual(rendered.diagnostics, [])
        self.assertEqual(len(selection.instances), 4)

        main = rendered.file("main.tf").content
        self.assertIn(
            '"./infra/modules/aws-cloudwatch-alarm"', main
        )
        self.assertIn(".api_id", main)
        self.assertIn(".function_name", main)

        contract = registry.get(ModuleId.CLOUDWATCH_ALARMS)
        tree_dir = local_source_tree_path(contract.source)
        self.assertIsNotNone(
            rendered.file(f"{tree_dir}/main.tf")
        )
        self.assertIsNotNone(
            rendered.file(f"{tree_dir}/variables.tf")
        )

        policy = module_guard_policy(selection, registry=registry)
        diagnostics = guard_terraform_source(
            rendered.source_tree(),
            allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
            module_policy=policy,
        )
        self.assertEqual([str(item) for item in diagnostics], [])

    def test_module_source_is_exactly_four_metric_alarms(self):
        rendered, _selection, registry = self._render()
        contract = registry.get(ModuleId.CLOUDWATCH_ALARMS)
        tree_dir = local_source_tree_path(contract.source)
        source = rendered.file(f"{tree_dir}/main.tf").content
        self.assertEqual(
            source.count('resource "aws_cloudwatch_metric_alarm"'), 4
        )
        for metric in ("5XXError", "Latency", "Errors", "Duration"):
            self.assertRegex(
                source, rf'metric_name\s+= "{metric}"'
            )
        self.assertIn('Stage = "$default"', source)
        self.assertRegex(source, r'extended_statistic\s+= "p99"')
        self.assertIn("local.alarm_actions", source)
        self.assertNotIn("aws_sns_topic", source)

    def test_raw_renderer_still_refuses_alarm_kind(self):
        spec = _supply(_build())
        rendered = render_infra_spec(spec)
        self.assertEqual(rendered.status, RenderStatus.UNREPRESENTABLE)
        self.assertEqual(rendered.files, [])
        self.assertTrue(rendered.diagnostics)
