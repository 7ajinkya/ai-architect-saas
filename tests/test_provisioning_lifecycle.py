"""Post-apply lifecycle primitives: outputs, smoke, destroy and drift.

Every subprocess is faked here; the live AWS behavior is covered by the
opt-in provisioning smoke run, not by the unit suite.
"""
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ai_architect.services.provisioning import executor
from ai_architect.services.provisioning.executor import (
    DESTROY_PLAN_FILENAME,
    SAVED_PLAN_FILENAME,
    ExecutionRefused,
    require_destroy_plan_review,
    run_terraform_apply,
    run_terraform_destroy,
    run_terraform_destroy_plan,
    run_terraform_drift_check,
    run_terraform_plan,
)

FILES = {"main.tf": 'output "unused" { value = "x" }\n'}


class _FakeResponse:
    status = 204

    def read(self, _size=-1):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeTerraform:
    def __init__(self, *, plan_rc=0, apply_rc=0, outputs=None):
        self.plan_rc = plan_rc
        self.apply_rc = apply_rc
        self.outputs = "{}" if outputs is None else json.dumps(outputs)

    def __call__(self, command, **kwargs):
        workdir = pathlib.Path(kwargs["cwd"])
        if command[1] == "init":
            return MagicMock(returncode=0, stdout="init ok", stderr="")
        if command[1] == "plan":
            if "-out" in command:
                name = command[command.index("-out") + 1]
                (workdir / name).write_bytes(b"fake-plan-bytes")
            return MagicMock(
                returncode=self.plan_rc,
                stdout="Plan: 1 to add, 0 to change, 0 to destroy.",
                stderr="",
            )
        if command[1] == "apply":
            return MagicMock(
                returncode=self.apply_rc,
                stdout="Apply complete!",
                stderr="",
            )
        if command[1] == "output":
            return MagicMock(
                returncode=0, stdout=self.outputs, stderr=""
            )
        raise AssertionError(f"unexpected terraform command: {command}")


class _LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = pathlib.Path(
            tempfile.mkdtemp(prefix="ai-architect-lifecycle-")
        )
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.available = patch.object(
            executor, "terraform_available", return_value=True
        )
        self.available.start()
        self.addCleanup(self.available.stop)

    def _runner(self, fake):
        patcher = patch.object(executor.subprocess, "run", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)


class ApplyInspectionTests(_LifecycleTestCase):
    def test_apply_records_outputs_and_passes_the_http_smoke(self):
        runner = _FakeTerraform(outputs={
            "http_api-api__api_endpoint": {
                "value": "https://example.execute-api.us-east-1.amazonaws.com"
            },
        })
        self._runner(runner)
        planned = run_terraform_plan(FILES, workspace_dir=self.workdir)
        self.assertEqual(planned["status"], "plan_ok")

        with patch.object(
            executor.urllib.request, "urlopen", return_value=_FakeResponse()
        ):
            outcome = run_terraform_apply(
                FILES,
                confirm="APPLY",
                workspace_dir=self.workdir,
                saved_plan_sha256=planned["plan_file"]["sha256"],
            )

        self.assertEqual(outcome["status"], "apply_ok")
        self.assertEqual(outcome["outputs"]["status"], "ok")
        self.assertEqual(
            outcome["outputs"]["values"]["http_api-api__api_endpoint"]["value"],
            "https://example.execute-api.us-east-1.amazonaws.com",
        )
        self.assertEqual(outcome["smoke"]["status"], "ok")
        self.assertEqual(outcome["smoke"]["http_status"], 204)
        record = json.loads(
            (self.workdir / "deployment.json").read_text("utf-8")
        )
        self.assertEqual(record["smoke"]["status"], "ok")

    def test_apply_without_an_endpoint_reports_smoke_not_applicable(self):
        runner = _FakeTerraform(outputs={})
        self._runner(runner)
        planned = run_terraform_plan(FILES, workspace_dir=self.workdir)
        outcome = run_terraform_apply(
            FILES,
            confirm="APPLY",
            workspace_dir=self.workdir,
            saved_plan_sha256=planned["plan_file"]["sha256"],
        )
        self.assertEqual(outcome["status"], "apply_ok")
        self.assertEqual(outcome["smoke"]["status"], "not_applicable")

    def test_a_failed_output_read_never_fails_the_apply(self):
        runner = _FakeTerraform()
        self._runner(runner)
        planned = run_terraform_plan(FILES, workspace_dir=self.workdir)
        with patch.object(
            executor,
            "_read_outputs",
            return_value={"status": "unavailable", "error": "boom"},
        ):
            outcome = run_terraform_apply(
                FILES,
                confirm="APPLY",
                workspace_dir=self.workdir,
                saved_plan_sha256=planned["plan_file"]["sha256"],
            )
        self.assertEqual(outcome["status"], "apply_ok")
        self.assertEqual(outcome["smoke"]["status"], "not_checked")


