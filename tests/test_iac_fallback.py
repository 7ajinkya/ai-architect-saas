"""Controlled LLM Terraform fallback tests: policy, validation, render.

The LLM never runs here: proposals are hand-written (adversarial) or
come from scripted fake generators. Production LLM calls happen only
in live verification, never in tests.

Run: .venv/Scripts/python -m pytest tests/test_iac_fallback.py
"""
import unittest

from ai_architect.services.provisioning import iac_fallback as iac


def _proposal(*resources):
    return {"resources": list(resources)}


def _res(type_, **attrs):
    return {"type": type_, "attributes": dict(attrs)}


class PolicyTests(unittest.TestCase):
    def test_policy_lists_exactly_the_reviewed_types(self):
        self.assertEqual(
            set(iac.FALLBACK_POLICY_TYPES),
            {
                "aws_amplify_app",
                "aws_amplify_branch",
                "aws_xray_group",
                "aws_xray_sampling_rule",
            },
        )

    def test_unknown_type_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res("aws_db_instance", name="db")),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("outside the" in e for e in errors))

    def test_unrelated_safe_type_refused_for_component(self):
        # aws_dynamodb_table is a real, safe type but outside the
        # fallback policy: the policy is closed, not advisory.
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res("aws_dynamodb_table", name="t")),
            component_name="AWS Amplify (Hosting)",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_provisioner_key_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_app", name="a",
                provisioner={"local-exec": {"command": "echo x"}},
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("sandbox" in e for e in errors))

    def test_local_exec_key_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res("aws_amplify_app", name="a", **{"local-exec": "x"})),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_meta_arguments_refused(self):
        for key in ("count", "for_each", "lifecycle", "depends_on",
                    "provider"):
            specs, errors = iac.validate_fallback_proposal(
                _proposal(_res("aws_xray_group", group_name="g", **{key: "x"})),
                component_name="X",
            )
            self.assertEqual(specs, [], msg=key)
            self.assertTrue(errors, msg=key)

    def test_url_value_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_app", name="a",
                repository="https://github.com/example/demo",
            )),
            component_name="AWS Amplify (Hosting)",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("URL-shaped" in e for e in errors))

    def test_arn_value_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_xray_sampling_rule", rule_name="r",
                resource_arn="arn:aws:lambda:us-east-1:123456789012:fn",
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("ARN-shaped" in e for e in errors))

    def test_secret_shaped_value_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_branch", branch_name="main",
                basic_auth_credentials="user:hunter2",
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("credential" in e for e in errors))

    def test_interpolation_and_heredoc_refused(self):
        for bad in ("prefix-${var.suffix}", "x <<EOF"):
            specs, errors = iac.validate_fallback_proposal(
                _proposal(_res("aws_amplify_app", name=bad)),
                component_name="X",
            )
            self.assertEqual(specs, [], msg=bad)
            self.assertTrue(errors, msg=bad)

    def test_nested_map_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_app", name="a",
                environment_variables={"KEY": "value"},
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("nested" in e for e in errors))

    def test_empty_and_oversized_proposals_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            {"resources": []}, component_name="X"
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)
        big = {"resources": [
            _res("aws_xray_group", group_name=f"g{i}") for i in range(5)
        ]}
        specs, errors = iac.validate_fallback_proposal(
            big, component_name="X"
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_unknown_top_level_shape_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            {"resources": [{"type": "aws_amplify_app"}], "zzz": 1},
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)


class RefTests(unittest.TestCase):
    def test_valid_sibling_ref_passes(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(
                _res("aws_amplify_app", name="demo"),
                _res("aws_amplify_branch", branch_name="main",
                     app_id={"$ref": [0, "id"]}),
            ),
            component_name="AWS Amplify (Hosting)",
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 2)

    def test_ref_out_of_range_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_branch", branch_name="main",
                app_id={"$ref": [3, "id"]},
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(any("out of range" in e for e in errors))

    def test_ref_to_self_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_xray_group", group_name="g",
                other={"$ref": [0, "arn"]},
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_ref_bad_attribute_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(
                _res("aws_amplify_app", name="demo"),
                _res("aws_amplify_branch", branch_name="main",
                     app_id={"$ref": [0, "secret"]}),
            ),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_malformed_ref_refused(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(_res(
                "aws_amplify_branch", branch_name="main",
                app_id={"$ref": "app-id"},
            )),
            component_name="X",
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)


