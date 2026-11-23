"""Boundary normalization tests for structured DynamoDB inputs."""
import unittest
from types import SimpleNamespace

from ai_architect.domain.provisioning.models import ResourceKind
from ai_architect.services.provisioning.structured_inputs import (
    StructuredInputError,
    normalize_resource_inputs,
)


def _table():
    return SimpleNamespace(
        resource_id="dynamodb-table", kind=ResourceKind.DYNAMODB
    )


class DynamoDbStructuredInputTests(unittest.TestCase):
    def test_json_attribute_definitions_and_key_schema_are_canonicalized(self):
        normalized = normalize_resource_inputs(
            _table(),
            {
                "attribute_definitions": (
                    '[{"attribute_name": "sk", "attribute_type": "N"}, '
                    '{"attribute_name": "pk", "attribute_type": "S"}]'
                ),
                "key_schema": (
                    '[{"attribute_name": "sk", "key_type": "RANGE"}, '
                    '{"attribute_name": "pk", "key_type": "HASH"}]'
                ),
            },
        )
        self.assertEqual(
            normalized["attribute_definitions"], "pk:S,sk:N"
        )
        self.assertEqual(normalized["key_schema"], "HASH:pk,RANGE:sk")

    def test_single_hash_key_is_canonicalized(self):
        normalized = normalize_resource_inputs(
            _table(),
            {
                "attribute_definitions": (
                    '[{"attribute_name": "pk", "attribute_type": "S"}]'
                ),
                "key_schema": (
                    '[{"attribute_name": "pk", "key_type": "HASH"}]'
                ),
            },
        )
        self.assertEqual(normalized["attribute_definitions"], "pk:S")
        self.assertEqual(normalized["key_schema"], "HASH:pk")

    def test_canonical_tokens_are_left_for_contract_decoder(self):
        normalized = normalize_resource_inputs(
            _table(),
            {
                "attribute_definitions": "sk:N,pk:S",
                "key_schema": "HASH:pk,RANGE:sk",
            },
        )
        self.assertEqual(
            normalized["attribute_definitions"], "sk:N,pk:S"
        )
        self.assertEqual(normalized["key_schema"], "HASH:pk,RANGE:sk")

    def test_object_json_is_rejected(self):
        with self.assertRaises(StructuredInputError) as ctx:
            normalize_resource_inputs(
                _table(),
                {
                    "attribute_definitions": (
                        '{"attribute_name": "pk", "attribute_type": "S"}'
                    )
                },
            )
        self.assertIn("must be a JSON array", str(ctx.exception))

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(StructuredInputError) as ctx:
            normalize_resource_inputs(
                _table(),
                {
                    "attribute_definitions": (
                        '[{"attribute_name": "pk", '
                        '"attribute_type": "S", "extra": true}]'
                    )
                },
            )
        self.assertIn("must contain exactly", str(ctx.exception))

    def test_missing_hash_key_is_rejected(self):
        with self.assertRaises(StructuredInputError) as ctx:
            normalize_resource_inputs(
                _table(),
                {
                    "key_schema": (
                        '[{"attribute_name": "sk", "key_type": "RANGE"}]'
                    )
                },
            )
        self.assertIn("exactly one HASH key", str(ctx.exception))

    def test_duplicate_attribute_name_is_rejected(self):
        with self.assertRaises(StructuredInputError) as ctx:
            normalize_resource_inputs(
                _table(),
                {
                    "attribute_definitions": (
                        '[{"attribute_name": "pk", "attribute_type": "S"}, '
                        '{"attribute_name": "pk", "attribute_type": "N"}]'
                    )
                },
            )
        self.assertIn("more than once", str(ctx.exception))

    def test_non_json_bracket_text_reports_parse_error(self):
        with self.assertRaises(StructuredInputError) as ctx:
            normalize_resource_inputs(
                _table(),
                {"attribute_definitions": "[not json]"},
            )
        self.assertIn("cannot be parsed", str(ctx.exception))

    def test_non_dynamodb_values_are_untouched(self):
        resource = SimpleNamespace(
            resource_id="s3-bucket", kind=ResourceKind.S3
        )
        values = {"attribute_definitions": "[{\"x\": 1}]"}
        self.assertEqual(normalize_resource_inputs(resource, values), values)


if __name__ == "__main__":
    unittest.main()
