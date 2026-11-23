"""Focused regression tests for Component Selection warnings normalization.

Covers the observed live failure shape where the LLM emitted warnings as
dictionaries containing a "message" field plus metadata, which the
boundary schema (LenientStrList -> list[str]) previously could not
coerce and therefore rejected.

All tests are pure unit checks: no LLM, Qdrant, AWS, or network access.
"""
import unittest
from pydantic import ValidationError

from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    _coerce_str_list,
    _dict_to_str,
)
from ai_architect.agents.component_selection.run import MAX_SELECTION_ATTEMPTS


class DictToStrMessageTests(unittest.TestCase):
    def test_message_dict_extracts_message(self):
        self.assertEqual(
            _dict_to_str({"message": "Live-provider evidence insufficient"}),
            "live-provider evidence insufficient",
        )

    def test_message_dict_with_metadata_keeps_message_only(self):
        self.assertEqual(
            _dict_to_str(
                {
                    "message": "Missing live-provider evidence for X",
                    "insufficient_evidence": True,
                    "detail": "metadata",
                }
            ),
            "missing live-provider evidence for x",
        )

    def test_empty_message_is_not_accepted(self):
        # Blank message folds to nothing; the dict is returned unchanged
        # and pydantic rejects it downstream.
        self.assertEqual(_dict_to_str({"message": "   "}), {"message": "   "})

    def test_non_string_message_is_not_accepted(self):
        self.assertEqual(_dict_to_str({"message": 42}), {"message": 42})

    def test_none_message_is_not_accepted(self):
        self.assertEqual(_dict_to_str({"message": None}), {"message": None})


class CoerceStrListMessageTests(unittest.TestCase):
    def test_list_of_message_dicts_normalizes(self):
        self.assertEqual(
            _coerce_str_list(
                [
                    {"message": "warning one"},
                    {"message": "warning two", "extra": "metadata"},
                ]
            ),
            ["warning one", "warning two"],
        )

    def test_single_string_warning_remains_supported(self):
        self.assertEqual(_coerce_str_list("single warning"), "single warning")

    def test_unrecognized_dict_shape_rejected_by_coercion(self):
        # Returned untouched so pydantic rejects it loudly downstream.
        self.assertEqual(
            _coerce_str_list([{"type": "x", "detail": "y"}]),
            [{"type": "x", "detail": "y"}],
        )


class SchemaValidationMessageTests(unittest.TestCase):
    BASE = {
        "selected_components": [
            {
                "name": "Amazon DynamoDB",
                "provider": "aws",
                "category": "database",
                "role": "store URL mappings",
                "rationale": "key-value store",
                "confidence": "high",
                "evidence_support": "strong",
                "evidence_refs": ["ev-1"],
            }
        ],
        "rejected_candidates": [],
        "unresolved_tradeoffs": [],
        "evidence_references": ["ev-1"],
        "assumptions": [],
        "warnings": [],
    }

    def test_warning_message_dict_normalizes_to_list_of_strings(self):
        payload = dict(self.BASE)
        payload["warnings"] = [
            {"message": "Live-provider evidence insufficient for X"},
            {"message": "Missing live-provider evidence for Y", "extra": "m"},
        ]
        out = ComponentSelectionLLMOutput.model_validate(payload)
        self.assertEqual(
            out.warnings,
            [
                "live-provider evidence insufficient for x",
                "missing live-provider evidence for y",
            ],
        )

    def test_single_string_warning_remains_valid(self):
        payload = dict(self.BASE)
        payload["warnings"] = "single warning"
        out = ComponentSelectionLLMOutput.model_validate(payload)
        self.assertEqual(out.warnings, "single warning")

    def test_single_string_warning_normalizes_to_list(self):
        from ai_architect.agents.component_selection.models import _to_str_list

        self.assertEqual(_to_str_list("single warning"), ["single warning"])

    def test_unrecognized_dict_warning_rejected(self):
        payload = dict(self.BASE)
        payload["warnings"] = [{"type": "insufficient_evidence", "detail": "auth layer"}]
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(payload)

    def test_malformed_message_value_rejected(self):
        payload = dict(self.BASE)
        payload["warnings"] = [{"message": 42}]
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(payload)


class RetryBoundTests(unittest.TestCase):
    def test_max_selection_attempts_unchanged(self):
        self.assertEqual(MAX_SELECTION_ATTEMPTS, 2)


if __name__ == "__main__":
    unittest.main()
