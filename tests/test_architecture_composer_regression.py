"""Regression tests for the whiteboard smoke-test parse failure.

The real model (gpt-oss:120b-cloud, function calling) emitted its own
conventional field names instead of the tool-schema names observed in
the captured raw output (composer_raw_args.json):
nodes[].name / capacity_metrics, edges from/to, decisions.description,
boundaries as an object, warnings as {id, message} objects, and kind
"read/write". These tests lock the tolerant handling of that exact
shape without weakening semantic validation.

Run: python -m unittest discover tests (with the project .venv)
"""
import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.architecture import run as run_module
from ai_architect.agents.architecture.run import ArchitectureLLMOutput
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
    UnresolvedTradeoff,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CostLineItem,
    PricingModel,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.validator import validate_architecture

GEN = datetime(2026, 10, 4, 12, 0, 0)
PRICE_TS = datetime(2026, 10, 4, 11, 0, 0)
REQ_SESSION = "req-s1"
DESIGN_SESSION = "design-s1"
PROVIDER = "aws"
REGION = "ap-south-1"


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description=(
            "Real-time collaborative whiteboard for 50000 DAU"
        ),
        cloud_provider="aws",
        region="Mumbai region",
        availability="99.9%",
        latency="p99 under 300ms",
        target_users_dau="50000",
        managed_service_preference="fully managed",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=69.4,
        peak_read_qps=347.2,
        concurrent_connections=6000.0,
        storage_growth_bytes_per_day=8000000.0,
        hot_working_set_bytes=240000000.0,
        egress_bytes_per_second=355532.8,
        formula_version="v1",
        calculation_trace={},
    )


def _comp(name, category, role, ev):
    return ComponentCandidate(
        name=name, provider="aws", category=category, role=role,
        rationale=f"{role} for the whiteboard",
        evidence_refs=[ev] if ev else [],
        confidence="high",
        evidence_support="strong" if ev else "insufficient",
    )


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            _comp("Amazon API Gateway", "networking",
                  "websocket entry", "ev-1"),
            _comp("AWS Lambda", "compute", "sync handlers", None),
            _comp("Amazon DynamoDB", "database",
                  "board state store", "ev-2"),
            _comp("Amazon Simple Storage Service", "storage",
                  "snapshot assets", "ev-3"),
            _comp("Amazon CloudFront", "cdn", "asset delivery", "ev-4"),
            _comp("Amazon Cognito", "identity", "user auth", "ev-5"),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
        evidence_references=["ev-1", "ev-2", "ev-3", "ev-4", "ev-5"],
        generated_at=GEN,
    )


def _line(component, unit, usage, price, total):
    return CostLineItem(
        component_name=component,
        pricing_model=PricingModel.ON_DEMAND,
        unit=unit,
        monthly_usage=usage,
        unit_price_usd=price,
        monthly_cost_usd=total,
        usage_formula="deterministic usage derivation",
        pricing_source="aws-price-list-api",
        pricing_retrieved_at=PRICE_TS,
        region_code=REGION,
    )


def _cost() -> CostEstimationResult:
    lines = [
        _line("Amazon API Gateway", "Requests", 1e7, 0.000001, 10.0),
        _line("Amazon DynamoDB", "WRU", 1e8, 0.0000014, 140.0),
        _line("Amazon Simple Storage Service", "GB-Mo", 240.0, 0.023, 5.52),
        _line("Amazon CloudFront", "GB", 355.0, 0.085, 30.175),
    ]
    return CostEstimationResult(
        lines=lines,
        totals_usd_per_month={
            "on_demand": sum(line.monthly_cost_usd for line in lines)
        },
        warnings=[],
        unpriced=[
            UnpricedComponent(
                component_name="Amazon Cognito",
                reason="Cannot price: rule and user-pool counts are "
                "not determined.",
            ),
            UnpricedComponent(
                component_name="AWS Lambda",
                reason="Cannot price: function duration and memory "
                "configuration are not determined.",
            ),
        ],
        generated_at=GEN,
        pricing_as_of=PRICE_TS,
    )


