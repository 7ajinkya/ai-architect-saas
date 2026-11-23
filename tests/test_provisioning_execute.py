"""Approval-gated execution tests: refusals, scoping, endpoints.

No terraform binary and no AWS credentials required: absence paths
are honest statuses, and live execution is faked at the executor
boundary. Secret-redaction and env-scoping tests use synthetic
values only.

Run: poetry run python -m unittest discover tests
"""
import json
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_architect.services.provisioning.executor import (
    DESTROY_PLAN_FILENAME,
    FORWARDABLE_AWS_ENV,
    ExecutionRefused,
    _minimal_env,
    _parse_plan_summary,
    _sanitize,
    build_provisioning_approval,
    consume_provisioning_approval,
    require_provisioning_approval,
    require_provisioning_review,
    run_terraform_apply,
    run_terraform_plan,
    seed_state_from_workspace,
    workspace_fingerprint,
    workspace_path,
)

PLAN = {
    "tree_hash": "tree-1",
    "spec_id": "spec-1",
    "artifact_id": "artifact-1",
    "files": {"main.tf": 'resource "x" "y" {}'},
}

# Bytes the fake terraform runner writes as the saved plan file: the
# hash binding tests hash these same bytes, no real terraform involved.
PLAN_BYTES = b"fake-plan-bytes"


def _destroy_review(**overrides):
    return _review(
        mode="destroy_plan",
        status="destroy_plan_ok",
        plan_file={
            "name": DESTROY_PLAN_FILENAME,
            "sha256": "cd" * 32,
        },
        **overrides,
    )


def _approval(**overrides):
    record = build_provisioning_approval(
        approved=True, note="", tree_hash="tree-1", spec_id="spec-1",
        artifact_id="artifact-1",
        artifacts=PLAN.get("artifacts"),
        session_id="design-s1",
        plan_file={"name": "ai-architect.tfplan", "sha256": "ab" * 32},
        workspace_hash=workspace_fingerprint(
            dict(PLAN["files"]), PLAN.get("artifacts")
        ),
    )
    record.update(overrides)
    return record


def _review(**overrides):
    record = {
        "status": "plan_ok",
        "mode": "plan",
        "artifact_id": "artifact-1",
        "tree_hash": "tree-1",
        "exit_codes": {"init": 0, "plan": 0},
        "output": "Plan: 1 to add, 0 to change, 0 to destroy.",
        "summary": {"add": 1, "change": 0, "destroy": 0},
        "plan_file": {"name": "ai-architect.tfplan", "sha256": "ab" * 32},
        "recorded_at": "2026-01-01T00:00:00Z",
    }
    record.update(overrides)
    return record


class ApprovalRecordTests(unittest.TestCase):
    def test_matching_approval_passes(self):
        self.assertTrue(
            require_provisioning_approval(_approval(), PLAN)["approved"]
        )

    def test_missing_or_negative_record_refuses(self):
        for bad in (None, {}, {"approved": False}):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutionRefused):
                    require_provisioning_approval(bad, PLAN)

    def test_drifted_hashes_refuse(self):
        for key in ("tree_hash", "spec_id", "artifact_id"):
            with self.subTest(key=key):
                with self.assertRaises(ExecutionRefused) as ctx:
                    require_provisioning_approval(
                        _approval(**{key: "other"}), PLAN
                    )
                self.assertIn(key, str(ctx.exception))

    def test_missing_plan_refuses(self):
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(_approval(), None)

    def test_consumed_approval_refuses(self):
        with self.assertRaises(ExecutionRefused) as ctx:
            require_provisioning_approval(
                _approval(consumed_at="2026-01-02T00:00:00Z"), PLAN
            )
        self.assertIn("consumed", str(ctx.exception).lower())


class RefusalPathTests(unittest.TestCase):
    def test_plan_without_terraform_is_honest(self):
        with patch(
            "ai_architect.services.provisioning.executor.terraform_available",
            return_value=False,
        ):
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_plan(PLAN["files"])
        self.assertIn("not installed", str(ctx.exception).lower())

    def test_apply_without_confirm_refuses_before_subprocess(self):
        with patch(
            "ai_architect.services.provisioning.executor.terraform_available",
            return_value=True,
        ):
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_apply(PLAN["files"], confirm="")
            self.assertIn("APPLY", str(ctx.exception))
            with self.assertRaises(ExecutionRefused):
                run_terraform_apply(PLAN["files"], confirm="yes")

    def test_empty_files_refuse(self):
        with patch(
            "ai_architect.services.provisioning.executor.terraform_available",
            return_value=True,
        ):
            with self.assertRaises(ExecutionRefused):
                run_terraform_plan({})
            with self.assertRaises(ExecutionRefused):
                run_terraform_apply({}, confirm="APPLY")

    def test_destroy_requires_its_own_confirmation(self):
        import ai_architect.services.provisioning.executor as executor

        self.assertTrue(hasattr(executor, "run_terraform_destroy"))
        self.assertFalse(hasattr(executor, "run_terraform_import"))
        with self.assertRaises(ExecutionRefused):
            executor.run_terraform_destroy({}, confirm="APPLY")


