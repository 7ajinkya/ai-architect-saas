"""Slice 1 tests: Composer closed-world via deterministic component refs.

Covers the four required smoke cases:
1. a valid architecture (by component_ref) produces a validated artifact;
2. an unknown component reference fails closed (never substituted);
3. an unknown service in decision/alternative prose is still rejected;
4. a valid reference and a valid short-form alias prose reference pass.

Plus: the reference vocabulary is deterministic and shared by the
prompt and the normalizer, and the bounded correction retry carries it.

Run: poetry run python -m unittest discover tests
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.architecture import run as run_module
from ai_architect.agents.architecture.run import (
    ArchitectureLLMOutput,
    LLMArchitectureNode,
    build_component_references,
)
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
        application_description="appointment booking platform",
        cloud_provider="aws",
        region="Mumbai region",
        availability="99.9 percent",
        latency="p99 under 300ms",
        target_users_dau="50000",
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
        rationale=f"{role} for the workload",
        evidence_refs=[ev] if ev else [],
        confidence="high",
        evidence_support="strong" if ev else "insufficient",
    )


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            _comp("Amazon API Gateway", "networking", "ingress", "ev-1"),
            _comp("AWS Lambda", "compute", "business logic", None),
            _comp("Amazon DynamoDB", "database", "appointment store", "ev-2"),
            _comp("Amazon Simple Queue Service", "messaging",
                  "booking events", None),
        ],
        rejected_candidates=[
            _comp("Amazon SNS", "messaging", "notifications", "ev-3"),
        ],
        unresolved_tradeoffs=[
            UnresolvedTradeoff(
                description="read consistency",
                options=["eventual", "strong"],
                impact="booking visibility",
            ),
        ],
        evidence_references=["ev-1", "ev-2", "ev-3"],
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
        _line("Amazon Simple Queue Service", "Requests", 1e7, 0.0000004, 4.0),
    ]
    return CostEstimationResult(
        lines=lines,
        totals_usd_per_month={
            "on_demand": sum(line.monthly_cost_usd for line in lines)
        },
        warnings=[],
        unpriced=[
            UnpricedComponent(
                component_name="AWS Lambda",
                reason="Cannot price: function duration and memory are "
                "not determined.",
            ),
        ],
        generated_at=GEN,
        pricing_as_of=PRICE_TS,
    )


def _upstream():
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": _selection(),
        "cost": _cost(),
    }


def _ref_shape():
    """LLM output referencing every component by its deterministic ref."""
    return {
        "nodes": [
            {"component_ref": "amazon-api-gateway",
             "role_in_system": "ingress for booking requests",
             "capacity_keys": ["peak_read_qps"]},
            {"component_ref": "aws-lambda",
             "role_in_system": "run booking business logic",
             "capacity_keys": ["peak_write_qps"]},
            {"component_ref": "amazon-dynamodb",
             "role_in_system": "persist appointments",
             "capacity_keys": ["storage_growth_bytes_per_day"]},
            {"component_ref": "amazon-simple-queue-service",
             "role_in_system": "buffer booking events",
             "capacity_keys": ["peak_write_qps"]},
        ],
        "edges": [
            {"from": "client", "to": "amazon-api-gateway",
             "kind": "request", "protocol": "HTTPS",
             "description": "booking API calls",
             "load_key": "peak_read_qps"},
            {"from": "amazon-api-gateway", "to": "aws-lambda",
             "kind": "request", "description": "invoke handler",
             "load_key": "peak_write_qps"},
            {"from": "aws-lambda", "to": "amazon-dynamodb",
             "kind": "write", "description": "store appointment"},
            {"from": "aws-lambda", "to": "amazon-simple-queue-service",
             "kind": "async", "description": "enqueue booking event",
             "load_key": "peak_write_qps"},
        ],
        "entry_points": ["amazon-api-gateway"],
        "data_stores": ["amazon-dynamodb"],
        "boundaries": ["region:ap-south-1"],
        "decisions": [],
        "assumptions": [],
        "warnings": [],
        "new_tradeoffs": [],
    }


def _normalize(shape, selection=None):
    up = _upstream()
    parsed = ArchitectureLLMOutput.model_validate(shape)
    return run_module.normalize_architecture(
        parsed,
        extraction=up["extraction"],
        capacity=up["capacity"],
        selection=selection if selection is not None else up["selection"],
        cost=up["cost"],
        region_code=REGION,
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
    )


def _check_valid(up, result, selection=None):
    return validate_architecture(
        result,
        selection=selection if selection is not None else up["selection"],
        capacity=up["capacity"],
        cost=up["cost"],
        extraction=up["extraction"],
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
        region_code=REGION,
    )


class ComponentReferenceTests(unittest.TestCase):
    def test_01_valid_architecture_by_reference(self):
        up = _upstream()
        result = _normalize(_ref_shape())
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(_check_valid(up, result), [])
        # node_id is the deterministic reference itself.
        self.assertEqual(
            sorted(node.node_id for node in result.nodes),
            [
                "amazon-api-gateway",
                "amazon-dynamodb",
                "amazon-simple-queue-service",
                "aws-lambda",
            ],
        )
        # Edges resolved through references.
        by_id = {edge.edge_id: edge for edge in result.edges}
        self.assertEqual(by_id["e1"].from_node, "client")
        self.assertEqual(by_id["e1"].to_node, "amazon-api-gateway")
        self.assertEqual(by_id["e2"].to_node, "aws-lambda")
        # Entry points / data stores resolved too.
        self.assertEqual(result.entry_points, ["amazon-api-gateway"])
        self.assertEqual(result.data_store_node_ids, ["amazon-dynamodb"])
        # Capacity came from the deterministic result.
        api = next(
            n for n in result.nodes if n.node_id == "amazon-api-gateway"
        )
        self.assertEqual(api.capacity[0].value, up["capacity"].peak_read_qps)

    def test_02_unknown_reference_fails_closed(self):
        for token in (
            "aws-qldb", "amazon-fargate", "aws-sns",
            "some-service-not-selected",
        ):
            with self.subTest(token=token):
                shape = _ref_shape()
                shape["nodes"][1]["component_ref"] = token
                with self.assertRaises(ValueError) as ctx:
                    _normalize(shape)
                message = str(ctx.exception)
                self.assertIn("unknown component", message)
                self.assertIn(token, message)
                # Allowed vocabulary supplied, never substituted.
                self.assertIn("Allowed references", message)
                self.assertIn("aws-lambda -> AWS Lambda", message)
                # Nothing was silently mapped onto a selected component.
                self.assertNotIn(
                    "references unknown component 'AWS Lambda'",
                    message,
                )

    def test_03_unknown_edge_endpoint_reference_fails_closed(self):
        shape = _ref_shape()
        shape["edges"][1]["to"] = "aws-rds-postgres"
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception)
        self.assertIn("unknown component", message)
        self.assertIn("aws-rds-postgres", message)

    def test_04_entry_point_unknown_reference_fails_closed(self):
        shape = _ref_shape()
        shape["entry_points"] = ["aws-msk"]
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        self.assertIn("unknown component", str(ctx.exception))

    def test_05_unknown_service_in_prose_still_rejected(self):
        # Node/edge references are fine; an unknown component_ref must
        # fail closed at normalize, while a smuggled note must still
        # fail at the validator boundary.
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events",
            "rationale": "queue decouples writes from persistence",
            "alternatives_considered": [
                {"component_ref": "Amazon Kinesis",
                 "note": "instead of the queue"},
            ],
        }]
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception)
        self.assertIn("unknown component", message)
        self.assertIn("kinesis", message.lower())

    def test_05b_smuggled_note_still_rejected_by_validator(self):
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events",
            "rationale": "queue decouples writes from persistence",
            "alternatives_considered": [
                {"component_ref": "",
                 "note": "Amazon Kinesis instead of the queue"},
            ],
        }]
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception)
        self.assertIn("outside the selected and rejected", message)
        self.assertIn("kinesis", message.lower())

    def test_06_valid_alias_prose_reference_allowed(self):
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events with SQS",
            "rationale": "managed queue decouples writes",
            "alternatives_considered": [
                {"component_ref": "Amazon SNS", "note": "rejected"},
            ],
        }]
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(_check_valid(up, result), [])

    def test_06b_rendered_alternative_carries_exact_name(self):
        # Deterministic rendering keeps the exact known name first so
        # the validator scrub and human readers see vocabulary text.
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events with SQS",
            "rationale": "managed queue decouples writes",
            "alternatives_considered": [
                {"component_ref": "amazon-simple-queue-service",
                 "note": "rejected: fan-out not required"},
            ],
        }]
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["Amazon Simple Queue Service: rejected: fan-out not required"],
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06c_empty_note_rejected(self):
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events with SQS",
            "rationale": "managed queue decouples writes",
            "alternatives_considered": [
                {"component_ref": "Amazon SNS", "note": "   "},
            ],
        }]
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        self.assertIn("empty note", str(ctx.exception).lower())

    def test_06d_generic_note_without_provider_words_accepted(self):
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events with SQS",
            "rationale": "managed queue decouples writes",
            "alternatives_considered": [
                {"component_ref": "",
                 "note": "a managed cache could cut read load"},
            ],
        }]
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["a managed cache could cut read load"],
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06e_historical_pair_rejected_as_refs(self):
        # Exact smoke-test pair, ref form: normalize fails fast naming
        # the first unknown component.
        for fragment, ref in (
            ("s3 static", "Amazon S3 static website"),
            ("global accelerator", "AWS Global Accelerator"),
        ):
            with self.subTest(fragment=fragment):
                shape = _ref_shape()
                shape["decisions"] = [{
                    "decision": "buffer booking events",
                    "rationale": "queue decouples writes",
                    "alternatives_considered": [
                        {"component_ref": ref, "note": "considered"},
                    ],
                }]
                with self.assertRaises(ValueError) as ctx:
                    _normalize(shape)
                message = str(ctx.exception).lower()
                self.assertIn("unknown component", message)
                self.assertIn(fragment, message)

    def test_06f_no_component_selection_leakage(self):
        # Alternatives can neither add to selected nor rejected sets.
        up = _upstream()
        before_selected = [c.name for c in up["selection"].selected_components]
        before_rejected = [c.name for c in up["selection"].rejected_candidates]
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": "buffer booking events with SQS",
            "rationale": "managed queue decouples writes",
            "alternatives_considered": [
                {"component_ref": "Amazon SNS", "note": "rejected"},
                {"component_ref": "", "note": "a managed cache alternative"},
            ],
        }]
        result = _normalize(shape)
        self.assertEqual(_check_valid(up, result), [])
        self.assertEqual(
            [c.name for c in up["selection"].selected_components],
            before_selected,
        )
        self.assertEqual(
            [c.name for c in up["selection"].rejected_candidates],
            before_rejected,
        )
        with self.assertRaises(ValueError):
            bad = _ref_shape()
            bad["decisions"] = [{
                "decision": "buffer booking events",
                "rationale": "queue decouples writes",
                "alternatives_considered": [
                    {"component_ref": "Amazon Pinpoint", "note": "push"},
                ],
            }]
            _normalize(bad, selection=up["selection"])
        self.assertEqual(
            [c.name for c in up["selection"].rejected_candidates],
            before_rejected,
        )

    def _decision_shape(self, about, decision_text="persist appointments"):
        shape = _ref_shape()
        shape["decisions"] = [{
            "decision": decision_text,
            "rationale": "key-value access fits the workload",
            "about_component_ref": about,
            "alternatives_considered": [],
        }]
        return shape

    def test_06g_subject_exact_selected_name_accepted(self):
        shape = self._decision_shape("Amazon DynamoDB")
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].decision,
            "[About: Amazon DynamoDB] persist appointments",
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06h_subject_slug_accepted(self):
        shape = self._decision_shape("amazon-dynamodb")
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].decision,
            "[About: Amazon DynamoDB] persist appointments",
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06i_subject_rejected_name_accepted(self):
        shape = self._decision_shape(
            "Amazon SNS", decision_text="skip push fan-out")
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].decision,
            "[About: Amazon SNS] skip push fan-out",
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06j_subject_unknown_ref_rejected(self):
        shape = self._decision_shape("Amazon RDS")
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception).lower()
        self.assertIn("unknown component", message)
        self.assertIn("amazon rds", message)

    def test_06k_empty_subject_cross_cutting_accepted(self):
        shape = self._decision_shape("")
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.decisions[0].decision, "persist appointments")
        self.assertEqual(_check_valid(up, result), [])

    def test_06l_decision_prose_naming_still_rejected(self):
        # Identity belongs in about_component_ref: a provider service
        # named in free decision text still fails at the validator.
        shape = self._decision_shape(
            "", decision_text="use amazon s3 static website for assets")
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception).lower()
        self.assertIn("outside the selected", message)
        self.assertIn("s3 static", message)

    def test_06m_nbsp_hyphen_component_name_resolved(self):
        # Component Selection returns "Amazon DynamoDB\u2011(on-demand)"
        # (U+2011 NBSP hyphen); the LLM node references "Amazon DynamoDB
        # (on-demand)" (U+002D hyphen). Both normalize to the same key
        # and resolve to the canonical approved name.
        selection = ComponentSelectionResult(
            selected_components=[
                ComponentCandidate(
                    name="Amazon DynamoDB\u2011(on-demand)", provider="aws",
                    category="database", role="board state store",
                    rationale="key-value store",
                    evidence_refs=["ev-1"], confidence="high",
                    evidence_support="strong", provider_specific=True,
                    alternatives=[], region_constraints=[]
                )
            ],
            rejected_candidates=[],
            unresolved_tradeoffs=[],
            evidence_references=["ev-1"],
            assumptions=[], warnings=[],
        )
        shape = {
            "nodes": [
                {"component_name": "Amazon DynamoDB (on-demand)",
                 "role_in_system": "persist board state",
                 "capacity_keys": ["storage_growth_bytes_per_day"]}
            ],
            "edges": [
                {"from": "client", "to": "Amazon DynamoDB (on-demand)",
                 "kind": "request", "protocol": "HTTPS",
                 "description": "client writes board state",
                 "load_key": "storage_growth_bytes_per_day"}
            ],
            "entry_points": ["Amazon DynamoDB (on-demand)"],
            "data_stores": ["Amazon DynamoDB (on-demand)"],
            "boundaries": ["region:ap-south-1"],
            "decisions": [{
                "decision": "use dynamodb for storage",
                "rationale": "key-value access fits",
                "about_component_ref": "Amazon DynamoDB (on-demand)",
                "alternatives_considered": [],
                "evidence_refs": [],
            }],
            "assumptions": [], "warnings": [], "new_tradeoffs": [],
        }
        up = _upstream()
        result = _normalize(shape, selection=selection)
        self.assertEqual(
            result.nodes[0].component_name,
            "Amazon DynamoDB\u2011(on-demand)",
        )
        self.assertIn(
            f"[About: Amazon DynamoDB\u2011(on-demand)]",
            result.decisions[0].decision,
        )
        self.assertEqual(_check_valid(up, result, selection), [])

    def test_06n_whitespace_variant_component_name_resolved(self):
        # Component Selection output may contain "Amazon Route\u201153"
        # (NBSP hyphen) while the strict domain schema uses
        # "Amazon Route 53" (regular hyphen). The _dict_to_str coercion
        # in _dict_to_str should normalize both to the same key so that
        # dict-based lookup works.
        from ai_architect.agents.component_selection.models import (
            _dict_to_str,
        )
        self.assertEqual(
            _dict_to_str({"message": "Amazon Route\u201153"}),
            _dict_to_str({"message": "Amazon Route 53"}),
        )
        self.assertEqual(
            _dict_to_str({"message": "Amazon Route\u201153"}),
            "amazon route 53",
        )

    def test_06o_genuinely_unknown_component_rejected(self):
        # An unrecognized component name in decision prose still fails.
        shape = self._decision_shape(
            "", decision_text="use amazon redshift for analytics"
        )
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception).lower()
        self.assertIn("amazon redshift", message)

    def test_06p_close_but_different_name_rejected(self):
        # "Amazon Dynamo DB" (extra space) differs from the canonical
        # "Amazon DynamoDB" and must not match.
        shape = self._decision_shape(
            "", decision_text="use amazon dynamo db for storage"
        )
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception).lower()
        self.assertIn("amazon dynamo db", message)

    def test_06q_all_unicode_hyphen_variants_normalize(self):
        # All Unicode hyphen variants normalize to the same canonical name.
        from ai_architect.services.architecture.validator import (
            _normalize_component_name,
        )
        name_variants = [
            "Amazon Route\u201053",  # hyphen
            "Amazon Route\u201153",  # non-breaking hyphen
            "Amazon Route\u201253",  # figure dash
            "Amazon Route\u201353",  # en dash
            "Amazon Route\u201453",  # em dash
            "Amazon Route\u221253",  # minus sign
        ]
        normalized = {
            _normalize_component_name(n) for n in name_variants
        }
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized.pop(), "amazon route 53")

    def test_06r_dynamodb_on_demand_nbsp_node_reference(self):
        # Exact reported failure: Component Selection returns
        # "Amazon DynamoDB\u2011(on-demand)" (U+2011 NBSP hyphen); the
        # LLM node references "Amazon DynamoDB (on-demand)" (U+002D
        # hyphen). Both normalize to the same key and resolve to the
        # canonical approved name.
        selection = ComponentSelectionResult(
            selected_components=[
                ComponentCandidate(
                    name="Amazon DynamoDB\u2011(on-demand)", provider="aws",
                    category="database", role="board state store",
                    rationale="key-value store",
                    evidence_refs=["ev-1"], confidence="high",
                    evidence_support="strong", provider_specific=True,
                    alternatives=[], region_constraints=[]
                )
            ],
            rejected_candidates=[],
            unresolved_tradeoffs=[],
            evidence_references=["ev-1"],
            assumptions=[], warnings=[],
        )
        shape = {
            "nodes": [
                {"component_name": "Amazon DynamoDB (on-demand)",
                 "role_in_system": "persist board state",
                 "capacity_keys": ["storage_growth_bytes_per_day"]}
            ],
            "edges": [
                {"from": "client", "to": "Amazon DynamoDB (on-demand)",
                 "kind": "request", "protocol": "HTTPS",
                 "description": "client writes board state",
                 "load_key": "storage_growth_bytes_per_day"}
            ],
            "entry_points": ["Amazon DynamoDB (on-demand)"],
            "data_stores": ["Amazon DynamoDB (on-demand)"],
            "boundaries": ["region:ap-south-1"],
            "decisions": [],
            "assumptions": [], "warnings": [], "new_tradeoffs": [],
        }
        up = _upstream()
        result = _normalize(shape, selection=selection)
        self.assertEqual(
            result.nodes[0].component_name,
            "Amazon DynamoDB\u2011(on-demand)",
        )
        self.assertEqual(_check_valid(up, result, selection), [])

    def test_06s_leading_trailing_whitespace_node_reference(self):
        # Leading/trailing and Unicode whitespace in a node reference is
        # folded before matching; the canonical approved name is kept.
        shape = _ref_shape()
        shape["nodes"][0]["component_ref"] = "  amazon-api-gateway  "
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(
            result.nodes[0].component_name, "Amazon API Gateway"
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_06t_ambiguous_normalized_match_rejected(self):
        # Two approved components whose names normalize to the same key
        # must be rejected as ambiguous, never resolved arbitrarily.
        selection = ComponentSelectionResult(
            selected_components=[
                ComponentCandidate(
                    name="Amazon DynamoDB\u2011On-Demand", provider="aws",
                    category="database", role="key-value store",
                    rationale="on-demand table",
                    evidence_refs=["ev-1"], confidence="high",
                    evidence_support="strong",
                    alternatives=[], region_constraints=[]
                ),
                ComponentCandidate(
                    name="Amazon DynamoDB On-Demand", provider="aws",
                    category="database", role="key-value store",
                    rationale="on-demand table",
                    evidence_refs=["ev-2"], confidence="high",
                    evidence_support="strong",
                    alternatives=[], region_constraints=[]
                ),
            ],
            rejected_candidates=[],
            unresolved_tradeoffs=[],
            evidence_references=["ev-1", "ev-2"],
            assumptions=[], warnings=[],
        )
        shape = {
            "nodes": [
                {"component_name": "Amazon DynamoDB On-Demand",
                 "role_in_system": "persist data",
                 "capacity_keys": ["storage_growth_bytes_per_day"]}
            ],
            "edges": [
                {"from": "client", "to": "Amazon DynamoDB On-Demand",
                 "kind": "request", "protocol": "HTTPS",
                 "description": "client writes data"}
            ],
            "entry_points": ["Amazon DynamoDB On-Demand"],
            "data_stores": ["Amazon DynamoDB On-Demand"],
            "boundaries": ["region:ap-south-1"],
            "decisions": [],
            "assumptions": [], "warnings": [], "new_tradeoffs": [],
        }
        up = _upstream()
        # Use a token that is not an exact match but normalizes to the
        # same ambiguous key as both approved components.
        shape["nodes"][0]["component_name"] = "amazon dynamodb on-demand"
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape, selection=selection)
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_07_exact_name_still_accepted(self):
        shape = _ref_shape()
        for node in shape["nodes"]:
            node["component_name"] = node.pop("component_ref")
        shape["edges"][1]["to"] = "AWS Lambda"
        up = _upstream()
        result = _normalize(shape)
        self.assertEqual(_check_valid(up, result), [])

    def test_08_duplicate_component_reference_rejected(self):
        shape = _ref_shape()
        shape["nodes"][2]["component_ref"] = "aws-lambda"
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        self.assertIn("more than once", str(ctx.exception))

    def test_09_reference_map_is_deterministic_and_shared(self):
        selection = _selection()
        first = build_component_references(
            [c.name for c in selection.selected_components]
        )
        second = build_component_references(
            [c.name for c in selection.selected_components]
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first["AWS Lambda"], "aws-lambda"
        )
        # Order-independent stability of the slug function itself.
        self.assertEqual(
            build_component_references(["Amazon S3", "Amazon SNS"]),
            build_component_references(["Amazon S3", "Amazon SNS"]),
        )

    def test_10_node_id_independent_of_llm_output_order(self):
        up = _upstream()
        reordered = _ref_shape()
        reordered["nodes"] = list(reversed(reordered["nodes"]))
        result = _normalize(reordered)
        self.assertEqual(_check_valid(up, result), [])
        # Same ids regardless of the order the model emitted them.
        base = _normalize(_ref_shape())
        self.assertEqual(
            sorted(n.node_id for n in result.nodes),
            sorted(n.node_id for n in base.nodes),
        )


class ComposerRunClosedWorldTests(unittest.TestCase):
    def _run(self, shape, second_shape=None):
        outputs = [ArchitectureLLMOutput.model_validate(shape)]
        if second_shape is not None:
            outputs.append(
                ArchitectureLLMOutput.model_validate(second_shape)
            )

        class _SeqBound:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.messages = messages
                index = min(self.calls, len(outputs) - 1)
                self.calls += 1
                return {
                    "raw": AIMessage(content=""),
                    "parsed": outputs[index],
                    "parsing_error": None,
                }

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        agent = _SeqBound()
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
            return (
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], up["selection"],
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                ),
                agent,
                up,
            )

    def test_11_prompt_carries_allowed_reference_vocabulary(self):
        result, agent, up = self._run(_ref_shape())
        self.assertIsInstance(result, ArchitectureResult)
        prompt = agent.messages[1].content
        self.assertIn("Allowed components", prompt)
        self.assertIn("aws-lambda -> AWS Lambda", prompt)
        self.assertIn("amazon-dynamodb -> Amazon DynamoDB", prompt)
        self.assertEqual(_check_valid(up, result), [])

    def test_12_correction_retry_recovers_from_bad_reference(self):
        bad = _ref_shape()
        bad["nodes"][1]["component_ref"] = "aws-qldb"
        result, agent, up = self._run(bad, _ref_shape())
        self.assertEqual(agent.calls, 2)
        retry_prompt = agent.messages[-1].content
        self.assertIn("REJECTED", retry_prompt)
        self.assertIn("Allowed components", retry_prompt)
        self.assertIn("aws-lambda -> AWS Lambda", retry_prompt)
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(_check_valid(up, result), [])

    def test_13_unknown_reference_fails_after_bounded_retry(self):
        bad = _ref_shape()
        bad["nodes"][1]["component_ref"] = "aws-qldb"
        with self.assertRaises(ValueError) as ctx:
            self._run(bad, bad)
        message = str(ctx.exception)
        self.assertIn("correction", message.lower())
        self.assertIn("unknown component", message)
        self.assertIn("aws-qldb", message)

    def test_14_subject_and_alternative_failures_both_diagnosed(self):
        # Attempt 1 fails on a bad decision subject, retry on a bad
        # alternative ref: both attempts stay in composer_diagnostics.
        bad_subject = _ref_shape()
        bad_subject["decisions"] = [{
            "decision": "persist appointments",
            "rationale": "key-value access fits the workload",
            "about_component_ref": "Amazon RDS",
            "alternatives_considered": [],
        }]
        bad_alternative = _ref_shape()
        bad_alternative["decisions"] = [{
            "decision": "buffer booking events",
            "rationale": "queue decouples writes",
            "about_component_ref": "",
            "alternatives_considered": [
                {"component_ref": "Amazon Pinpoint", "note": "push"},
            ],
        }]
        with self.assertRaises(
            run_module.ArchitectureCompositionError
        ) as ctx:
            self._run(bad_subject, bad_alternative)
        diagnostics = ctx.exception.diagnostics
        self.assertEqual(
            [entry["attempt"] for entry in diagnostics], [1, 2])
        self.assertIn("amazon rds",
                      diagnostics[0]["errors"][0].lower())
        self.assertIn("amazon pinpoint",
                      diagnostics[1]["errors"][0].lower())

    def test_15_prompts_document_subject_contract(self):
        from ai_architect.agents.architecture.prompt import (
            ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
        )
        self.assertIn("about_component_ref",
                      ARCHITECTURE_COMPOSER_SYSTEM_PROMPT)
        bad = _ref_shape()
        bad["decisions"] = [{
            "decision": "persist appointments",
            "rationale": "key-value access fits the workload",
            "about_component_ref": "Amazon RDS",
            "alternatives_considered": [],
        }]
        result, agent, up = self._run(bad, _ref_shape())
        self.assertIsInstance(result, ArchitectureResult)
        retry_prompt = agent.messages[-1].content
        self.assertIn("REJECTED", retry_prompt)
        self.assertIn("about_component_ref", retry_prompt)

    def test_16_prompts_explicit_observability_and_messaging_wiring(self):
        from ai_architect.agents.architecture.prompt import (
            ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
        )
        self.assertIn(
            "HTTP API -> CloudWatch Logs",
            ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
        )
        self.assertIn(
            "CloudWatch Alarms ->",
            ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
        )
        self.assertIn(
            "SNS -> SQS",
            ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
        )


def _mq_selection() -> ComponentSelectionResult:
    """WhatsApp/CRUD-shaped selection: 3 selected, 3 rejected."""
    return ComponentSelectionResult(
        selected_components=[
            _comp("Amazon API Gateway", "networking", "ingress", "ev-1"),
            _comp("AWS Lambda", "compute", "business logic", None),
            _comp("Amazon DynamoDB", "database", "store", "ev-2"),
        ],
        rejected_candidates=[
            _comp("Amazon MQ (ActiveMQ)", "messaging", "broker", None),
            _comp("Amazon SQS", "messaging", "queue", None),
            _comp("AWS Elastic Load Balancer (ALB)", "networking",
                  "balancing", None),
        ],
        unresolved_tradeoffs=[],
        evidence_references=["ev-1", "ev-2"],
        generated_at=GEN,
    )


def _alt_shape(selection, alternatives, about=""):
    """Minimal valid LLM shape whose decisions carry `alternatives`."""
    names = [c.name for c in selection.selected_components]
    return {
        "nodes": [
            {"component_ref": n, "role_in_system": "r",
             "capacity_keys": ["peak_read_qps"]}
            for n in names
        ],
        "edges": [
            {"from": "client", "to": names[0],
             "kind": "request", "protocol": "HTTPS",
             "description": "ingress", "load_key": "peak_read_qps"},
            *[
                {"from": source, "to": target,
                 "kind": "request", "description": "downstream call"}
                for source, target in zip(names, names[1:])
            ],
        ],
        "entry_points": [names[0]],
        "data_stores": [],
        "boundaries": ["region:ap-south-1"],
        "decisions": [{
            "decision": "route traffic",
            "rationale": "managed ingress fits the workload",
            "about_component_ref": about,
            "alternatives_considered": alternatives,
        }],
        "assumptions": [],
        "warnings": [],
        "new_tradeoffs": [],
    }


def _normalize_sel(shape, selection):
    up = _upstream()
    parsed = ArchitectureLLMOutput.model_validate(shape)
    return run_module.normalize_architecture(
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


class RejectedReferenceResolutionTests(unittest.TestCase):
    """P0 Slice 2: rejected refs resolve in reference contexts only."""

    def test_01_rejected_exact_name_resolves_in_alternative(self):
        sel = _mq_selection()
        result = _normalize_sel(_alt_shape(sel, [
            {"component_ref": "Amazon MQ (ActiveMQ)",
             "note": "ordered delivery not needed"},
        ]), sel)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["Amazon MQ (ActiveMQ): ordered delivery not needed"],
        )
        self.assertEqual(_check_valid(_upstream(), result, sel), [])

    def test_02_rejected_slugs_resolve_in_alternatives(self):
        sel = _mq_selection()
        cases = [
            ("amazon-mq-activemq", "Amazon MQ (ActiveMQ)"),
            ("amazon-sqs", "Amazon SQS"),
            ("aws-elastic-load-balancer-alb",
             "AWS Elastic Load Balancer (ALB)"),
        ]
        notes = [
            "ordered delivery not needed",
            "pull-based consumption preferred",
            "single entry point suffices",
        ]
        for (slug, expected), note in zip(cases, notes):
            with self.subTest(slug=slug):
                result = _normalize_sel(
                    _alt_shape(sel, [{"component_ref": slug,
                                      "note": note}]), sel)
                self.assertEqual(
                    result.decisions[0].alternatives_considered,
                    [f"{expected}: {note}"],
                )
                self.assertEqual(
                    _check_valid(_upstream(), result, sel), [])

    def test_04_selected_refs_still_resolve(self):
        sel = _mq_selection()
        result = _normalize_sel(_alt_shape(sel, [
            {"component_ref": "amazon-dynamodb",
             "note": "key-value access fits"},
            {"component_ref": "Amazon API Gateway", "note": "ingress"},
        ]), sel)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["Amazon DynamoDB: key-value access fits",
             "Amazon API Gateway: ingress"],
        )

    def test_05_selected_wins_slug_collision(self):
        sel = ComponentSelectionResult(
            selected_components=[
                _comp("Amazon SQS", "messaging", "queue", None),
            ],
            rejected_candidates=[
                _comp("Amazon SQS!", "messaging", "other queue", None),
            ],
            unresolved_tradeoffs=[],
            evidence_references=[],
            generated_at=GEN,
        )
        # Colliding slug resolves to the selected component.
        result = _normalize_sel(_alt_shape(sel, [
            {"component_ref": "amazon-sqs", "note": "buffering"},
        ]), sel)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["Amazon SQS: buffering"],
        )
        # The rejected twin still resolves by its exact name.
        result = _normalize_sel(_alt_shape(sel, [
            {"component_ref": "Amazon SQS!", "note": "other buffering"},
        ]), sel)
        self.assertEqual(
            result.decisions[0].alternatives_considered,
            ["Amazon SQS!: other buffering"],
        )

    def test_06_unknown_refs_fail_closed_naming_token(self):
        sel = _mq_selection()
        for token in ("amazon-qldb", "Amazon QLDB", "aws-fargate"):
            with self.subTest(token=token):
                with self.assertRaises(ValueError) as ctx:
                    _normalize_sel(_alt_shape(sel, [
                        {"component_ref": token, "note": "considered"},
                    ]), sel)
                message = str(ctx.exception)
                self.assertIn("unknown component", message)
                self.assertIn(token, message)

    def test_07_rejected_component_cannot_become_a_node(self):
        sel = _mq_selection()
        for token in ("amazon-mq-activemq", "Amazon MQ (ActiveMQ)"):
            with self.subTest(token=token):
                shape = _alt_shape(sel, [])
                shape["nodes"].append(
                    {"component_ref": token, "role_in_system": "broker",
                     "capacity_keys": ["peak_read_qps"]})
                with self.assertRaises(ValueError) as ctx:
                    _normalize_sel(shape, sel)
                self.assertIn("unknown component", str(ctx.exception))

    def test_08_about_component_ref_rejected_slug(self):
        sel = _mq_selection()
        result = _normalize_sel(
            _alt_shape(sel, [], about="amazon-mq-activemq"), sel)
        self.assertTrue(
            result.decisions[0].decision.startswith(
                "[About: Amazon MQ (ActiveMQ)]"))
        self.assertEqual(
            _check_valid(_upstream(), result, sel), [])

    def test_10_double_failure_still_diagnosed(self):
        bad_subject = _alt_shape(_mq_selection(), [])
        bad_subject["decisions"][0]["about_component_ref"] = "Amazon RDS"
        bad_alternative = _alt_shape(_mq_selection(), [
            {"component_ref": "amazon-qldb", "note": "ledger"},
        ])

        class _SeqBad:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                shape = bad_subject if self.calls == 1 else bad_alternative
                return {"raw": AIMessage(content=""),
                        "parsed": ArchitectureLLMOutput.model_validate(
                            shape),
                        "parsing_error": None}

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        sel = _mq_selection()
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
        diagnostics = ctx.exception.diagnostics
        self.assertEqual(
            [entry["attempt"] for entry in diagnostics], [1, 2])
        self.assertIn("amazon rds",
                      diagnostics[0]["errors"][0].lower())
        self.assertIn("amazon-qldb",
                      diagnostics[1]["errors"][0].lower())


if __name__ == "__main__":
    unittest.main()
