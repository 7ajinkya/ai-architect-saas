"""Evidence for the serverless_function module contract (Slice 4D).

The platform never authors application code: the deployment package is
externally produced (see infra/lambda/build_demo_artifact.py) and arrives
as two human-supplied, contract-validated inputs (basename + sha256).
These tests pin that boundary:

- the basename accepts only a bare ``*.zip`` filename (no traversal,
  no directories, no other suffix);
- the sha256 accepts only 64 lowercase hex characters;
- rendering embeds the file reference plus the base64 of the verified
  hash without reading the operator's filesystem;
- without both artifact inputs the Lambda is unrenderable, loudly.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_lambda_artifact.py
"""
import base64
import binascii
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
from ai_architect.domain.provisioning.module import ModuleId
from ai_architect.domain.provisioning.render import (
    RenderErrorCode,
    RenderStatus,
)
from ai_architect.services.provisioning.module_catalog import (
    SERVERLESS_FUNCTION,
)
from ai_architect.services.provisioning.terraform_renderer import (
    render_infra_spec,
)

NOW = datetime(2026, 1, 1)
PROVIDER = "aws"
REGION = "us-east-1"

ARTIFACT_PATH = "demo-function.zip"
ARTIFACT_SHA256 = "ab" * 32

FULL_LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "index.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
    "code_artifact_path": ARTIFACT_PATH,
    "code_artifact_sha256": ARTIFACT_SHA256,
}

DDB_VALUES = {
    "billing_mode": "PAY_PER_REQUEST",
    "attribute_definitions": "pk:S",
    "key_schema": "HASH:pk",
    "point_in_time_recovery": "true",
}

LOG_GROUP_VALUES = {"retention_days": "30"}


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


def _golden_spec(lambda_values=None) -> InfraSpec:
    return _spec([
        _resource(ResourceKind.APIGATEWAY_HTTP, "apigateway-api", {}),
        _resource(
            ResourceKind.LAMBDA, "lambda-fn",
            dict(FULL_LAMBDA_VALUES, **(lambda_values or {})),
        ),
        _resource(ResourceKind.DYNAMODB, "dynamodb-table", DDB_VALUES),
        _resource(
            ResourceKind.CLOUDWATCH_LOG_GROUP, "log-group-fn",
            LOG_GROUP_VALUES,
        ),
    ])


class ArtifactContractTests(unittest.TestCase):
    """The catalog declares basename + sha256, both constrained."""

    def test_contract_declares_artifact_inputs(self):
        by_name = {i.name: i for i in SERVERLESS_FUNCTION.inputs}
        self.assertIn("code_artifact_path", by_name)
        self.assertIn("code_artifact_sha256", by_name)
        self.assertTrue(by_name["code_artifact_path"].required)
        self.assertTrue(by_name["code_artifact_sha256"].required)

    def test_module_id_is_capability_level(self):
        self.assertEqual(SERVERLESS_FUNCTION.module_id, ModuleId.SERVERLESS_FUNCTION)


class ArtifactValidationTests(unittest.TestCase):
    """Traversal, directories, wrong suffixes and malformed hashes fail."""

    def _status_and_codes(self, lambda_values):
        result = render_infra_spec(_golden_spec(lambda_values))
        return result.status, {d.code for d in result.diagnostics}

    def test_valid_artifact_renders(self):
        result = render_infra_spec(_golden_spec())
        self.assertEqual(result.status, RenderStatus.RENDERED)

    def test_traversal_path_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_path": "../evil.zip"}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)

    def test_absolute_path_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_path": "/tmp/evil.zip"}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)

    def test_non_zip_suffix_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_path": "code.tar.gz"}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)

    def test_short_hash_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_sha256": "abc"}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)

    def test_uppercase_hash_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_sha256": "AB" * 32}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)

    def test_non_hex_hash_rejected(self):
        status, _ = self._status_and_codes(
            {"code_artifact_sha256": "zz" * 32}
        )
        self.assertNotEqual(status, RenderStatus.RENDERED)


class ArtifactRenderTests(unittest.TestCase):
    """The render references the bundle; it never reads the filesystem."""

    def test_filename_and_hash_are_embedded(self):
        result = render_infra_spec(_golden_spec())
        self.assertEqual(result.status, RenderStatus.RENDERED)
        combined = "\n".join(result.source_tree().values())
        self.assertRegex(combined, rf'filename\s+= "{ARTIFACT_PATH}"')
        expected_b64 = base64.b64encode(
            binascii.unhexlify(ARTIFACT_SHA256)
        ).decode("ascii")
        self.assertRegex(combined, rf'source_code_hash\s+= "{expected_b64}"')

    def test_render_reads_no_filesystem(self):
        # The referenced zip does not exist on disk; rendering must still
        # succeed because only the hash shape is verified, never the bytes.
        import pathlib

        self.assertFalse(
            (pathlib.Path.cwd() / ARTIFACT_PATH).exists(),
            msg="test precondition: the artifact must not exist locally",
        )
        result = render_infra_spec(_golden_spec())
        self.assertEqual(result.status, RenderStatus.RENDERED)

    def test_missing_artifact_is_unrenderable(self):
        values = {
            key: value for key, value in FULL_LAMBDA_VALUES.items()
            if not key.startswith("code_artifact_")
        }
        spec = _spec([_resource(ResourceKind.LAMBDA, "lambda-fn", values)])
        result = render_infra_spec(spec)
        self.assertEqual(result.status, RenderStatus.MISSING_CODE_ARTIFACT)
        codes = {d.code for d in result.diagnostics}
        self.assertIn(RenderErrorCode.MISSING_CODE_ARTIFACT, codes)


if __name__ == "__main__":
    unittest.main()