class ReviewRecordTests(unittest.TestCase):
    """Apply runs only on a stored, successful review of the same tree."""

    def test_matching_review_passes(self):
        passed = require_provisioning_review(_review(), PLAN)
        self.assertEqual(passed["status"], "plan_ok")

    def test_missing_or_failed_review_refuses(self):
        for bad in (
            None,
            {},
            {"status": "plan_failed", "mode": "plan",
             "tree_hash": "tree-1"},
            {"status": "plan_ok", "mode": "apply",
             "tree_hash": "tree-1"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutionRefused):
                    require_provisioning_review(bad, PLAN)

    def test_review_of_other_tree_refuses(self):
        stale = _review(tree_hash="tree-other")
        with self.assertRaises(ExecutionRefused) as ctx:
            require_provisioning_review(stale, PLAN)
        self.assertIn("different plan tree", str(ctx.exception))

    def test_review_without_saved_plan_binding_refuses(self):
        """A review must bind the exact saved plan it examined."""
        for bad in (
            None,
            {},
            "ai-architect.tfplan",
            {"name": "other.tfplan", "sha256": "ab" * 32},
            {"name": "ai-architect.tfplan", "sha256": "abc"},
            {"name": "ai-architect.tfplan"},
            {"sha256": "ab" * 32},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutionRefused) as ctx:
                    require_provisioning_review(
                        _review(plan_file=bad), PLAN
                    )
                self.assertIn("saved plan", str(ctx.exception))

    def test_consumed_approval_is_single_use(self):
        record = consume_provisioning_approval(_approval())
        self.assertIn("consumed_at", record)
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(record, PLAN)


class WorkspaceTests(unittest.TestCase):
    """Durable workspaces: bound to one tree, never deleted, never shared."""

    def _durable_dir(self):
        workdir = Path(tempfile.mkdtemp(prefix="ai-architect-ws-test-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        return workdir

    @staticmethod
    def _runner(workdir: Path, status_line: str):
        def fake_run(cmd, **kwargs):
            (workdir / "terraform.tfstate").write_text(
                "{}", encoding="utf-8"
            )
            if "-out" in cmd:
                (workdir / "ai-architect.tfplan").write_bytes(
                    b"fake-plan-bytes"
                )
            stdout = "init ok" if cmd[1] == "init" else status_line
            return MagicMock(returncode=0, stdout=stdout, stderr="")
        return fake_run

    @staticmethod
    def _available():
        return patch(
            "ai_architect.services.provisioning.executor"
            ".terraform_available",
            return_value=True,
        )

    @staticmethod
    def _runner_patch(workdir: Path, status_line: str):
        return patch(
            "ai_architect.services.provisioning.executor.subprocess.run",
            side_effect=WorkspaceTests._runner(workdir, status_line),
        )

    def test_workspace_path_is_session_and_tree_scoped(self):
        path = workspace_path("design-s1", "ab" * 32)
        self.assertIn("var", path.parts)
        self.assertIn("terraform", path.parts)
        self.assertEqual(path.parts[-2], "design-s1")
        self.assertEqual(path.parts[-1], "ab" * 8)
        self.assertEqual(len(path.parts[-1]), 16)

    def test_workspace_path_refuses_unsafe_segments(self):
        for session, tree in (
            ("../evil", "ab" * 32),
            ("a/b", "ab" * 32),
            ("", "ab" * 32),
            ("design s1", "ab" * 32),
            ("design-s1", "not hex"),
            ("design-s1", ""),
            ("design-s1", None),
        ):
            with self.subTest(session=session, tree=tree):
                with self.assertRaises(ExecutionRefused):
                    workspace_path(session, tree)

    def test_durable_plan_workspace_persists_reuses_and_binds(self):
        workdir = self._durable_dir()
        fingerprint = workspace_fingerprint(PLAN["files"])
        with self._available(), self._runner_patch(
            workdir, "Plan: 1 to add, 0 to change, 0 to destroy."
        ):
            outcome = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(outcome["status"], "plan_ok")
        self.assertTrue((workdir / "terraform.tfstate").exists())
        marker = json.loads(
            (workdir / ".ai-architect-workspace.json").read_text("utf-8")
        )
        self.assertEqual(marker["fingerprint"], fingerprint)
        # A rerun for the same tree is allowed and keeps the state...
        with self._available(), self._runner_patch(
            workdir, "Plan: 0 to add, 0 to change, 0 to destroy."
        ):
            again = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(again["status"], "plan_ok")
        # ...a different tree is refused before anything runs.
        with self._available():
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_plan(
                    {"main.tf": 'resource "x" "z" {}'},
                    workspace_dir=workdir,
                )
        self.assertIn("exact plan tree", str(ctx.exception))

    def test_unmarked_nonempty_directory_is_never_touched(self):
        workdir = self._durable_dir()
        (workdir / "keep.txt").write_text(
            "someone else's data", encoding="utf-8"
        )
        with self._available():
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_plan(PLAN["files"], workspace_dir=workdir)
        self.assertIn("marker", str(ctx.exception))
        self.assertTrue((workdir / "keep.txt").exists())
        self.assertEqual(
            (workdir / "keep.txt").read_text("utf-8"),
            "someone else's data",
        )

    def test_apply_records_deployment_metadata_and_keeps_state(self):
        workdir = self._durable_dir()
        with self._available(), self._runner_patch(
            workdir, "Plan: 1 to add, 0 to change, 0 to destroy."
        ):
            planned = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(planned["status"], "plan_ok")
        with self._available(), self._runner_patch(
            workdir, "Apply complete!"
        ):
            outcome = run_terraform_apply(
                PLAN["files"], confirm="APPLY", workspace_dir=workdir,
                saved_plan_sha256=planned["plan_file"]["sha256"],
            )
        self.assertEqual(outcome["status"], "apply_ok")
        record = json.loads(
            (workdir / "deployment.json").read_text("utf-8")
        )
        self.assertEqual(record["status"], "apply_ok")
        self.assertTrue((workdir / "terraform.tfstate").exists())
        self.assertTrue((workdir / "ai-architect.tfplan").exists())


class SavedPlanContractTests(unittest.TestCase):
    """Apply consumes the exact reviewed saved plan; never a fresh one.

    All subprocess execution is mocked: the fake runner simulates
    terraform by writing the plan file when ``-out`` is passed, so the
    real hashing, binding, and refusal code paths run unmodified. No
    terraform binary, no AWS credentials, no network.
    """

    def _durable_dir(self):
        workdir = Path(tempfile.mkdtemp(prefix="ai-architect-saved-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        return workdir

    @staticmethod
    def _available():
        return patch(
            "ai_architect.services.provisioning.executor"
            ".terraform_available",
            return_value=True,
        )

    @staticmethod
    def _fake_terraform(plan_bytes: bytes = PLAN_BYTES):
        def fake_run(cmd, **kwargs):
            if cmd[1] == "init":
                return MagicMock(
                    returncode=0, stdout="init ok", stderr=""
                )
            if cmd[1] == "plan":
                out = cmd[cmd.index("-out") + 1]
                Path(kwargs["cwd"], out).write_bytes(plan_bytes)
                return MagicMock(
                    returncode=0,
                    stdout="Plan: 1 to add, 0 to change, 0 to destroy.",
                    stderr="",
                )
            return MagicMock(
                returncode=0, stdout="Apply complete!", stderr=""
            )
        return fake_run

    def _runner_patch(self):
        return patch(
            "ai_architect.services.provisioning.executor.subprocess.run",
            side_effect=self._fake_terraform(),
        )

    def _planned(self, workdir: Path) -> dict:
        with self._available(), self._runner_patch():
            outcome = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(outcome["status"], "plan_ok")
        return outcome

    def test_plan_mode_writes_and_binds_the_saved_plan_argument(self):
        workdir = self._durable_dir()
        with self._available(), self._runner_patch() as run:
            outcome = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(outcome["status"], "plan_ok")
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["terraform", "plan", "-no-color", "-input=false",
             "-out", "ai-architect.tfplan"],
        )
        self.assertTrue((workdir / "ai-architect.tfplan").exists())
        self.assertEqual(
            outcome["plan_file"],
            {
                "name": "ai-architect.tfplan",
                "sha256": hashlib.sha256(PLAN_BYTES).hexdigest(),
            },
        )

    def test_apply_uses_exact_saved_plan_not_bare_apply(self):
        workdir = self._durable_dir()
        planned = self._planned(workdir)
        with self._available(), self._runner_patch() as run:
            outcome = run_terraform_apply(
                PLAN["files"], confirm="APPLY", workspace_dir=workdir,
                saved_plan_sha256=planned["plan_file"]["sha256"],
            )
        self.assertEqual(outcome["status"], "apply_ok")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["terraform", "init"])
        self.assertEqual(
            commands[1],
            ["terraform", "apply", "-no-color", "-input=false",
             "-auto-approve", "ai-architect.tfplan"],
        )

    def test_apply_refuses_when_saved_plan_missing(self):
        workdir = self._durable_dir()
        planned = self._planned(workdir)
        (workdir / "ai-architect.tfplan").unlink()
        with self._available(), self._runner_patch() as run:
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_apply(
                    PLAN["files"], confirm="APPLY",
                    workspace_dir=workdir,
                    saved_plan_sha256=planned["plan_file"]["sha256"],
                )
            run.assert_not_called()
        self.assertIn("missing", str(ctx.exception))

    def test_apply_refuses_when_saved_plan_tampered(self):
        workdir = self._durable_dir()
        planned = self._planned(workdir)
        (workdir / "ai-architect.tfplan").write_bytes(b"tampered-bytes")
        with self._available(), self._runner_patch() as run:
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_apply(
                    PLAN["files"], confirm="APPLY",
                    workspace_dir=workdir,
                    saved_plan_sha256=planned["plan_file"]["sha256"],
                )
            run.assert_not_called()
        self.assertIn("does not match", str(ctx.exception))

    def test_apply_refuses_without_reviewed_plan_binding(self):
        """The file existing is not proof; the review must bind it."""
        workdir = self._durable_dir()
        self._planned(workdir)
        for bad in (None, "", "abc", 1234):
            with self.subTest(bad=bad):
                with self._available(), self._runner_patch() as run:
                    with self.assertRaises(ExecutionRefused) as ctx:
                        run_terraform_apply(
                            PLAN["files"], confirm="APPLY",
                            workspace_dir=workdir,
                            saved_plan_sha256=bad,
                        )
                    run.assert_not_called()
                self.assertIn(
                    "no reviewed saved plan", str(ctx.exception)
                )

    def test_apply_refuses_workspace_of_another_tree(self):
        """A workspace bound to another tree refuses before any run."""
        workdir = self._durable_dir()
        planned = self._planned(workdir)
        with self._available(), self._runner_patch() as run:
            with self.assertRaises(ExecutionRefused) as ctx:
                run_terraform_apply(
                    {"main.tf": 'resource "x" "z" {}'}, confirm="APPLY",
                    workspace_dir=workdir,
                    saved_plan_sha256=planned["plan_file"]["sha256"],
                )
            run.assert_not_called()
        self.assertIn("exact plan tree", str(ctx.exception))

    def test_plan_without_saved_file_is_never_reviewable(self):
        """Exit 0 without the plan file is a failure, not a review."""
        workdir = self._durable_dir()

        def fake_run(cmd, **kwargs):
            return MagicMock(
                returncode=0,
                stdout="Plan: 1 to add, 0 to change, 0 to destroy.",
                stderr="",
            )

        with self._available(), patch(
            "ai_architect.services.provisioning.executor.subprocess.run",
            side_effect=fake_run,
        ):
            outcome = run_terraform_plan(
                PLAN["files"], workspace_dir=workdir
            )
        self.assertEqual(outcome["status"], "plan_failed")
        self.assertNotIn("plan_file", outcome)


class HygieneTests(unittest.TestCase):
    def test_output_redacts_secrets(self):
        dirty = 'AKIAIOSFODNN7EXAMPLE failed\npassword= hunter2-hunter2'
        clean = _sanitize(dirty)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", clean)
        self.assertNotIn("hunter2", clean)

    def test_child_env_carries_no_ambient_credentials(self):
        with patch.dict(os.environ, {
            "AWS_SECRET_ACCESS_KEY": "shh",
            "AWS_SESSION_TOKEN": "tok",
            "SOME_OTHER": "kept-out",
        }, clear=False):
            env = _minimal_env()
        for key in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                    "SOME_OTHER"):
            self.assertNotIn(key, env)
        self.assertEqual(env.get("TF_INPUT"), "0")

    def test_only_allowlisted_entries_forward_explicitly(self):
        env = _minimal_env({
            "AWS_REGION": "us-east-1",
            "AWS_SECRET_ACCESS_KEY": "shh",
            "EVIL": "x",
        })
        self.assertEqual(env["AWS_REGION"], "us-east-1")
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "shh")
        self.assertNotIn("EVIL", env)
        for key in FORWARDABLE_AWS_ENV:
            self.assertIsInstance(key, str)

    def test_plan_summary_parses_or_returns_none(self):
        parsed = _parse_plan_summary(
            "Plan: 3 to add, 1 to change, 0 to destroy."
        )
        self.assertEqual(parsed, {"add": 3, "change": 1, "destroy": 0})
        self.assertIsNone(_parse_plan_summary("no summary here"))

    def test_plan_summary_survives_output_truncation(self):
        """The add/change/destroy line is parsed before truncation."""
        from unittest.mock import MagicMock

        padding = "x" * 9000
        init_result = MagicMock(returncode=0, stdout="init ok", stderr="")
        plan_result = MagicMock(
            returncode=0,
            stdout=f"{padding}\nPlan: 9 to add, 0 to change, 0 to destroy.\n",
            stderr="",
        )

        def fake_run(cmd, **kwargs):
            if cmd[1] == "plan":
                Path(
                    kwargs["cwd"], "ai-architect.tfplan"
                ).write_bytes(b"fake-plan-bytes")
                return plan_result
            return init_result

        with patch(
            "ai_architect.services.provisioning.executor.terraform_available",
            return_value=True,
        ), patch(
            "ai_architect.services.provisioning.executor.subprocess.run",
            side_effect=fake_run,
        ):
            outcome = run_terraform_plan(PLAN["files"])
        self.assertEqual(outcome["status"], "plan_ok")
        self.assertEqual(
            outcome["summary"], {"add": 9, "change": 0, "destroy": 0}
        )
        self.assertIn("[truncated]", outcome["output"])

    def test_truncation_preserves_the_failure_tail(self):
        padding = "x" * 9000
        tail = "Error: creating Cognito User Pool: InvalidParameterException"
        sanitized = _sanitize(f"{padding}\n{tail}")
        self.assertIn("[truncated]", sanitized)
        self.assertTrue(sanitized.endswith(tail))
        self.assertLessEqual(len(sanitized), 8000)

    def test_state_seed_never_overwrites_a_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "source")
            target = Path(tmp, "target")
            source.mkdir()
            target.mkdir()
            (source / "terraform.tfstate").write_text(
                "old", encoding="utf-8"
            )
            target_state = target / "terraform.tfstate"
            self.assertTrue(seed_state_from_workspace(source, target))
            self.assertEqual(
                target_state.read_text(encoding="utf-8"), "old"
            )
            (source / "terraform.tfstate").write_text(
                "newer", encoding="utf-8"
            )
            self.assertFalse(seed_state_from_workspace(source, target))
            self.assertEqual(
                target_state.read_text(encoding="utf-8"), "old"
            )

    def test_state_seed_requires_a_source_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                seed_state_from_workspace(
                    Path(tmp), Path(tmp, "missing-target")
                )
            )

    def test_plan_seeds_state_before_init_and_says_so(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            if cmd[1] == "init":
                seen["state"] = (
                    Path(kwargs["cwd"]) / "terraform.tfstate"
                ).read_text(encoding="utf-8")
                return MagicMock(
                    returncode=0, stdout="init ok", stderr=""
                )
            Path(
                kwargs["cwd"], "ai-architect.tfplan"
            ).write_bytes(b"fake-plan-bytes")
            return MagicMock(
                returncode=0,
                stdout="Plan: 0 to add, 0 to change, 1 to destroy.",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "source")
            source.mkdir()
            (source / "terraform.tfstate").write_text(
                "partial-state", encoding="utf-8"
            )
            with patch(
                "ai_architect.services.provisioning.executor."
                "terraform_available",
                return_value=True,
            ), patch(
                "ai_architect.services.provisioning.executor."
                "subprocess.run",
                side_effect=fake_run,
            ):
                outcome = run_terraform_plan(
                    PLAN["files"],
                    workspace_dir=Path(tmp, "target"),
                    seed_state_from=source,
                )
        self.assertEqual(outcome["status"], "plan_ok")
        self.assertEqual(seen["state"], "partial-state")
        self.assertIn("Reused Terraform state", outcome["output"])


class ProvisioningEndpointTests(unittest.TestCase):
    def _snapshot(self, plan=True, approval=True, review=None):
        return SimpleNamespace(
            session_id="design-s1",
            status="ready",
            error=None,
            requirements_state=object(),
            architecture=SimpleNamespace(artifact_id="artifact-1"),
            terraform_artifact=dict(PLAN) if plan else None,
            provisioning_approval=_approval() if approval else None,
            provisioning_result=review,
        )

    def _client(self):
        from tests.api_auth import api_client

        from ai_architect.api.main import create_app

        return api_client(create_app())

    def test_approve_provisioning_needs_stored_plan(self):
        from ai_architect.api import main as main_module

        client = self._client()
        snapshot = self._snapshot(plan=False)
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = client.post(
                "/design/sessions/design-s1/approve-provisioning",
                json={"approved": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_not_allowed"
        )

    def test_approve_provisioning_records_against_plan(self):
        from ai_architect.api import main as main_module

        client = self._client()
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["field"] = field
            saved["record"] = record

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(review=_review()),
            ),
            patch.object(main_module, "save_session_record", fake_save),
        ):
            response = client.post(
                "/design/sessions/design-s1/approve-provisioning",
                json={"approved": True, "note": "ship"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(saved["field"], "provisioning_approval")
        self.assertEqual(saved["record"]["tree_hash"], "tree-1")
        self.assertEqual(saved["record"]["session_id"], "design-s1")
        self.assertEqual(
            saved["record"]["plan_file"]["sha256"], "ab" * 32
        )
        self.assertEqual(
            saved["record"]["workspace_hash"],
            workspace_fingerprint(dict(PLAN["files"]), PLAN.get("artifacts")),
        )

    def test_approve_provisioning_requires_stored_review(self):
        from ai_architect.api import main as main_module

        client = self._client()
        saved = []

        async def fake_save(session_id, *, field, record):
            saved.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "save_session_record", fake_save),
        ):
            response = client.post(
                "/design/sessions/design-s1/approve-provisioning",
                json={"approved": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "plan_review_required"
        )
        self.assertEqual(saved, [])

    def test_provision_plan_mode_runs_without_approval(self):
        from ai_architect.api import main as main_module

        client = self._client()
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["field"] = field
            saved["record"] = record

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(approval=False),
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_plan",
                return_value={
                    "status": "plan_ok", "exit_codes": {},
                    "output": "ok", "summary": None,
                },
            ) as run_plan,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "plan"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "plan_ok")
        self.assertEqual(body["mode"], "plan")
        self.assertEqual(saved["field"], "provisioning_result")
        _, kwargs = run_plan.call_args
        self.assertEqual(
            Path(kwargs.get("workspace_dir")).name, "tree-1"
        )

    def test_plan_mode_seeds_prior_failed_apply_state(self):
        from ai_architect.api import main as main_module

        client = self._client()
        review = {
            "status": "apply_failed",
            "mode": "apply",
            "tree_hash": "tree-old",
            "artifact_id": "artifact-1",
        }

        async def fake_save(session_id, *, field, record):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            prior = Path(tmp, "tree-old")
            prior.mkdir()
            (prior / "terraform.tfstate").write_text(
                "{}", encoding="utf-8"
            )
            target = Path(tmp, "tree-1")

            def fake_workspace(session_id, tree_hash):
                return prior if tree_hash == "tree-old" else target

            with (
                patch.object(
                    main_module, "load_design_snapshot",
                    return_value=self._snapshot(review=review),
                ),
                patch.object(
                    main_module, "save_session_record", fake_save
                ),
                patch.object(
                    main_module, "workspace_path",
                    side_effect=fake_workspace,
                ),
                patch.object(
                    main_module, "run_terraform_plan",
                    return_value={
                        "status": "plan_ok", "exit_codes": {},
                        "output": "ok", "summary": None,
                    },
                ) as run_plan,
            ):
                response = client.post(
                    "/design/sessions/design-s1/provision",
                    json={"mode": "plan"},
                )
        self.assertEqual(response.status_code, 200)
        _, kwargs = run_plan.call_args
        self.assertEqual(kwargs["seed_state_from"], prior)

    def test_plan_mode_does_not_seed_from_other_outcomes(self):
        from ai_architect.api import main as main_module

        client = self._client()
        review = {
            "status": "apply_ok",
            "mode": "apply",
            "tree_hash": "tree-old",
            "artifact_id": "artifact-1",
        }

        async def fake_save(session_id, *, field, record):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            prior = Path(tmp, "tree-old")
            prior.mkdir()
            (prior / "terraform.tfstate").write_text(
                "{}", encoding="utf-8"
            )
            target = Path(tmp, "tree-1")

            def fake_workspace(session_id, tree_hash):
                return prior if tree_hash == "tree-old" else target

            with (
                patch.object(
                    main_module, "load_design_snapshot",
                    return_value=self._snapshot(review=review),
                ),
                patch.object(
                    main_module, "save_session_record", fake_save
                ),
                patch.object(
                    main_module, "workspace_path",
                    side_effect=fake_workspace,
                ),
                patch.object(
                    main_module, "run_terraform_plan",
                    return_value={
                        "status": "plan_ok", "exit_codes": {},
                        "output": "ok", "summary": None,
                    },
                ) as run_plan,
            ):
                response = client.post(
                    "/design/sessions/design-s1/provision",
                    json={"mode": "plan"},
                )
        self.assertEqual(response.status_code, 200)
        _, kwargs = run_plan.call_args
        self.assertIsNone(kwargs["seed_state_from"])

    def test_provision_plan_mode_does_not_consume_approval(self):
        from ai_architect.api import main as main_module

        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_plan",
                return_value={
                    "status": "plan_ok", "exit_codes": {},
                    "output": "ok",
                    "summary": {"add": 1, "change": 0, "destroy": 0},
                },
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "plan"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([field for field, _ in calls],
                         ["provisioning_result"])
        self.assertNotIn("consumed_at", calls[0][1])

    def test_provision_apply_needs_confirm(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=self._snapshot(review=_review()),
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "yes"},
            )
        # Refused before anything runs or is recorded: confirm is
        # checked first, then the executor re-checks it as the final
        # boundary.
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )

    def test_apply_requires_stored_review(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "plan_review_required"
        )
        run_apply.assert_not_called()

    def test_apply_review_of_other_tree_refused(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(
                    review=_review(tree_hash="tree-other")
                ),
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "plan_review_required"
        )
        run_apply.assert_not_called()

    def test_apply_refused_after_interrupted_apply(self):
        from ai_architect.api import main as main_module

        interrupted = _review(mode="apply", status="apply_running")
        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(review=interrupted),
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "apply_interrupted"
        )
        run_apply.assert_not_called()

    def test_apply_refused_with_consumed_approval(self):
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        snapshot.provisioning_approval = _approval(
            consumed_at="2026-01-02T00:00:00Z"
        )
        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_apply.assert_not_called()

    def test_provision_apply_refused_without_approval(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(
                    approval=False, review=_review()
                ),
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_apply.assert_not_called()

    def test_plan_for_different_architecture_artifact_refused(self):
        from ai_architect.api import main as main_module

        client = self._client()
        snapshot = self._snapshot()
        snapshot.architecture = SimpleNamespace(artifact_id="artifact-other")
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "plan"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_not_allowed"
        )

    def test_apply_refused_when_approval_binds_other_session(self):
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        snapshot.provisioning_approval = _approval(session_id="design-other")
        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_apply.assert_not_called()

    def test_apply_refused_when_workspace_fingerprint_drifts(self):
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        snapshot.provisioning_approval = _approval(workspace_hash="cd" * 32)
        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_apply.assert_not_called()

    def test_apply_refused_when_approval_binds_other_saved_plan(self):
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        snapshot.provisioning_approval = _approval(
            plan_file={
                "name": "ai-architect.tfplan", "sha256": "ef" * 32,
            }
        )
        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot
            ),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_apply.assert_not_called()

    def test_unknown_mode_rejected(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=self._snapshot(),
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "rollback"},
            )
        self.assertEqual(response.status_code, 400)

    def test_destroy_needs_its_own_confirmation(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(review=_destroy_review()),
            ),
            patch.object(main_module, "run_terraform_destroy") as run_destroy,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "destroy"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        run_destroy.assert_not_called()

    def test_destroy_requires_a_reviewed_destroy_plan(self):
        from ai_architect.api import main as main_module

        client = self._client()
        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(review=_review()),
            ),
            patch.object(main_module, "run_terraform_destroy") as run_destroy,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "destroy", "confirm": "DESTROY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"],
            "destroy_plan_review_required",
        )
        run_destroy.assert_not_called()

    def test_destroy_forwards_the_reviewed_destroy_plan_binding(self):
        from ai_architect.api import main as main_module

        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(review=_destroy_review()),
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_destroy",
                return_value={"status": "destroy_ok", "exit_codes": {}},
            ) as run_destroy,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "destroy", "confirm": "DESTROY"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "destroy_ok")
        kwargs = run_destroy.call_args.kwargs
        self.assertEqual(kwargs["confirm"], "DESTROY")
        self.assertEqual(kwargs["destroy_plan_sha256"], "cd" * 32)
        self.assertEqual(
            [record["status"] for _, record in calls],
            ["destroy_running", "destroy_ok"],
        )

    def test_destroy_plan_and_drift_are_distinct_modes(self):
        from ai_architect.api import main as main_module

        client = self._client()
        saved = []

        async def fake_save(session_id, *, field, record):
            saved.append(record)

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_destroy_plan",
                return_value={
                    "status": "destroy_plan_ok",
                    "exit_codes": {},
                    "plan_file": {
                        "name": DESTROY_PLAN_FILENAME,
                        "sha256": "cd" * 32,
                    },
                },
            ),
            patch.object(
                main_module, "run_terraform_drift_check",
                return_value={"status": "no_drift", "exit_codes": {}},
            ),
        ):
            planned = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "destroy-plan"},
            )
            drifted = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "drift"},
            )
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(planned.json()["mode"], "destroy_plan")
        self.assertEqual(drifted.status_code, 200)
        self.assertEqual(drifted.json()["mode"], "drift")
        self.assertEqual(drifted.json()["status"], "no_drift")

    def test_provision_apply_forwards_bound_manifest(self):
        """Review + approval -> executor carries manifest; consumed."""
        from ai_architect.api import main as main_module

        manifest = [{"name": "demo-handler.zip", "sha256": "ab" * 32}]
        plan = dict(PLAN)
        plan["artifacts"] = list(manifest)
        approval = _approval(
            artifacts=manifest,
            workspace_hash=workspace_fingerprint(
                dict(plan["files"]), manifest
            ),
        )
        snapshot = self._snapshot(review=_review())
        snapshot.terraform_artifact = plan
        snapshot.provisioning_approval = approval
        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_apply",
                return_value={
                    "status": "apply_ok", "exit_codes": {},
                    "output": "ok",
                },
            ) as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 200)
        _, kwargs = run_apply.call_args
        self.assertEqual(kwargs.get("artifact_manifest"), manifest)
        self.assertEqual(
            Path(kwargs.get("workspace_dir")).name, "tree-1"
        )
        self.assertEqual(
            [field for field, _ in calls],
            ["provisioning_result", "provisioning_approval",
             "provisioning_result"],
        )
        self.assertEqual(calls[0][1]["status"], "apply_running")
        self.assertIn("consumed_at", calls[1][1])
        self.assertEqual(calls[2][1]["status"], "apply_ok")
        self.assertEqual(response.json()["status"], "apply_ok")

    def test_apply_forwards_exact_saved_plan_binding(self):
        """The reviewed saved-plan hash reaches the executor unchanged."""
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        client = self._client()

        async def fake_save(session_id, *, field, record):
            pass

        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_apply",
                return_value={
                    "status": "apply_ok", "exit_codes": {},
                    "output": "ok",
                },
            ) as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 200)
        _, kwargs = run_apply.call_args
        self.assertEqual(kwargs.get("saved_plan_sha256"), "ab" * 32)

    def test_apply_with_unbound_review_refused_unconsumed(self):
        """A review without the saved-plan binding never consumes."""
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review(plan_file=None))
        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(main_module, "run_terraform_apply") as run_apply,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "plan_review_required"
        )
        run_apply.assert_not_called()
        # Refused pre-flight: no marker, no approval consumption.
        self.assertEqual(calls, [])

    def test_apply_preflight_refusal_restores_records(self):
        """A pre-flight refusal never burns the approval or review."""
        from ai_architect.api import main as main_module

        snapshot = self._snapshot(review=_review())
        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_apply",
                side_effect=ExecutionRefused("terraform not installed"),
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "apply", "confirm": "APPLY"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "provisioning_refused"
        )
        self.assertEqual(
            [field for field, _ in calls],
            ["provisioning_result", "provisioning_approval",
             "provisioning_result", "provisioning_approval"],
        )
        self.assertEqual(calls[0][1]["status"], "apply_running")
        self.assertIn("consumed_at", calls[1][1])
        self.assertEqual(calls[2][1], _review())
        self.assertNotIn("consumed_at", calls[3][1])

    def test_provision_plan_forwards_bound_manifest(self):
        from ai_architect.api import main as main_module

        manifest = [{"name": "demo-handler.zip", "sha256": "ab" * 32}]
        plan = dict(PLAN)
        plan["artifacts"] = list(manifest)
        approval = _approval(
            artifacts=manifest,
            workspace_hash=workspace_fingerprint(
                dict(plan["files"]), manifest
            ),
        )
        snapshot = self._snapshot()
        snapshot.terraform_artifact = plan
        snapshot.provisioning_approval = approval
        client = self._client()
        calls = []

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch.object(
                main_module, "run_terraform_plan",
                return_value={
                    "status": "plan_ok", "exit_codes": {},
                    "output": "ok", "summary": None,
                },
            ) as run_plan,
        ):
            response = client.post(
                "/design/sessions/design-s1/provision",
                json={"mode": "plan"},
            )
        self.assertEqual(response.status_code, 200)
        _, kwargs = run_plan.call_args
        self.assertEqual(kwargs.get("artifact_manifest"), manifest)
        self.assertEqual([field for field, _ in calls],
                         ["provisioning_result"])
        self.assertEqual(calls[0][1]["mode"], "plan")