class DestroyTests(_LifecycleTestCase):
    def test_destroy_uses_a_distinct_reviewed_plan(self):
        planned = run_terraform_destroy_plan(
            FILES, workspace_dir=self.workdir
        )
        self.assertEqual(planned["status"], "destroy_plan_ok")
        self.assertEqual(planned["plan_file"]["name"], DESTROY_PLAN_FILENAME)
        self.assertTrue(
            (self.workdir / DESTROY_PLAN_FILENAME).exists()
        )

        outcome = run_terraform_destroy(
            FILES,
            confirm="DESTROY",
            workspace_dir=self.workdir,
            destroy_plan_sha256=planned["plan_file"]["sha256"],
        )
        self.assertEqual(outcome["status"], "destroy_ok")
        self.assertTrue((self.workdir / "destroy.json").exists())

    def test_destroy_refuses_the_apply_confirmation(self):
        with self.assertRaises(ExecutionRefused):
            run_terraform_destroy(
                FILES, confirm="APPLY", workspace_dir=self.workdir
            )

    def test_destroy_refuses_a_mismatched_or_missing_plan_binding(self):
        run_terraform_destroy_plan(FILES, workspace_dir=self.workdir)
        with self.assertRaises(ExecutionRefused):
            run_terraform_destroy(
                FILES,
                confirm="DESTROY",
                workspace_dir=self.workdir,
                destroy_plan_sha256="0" * 64,
            )
        with self.assertRaises(ExecutionRefused):
            run_terraform_destroy(
                FILES,
                confirm="DESTROY",
                workspace_dir=self.workdir,
                destroy_plan_sha256=None,
            )

    def test_destroy_requires_a_durable_workspace(self):
        with self.assertRaises(ExecutionRefused):
            run_terraform_destroy(
                FILES, confirm="DESTROY", destroy_plan_sha256="0" * 64
            )


class DestroyReviewTests(unittest.TestCase):
    def _plan(self):
        return {"tree_hash": "a" * 64}

    def _review(self, **overrides):
        review = {
            "mode": "destroy_plan",
            "status": "destroy_plan_ok",
            "tree_hash": "a" * 64,
            "plan_file": {
                "name": DESTROY_PLAN_FILENAME,
                "sha256": "b" * 64,
            },
        }
        review.update(overrides)
        return review

    def test_a_destroy_plan_review_authorizes_the_same_tree(self):
        review = self._review()
        self.assertEqual(
            require_destroy_plan_review(review, self._plan()), review
        )

    def test_an_apply_review_never_authorizes_destroy(self):
        with self.assertRaises(ExecutionRefused):
            require_destroy_plan_review(
                self._review(mode="plan", status="plan_ok"), self._plan()
            )

    def test_a_different_tree_or_plan_file_is_refused(self):
        with self.assertRaises(ExecutionRefused):
            require_destroy_plan_review(
                self._review(tree_hash="c" * 64), self._plan()
            )
        with self.assertRaises(ExecutionRefused):
            require_destroy_plan_review(
                self._review(plan_file={
                    "name": SAVED_PLAN_FILENAME, "sha256": "b" * 64,
                }),
                self._plan(),
            )
        with self.assertRaises(ExecutionRefused):
            require_destroy_plan_review(None, self._plan())


class DriftTests(_LifecycleTestCase):
    def test_detailed_exit_codes_map_to_explicit_states(self):
        for returncode, expected in (
            (0, "no_drift"),
            (2, "drift"),
            (1, "drift_error"),
        ):
            with self.subTest(returncode=returncode):
                self._runner(_FakeTerraform(plan_rc=returncode))
                outcome = run_terraform_drift_check(
                    FILES, workspace_dir=self.workdir
                )
                self.assertEqual(outcome["status"], expected)
                self.assertEqual(
                    outcome["exit_codes"]["plan"], returncode
                )

    def test_drift_requires_a_durable_workspace(self):
        with self.assertRaises(ExecutionRefused):
            run_terraform_drift_check(FILES)


if __name__ == "__main__":
    unittest.main()
