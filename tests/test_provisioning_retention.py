"""Slice 1 tests: requirements -> provisioning retention propagation.

An explicit CloudWatch log-retention requirement must reach the log-group
InfraSpec as a resolved retention_days parameter and must no longer be a
pending input. Absent or ambiguous retention must remain unresolved rather
than fabricated. No other resource kind gains or loses behavior.

Run: .venv/Scripts/python -m unittest tests.test_provisioning_retention
"""
import unittest
import uuid
from datetime import datetime

from ai_architect.domain.architecture.models import (
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
)
from ai_architect.domain.provisioning.models import (
    ParameterSource,
    ResourceKind,
)
from ai_architect.domain.requirements.models import (
    AdditionalRequirement,
    RequirementsExtraction,
)
from ai_architect.services.provisioning.spec_builder import (
    ResolvedRetention,
    _resolve_retention_days,
    build_infra_spec,
)

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"


def _architecture() -> ArchitectureResult:
    return ArchitectureResult(
        artifact_id=str(uuid.uuid4()),
        session_id="design-1",
        version=1,
        nodes=[
            ArchitectureNode(
                node_id="logs",
                component_name="Amazon CloudWatch Logs",
                category="monitoring",
                role_in_system="log aggregation",
            ),
        ],
        edges=[],
        entry_points=[],
        data_store_node_ids=[],
        boundaries=[],
        decisions=[],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id="req-1",
            capacity_formula_version="v1",
            selection_generated_at=NOW,
            cost_generated_at=NOW,
            cost_pricing_as_of=NOW,
            region_code=REGION,
            upstream_inputs_hash="upstream",
        ),
        generated_at=NOW,
    )


def _extraction(retention="", additional=None) -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="order api",
        cloud_provider="aws",
        region="us-east-1",
        target_users_dau="20000",
        retention=retention,
        additional_requirements=additional or [],
    )


def _log_group(spec):
    return next(
        r for r in spec.resources if r.kind is ResourceKind.CLOUDWATCH_LOG_GROUP
    )


class ExplicitRetentionTests(unittest.TestCase):
    """A. Explicit 30-day requirement resolves retention_days = 30."""

    def test_explicit_30_day_retention_is_resolved(self):
        spec = build_infra_spec(
            _architecture(),
            provider=PROVIDER,
            requirements=_extraction(
                additional=[
                    AdditionalRequirement(
                        name="log retention period", value="30", unit="days"
                    )
                ]
            ),
        )
        resource = _log_group(spec)
        retention = next(
            (p for p in resource.parameters if p.name == "retention_days"), None
        )
        self.assertIsNotNone(retention)
        self.assertEqual(retention.value, "30")

    def test_resolved_retention_is_marked_derived(self):
        spec = build_infra_spec(
            _architecture(),
            provider=PROVIDER,
            requirements=_extraction(retention="30 days"),
        )
        retention = next(
            p
            for p in _log_group(spec).parameters
            if p.name == "retention_days"
        )
        self.assertEqual(retention.source, ParameterSource.DERIVED)

    def test_retention_is_not_pending_when_resolved(self):
        """B. A resolved retention leaves the log group's pending list."""
        spec = build_infra_spec(
            _architecture(),
            provider=PROVIDER,
            requirements=_extraction(
                additional=[
                    AdditionalRequirement(
                        name="log retention period", value="90", unit="days"
                    )
                ]
            ),
        )
        pending_names = {p.name for p in _log_group(spec).pending_inputs}
        self.assertNotIn("retention_days", pending_names)

    def test_unlisted_explicit_retention_is_refused(self):
        """An explicit value outside the allowlist is not coerced."""
        spec = build_infra_spec(
            _architecture(),
            provider=PROVIDER,
            requirements=_extraction(retention="14 days"),
        )
        pending_names = {p.name for p in _log_group(spec).pending_inputs}
        self.assertIn("retention_days", pending_names)
        self.assertEqual(
            [p.name for p in _log_group(spec).parameters], []
        )


class AbsentRetentionTests(unittest.TestCase):
    """C. No retention requirement never invents 30 days."""

    def test_absent_retention_stays_pending(self):
        spec = build_infra_spec(
            _architecture(), provider=PROVIDER, requirements=_extraction()
        )
        resource = _log_group(spec)
        self.assertEqual([p.name for p in resource.parameters], [])
        self.assertIn(
            "retention_days", {p.name for p in resource.pending_inputs}
        )

    def test_no_requirements_argument_stays_pending(self):
        spec = build_infra_spec(_architecture(), provider=PROVIDER)
        resource = _log_group(spec)
        self.assertEqual([p.name for p in resource.parameters], [])
        self.assertIn(
            "retention_days", {p.name for p in resource.pending_inputs}
        )


