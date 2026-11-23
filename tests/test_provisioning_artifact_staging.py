"""Artifact staging + approval binding tests for the HITL provision flow.

The approved workspace is plan files plus ONLY hash-verified local
artifacts explicitly referenced by the approved spec. Every refusal
below happens before any subprocess starts.

No terraform binary, no AWS credentials, no network: subprocess
execution is mocked at the boundary; the real staging code runs
against temporary directories.

Run: .venv/Scripts/python -m pytest tests/test_provisioning_artifact_staging.py
"""
import hashlib
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ParameterSource,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
)
from ai_architect.services.provisioning.executor import (
    ExecutionRefused,
    _write_workspace,
    artifact_manifest_for_spec,
    build_provisioning_approval,
    load_verified_artifacts,
    require_provisioning_approval,
    run_terraform_apply,
    run_terraform_plan,
)

SHA = hashlib.sha256(b"demo-bytes").hexdigest()
LAMBDA_VALUES = {
    "runtime": "python3.12",
    "handler": "handler.handler",
    "memory_mb": "512",
    "timeout_seconds": "30",
    "code_artifact_path": "demo-handler.zip",
    "code_artifact_sha256": SHA,
}
FILES = {"main.tf": 'resource "x" "y" {}'}
MANIFEST = [{"name": "demo-handler.zip", "sha256": SHA}]


def _resource(kind, resource_id, values) -> ResourceInstance:
    return ResourceInstance(
        resource_id=resource_id,
        node_id=resource_id.split("-", 1)[-1],
        kind=kind,
        component_name=f"{kind.value} component",
        parameters=[
            ResolvedParameter(
                name=name, value=value,
                source=ParameterSource.REQUIRED_INPUT,
            )
            for name, value in values.items()
        ],
    )


def _spec() -> InfraSpec:
    import uuid
    from datetime import datetime

    return InfraSpec(
        spec_id="spec-1",
        provider="aws",
        region_code="ap-south-1",
        source_architecture=SourceArchitectureRef(
            artifact_id=str(uuid.uuid4()), version=1,
            session_id="design-1", upstream_inputs_hash="u",
            architecture_hash="a",
        ),
        resources=[
            _resource(
                ResourceKind.LAMBDA, "lambda-fn", LAMBDA_VALUES
            )
        ],
    )


def _artifact_dir(payload: bytes = b"demo-bytes") -> str:
    directory = tempfile.mkdtemp(prefix="artifacts-")
    (pathlib.Path(directory) / "demo-handler.zip").write_bytes(payload)
    return directory


class ManifestTests(unittest.TestCase):
    def test_manifest_projects_lambda_artifact(self):
        self.assertEqual(
            artifact_manifest_for_spec(_spec()), MANIFEST
        )

    def test_manifest_ignores_non_lambda_resources(self):
        spec = _spec()
        spec.resources.append(
            _resource(ResourceKind.S3, "s3-b", {"bucket_name": "b"})
        )
        self.assertEqual(artifact_manifest_for_spec(spec), MANIFEST)

    def test_manifest_empty_without_artifact_params(self):
        spec = _spec()
        spec.resources[0].parameters = []
        self.assertEqual(artifact_manifest_for_spec(spec), [])