def _observed_shape() -> dict:
    """The exact field-naming shape captured from the live model."""
    return {
        "assumptions": [],
        "boundaries": {
            "availability": "99.9%",
            "latency_p99_ms": 300,
            "region": "ap-south-1",
        },
        "data_stores": [
            "Amazon DynamoDB",
            "Amazon Simple Storage Service",
        ],
        "decisions": [
            {
                "alternatives_considered": "None, as component selection fixed.",
                "description": "Use API Gateway WebSocket as the entry point.",
                "id": "decision-1",
                "rationale": "Managed bidirectional sync; supported by ev-1.",
            },
        ],
        "edges": [
            {
                "description": "WebSocket collaboration.",
                "from": "client",
                "kind": "request",
                "protocol": "wss",
                "to": "Amazon API Gateway",
            },
            {
                "description": "Route messages to sync handlers.",
                "from": "Amazon API Gateway",
                "kind": "request",
                "protocol": "",
                "to": "AWS Lambda",
            },
            {
                "description": "Persist board snapshots.",
                "from": "AWS Lambda",
                "kind": "write",
                "protocol": "",
                "to": "Amazon Simple Storage Service",
            },
        ],
        "entry_points": ["Amazon API Gateway"],
        "new_tradeoffs": [{"description": "Lambda scaling evidence gap.",
                           "id": "tradeoff-1"}],
        "nodes": [
            {"capacity_metrics": ["peak_write_qps", "concurrent_connections"],
             "name": "Amazon API Gateway"},
            {"capacity_metrics": ["peak_write_qps"],
             "name": "AWS Lambda"},
            {"capacity_metrics": ["peak_write_qps", "hot_working_set_bytes"],
             "name": "Amazon DynamoDB"},
            {"capacity_metrics": ["storage_growth_bytes_per_day"],
             "name": "Amazon Simple Storage Service"},
            {"capacity_metrics": ["egress_bytes_per_second"],
             "name": "Amazon CloudFront"},
            {"capacity_metrics": ["concurrent_connections"],
             "name": "Amazon Cognito"},
        ],
        "warnings": [
            {"id": "warning-1",
             "message": "Lambda scaling evidence is insufficient."},
        ],
    }


def _upstream():
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": _selection(),
        "cost": _cost(),
    }


def _normalize(shape):
    up = _upstream()
    parsed = ArchitectureLLMOutput.model_validate(copy.deepcopy(shape))
    return run_module.normalize_architecture(
        parsed,
        extraction=up["extraction"],
        capacity=up["capacity"],
        selection=up["selection"],
        cost=up["cost"],
        region_code=REGION,
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
    ), up


def _check_valid(up, result):
    return validate_architecture(
        result,
        selection=up["selection"],
        capacity=up["capacity"],
        cost=up["cost"],
        extraction=up["extraction"],
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
        region_code=REGION,
    )