class AmbiguousRetentionTests(unittest.TestCase):
    """D. Ambiguous/unsupported text is not turned into a number."""

    def test_non_day_unit_is_ambiguous(self):
        resolved = _resolve_retention_days(_extraction(retention="1 year"))
        self.assertFalse(resolved.is_explicit)
        self.assertIsNone(resolved.value)

    def test_range_is_ambiguous(self):
        resolved = _resolve_retention_days(
            _extraction(retention="30-90 days")
        )
        self.assertFalse(resolved.is_explicit)
        self.assertIsNone(resolved.value)

    def test_fraction_is_ambiguous(self):
        resolved = _resolve_retention_days(_extraction(retention="1.5 days"))
        self.assertFalse(resolved.is_explicit)
        self.assertIsNone(resolved.value)

    def test_word_form_is_ambiguous(self):
        resolved = _resolve_retention_days(
            _extraction(retention="thirty days")
        )
        self.assertFalse(resolved.is_explicit)
        self.assertIsNone(resolved.value)

    def test_ambiguous_retention_is_not_resolved_in_spec(self):
        spec = build_infra_spec(
            _architecture(),
            provider=PROVIDER,
            requirements=_extraction(retention="indefinitely"),
        )
        resource = _log_group(spec)
        self.assertEqual([p.name for p in resource.parameters], [])
        self.assertIn(
            "retention_days", {p.name for p in resource.pending_inputs}
        )

    def test_storage_retention_is_not_log_retention(self):
        """A data/storage retention entry must not drive the log group."""
        resolved = _resolve_retention_days(
            _extraction(
                additional=[
                    AdditionalRequirement(
                        name="data retention period",
                        value="365",
                        unit="days",
                        category="storage",
                    )
                ]
            )
        )
        self.assertFalse(resolved.is_explicit)


class ResolverStateTests(unittest.TestCase):
    """The three-state contract is explicit, ambiguous, or missing."""

    def test_missing_state(self):
        self.assertEqual(
            _resolve_retention_days(_extraction()).status, "missing"
        )

    def test_explicit_state(self):
        resolved = _resolve_retention_days(
            _extraction(
                additional=[
                    AdditionalRequirement(
                        name="log retention period", value="30", unit="days"
                    )
                ]
            )
        )
        self.assertEqual(
            resolved,
            ResolvedRetention(
                status="explicit", value="30", source="log retention period"
            ),
        )

    def test_ambiguous_state(self):
        resolved = _resolve_retention_days(_extraction(retention="1 year"))
        self.assertEqual(resolved.status, "ambiguous")


class UnrelatedResourceTests(unittest.TestCase):
    """E. Other resource kinds and the no-invention rule are unchanged."""

    def test_no_parameter_is_invented_for_other_kinds(self):
        architecture = ArchitectureResult(
            artifact_id=str(uuid.uuid4()),
            session_id="design-1",
            version=1,
            nodes=[
                ArchitectureNode(
                    node_id="fn",
                    component_name="AWS Lambda",
                    category="compute",
                    role_in_system="compute",
                ),
            ],
            edges=[],
            entry_points=[],
            data_store_node_ids=[],
            boundaries=[],
            decisions=[],
            inputs_ref=ArchitectureInputsRef(
                requirements_session_id="req-1",
                capacity_formula_version="v1",
                selection_generated_at=NOW,
                cost_generated_at=NOW,
                cost_pricing_as_of=NOW,
                region_code=REGION,
                upstream_inputs_hash="upstream",
            ),
            generated_at=NOW,
        )
        spec = build_infra_spec(
            architecture,
            provider=PROVIDER,
            requirements=_extraction(retention="30 days"),
        )
        lambda_resource = spec.resources[0]
        self.assertEqual(lambda_resource.parameters, [])
        self.assertIn(
            "runtime", {p.name for p in lambda_resource.pending_inputs}
        )

    def test_retention_not_applied_to_non_log_kinds(self):
        """A log-retention requirement must not attach to a Lambda node."""
        architecture = ArchitectureResult(
            artifact_id=str(uuid.uuid4()),
            session_id="design-1",
            version=1,
            nodes=[
                ArchitectureNode(
                    node_id="fn",
                    component_name="AWS Lambda",
                    category="compute",
                    role_in_system="compute",
                ),
            ],
            edges=[],
            entry_points=[],
            data_store_node_ids=[],
            boundaries=[],
            decisions=[],
            inputs_ref=ArchitectureInputsRef(
                requirements_session_id="req-1",
                capacity_formula_version="v1",
                selection_generated_at=NOW,
                cost_generated_at=NOW,
                cost_pricing_as_of=NOW,
                region_code=REGION,
                upstream_inputs_hash="upstream",
            ),
            generated_at=NOW,
        )
        spec = build_infra_spec(
            architecture,
            provider=PROVIDER,
            requirements=_extraction(retention="30 days"),
        )
        self.assertEqual(spec.resources[0].parameters, [])


if __name__ == "__main__":
    unittest.main()
