"""Effective-region resolution: canonical field vs region assumptions.

Locks the verified E-commerce/Video defect: a canonical artifact with
``extraction.region == ""`` but a ``us-east-1`` region assumption
failed Cost Estimation with "Cannot normalize an empty region" while
the UI displayed the assumption. The resolver promotes a single valid
region assumption deterministically and stays fail-closed otherwise.

No network and no LLM.
"""
import unittest

from ai_architect.domain.requirements.models import (
    Assumption,
    RequirementsExtraction,
)
from ai_architect.services.region.normalizer import (
    RegionNormalizationError,
    resolve_effective_region,
)


def _extraction(region="", assumptions=(), **overrides):
    fields = {
        "application_description": "app",
        "cloud_provider": "aws",
    }
    fields.update(overrides)
    return RequirementsExtraction(
        **fields,
        region=region,
        assumptions=list(assumptions),
    )


def _assumption(name, value, source="assumption"):
    return Assumption(name=name, value=value, source=source)


class ExplicitRegionTests(unittest.TestCase):
    def test_explicit_region_wins_without_assumptions(self):
        extraction = _extraction(region="us-east-1")
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_explicit_region_wins_over_assumption(self):
        extraction = _extraction(
            region="ap-south-1",
            assumptions=[_assumption("region", "us-east-1")],
        )
        self.assertEqual(resolve_effective_region(extraction), "ap-south-1")

    def test_malformed_explicit_region_still_fails(self):
        extraction = _extraction(region="Atlantis")
        with self.assertRaises(RegionNormalizationError):
            resolve_effective_region(extraction)


class AssumptionPromotionTests(unittest.TestCase):
    def test_single_region_assumption_promoted(self):
        extraction = _extraction(
            assumptions=[_assumption("region", "us-east-1")])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_primary_region_assumption_promoted(self):
        extraction = _extraction(
            assumptions=[_assumption("primary region", "us-east-1",
                                     source="architect_default")])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_display_name_assumption_promoted(self):
        extraction = _extraction(
            assumptions=[_assumption("region", "US East (N. Virginia)")])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_duplicate_same_value_assumptions_agree(self):
        extraction = _extraction(assumptions=[
            _assumption("region", "us-east-1"),
            _assumption("primary region", "us-east-1"),
        ])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_unrelated_assumptions_ignored(self):
        extraction = _extraction(assumptions=[
            _assumption("retention period", "30"),
            _assumption("region", "us-east-1"),
        ])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_garbage_assumption_value_ignored(self):
        extraction = _extraction(assumptions=[
            _assumption("region", "somewhere nice"),
            _assumption("primary region", "us-east-1"),
        ])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")


class ConflictAndAbsenceTests(unittest.TestCase):
    def test_conflicting_assumptions_fail_deterministically(self):
        extraction = _extraction(assumptions=[
            _assumption("region", "us-east-1"),
            _assumption("primary region", "ap-south-1"),
        ])
        with self.assertRaises(RegionNormalizationError) as ctx:
            resolve_effective_region(extraction)
        message = str(ctx.exception)
        self.assertIn("Conflicting region assumptions", message)
        self.assertIn("us-east-1", message)
        self.assertIn("ap-south-1", message)

    def test_no_region_anywhere_preserves_empty_region_failure(self):
        extraction = _extraction()
        with self.assertRaises(RegionNormalizationError) as ctx:
            resolve_effective_region(extraction)
        self.assertIn("Cannot normalize an empty region",
                      str(ctx.exception))

    def test_only_garbage_assumption_preserves_empty_region_failure(self):
        extraction = _extraction(
            assumptions=[_assumption("region", "somewhere nice")])
        with self.assertRaises(RegionNormalizationError) as ctx:
            resolve_effective_region(extraction)
        self.assertIn("Cannot normalize an empty region",
                      str(ctx.exception))


class ConstrainedRegionTests(unittest.TestCase):
    def test_regional_constraint_blocks_promotion(self):
        extraction = _extraction(
            assumptions=[_assumption("region", "us-east-1")],
            compliance_requirements="Must satisfy GDPR data residency.",
        )
        with self.assertRaises(RegionNormalizationError) as ctx:
            resolve_effective_region(extraction)
        self.assertIn("explicitly", str(ctx.exception))

    def test_regional_constraint_with_explicit_region_uses_it(self):
        extraction = _extraction(
            region="eu-west-1",
            assumptions=[_assumption("region", "us-east-1")],
            compliance_requirements="Must satisfy GDPR data residency.",
        )
        self.assertEqual(resolve_effective_region(extraction), "eu-west-1")


class EcommerceVideoRegressionTests(unittest.TestCase):
    """Shapes replayed from the failed manual sessions."""

    def test_ecommerce_shape_proceeds(self):
        extraction = _extraction(assumptions=[
            _assumption("region", "us-east-1"),
            _assumption("deployment_scope", "regional"),
        ])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_video_shape_proceeds(self):
        extraction = _extraction(assumptions=[
            _assumption("primary region", "us-east-1",
                        source="architect_default"),
        ])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")

    def test_promotion_leaves_provenance_intact(self):
        assumption = _assumption("region", "us-east-1")
        extraction = _extraction(assumptions=[assumption])
        self.assertEqual(resolve_effective_region(extraction), "us-east-1")
        # Canonical field untouched; assumption untouched: downstream can
        # distinguish promoted (field empty) from explicitly stated.
        self.assertEqual(extraction.region, "")
        self.assertEqual(extraction.assumptions[0].value, "us-east-1")
        self.assertEqual(extraction.assumptions[0].source, "assumption")


if __name__ == "__main__":
    unittest.main()