class TerraformGenerationEndpointTests(unittest.TestCase):
    """POST /terraform: the UI generation trigger's contract."""

    def _snapshot(self):
        return SimpleNamespace(
            session_id="design-s1",
            status="ready",
            error=None,
            requirements_state=SimpleNamespace(
                extraction=SimpleNamespace(cloud_provider="aws")
            ),
            architecture=object(),
            design_approval={"approved": True},
        )

    def _plan_dict(self):
        return {
            "status": "incomplete",
            "artifact_id": "artifact-1",
            "spec_id": "spec-1",
            "tree_hash": "tree-1",
            "provider": "aws",
            "region_code": "ap-south-1",
            "resources": [],
            "modules": [],
            "selection_errors": [],
            "spec_diagnostics": [],
            "render_status": "rendered",
            "render_diagnostics": [],
            "files": {},
            "resource_types": [],
            "terraform_validation": {},
            "inventory": {},
            "spec_warnings": [],
            "artifacts": [],
        }

    def _client(self):
        from tests.api_auth import api_client

        from ai_architect.api.main import create_app

        return api_client(create_app())

    def test_generate_forwards_inputs_and_stores_plan(self):
        from ai_architect.api import main as main_module

        client = self._client()
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["field"] = field
            saved["record"] = record

        sent = {}

        def fake_build(
            *, architecture, provider, approval, inputs,
            prior_inputs=None, requirements=None,
        ):
            sent["inputs"] = inputs
            sent["prior_inputs"] = prior_inputs
            return self._plan_dict()

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch(
                "ai_architect.services.provisioning.plan_builder"
                ".build_provisioning_plan",
                side_effect=fake_build,
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform",
                json={"inputs": {"lambda-fn": {"runtime": "python3.12"}}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sent["inputs"], {"lambda-fn": {"runtime": "python3.12"}}
        )
        self.assertIsNone(sent["prior_inputs"])
        self.assertEqual(saved["field"], "terraform_artifact")
        self.assertEqual(response.json()["spec_id"], "spec-1")

    def test_generate_carries_prior_inputs_forward(self):
        from ai_architect.api import main as main_module

        client = self._client()
        snapshot = self._snapshot()
        snapshot.terraform_artifact = {
            "tree_hash": "tree-0", "spec_id": "spec-0",
            "artifact_id": "artifact-0",
            "inputs": {"lambda-fn": {"code_artifact_path": "a.zip"}},
        }
        sent = {}

        def fake_build(
            *, architecture, provider, approval, inputs,
            prior_inputs=None, requirements=None,
        ):
            sent["prior_inputs"] = prior_inputs
            return self._plan_dict()

        async def fake_save(session_id, *, field, record):
            pass

        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch(
                "ai_architect.services.provisioning.plan_builder"
                ".build_provisioning_plan",
                side_effect=fake_build,
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform", json={"inputs": {}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sent["prior_inputs"],
            {"lambda-fn": {"code_artifact_path": "a.zip"}},
        )

    def test_generate_supersedes_prior_positive_approval(self):
        from ai_architect.api import main as main_module

        client = self._client()
        snapshot = self._snapshot()
        snapshot.provisioning_approval = {
            "approved": True, "tree_hash": "tree-1",
            "spec_id": "spec-1", "artifact_id": "artifact-1",
        }
        saved = []

        async def fake_save(session_id, *, field, record):
            saved.append((field, record))

        with (
            patch.object(
                main_module, "load_design_snapshot", return_value=snapshot,
            ),
            patch.object(main_module, "save_session_record", fake_save),
            patch(
                "ai_architect.services.provisioning.plan_builder"
                ".build_provisioning_plan",
                side_effect=lambda **kwargs: self._plan_dict(),
            ),
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform", json={"inputs": {}},
            )
        self.assertEqual(response.status_code, 200)
        fields = [field for field, _ in saved]
        self.assertEqual(
            fields, ["terraform_artifact", "provisioning_approval"]
        )
        superseded = saved[1][1]
        self.assertFalse(superseded["approved"])
        self.assertIn("superseded_at", superseded)
        self.assertIn("superseded_reason", superseded)

    def test_generate_refused_without_approval(self):
        from ai_architect.api import main as main_module

        client = self._client()
        snapshot = self._snapshot()
        snapshot.design_approval = {"approved": False}
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = client.post(
                "/design/sessions/design-s1/terraform", json={"inputs": {}}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "terraform_not_allowed"
        )


class ProvisioningCliTests(unittest.TestCase):
    def test_parsers_wire_subcommands(self):
        from ai_architect.cli.main import build_parser

        args = build_parser().parse_args(
            ["approve-provisioning", "--session-id", "design-s1"]
        )
        self.assertEqual(args.command, "approve-provisioning")
        args = build_parser().parse_args(
            ["provision", "--session-id", "design-s1", "--mode", "plan"]
        )
        self.assertEqual(args.command, "provision")
        self.assertEqual(args.mode, "plan")
        self.assertFalse(args.pass_aws_env)


class ProvisioningCliExecutionTests(unittest.TestCase):
    """The CLI shares the API's exact-saved-plan apply contract."""

    def _snapshot(self, review=None):
        return SimpleNamespace(
            requirements_state=object(),
            architecture=SimpleNamespace(artifact_id="artifact-1"),
            terraform_artifact=dict(PLAN),
            provisioning_approval=_approval(),
            provisioning_result=review,
        )

    def _run(self, snapshot, mode="apply", confirm="APPLY"):
        import asyncio

        from ai_architect.cli import provision as cli

        args = SimpleNamespace(
            session_id="design-s1", mode=mode, confirm=confirm,
            pass_aws_env=False,
        )
        calls = []

        async def fake_load(session_id):
            return snapshot

        async def fake_save(session_id, *, field, record):
            calls.append((field, record))

        runner = (
            "run_terraform_apply" if mode == "apply" else "run_terraform_plan"
        )
        with (
            patch.object(cli, "load_design_snapshot", fake_load),
            patch.object(cli, "save_session_record", fake_save),
            patch.object(cli, "terraform_available", return_value=True),
            patch.object(
                cli, runner,
                return_value={
                    "status": f"{mode}_ok" if mode == "plan"
                    else "apply_ok",
                    "exit_codes": {},
                    "output": "ok",
                },
            ) as run_cmd,
        ):
            code = asyncio.run(cli._provision_async(args))
        return code, run_cmd, calls

    def test_cli_plan_mode_runs_without_approval(self):
        snapshot = self._snapshot()
        snapshot.provisioning_approval = None
        code, run_plan, calls = self._run(snapshot, mode="plan", confirm="")
        self.assertEqual(code, 0)
        run_plan.assert_called_once()
        self.assertEqual([field for field, _ in calls],
                         ["provisioning_result"])

    def test_cli_apply_without_approval_refused(self):
        snapshot = self._snapshot(_review())
        snapshot.provisioning_approval = None
        code, run_apply, calls = self._run(snapshot)
        self.assertEqual(code, 1)
        run_apply.assert_not_called()
        self.assertEqual(calls, [])

    def test_cli_apply_forwards_exact_saved_plan_binding(self):
        code, run_apply, calls = self._run(self._snapshot(_review()))
        self.assertEqual(code, 0)
        kwargs = run_apply.call_args.kwargs
        self.assertEqual(kwargs.get("saved_plan_sha256"), "ab" * 32)
        self.assertEqual(kwargs.get("confirm"), "APPLY")
        self.assertEqual(
            [field for field, _ in calls],
            ["provisioning_result", "provisioning_approval",
             "provisioning_result"],
        )

    def test_cli_apply_with_unbound_review_refused_unconsumed(self):
        code, run_apply, calls = self._run(
            self._snapshot(_review(plan_file=None))
        )
        self.assertEqual(code, 1)
        run_apply.assert_not_called()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