class ComposerRegressionTests(unittest.TestCase):
    def test_01_observed_node_shape_normalized(self):
        result, up = _normalize(_observed_shape())
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(
            {node.component_name for node in result.nodes},
            {comp.name for comp in up["selection"].selected_components},
        )
        gateway = next(
            node for node in result.nodes
            if node.component_name == "Amazon API Gateway"
        )
        self.assertEqual(
            [str(item.metric) for item in gateway.capacity],
            ["peak_write_qps", "concurrent_connections"],
        )
        self.assertEqual(
            gateway.capacity[0].value, up["capacity"].peak_write_qps
        )
        # Role falls back to the validated selection role, recorded openly.
        lambda_node = next(
            node for node in result.nodes
            if node.component_name == "AWS Lambda"
        )
        self.assertEqual(lambda_node.role_in_system, "sync handlers")
        self.assertTrue(
            any("copied from component selection" in w
                for w in result.warnings)
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_02_observed_edge_shape_normalized(self):
        result, up = _normalize(_observed_shape())
        by_id = {edge.edge_id: edge for edge in result.edges}
        self.assertEqual(by_id["e1"].from_node, "client")
        api_id = next(
            node.node_id for node in result.nodes
            if node.component_name == "Amazon API Gateway"
        )
        self.assertEqual(by_id["e1"].to_node, api_id)
        self.assertEqual(_check_valid(up, result), [])

    def test_03_observed_boundaries_dict_handled(self):
        result, up = _normalize(_observed_shape())
        self.assertIn("region:ap-south-1", result.boundaries)
        self.assertIn("availability:99.9%", result.boundaries)
        self.assertIn("latency_p99_ms:300", result.boundaries)
        self.assertEqual(_check_valid(up, result), [])

    def test_04_compound_kind_rejected_with_useful_error(self):
        # "read/write" cannot map to one taxonomy value without
        # inventing direction semantics, so it stays a loud parse
        # rejection that names the valid choices.
        shape = _observed_shape()
        shape["edges"][2]["kind"] = "read/write"
        with self.assertRaises(ValueError) as ctx:
            ArchitectureLLMOutput.model_validate(shape)
        message = str(ctx.exception).lower()
        self.assertIn("read/write", message)
        for valid in ("request", "read", "write", "async", "media",
                      "control"):
            self.assertIn(valid, message)

    def test_05_invalid_kind_cannot_bypass_taxonomy(self):
        shape = _observed_shape()
        shape["edges"][0]["kind"] = "teleport"
        with self.assertRaises(ValueError):
            ArchitectureLLMOutput.model_validate(shape)

    def test_06_unknown_component_still_rejected(self):
        shape = _observed_shape()
        shape["nodes"].append({"name": "Amazon Neptune"})
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        self.assertIn("unknown component",
                      str(ctx.exception).lower())

    def test_07_invented_evidence_still_rejected(self):
        shape = _observed_shape()
        shape["nodes"][0]["evidence_refs"] = ["ev-forged"]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        self.assertIn("ev-forged", str(ctx.exception))

    def test_08_numeric_injection_still_neutralized(self):
        shape = _observed_shape()
        shape["nodes"][0]["peak_write_qps"] = 99999.0
        shape["nodes"][0]["monthly_cost_usd"] = 1.23
        result, up = _normalize(shape)
        gateway = next(
            node for node in result.nodes
            if node.component_name == "Amazon API Gateway"
        )
        self.assertEqual(
            gateway.capacity[0].value, up["capacity"].peak_write_qps
        )

    def test_09_fabricated_prices_impossible(self):
        forbidden = {
            "price", "prices", "pricing", "monthly_cost_usd",
            "unit_price_usd", "cost", "costs",
        }
        fields = set(ArchitectureLLMOutput.model_fields)
        self.assertEqual(fields & forbidden, set())
        result, up = _normalize(_observed_shape())
        gateway = next(
            link for link in result.cost_links
            if link.component_name == "Amazon API Gateway"
        )
        self.assertAlmostEqual(gateway.monthly_cost_usd, 10.0)
        self.assertEqual(
            result.cost_totals_snapshot,
            up["cost"].totals_usd_per_month,
        )

    def test_10_envelope_fallback_with_observed_shape(self):
        envelope = json.dumps(_observed_shape())

        class _FakeBound:
            def invoke(self, messages):
                return {"raw": AIMessage(content=envelope),
                        "parsed": None, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("retry must not be needed")

        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=_FakeBound(),
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            result = run_module.run_architecture_composer(
                up["extraction"], up["capacity"], up["selection"],
                up["cost"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(_check_valid(up, result), [])
        self.assertIn(run_module.FALLBACK_NOTE, result.warnings)


    def test_11_decision_prose_numerics_rejected(self):
        # Live whiteboard output put load numbers into decision
        # rationale/assumptions prose; those belong only in capacity
        # linkage, so normalization must fail loudly.
        shape = _observed_shape()
        shape["decisions"][0]["rationale"] = (
            "Managed sync sized for 347 QPS peak read load."
        )
        shape["assumptions"] = ["Growth headroom of 50 GB per month."]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        message = str(ctx.exception).lower()
        self.assertIn("throughput", message)
        self.assertIn("storage", message)

    def test_12_unknown_service_in_decisions_rejected(self):
        # Live whiteboard output named services outside the selected
        # and rejected sets inside decisions; those must be rejected,
        # not carried into the artifact.
        shape = _observed_shape()
        shape["decisions"][0]["alternatives_considered"] = [
            "Amazon ElastiCache instead of DynamoDB"
        ]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        self.assertIn("outside the selected", str(ctx.exception).lower())


    def test_13_wordform_availability_is_traceable(self):
        # Requirements stating "99.9 percent" (word form) support a
        # restated availability note just like the "%" form does.
        shape = _observed_shape()
        shape["nodes"][2]["availability_note"] = (
            "designed for the 99.9 percent availability target"
        )
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        wordform = copy.deepcopy(up["extraction"])
        wordform.availability = "99.9 percent"
        result = run_module.normalize_architecture(
            parsed,
            extraction=wordform,
            capacity=up["capacity"],
            selection=up["selection"],
            cost=up["cost"],
            region_code=REGION,
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
        )
        errors = _check_valid(
            {**up, "extraction": wordform}, result
        )
        self.assertEqual(errors, [])


    def test_14_correction_retry_recovers(self):
        # First output violates prose-numeric rules; the bounded
        # correction retry resubmits through the same path and succeeds.
        bad = ArchitectureLLMOutput.model_validate(_observed_shape())
        bad.decisions[0].rationale = "Managed sync sized for 347 QPS."
        good = ArchitectureLLMOutput.model_validate(_observed_shape())

        class _SeqBound:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                parsed = bad if self.calls == 1 else good
                return {"raw": AIMessage(content=""),
                        "parsed": parsed, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=_SeqBound(),
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            result = run_module.run_architecture_composer(
                up["extraction"], up["capacity"], up["selection"],
                up["cost"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(_check_valid(up, result), [])

    def test_15_double_failure_still_fails_loudly(self):
        bad = ArchitectureLLMOutput.model_validate(_observed_shape())
        bad.decisions[0].rationale = "Managed sync sized for 347 QPS."

        class _AlwaysBad:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                self.last_messages = messages
                return {"raw": AIMessage(content=""),
                        "parsed": bad, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        agent = _AlwaysBad()
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=agent,
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], up["selection"],
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                )
        # Exactly one correction retry: two bound calls, then loud failure.
        self.assertEqual(agent.calls, 2)
        self.assertIn("correction", str(ctx.exception).lower())
        # The retry carried the validator's complaints to the model.
        retry_prompt = agent.last_messages[-1].content
        self.assertIn("REJECTED", retry_prompt)
        # The retry states the concrete naming rule for provider
        # services in decision prose.
        self.assertIn("generic", retry_prompt.lower())
        self.assertIn("allowed components", retry_prompt.lower())
        self.assertIn("component_ref", retry_prompt)


    def test_16_malformed_text_recovers_via_strict_retry(self):
        # Live failure shape: text envelope with a mid-text JSON syntax
        # error (e.g. trailing comma). One strict-format retry through
        # the same schema recovers; parsing is never loosened.
        malformed = (
            '{"nodes": [{"component_name": '
            '"Amazon API Gateway (WebSocket)",},]}'
        )

        class _SeqBound:
            def __init__(self, valid):
                self.calls = 0
                self.valid = valid

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return {"raw": AIMessage(content=malformed),
                            "parsed": None, "parsing_error": None}
                return {"raw": AIMessage(content=""),
                        "parsed": self.valid, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        valid = ArchitectureLLMOutput.model_validate(_observed_shape())
        agent = _SeqBound(valid)
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=agent,
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            result = run_module.run_architecture_composer(
                up["extraction"], up["capacity"], up["selection"],
                up["cost"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertEqual(agent.calls, 2)
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(_check_valid(up, result), [])

    def test_17_repeated_malformed_text_fails_loudly(self):
        # Truncated/incomplete text that never parses must fail loudly
        # with both attempts reported, never a partial artifact.
        truncated = (
            '{"nodes": [{"component_name": '
            '"Amazon API Gateway (WebSocket)", "role_in_system": "entry'
        )

        class _AlwaysBad:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                return {"raw": AIMessage(content=truncated),
                        "parsed": None, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        agent = _AlwaysBad()
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=agent,
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], up["selection"],
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                )
        self.assertEqual(agent.calls, 2)
        self.assertIn("strict-format retry",
                      str(ctx.exception).lower())


    def test_18_metric_key_aliases_normalized(self):
        shape = _observed_shape()
        shape["edges"][1]["load"] = "write_qps"
        shape["nodes"][2]["capacity_metrics"] = [
            "read_qps", "hot_working_set",
        ]
        result, up = _normalize(shape)
        by_id = {edge.edge_id: edge for edge in result.edges}
        self.assertEqual(by_id["e2"].load.metric, "peak_write_qps")
        self.assertEqual(
            by_id["e2"].load.value, up["capacity"].peak_write_qps
        )
        dynamo = next(
            node for node in result.nodes
            if node.component_name == "Amazon DynamoDB"
        )
        self.assertEqual(
            [str(item.metric) for item in dynamo.capacity],
            ["peak_read_qps", "hot_working_set_bytes"],
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_19_ambiguous_metric_key_still_rejected(self):
        from pydantic import ValidationError

        shape = _observed_shape()
        shape["edges"][1]["load"] = "qps"
        with self.assertRaises(ValidationError):
            ArchitectureLLMOutput.model_validate(shape)
        shape["edges"][1]["load"] = "bandwidth"
        with self.assertRaises(ValidationError):
            ArchitectureLLMOutput.model_validate(shape)

    def test_20_duplicate_tradeoffs_deduplicated(self):
        carried = UnresolvedTradeoff(
            description="Authentication method",
            options=["cognito", "custom"],
            impact="login UX and security posture",
        )
        up = _upstream()
        selection = up["selection"].model_copy(
            update={"unresolved_tradeoffs": [carried]}
        )
        shape = _observed_shape()
        shape["new_tradeoffs"] = [
            {"description": "  AUTHENTICATION   method  "},
            {"description": "Cache invalidation strategy"},
        ]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        result = run_module.normalize_architecture(
            parsed,
            extraction=up["extraction"],
            capacity=up["capacity"],
            selection=selection,
            cost=up["cost"],
            region_code=REGION,
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
        )
        descriptions = [
            t.description for t in result.unresolved_tradeoffs
        ]
        self.assertEqual(
            descriptions,
            ["Authentication method", "Cache invalidation strategy"],
        )


    def test_21_correction_retry_lists_allowed_vocabulary(self):
        bad = ArchitectureLLMOutput.model_validate(_observed_shape())
        bad.decisions[0].alternatives_considered = [
            "Amazon ElastiCache instead of DynamoDB"
        ]

        class _AlwaysBad:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                self.last_messages = messages
                return {"raw": AIMessage(content=""),
                        "parsed": bad, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        agent = _AlwaysBad()
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=agent,
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            with self.assertRaises(ValueError):
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], up["selection"],
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                )
        # Exactly one correction retry, carrying the exact allowed refs.
        self.assertEqual(agent.calls, 2)
        retry_prompt = agent.last_messages[-1].content
        self.assertIn("Allowed components", retry_prompt)
        self.assertIn("component_ref", retry_prompt)
        self.assertIn("amazon-dynamodb -> Amazon DynamoDB", retry_prompt)
        self.assertIn("aws-lambda -> AWS Lambda", retry_prompt)


def _selection_no_s3():
    """Upstream selection without S3, so S3 mentions stay out-of-vocabulary."""
    sel = _selection()
    kept = [
        comp for comp in sel.selected_components
        if comp.name != "Amazon Simple Storage Service"
    ]
    return sel.model_copy(update={"selected_components": kept})


def _shape_no_s3() -> dict:
    """Observed shape with the S3 node/edge references removed."""
    shape = _observed_shape()
    shape["nodes"] = [
        node for node in shape["nodes"]
        if node.get("name") != "Amazon Simple Storage Service"
    ]
    shape["edges"] = [
        {
            "description": "Persist board state.",
            "from": "AWS Lambda",
            "kind": "write",
            "protocol": "",
            "to": "Amazon DynamoDB",
        }
        if edge.get("to") == "Amazon Simple Storage Service"
        else edge
        for edge in shape["edges"]
    ]
    shape["data_stores"] = [
        name for name in shape["data_stores"]
        if name != "Amazon Simple Storage Service"
    ]
    return shape


class ComposerFailurePairRegressionTests(unittest.TestCase):
    """Slice A R1-R4: replay the smoke-test failure pair without weakening
    either validator. No LLM, no network; all shapes are hand-built."""

    def test_R1_numeric_throughput_assumption_rejected(self):
        # Attempt 1 from the smoke test: assumption[0] carried a numeric
        # throughput claim. Qualitative "throughput" stays legal; digits
        # attached to a throughput unit must fail with the capacity-linkage
        # error.
        shape = _observed_shape()
        shape["assumptions"] = [
            "Board sync sustains 5200 requests/s peak write load."
        ]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        message = str(ctx.exception).lower()
        self.assertIn("assumption[0]", message)
        self.assertIn("states throughput", message)
        self.assertIn("capacity linkage", message)

    def test_R2_closed_world_retry_mentions_rejected(self):
        # Retry from the smoke test: six provider-led service mentions
        # outside the selected AND rejected sets. S3 is excluded from
        # the selection here so the two S3 mentions stay out-of-vocabulary
        # exactly as in the failed run.
        shape = _shape_no_s3()
        shape["decisions"] = [
            {
                "decision": "Use API Gateway WebSocket as the entry point.",
                "rationale": "Managed bidirectional sync; supported by ev-1.",
                "alternatives_considered": [
                    "AWS App Runner instead of the gateway",
                    "a managed cache for session state",
                ],
            },
            {
                "decision": "Run sync handlers on Lambda.",
                "rationale": "Event-driven scale; supported by ev-1.",
                "alternatives_considered": [
                    "AWS Fargate for long-lived handlers",
                    "Amazon EC2 with auto scaling",
                ],
            },
            {
                "decision": "Persist board state in DynamoDB.",
                "rationale": "Key-value access; supported by ev-2.",
                "alternatives_considered": [
                    "Amazon Aurora Serverless for relational queries",
                    "Amazon S3 static mapping for snapshots",
                ],
            },
            {
                "decision": "Host public assets via Amazon S3 website hosting.",
                "rationale": "Static delivery; supported by ev-4.",
                "alternatives_considered": ["CloudFront caching in front"],
            },
        ]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        sel = _selection_no_s3()
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=sel,
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        message = str(ctx.exception).lower()
        self.assertEqual(message.count("outside the selected"), 6)
        for fragment in (
            "app runner", "fargate", "ec2", "aurora",
            "s3 static", "s3 website",
        ):
            self.assertIn(fragment, message)

    def test_R3_double_failure_carries_structured_diagnostics(self):
        # Both rejected payloads plus both validator error strings must
        # survive on the raised error as plain-JSON diagnostics, while the
        # message itself keeps the existing First/Retry wording.
        bad_numeric = ArchitectureLLMOutput.model_validate(
            {**_shape_no_s3(),
             "assumptions": ["Board sync sustains 5200 requests/s."]}
        )
        retry_shape = _shape_no_s3()
        retry_shape["decisions"] = [
            {
                "decision": "Run sync handlers on Lambda.",
                "rationale": "Event-driven scale.",
                "alternatives_considered": [
                    "AWS Fargate for long-lived handlers"
                ],
            },
        ]
        bad_naming = ArchitectureLLMOutput.model_validate(retry_shape)

        class _SeqBad:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                parsed = bad_numeric if self.calls == 1 else bad_naming
                return {"raw": AIMessage(content=""),
                        "parsed": parsed, "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        # S3-free selection so the retry failure is the naming
        # violation, not a node-reference mismatch.
        sel = _selection_no_s3()
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=_SeqBad(),
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            with self.assertRaises(
                run_module.ArchitectureCompositionError
            ) as ctx:
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], sel,
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                )
        exc = ctx.exception
        # Still a ValueError for every existing caller.
        self.assertIsInstance(exc, ValueError)
        self.assertIn("first failure", str(exc).lower())
        self.assertIn("retry failure", str(exc).lower())
        diagnostics = exc.diagnostics
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(
            [entry["attempt"] for entry in diagnostics], [1, 2]
        )
        for entry in diagnostics:
            self.assertFalse(entry["truncated"])
            self.assertTrue(entry["errors"])
            # Plain JSON: must round-trip through json.dumps.
            json.dumps(entry)
        self.assertIn("states throughput", diagnostics[0]["errors"][0].lower())
        self.assertIn(
            "outside the selected", diagnostics[1]["errors"][0].lower()
        )
        self.assertIn(
            "5200 requests/s",
            json.dumps(diagnostics[0]["rejected_output"]),
        )
        self.assertIn(
            "fargate",
            json.dumps(diagnostics[1]["rejected_output"]).lower(),
        )
        # Size bounds hold on realistic outputs.
        for entry in diagnostics:
            self.assertLessEqual(
                len(json.dumps(entry["rejected_output"])),
                run_module.MAX_DIAGNOSTIC_OUTPUT_CHARS,
            )
            self.assertLessEqual(
                len(entry["errors"][0]),
                run_module.MAX_DIAGNOSTIC_ERROR_CHARS,
            )

    def test_R4_public_snapshot_ignores_diagnostics_channel(self):
        # The new internal channel must not leak into the public session
        # view: snapshots built with and without it are identical.
        from ai_architect.infrastructure.checkpoint.design import (
            _snapshot_from_channels,
        )
        base_channels = {
            "session_id": "design-s9",
            "user_request": "whiteboard",
            "requirements_state": None,
            "capacity_result": None,
            "component_selection": None,
            "cost_result": None,
            "architecture": None,
            "status": "failed",
            "error": "Architecture composer stage failed: bad",
            "warnings": [],
        }
        without = _snapshot_from_channels("design-s9", dict(base_channels))
        with_diagnostics = _snapshot_from_channels(
            "design-s9",
            {**base_channels,
             "composer_diagnostics": [
                 {"attempt": 1, "rejected_output": {"nodes": []},
                  "errors": ["assumption[0] states throughput"],
                  "truncated": False},
                 {"attempt": 2, "rejected_output": {"nodes": []},
                  "errors": ["decision[0] names aws fargate"],
                  "truncated": False},
             ]},
        )
        self.assertEqual(with_diagnostics, without)
        from ai_architect.api.design_state import design_stages
        self.assertEqual(
            design_stages(with_diagnostics).model_dump(mode="json"),
            design_stages(without).model_dump(mode="json"),
        )


if __name__ == "__main__":
    unittest.main()
