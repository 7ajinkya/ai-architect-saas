"""Regression tests for Component Selection tradeoff tolerance.

Live e-commerce smoke: the model emitted per-component tradeoffs as
objects ({"description": ..., "options": [...]}) while the boundary
expected strings, failing the whole stage with 13 validation errors.
The boundary now accepts that exact shape and normalizes it
deterministically; garbage shapes still fail loudly.

Run: python -m unittest discover tests (with the project .venv)
"""
import unittest

from pydantic import ValidationError

from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    normalize_component_selection,
)


def _output(**overrides):
    payload = {
        "selected_components": [
            {
                "name": "Amazon SQS",
                "provider": "aws",
                "category": "messaging",
                "role": "buffer order events",
                "rationale": "decouples checkout from fulfillment",
                "evidence_refs": [],
                "assumptions": [],
                "tradeoffs": [],
                "confidence": "high",
                "evidence_support": "insufficient",
                "provider_specific": False,
                "alternatives": [],
                "region_constraints": [],
            }
        ],
        "rejected_candidates": [],
        "unresolved_tradeoffs": [],
        "evidence_references": [],
        "assumptions": [],
        "warnings": [],
    }
    payload["selected_components"][0].update(
        overrides.pop("component", {})
    )
    payload.update(overrides)
    return ComponentSelectionLLMOutput.model_validate(payload)


class SelectionTradeoffTests(unittest.TestCase):
    def test_object_tradeoff_normalized(self):
        out = _output(component={
            "tradeoffs": [
                {
                    "description": "Standard versus FIFO queue",
                    "options": ["standard", "fifo"],
                }
            ]
        })
        result = normalize_component_selection(out)
        self.assertEqual(
            result.selected_components[0].tradeoffs,
            ["Standard versus FIFO queue (options: standard; fifo)"],
        )

    def test_object_tradeoff_without_options_normalized(self):
        out = _output(component={
            "tradeoffs": [{"description": "Managed versus self-hosted"}]
        })
        result = normalize_component_selection(out)
        self.assertEqual(
            result.selected_components[0].tradeoffs,
            ["Managed versus self-hosted"],
        )

    def test_string_tradeoffs_unchanged(self):
        out = _output(component={
            "tradeoffs": ["SQS standard vs FIFO: ordering against throughput"]
        })
        result = normalize_component_selection(out)
        self.assertEqual(
            result.selected_components[0].tradeoffs,
            ["SQS standard vs FIFO: ordering against throughput"],
        )

    def test_garbage_tradeoff_object_still_rejected(self):
        with self.assertRaises(ValidationError):
            _output(component={"tradeoffs": [{"foo": 1}]})
        with self.assertRaises(ValidationError):
            _output(component={"tradeoffs": [{"description": ""}]})
        with self.assertRaises(ValidationError):
            _output(component={"tradeoffs": [42]})


if __name__ == "__main__":
    unittest.main()
