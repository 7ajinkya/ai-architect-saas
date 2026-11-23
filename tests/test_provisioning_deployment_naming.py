"""Deployment namespace derivation tests."""

import unittest

from ai_architect.services.provisioning.deployment_naming import (
    NAMESPACE_PATTERN,
    deployment_namespace,
    namespaced_name,
)


class DeploymentNamespaceTests(unittest.TestCase):
    def test_namespace_is_stable_for_a_session(self):
        self.assertEqual(
            deployment_namespace("design-s1"),
            deployment_namespace(" design-s1 "),
        )
        self.assertRegex(deployment_namespace("design-s1"), NAMESPACE_PATTERN)
        self.assertEqual(len(deployment_namespace("design-s1")), 12)

    def test_different_sessions_get_different_namespaces(self):
        self.assertNotEqual(
            deployment_namespace("design-s1"),
            deployment_namespace("design-s2"),
        )

    def test_missing_session_keeps_legacy_names(self):
        self.assertEqual(deployment_namespace(None), "")
        self.assertEqual(namespaced_name("lambda-fn", ""), "lambda-fn")

    def test_namespaced_name_appends_validated_suffix(self):
        self.assertEqual(
            namespaced_name("lambda-fn", "a1b2c3d4"),
            "lambda-fn-a1b2c3d4",
        )
        with self.assertRaises(ValueError):
            namespaced_name("lambda-fn", "Not Safe")


if __name__ == "__main__":
    unittest.main()
