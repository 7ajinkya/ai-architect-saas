"""Retrieval provider-construction fault isolation.

Regression test for the WinError 10061 component-selection failure:
several providers perform I/O in their constructors (the Qdrant
provider verifies its collection on connect), which used to escape
RetrievalService's retrieve-time fault isolation and abort the whole
component-selection stage when the dependency was down.

A provider whose construction fails must degrade to "unavailable"
(exactly like a retrieve-time failure), never abort retrieval.

Run: poetry run python -m unittest discover tests
"""
import asyncio
import unittest
from unittest.mock import patch

from ai_architect.services.knowledge import retrieval as retrieval_mod
from ai_architect.services.knowledge.retrieval import RetrievalService


class ProviderConstructionGuardTests(unittest.TestCase):
    def test_failing_provider_construction_is_skipped(self):
        with patch.object(
            retrieval_mod, "QdrantProvider", side_effect=ConnectionError("refused")
        ):
            service = RetrievalService()
        self.assertNotIn("qdrant-indexed", service.list_providers())
        # The healthy provider is unaffected.
        self.assertIn("aws-mcp", service.list_providers())

    def test_all_providers_failing_still_returns_empty_bundle(self):
        with (
            patch.object(
                retrieval_mod, "QdrantProvider", side_effect=ConnectionError("down")
            ),
            patch.object(
                retrieval_mod, "AWSProvider", side_effect=ConnectionError("down")
            ),
        ):
            service = RetrievalService()
            bundle = asyncio.run(service.retrieve("test query", top_k=2))
        self.assertEqual(service.list_providers(), [])
        self.assertEqual(bundle.evidence, [])

    def test_successful_construction_registers_both_providers(self):
        class _StubProvider:
            def __init__(self, *args, **kwargs):
                pass

            def is_available(self):
                return True

            async def retrieve(self, query="", top_k=5, filters=None):
                return []

        with (
            patch.object(retrieval_mod, "QdrantProvider", _StubProvider),
            patch.object(retrieval_mod, "AWSProvider", _StubProvider),
        ):
            service = RetrievalService()
            bundle = asyncio.run(service.retrieve("test query", top_k=2))
        # Healthy path is unchanged: both providers registered, empty
        # results still build a valid (empty) bundle.
        self.assertEqual(
            sorted(service.list_providers()), ["aws-mcp", "qdrant-indexed"]
        )
        self.assertEqual(bundle.evidence, [])


if __name__ == "__main__":
    unittest.main()
