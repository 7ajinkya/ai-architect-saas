"""Region normalization regression tests.

Covers the defect found by the first manual end-to-end smoke test: a user
stating "US East (N. Virginia)" produced the literal string in the
requirements artifact, and Cost Estimation then aborted the whole run with
"Cannot normalize region 'US East (N. Virginia)' to an AWS region code".

No network and no LLM: region metadata comes from botocore's bundled
endpoint data and pricing is faked.
"""
import unittest
from unittest.mock import patch

from ai_architect.services.cost import pricing_service as ps
from ai_architect.services.region import normalizer as region_normalizer
from ai_architect.services.region.normalizer import (
    LEGACY_REGION_ALIASES,
    RegionNormalizationError,
    normalize_region,
    region_display_names,
    supported_region_codes,
)


class AwsDisplayNameTests(unittest.TestCase):
    """The exact human-readable forms a user is likely to type."""

    def test_us_east_n_virginia(self):
        self.assertEqual(normalize_region("US East (N. Virginia)"), "us-east-1")

    def test_us_west_oregon(self):
        self.assertEqual(normalize_region("US West (Oregon)"), "us-west-2")

    def test_europe_ireland(self):
        self.assertEqual(normalize_region("Europe (Ireland)"), "eu-west-1")

    def test_asia_pacific_mumbai(self):
        self.assertEqual(normalize_region("Asia Pacific (Mumbai)"), "ap-south-1")

    def test_every_known_display_name_resolves_to_its_own_code(self):
        for code, display in region_display_names().items():
            if not display:
                continue
            with self.subTest(code=code):
                self.assertEqual(normalize_region(display), code)


class CanonicalCodeTests(unittest.TestCase):
    def test_codes_pass_through(self):
        for code in ("us-east-1", "us-west-2", "eu-west-1", "ap-south-1"):
            with self.subTest(code=code):
                self.assertEqual(normalize_region(code), code)

    def test_every_known_code_passes_through(self):
        for code in supported_region_codes():
            with self.subTest(code=code):
                self.assertEqual(normalize_region(code), code)

    def test_code_matching_is_case_insensitive(self):
        self.assertEqual(normalize_region("US-EAST-1"), "us-east-1")


class FormattingTests(unittest.TestCase):
    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(normalize_region("  US East (N. Virginia)  "), "us-east-1")
        self.assertEqual(normalize_region(" us-east-1 "), "us-east-1")

    def test_case_insensitive_display_name(self):
        self.assertEqual(normalize_region("us east (n. virginia)"), "us-east-1")
        self.assertEqual(normalize_region("US WEST (OREGON)"), "us-west-2")

    def test_punctuation_tolerance(self):
        for text in (
            "US East (N. Virginia)",
            "US East (N Virginia)",
            "US East, N. Virginia",
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_region(text), "us-east-1")


class LegacyAliasTests(unittest.TestCase):
    """Pre-existing curated aliases must keep working."""

    def test_legacy_aliases_resolve(self):
        expected = {
            "mumbai region": "ap-south-1",
            "ireland": "eu-west-1",
        }
        for alias, code in expected.items():
            with self.subTest(alias=alias):
                self.assertEqual(normalize_region(alias), code)

    def test_legacy_alias_case_and_whitespace(self):
        self.assertEqual(normalize_region("Mumbai Region"), "ap-south-1")
        self.assertEqual(normalize_region("  ireland "), "eu-west-1")

    def test_legacy_alias_table_preserved(self):
        for alias in ("mumbai region", "mumbai", "us east 1", "n. virginia",
                      "north virginia", "ireland"):
            with self.subTest(alias=alias):
                self.assertIn(alias, LEGACY_REGION_ALIASES)

    def test_pricing_service_delegate_resolves_display_names(self):
        """Existing pricing callers keep working through the delegate."""
        self.assertEqual(
            ps.normalize_region("US East (N. Virginia)"), "us-east-1"
        )
        self.assertEqual(ps.normalize_region("Mumbai region"), "ap-south-1")

    def test_pricing_service_delegate_still_raises_on_unknown(self):
        with self.assertRaises(ValueError):
            ps.normalize_region("Atlantis")

    def test_pricing_service_reexports_aliases(self):
        self.assertIs(ps.REGION_ALIASES, LEGACY_REGION_ALIASES)