class StagingTests(unittest.TestCase):
    def test_matching_bytes_stage(self):
        staged = load_verified_artifacts(
            MANIFEST, artifact_dir=_artifact_dir()
        )
        self.assertEqual(staged, {"demo-handler.zip": b"demo-bytes"})

    def test_changed_bytes_refuse(self):
        with self.assertRaises(ExecutionRefused) as ctx:
            load_verified_artifacts(
                MANIFEST, artifact_dir=_artifact_dir(b"other-bytes")
            )
        self.assertIn("does not match", str(ctx.exception))

    def test_missing_file_refuses(self):
        with self.assertRaises(ExecutionRefused) as ctx:
            load_verified_artifacts(
                MANIFEST, artifact_dir=tempfile.mkdtemp()
            )
        self.assertIn("missing or unreadable", str(ctx.exception))

    def test_traversal_name_refuses(self):
        evil = [{"name": "../evil.zip", "sha256": SHA}]
        with self.assertRaises(ExecutionRefused):
            load_verified_artifacts(evil, artifact_dir=_artifact_dir())

    def test_absolute_name_refuses(self):
        evil = [{"name": "/tmp/evil.zip", "sha256": SHA}]
        with self.assertRaises(ExecutionRefused):
            load_verified_artifacts(evil, artifact_dir=_artifact_dir())

    def test_non_zip_refuses(self):
        evil = [{"name": "code.tar.gz", "sha256": SHA}]
        with self.assertRaises(ExecutionRefused):
            load_verified_artifacts(evil, artifact_dir=_artifact_dir())

    def test_malformed_hash_refuses(self):
        evil = [{"name": "demo-handler.zip", "sha256": "abc"}]
        with self.assertRaises(ExecutionRefused):
            load_verified_artifacts(evil, artifact_dir=_artifact_dir())

    def test_malformed_manifest_is_a_caller_bug(self):
        with self.assertRaises(ValueError):
            load_verified_artifacts({"name": "x"})
        with self.assertRaises(ValueError):
            load_verified_artifacts([{"name": "x"}])

    def test_workspace_contains_files_and_bytes(self):
        import shutil

        staged = load_verified_artifacts(
            MANIFEST, artifact_dir=_artifact_dir()
        )
        workdir = _write_workspace(FILES, staged)
        try:
            self.assertEqual(
                (workdir / "main.tf").read_text(encoding="utf-8"),
                FILES["main.tf"],
            )
            self.assertEqual(
                (workdir / "demo-handler.zip").read_bytes(), b"demo-bytes"
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_workspace_rejects_escaping_relative_path(self):
        with self.assertRaises(ExecutionRefused):
            _write_workspace({"../evil.tf": "x"}, {})


class ApprovalBindingTests(unittest.TestCase):
    def _plan(self, **overrides):
        plan = {
            "tree_hash": "tree-1", "spec_id": "spec-1",
            "artifact_id": "artifact-1", "artifacts": list(MANIFEST),
            "files": dict(FILES),
        }
        plan.update(overrides)
        return plan

    def _approval(self, **overrides):
        record = build_provisioning_approval(
            approved=True, note="", tree_hash="tree-1", spec_id="spec-1",
            artifact_id="artifact-1", artifacts=list(MANIFEST),
        )
        record.update(overrides)
        return record

    def test_matching_artifacts_pass(self):
        self.assertTrue(
            require_provisioning_approval(
                self._approval(), self._plan()
            )["approved"]
        )

    def test_stale_artifact_hash_refuses(self):
        stale = [{"name": "demo-handler.zip", "sha256": "00" * 32}]
        with self.assertRaises(ExecutionRefused) as ctx:
            require_provisioning_approval(
                self._approval(), self._plan(artifacts=stale)
            )
        self.assertIn("artifacts", str(ctx.exception))

    def test_added_artifact_refuses_old_approval(self):
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(
                self._approval(artifacts=[]), self._plan()
            )

    def test_legacy_approval_without_manifest_refuses(self):
        legacy = {
            "approved": True, "note": "", "tree_hash": "tree-1",
            "spec_id": "spec-1", "artifact_id": "artifact-1",
        }
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(legacy, self._plan())

    def test_legacy_plan_without_manifest_matches_empty_approval(self):
        plan = self._plan()
        del plan["artifacts"]
        self.assertTrue(
            require_provisioning_approval(
                self._approval(artifacts=[]), plan
            )["approved"]
        )


class MockedExecutionTests(unittest.TestCase):
    def test_plan_stages_verified_bytes(self):
        stdout = "Plan: 1 to add, 0 to change, 0 to destroy."

        def fake_run(cmd, **kwargs):
            if cmd[1] == "plan":
                (
                    pathlib.Path(kwargs["cwd"]) / "ai-architect.tfplan"
                ).write_bytes(b"fake-plan-bytes")
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            return MagicMock(returncode=0, stdout="init ok", stderr="")

        with (
            patch(
                "ai_architect.services.provisioning.executor"
                ".terraform_available",
                return_value=True,
            ),
            patch(
                "ai_architect.services.provisioning.executor"
                ".subprocess.run",
                side_effect=fake_run,
            ) as run,
        ):
            outcome = run_terraform_plan(
                dict(FILES), artifact_manifest=MANIFEST,
                artifact_dir=_artifact_dir(),
            )
        self.assertEqual(outcome["status"], "plan_ok")
        self.assertEqual(
            outcome["summary"], {"add": 1, "change": 0, "destroy": 0}
        )
        self.assertEqual(
            outcome["plan_file"],
            {
                "name": "ai-architect.tfplan",
                "sha256": hashlib.sha256(b"fake-plan-bytes").hexdigest(),
            },
        )
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [["terraform", "init"], ["terraform", "plan"]],
        )

    def test_apply_stages_and_succeeds_mocked(self):
        workdir = pathlib.Path(
            tempfile.mkdtemp(prefix="ai-architect-apply-test-")
        )
        self.addCleanup(shutil.rmtree, workdir, True)

        def fake_run(cmd, **kwargs):
            if cmd[1] == "init":
                return MagicMock(returncode=0, stdout="init ok", stderr="")
            if cmd[1] == "plan":
                (workdir / "ai-architect.tfplan").write_bytes(
                    b"fake-plan-bytes"
                )
                return MagicMock(
                    returncode=0,
                    stdout="Plan: 1 to add, 0 to change, 0 to destroy.",
                    stderr="",
                )
            return MagicMock(
                returncode=0, stdout="apply complete", stderr=""
            )

        with (
            patch(
                "ai_architect.services.provisioning.executor"
                ".terraform_available",
                return_value=True,
            ),
            patch(
                "ai_architect.services.provisioning.executor"
                ".subprocess.run",
                side_effect=fake_run,
            ) as run,
        ):
            planned = run_terraform_plan(
                dict(FILES), artifact_manifest=MANIFEST,
                artifact_dir=_artifact_dir(), workspace_dir=workdir,
            )
            outcome = run_terraform_apply(
                dict(FILES), confirm="APPLY",
                artifact_manifest=MANIFEST,
                artifact_dir=_artifact_dir(),
                workspace_dir=workdir,
                saved_plan_sha256=planned["plan_file"]["sha256"],
            )
        self.assertEqual(planned["status"], "plan_ok")
        self.assertEqual(outcome["status"], "apply_ok")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [command[:2] for command in commands],
            [["terraform", "init"], ["terraform", "plan"],
             ["terraform", "init"], ["terraform", "apply"],
             ["terraform", "output"]],
        )
        self.assertEqual(
            commands[3],
            ["terraform", "apply", "-no-color", "-input=false",
             "-auto-approve", "ai-architect.tfplan"],
        )

    def test_apply_refuses_before_subprocess_on_bad_artifact(self):
        with (
            patch(
                "ai_architect.services.provisioning.executor"
                ".terraform_available",
                return_value=True,
            ),
            patch(
                "ai_architect.services.provisioning.executor"
                ".subprocess.run",
            ) as run,
        ):
            with self.assertRaises(ExecutionRefused):
                run_terraform_apply(
                    dict(FILES), confirm="APPLY",
                    artifact_manifest=MANIFEST,
                    artifact_dir=_artifact_dir(b"tampered"),
                )
        run.assert_not_called()

    def test_apply_still_needs_confirm(self):
        with self.assertRaises(ExecutionRefused):
            run_terraform_apply(
                dict(FILES), confirm="yes",
                artifact_manifest=MANIFEST,
                artifact_dir=_artifact_dir(),
            )


class NoExecutionBeforeApprovalTests(unittest.TestCase):
    def test_apply_requires_record(self):
        plan = {
            "tree_hash": "t", "spec_id": "s", "artifact_id": "a",
            "artifacts": [], "files": dict(FILES),
        }
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(None, plan)
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval({"approved": False}, plan)


if __name__ == "__main__":
    unittest.main()