class IdentityTests(unittest.TestCase):
    def test_resource_id_derives_deterministically(self):
        self.assertEqual(
            iac.fallback_resource_id("aws-amplify-hosting"),
            "iac_fallback-aws-amplify-hosting",
        )

    def test_empty_node_id_refused(self):
        with self.assertRaises(iac.FallbackError):
            iac.fallback_resource_id("  ")

    def test_unsafe_node_id_refused(self):
        with self.assertRaises(iac.FallbackError):
            iac.fallback_resource_id("has space")


class RenderTests(unittest.TestCase):
    def test_golden_render_is_deterministic_hcl(self):
        specs, errors = iac.validate_fallback_proposal(
            _proposal(
                _res("aws_amplify_app", name="demo",
                     description="Demo hosting"),
                _res("aws_amplify_branch", branch_name="main",
                     app_id={"$ref": [0, "id"]}, enable_auto_build=False),
            ),
            component_name="AWS Amplify (Hosting)",
        )
        self.assertEqual(errors, [])
        first = iac.render_fallback_file([("iac_fallback-demo", specs)])
        second = iac.render_fallback_file([("iac_fallback-demo", specs)])
        self.assertEqual(first, second)
        self.assertIn('resource "aws_amplify_app" "iac_fallback-demo-1" {',
                      first)
        self.assertRegex(first, r'name\s+= "demo"')
        self.assertRegex(
            first, r'app_id\s+= aws_amplify_app\.iac_fallback-demo-1\.id'
        )
        self.assertRegex(first, r'enable_auto_build\s+= false')
        self.assertTrue(first.endswith("}\n"))
        self.assertNotIn("${", first)

    def test_label_collision_refused(self):
        specs, _ = iac.validate_fallback_proposal(
            _proposal(_res("aws_xray_group", group_name="g")),
            component_name="X",
        )
        with self.assertRaises(iac.FallbackError):
            iac.render_fallback_file([
                ("iac_fallback-dup", specs),
                ("iac_fallback-dup", specs),
            ])

    def test_sha_binds_content(self):
        specs, _ = iac.validate_fallback_proposal(
            _proposal(_res("aws_xray_group", group_name="g")),
            component_name="X",
        )
        first = iac.proposal_sha256(specs)
        self.assertEqual(len(first), 64)
        self.assertEqual(first, iac.proposal_sha256(specs))
        altered = [dict(specs[0], attributes={"group_name": "h"})]
        self.assertNotEqual(first, iac.proposal_sha256(altered))