class FailClosedTests(unittest.TestCase):
    def test_unknown_region_raises(self):
        with self.assertRaises(RegionNormalizationError):
            normalize_region("Atlantis")

    def test_empty_region_raises(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(RegionNormalizationError):
                    normalize_region(value)

    def test_plausible_but_unknown_code_raises(self):
        with self.assertRaises(RegionNormalizationError) as ctx:
            normalize_region("us-east-99")
        self.assertIn("us-east-99", str(ctx.exception))

    def test_near_miss_phrases_are_not_fuzzy_matched(self):
        """A region embedded in a longer phrase must not be guessed at.

        Substring or token matching would let "us-east-1 north america" or
        "somewhere in Europe" silently pick a region the user never named
        unambiguously.
        """
        for text in (
            "us-east-1 north america",
            "somewhere in Europe",
            "US East (N. Virginia) region",
            "east coast",
        ):
            with self.subTest(text=text):
                with self.assertRaises(RegionNormalizationError):
                    normalize_region(text)

    def test_never_silently_defaults(self):
        """The error must not contain a substituted region code."""
        with self.assertRaises(RegionNormalizationError) as ctx:
            normalize_region("Atlantis")
        message = str(ctx.exception)
        self.assertNotIn("no default", message.replace(
            "no default is assumed", ""
        ))

    def test_ambiguous_display_name_raises_rather_than_guessing(self):
        fake_metadata = (
            frozenset({"test-a-1", "test-b-1"}),
            {"same place": {"test-a-1", "test-b-1"}},
        )
        with patch.object(
            region_normalizer, "_aws_region_metadata", return_value=fake_metadata
        ):
            with self.assertRaises(RegionNormalizationError) as ctx:
                normalize_region("Same Place")
        message = str(ctx.exception)
        self.assertIn("ambiguous", message.lower())
        self.assertIn("test-a-1", message)
        self.assertIn("test-b-1", message)


class ProviderAwarenessTests(unittest.TestCase):
    def test_other_providers_are_refused(self):
        for provider in ("gcp", "azure", "google", ""):
            with self.subTest(provider=provider):
                with self.assertRaises(RegionNormalizationError):
                    normalize_region("us-east-1", provider=provider)

    def test_aws_region_name_is_not_valid_for_another_provider(self):
        with self.assertRaises(RegionNormalizationError) as ctx:
            normalize_region("US East (N. Virginia)", provider="gcp")
        self.assertIn("provider", str(ctx.exception).lower())

    def test_aws_provider_is_accepted(self):
        self.assertEqual(
            normalize_region("US East (N. Virginia)", provider="aws"), "us-east-1"
        )
        self.assertEqual(
            normalize_region("US East (N. Virginia)", provider="AWS"), "us-east-1"
        )


class MetadataTests(unittest.TestCase):
    def test_supported_codes_are_non_trivial(self):
        codes = supported_region_codes()
        self.assertGreater(len(codes), 20)
        for code in ("us-east-1", "us-west-2", "eu-west-1", "ap-south-1"):
            self.assertIn(code, codes)

    def test_display_names_come_from_aws_metadata(self):
        names = region_display_names()
        self.assertEqual(names["us-east-1"], "US East (N. Virginia)")
        self.assertEqual(names["ap-south-1"], "Asia Pacific (Mumbai)")

    def test_no_llm_or_mcp_participates(self):
        """Region resolution must stay deterministic."""
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "src"
            / "ai_architect"
            / "services"
            / "region"
            / "normalizer.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "langchain",
            "create_llm",
            "aws_mcp",
            "AWSMCPAdapter",
            "infrastructure.llm",
        ):
            self.assertNotIn(forbidden, source)


class PricingPrefixBoundaryTests(unittest.TestCase):
    """A missing usage prefix is unpriced, not a pipeline-wide crash."""

    def _selection(self, name: str, category: str):
        from ai_architect.agents.component_selection.models import (
            ComponentCandidate,
            ComponentSelectionResult,
        )

        return ComponentSelectionResult(
            selected_components=[
                ComponentCandidate(
                    name=name, provider="aws", category=category,
                    role="r", rationale="x", confidence="high",
                    evidence_support="strong",
                )
            ]
        )

    def test_prefix_free_component_prices_in_unmapped_region(self):
        """DynamoDB needs no prefix, so it prices normally anywhere."""
        client = ps.PricingService(
            client=_NoCallsClient([{"PriceList": []}]),
        )
        components, warnings = client.price_components(
            self._selection("Amazon DynamoDB", "database"), "eu-west-1"
        )
        self.assertEqual(len(components), 1)
        # No usable SKU returned, but that is an unpriced outcome, not a raise.
        self.assertTrue(components[0].unavailable_reason)
        self.assertNotIn("prefix", components[0].unavailable_reason.lower())

    def test_prefix_requiring_component_is_explicitly_unpriced(self):
        """S3 needs a usage prefix; without one it must not be guessed."""
        service = ps.PricingService(client=_NoCallsClient([]))
        components, _ = service.price_components(
            self._selection("Amazon S3", "storage"), "eu-west-1"
        )
        self.assertEqual(len(components), 1)
        reason = components[0].unavailable_reason
        self.assertIn("prefix", reason.lower())
        self.assertIn("eu-west-1", reason)
        self.assertEqual(components[0].facts, [])

    def test_unmapped_region_no_longer_raises(self):
        """The pre-fix behaviour was a ValueError for the whole region."""
        service = ps.PricingService(client=_NoCallsClient([]))
        components, _ = service.price_components(
            self._selection("Amazon S3", "storage"), "us-west-2"
        )
        self.assertEqual(len(components), 1)

    def test_mapped_region_still_prices_prefix_requiring_component(self):
        self.assertIn("us-east-1", ps.REGION_USAGE_PREFIXES)
        service = ps.PricingService(client=_NoCallsClient([]))
        components, _ = service.price_components(
            self._selection("Amazon S3", "storage"), "us-east-1"
        )
        # With a prefix available the request is constructed; the fake client
        # returns nothing, so the outcome is the ordinary "no usable SKU".
        self.assertNotIn("prefix", components[0].unavailable_reason.lower())


class _NoCallsClient:
    """Pricing API stub that records calls and returns nothing usable."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get_products(self, **kwargs):
        self.calls.append(kwargs)
        return self._pages.pop(0) if self._pages else {"PriceList": []}


if __name__ == "__main__":
    unittest.main()