class RetryTests(unittest.TestCase):
    def _component(self):
        return {
            "component_name": "AWS X-Ray (Tracing)",
            "node_id": "x",
            "category": "",
            "role_in_system": "",
        }

    def _context(self):
        return {"provider": "aws", "region_code": "r",
                "design_artifact_id": "a", "evidence_refs": []}

    def test_correction_retry_recovers(self):
        calls = []

        class FakeLLM:
            def invoke(self, messages):
                calls.append(len(messages))
                if len(calls) == 1:
                    return {"resources": [
                        {"type": "aws_db_instance", "attributes": {}},
                    ]}
                return {"resources": [
                    {"type": "aws_xray_group",
                     "attributes": {"group_name": "g"}},
                ]}

        specs, sha = iac.generate_fallback_content(
            component=self._component(), context=self._context(),
            llm=FakeLLM(),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(specs[0]["type"], "aws_xray_group")
        self.assertEqual(len(sha), 64)

    def test_repeated_failure_fails_closed(self):
        class FakeLLM:
            def invoke(self, messages):
                return {"resources": [
                    {"type": "aws_db_instance", "attributes": {}},
                ]}

        with self.assertRaises(iac.FallbackError) as ctx:
            iac.generate_fallback_content(
                component=self._component(), context=self._context(),
                llm=FakeLLM(),
            )
        self.assertIn("2 attempt", str(ctx.exception))

    def test_transport_failure_fails_closed_without_retry(self):
        calls = []

        class FakeLLM:
            def invoke(self, messages):
                calls.append(1)
                raise RuntimeError("boom")

        with self.assertRaises(iac.FallbackError):
            iac.generate_fallback_content(
                component=self._component(), context=self._context(),
                llm=FakeLLM(),
            )
        self.assertEqual(len(calls), 1)

    def test_unconfigured_llm_fails_closed(self):
        import ai_architect.infrastructure.llm.factory as factory

        original = factory.create_llm
        factory.create_llm = lambda profile: (_ for _ in ()).throw(
            Exception("no model")
        )
        try:
            with self.assertRaises(iac.FallbackError):
                iac.generate_fallback_content(
                    component=self._component(), context=self._context(),
                    llm=None,
                )
        finally:
            factory.create_llm = original


class EligibilityTests(unittest.TestCase):
    """Only the explicitly reviewed fallback services are eligible."""

    def test_eligible_names_cover_amplify_and_xray(self):
        for name in (
            "AWS Amplify (Hosting)",
            "Amplify Hosting",
            "AWS X-Ray (Tracing)",
            "X-Ray",
            "xray",
        ):
            with self.subTest(name=name):
                self.assertIsNone(iac.fallback_refusal_reason(name))

    def test_every_other_component_is_refused(self):
        for name in (
            "Amazon CloudFront",
            "Amazon Route 53",
            "Amazon S3 static website hosting",
            "Amazon API Gateway REST API",
            "Amazon SQS",
        ):
            with self.subTest(name=name):
                reason = iac.fallback_refusal_reason(name)
                self.assertIsNotNone(reason)
                self.assertIn("outside the controlled fallback", reason)

    def test_generator_refuses_ineligible_component_without_llm(self):
        calls = []

        class FakeLLM:
            def invoke(self, messages):
                calls.append(1)
                return _proposal(_res("aws_amplify_app", name="a"))

        with self.assertRaises(iac.FallbackError) as ctx:
            iac.generate_fallback_content(
                component={
                    "component_name": "Amazon CloudFront",
                    "node_id": "cdn",
                    "category": "networking",
                    "role_in_system": "delivery",
                },
                context={
                    "provider": "aws", "region_code": "us-east-1",
                    "design_artifact_id": "a", "evidence_refs": [],
                },
                llm=FakeLLM(),
            )
        self.assertIn("outside the controlled fallback", str(ctx.exception))
        self.assertEqual(calls, [])


class PartitionTests(unittest.TestCase):
    def _arch(self, *nodes):
        from types import SimpleNamespace

        return SimpleNamespace(nodes=list(nodes))

    def _node(self, node_id, name):
        from types import SimpleNamespace

        return SimpleNamespace(
            node_id=node_id, component_name=name, category="",
            role_in_system="",
        )

    def test_mixed_splits_supported_unsupported_iam(self):
        supported, unsupported, skipped = iac.partition_architecture(
            self._arch(
                self._node("b", "Amazon S3"),
                self._node("a", "AWS Amplify (Hosting)"),
                self._node("r", "AWS IAM (service role)"),
            )
        )
        self.assertEqual([n.node_id for n in supported], ["b"])
        self.assertEqual([n.node_id for n in unsupported], ["a"])
        self.assertEqual([n.node_id for n in skipped], ["r"])

    def test_specific_iam_still_unsupported_not_skipped(self):
        _, unsupported, skipped = iac.partition_architecture(
            self._arch(self._node("u", "AWS IAM User"))
        )
        self.assertEqual([n.node_id for n in unsupported], ["u"])
        self.assertEqual(skipped, [])


class PlanIntegrationTests(unittest.TestCase):
    """Fallback through the real plan_builder with injected generators."""

    def _architecture(self, *nodes):
        from datetime import datetime

        from ai_architect.domain.architecture.models import (
            ArchitectureInputsRef,
            ArchitectureNode,
            ArchitectureResult,
        )

        return ArchitectureResult(
            artifact_id="artifact-9",
            session_id="design-9",
            version=1,
            nodes=[
                ArchitectureNode(
                    node_id=node_id, component_name=name,
                    category="other", role_in_system="role",
                )
                for node_id, name in nodes
            ],
            edges=[],
            entry_points=[],
            data_store_node_ids=[],
            boundaries=[],
            cost_links=[],
            cost_totals_snapshot={},
            decisions=[],
            assumptions=[],
            warnings=[],
            unresolved_tradeoffs=[],
            evidence_references=[],
            inputs_ref=ArchitectureInputsRef(
                requirements_session_id="req-9",
                capacity_formula_version="v1",
                selection_generated_at=datetime(2026, 1, 1),
                cost_generated_at=datetime(2026, 1, 1),
                cost_pricing_as_of=datetime(2026, 1, 1),
                region_code="us-east-1",
                upstream_inputs_hash="hash-9",
            ),
            generated_at=datetime(2026, 1, 1),
        )

    def _approval(self):
        from ai_architect.services.approvals import build_design_approval

        return build_design_approval(
            approved=True, note="", artifact_id="artifact-9",
            gate_outcome="approved_for_review",
        )

    def _plan(self, arch, generator):
        from ai_architect.services.provisioning.plan_builder import (
            build_provisioning_plan,
        )

        return build_provisioning_plan(
            architecture=arch, provider="aws", approval=self._approval(),
            iac_generator=generator,
        )

    def test_supported_only_never_calls_generator(self):
        calls = []
        plan = self._plan(
            self._architecture(("b", "Amazon S3")),
            lambda *, component, context, prior_errors=None: (
                calls.append(1), ([], ""))[1],
        )
        self.assertEqual(calls, [])
        self.assertEqual(plan["generated"], [])

    def test_ineligible_unsupported_component_invalidates_plan(self):
        """CloudFront Functions/Route 53/S3 website never fall back."""
        calls = []
        plan = self._plan(
            self._architecture(
                ("b", "Amazon S3"),
                ("cdn", "Amazon CloudFront Functions"),
                ("dns", "Amazon Route 53"),
                (
                    "site",
                    "Amazon S3 static website hosting",
                ),
            ),
            lambda *, component, context, prior_errors=None: (
                calls.append(1), ([], ""))[1],
        )
        self.assertEqual(calls, [])
        self.assertEqual(plan["status"], "incomplete")
        self.assertEqual(plan["tree_hash"], "")
        self.assertEqual(plan["files"], {})
        refused = [
            item for item in plan["render_diagnostics"]
            if item["category"] == "fallback_refused"
        ]
        self.assertEqual(len(refused), 3)
        self.assertTrue(all(
            "outside the controlled fallback" in item["message"]
            for item in refused
        ))
        failed = [
            entry for entry in plan["generated"]
            if entry["status"] == "failed"
        ]
        self.assertEqual(
            {entry["component_name"] for entry in failed},
            {
                "Amazon CloudFront Functions", "Amazon Route 53",
                "Amazon S3 static website hosting",
            },
        )
        self.assertFalse(any(
            entry["status"] == "rendered" for entry in plan["generated"]
        ))

    def test_fallback_renders_merges_and_binds(self):
        def fake(*, component, context, prior_errors=None):
            self.assertEqual(
                component["component_name"], "AWS Amplify (Hosting)"
            )
            self.assertEqual(context["provider"], "aws")
            specs = [
                {"type": "aws_amplify_app",
                 "attributes": {"name": "demo"}},
                {"type": "aws_amplify_branch",
                 "attributes": {"branch_name": "main",
                                "app_id": {"$ref": [0, "id"]}}},
            ]
            return specs, iac.proposal_sha256(specs)

        plan = self._plan(
            self._architecture(("web", "AWS Amplify (Hosting)")), fake
        )
        self.assertIn("fallback.tf", plan["files"])
        self.assertEqual(len(plan["generated"]), 1)
        entry = plan["generated"][0]
        self.assertEqual(entry["status"], "rendered")
        self.assertEqual(
            entry["resource_types"],
            ["aws_amplify_app", "aws_amplify_branch"],
        )
        self.assertEqual(len(entry["proposal_sha256"]), 64)
        self.assertEqual(
            entry["resource_id"], "iac_fallback-web"
        )
        # Tree hash covers the merged tree (fallback included).
        self.assertIn("fallback.tf", plan["files"])
        by_kind = plan["inventory"]["by_kind"]
        self.assertEqual(by_kind.get("iac_fallback"), 1)
        self.assertIsNone(plan["inventory"]["destroys"])
        # Deterministic: identical inputs rebuild the identical tree.
        again = self._plan(
            self._architecture(("web", "AWS Amplify (Hosting)")), fake
        )
        self.assertEqual(again["tree_hash"], plan["tree_hash"])
        # Validation ran on the merged tree (or honestly absent).
        self.assertIn(
            plan["terraform_validation"]["status"],
            ("validate_ok", "not_installed"),
        )

    def test_failed_fallback_is_all_or_nothing(self):
        def failing(*, component, context):
            raise iac.FallbackError("LLM exploded")

        plan = self._plan(
            self._architecture(
                ("web", "AWS Amplify (Hosting)"),
                ("b", "Amazon S3"),
            ),
            failing,
        )
        self.assertEqual(plan["status"], "incomplete")
        self.assertEqual(plan["files"], {})
        self.assertEqual(len(plan["generated"]), 1)
        self.assertEqual(plan["generated"][0]["status"], "failed")
        self.assertTrue(plan["generated"][0]["notes"])

    def test_changed_proposal_invalidates_approval(self):
        from ai_architect.services.provisioning.executor import (
            build_provisioning_approval,
            require_provisioning_approval,
        )
        from ai_architect.services.provisioning.executor import (
            ExecutionRefused,
        )

        def fake_a(*, component, context, prior_errors=None):
            specs = [{"type": "aws_xray_group",
                      "attributes": {"group_name": "g-a"}}]
            return specs, iac.proposal_sha256(specs)

        def fake_b(*, component, context, prior_errors=None):
            specs = [{"type": "aws_xray_group",
                      "attributes": {"group_name": "g-b"}}]
            return specs, iac.proposal_sha256(specs)

        arch = self._architecture(("t", "AWS X-Ray (Tracing)"))
        plan_a = self._plan(arch, fake_a)
        plan_b = self._plan(arch, fake_b)
        self.assertNotEqual(plan_a["tree_hash"], plan_b["tree_hash"])
        approval = build_provisioning_approval(
            approved=True, note="", tree_hash=plan_a["tree_hash"],
            spec_id=plan_a["spec_id"], artifact_id=plan_a["artifact_id"],
            artifacts=plan_a.get("artifacts"),
        )
        with self.assertRaises(ExecutionRefused):
            require_provisioning_approval(approval, plan_b)

    def test_guard_union_admits_policy_rejects_others(self):
        from ai_architect.services.provisioning.terraform_guard import (
            guard_terraform_source,
        )
        from ai_architect.services.provisioning.terraform_renderer import (
            ALLOWED_TF_RESOURCE_TYPES,
        )

        allowed = ALLOWED_TF_RESOURCE_TYPES | iac.FALLBACK_POLICY_TYPES
        good = {
            "fallback.tf": 'resource "aws_amplify_app" "x" {\n'
                           '  name = "n"\n}\n'
        }
        self.assertEqual(
            guard_terraform_source(good, allowed_resource_types=allowed),
            [],
        )
        bad = {
            "fallback.tf": 'resource "aws_db_instance" "x" {\n'
                           '  name = "n"\n}\n'
        }
        self.assertTrue(
            guard_terraform_source(bad, allowed_resource_types=allowed)
        )

    def test_validate_failure_retries_once_with_attributed_errors(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from ai_architect.services.provisioning import plan_builder
        from ai_architect.services.provisioning.terraform_cli import (
            TerraformStatus,
        )

        seen_priors = []

        def fake(*, component, context, prior_errors=None):
            seen_priors.append(list(prior_errors or []))
            if not prior_errors:
                specs = [{"type": "aws_xray_group",
                          "attributes": {"group_name": "g-old"}}]
            else:
                specs = [{"type": "aws_xray_group",
                          "attributes": {"group_name": "g-new"}}]
            return specs, iac.proposal_sha256(specs)

        def fake_checks(tree_files):
            if fake_checks.calls == 0:
                fake_checks.calls += 1
                return SimpleNamespace(
                    status=TerraformStatus.VALIDATE_FAILED,
                    installed=True,
                    version="Terraform v1.9.8",
                    exit_codes={"validate": 1},
                    diagnostics=[SimpleNamespace(
                        category="tooling",
                        message=(
                            'on fallback.tf line 2, in resource '
                            '"aws_xray_group" '
                            '"iac_fallback-trace-1": bad argument'
                        ),
                    )],
                )
            return SimpleNamespace(
                status=TerraformStatus.VALIDATE_OK,
                installed=True,
                version="Terraform v1.9.8",
                exit_codes={"validate": 0},
                diagnostics=[],
            )

        fake_checks.calls = 0
        arch = self._architecture(("trace", "AWS X-Ray (Tracing)"))
        with patch.object(
            plan_builder, "run_terraform_checks",
            side_effect=lambda files: fake_checks(files),
        ):
            plan = self._plan(arch, fake)
        # Exactly one extra round: fresh attempt + one correction.
        self.assertEqual(len(seen_priors), 2)
        self.assertEqual(seen_priors[0], [])
        self.assertTrue(
            any("fallback.tf" in message for message in seen_priors[1])
        )
        self.assertEqual(plan["terraform_validation"]["status"],
                         "validate_ok")
        self.assertEqual(plan["status"], "valid")
        self.assertEqual(plan["generated"][0]["status"], "rendered")

    def test_validate_failure_without_fallback_mention_does_not_retry(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from ai_architect.services.provisioning import plan_builder
        from ai_architect.services.provisioning.terraform_cli import (
            TerraformStatus,
        )

        calls = []

        def fake(*, component, context, prior_errors=None):
            calls.append(1)
            specs = [{"type": "aws_xray_group",
                      "attributes": {"group_name": "g"}}]
            return specs, iac.proposal_sha256(specs)

        def fake_checks(tree_files):
            return SimpleNamespace(
                status=TerraformStatus.VALIDATE_FAILED,
                installed=True,
                version="Terraform v1.9.8",
                exit_codes={"validate": 1},
                diagnostics=[SimpleNamespace(
                    category="tooling",
                    message="on main.tf line 1: verified tree is broken",
                )],
            )

        arch = self._architecture(("trace", "AWS X-Ray (Tracing)"))
        with patch.object(
            plan_builder, "run_terraform_checks",
            side_effect=lambda files: fake_checks(files),
        ):
            plan = self._plan(arch, fake)
        # Single generation round: nothing implicates the fallback.
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan["status"], "incomplete")


class TextEnvelopeTests(unittest.TestCase):
    """Providers without function calling still go through the schema."""

    def _component(self):
        return {
            "component_name": "AWS X-Ray (Tracing)",
            "node_id": "x",
            "category": "",
            "role_in_system": "",
        }

    def _context(self):
        return {"provider": "aws", "region_code": "r",
                "design_artifact_id": "a", "evidence_refs": []}

    def _good_text(self):
        return (
            'Here is the proposal: {"resources": '
            '[{"type": "aws_xray_group", '
            '"attributes": {"group_name": "g"}}]} done.'
        )

    def test_none_tool_call_falls_back_to_text(self):
        from types import SimpleNamespace

        bound_calls = []
        text = self._good_text()

        class Bound:
            def invoke(self, messages):
                bound_calls.append(1)
                return None

        class Raw:
            def invoke(self, messages):
                from types import SimpleNamespace

                return SimpleNamespace(content=text)

        specs, sha = iac.generate_fallback_content(
            component=self._component(), context=self._context(),
            llm=Bound(), raw_llm=Raw(),
        )
        self.assertEqual(len(bound_calls), 1)
        self.assertEqual(specs[0]["type"], "aws_xray_group")
        self.assertEqual(len(sha), 64)

    def test_garbage_text_fails_closed(self):
        from types import SimpleNamespace

        class Bound:
            def invoke(self, messages):
                return None

        class Raw:
            def invoke(self, messages):
                return SimpleNamespace(content="not json at all {{{")

        with self.assertRaises(iac.FallbackError):
            iac.generate_fallback_content(
                component=self._component(), context=self._context(),
                llm=Bound(), raw_llm=Raw(),
            )


if __name__ == "__main__":
    unittest.main()
