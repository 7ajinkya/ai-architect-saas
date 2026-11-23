"""Assisted suggestions for the pending human decisions of a plan.

The panel asks the backend for a starting point instead of making the
operator type every required input. These tests pin the boundary that
makes that safe:

1. Values the platform owns (the packaged demo artifact's runtime,
   handler and key schema) come from the platform, never from the
   model, and the model is never asked about them;
2. A value only the operator can decide (a globally unique S3 bucket
   name) is never proposed and is reported as skipped with a reason;
3. A model proposal outside the module contract's closed allowlist is
   dropped, never replaced, and the input stays for the operator;
4. A proposal for an input the model was not asked about is ignored;
5. One bounded correction round can repair a refused proposal;
6. The prompt carries the user's own request and the accepted values,
   and never asks for a bucket name;
7. A scalar spelling (1024, true) is normalized to the plan's string
   form instead of aborting the round, and a malformed or unreachable
   model degrades to the platform-owned values with a note;
8. The model proposes sensible defaults even when the request does not
   mention an input: a detailed request is context, not a prerequisite.

No LLM, AWS or network access: the model is a scripted fake.

Run: python -m unittest discover -s tests
"""
import unittest

from langchain_core.messages import AIMessage

from ai_architect.services.provisioning.input_suggestions import (
    SOURCE_AI_SUGGESTION,
    SOURCE_PLATFORM_DEFAULT,
    PendingInput,
    suggest_pending_inputs,
)


def _pending(
    resource_id: str, kind: str, name: str, component: str = "Component"
) -> PendingInput:
    return PendingInput(
        resource_id=resource_id,
        kind=kind,
        component_name=component,
        name=name,
        reason=f"{name} is required and is not derivable.",
    )


def _full_pending() -> list[PendingInput]:
    return [
        _pending("lambda-aws-lambda", "lambda", "runtime", "AWS Lambda"),
        _pending("lambda-aws-lambda", "lambda", "handler", "AWS Lambda"),
        _pending("lambda-aws-lambda", "lambda", "memory_mb", "AWS Lambda"),
        _pending("lambda-aws-lambda", "lambda", "timeout_seconds", "AWS Lambda"),
        _pending(
            "dynamodb-amazon-dynamodb", "dynamodb", "billing_mode", "Amazon DynamoDB"
        ),
        _pending(
            "dynamodb-amazon-dynamodb", "dynamodb", "point_in_time_recovery",
            "Amazon DynamoDB",
        ),
        _pending(
            "dynamodb-amazon-dynamodb", "dynamodb", "attribute_definitions",
            "Amazon DynamoDB",
        ),
        _pending(
            "dynamodb-amazon-dynamodb", "dynamodb", "key_schema", "Amazon DynamoDB"
        ),
        _pending("s3-amazon-s3", "s3", "bucket_name", "Amazon S3"),
        _pending("s3-amazon-s3", "s3", "versioning", "Amazon S3"),
        _pending("s3-amazon-s3", "s3", "encryption", "Amazon S3"),
        _pending("s3-amazon-s3", "s3", "public_access_block", "Amazon S3"),
        _pending(
            "cloudwatch_alarm-amazon-cloudwatch-alarms", "cloudwatch_alarm",
            "api_5xx_error_threshold", "Amazon CloudWatch Alarms",
        ),
    ]


class _ScriptedModel:
    """Replays one scripted payload per invoke; records the messages."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0
        self.seen_messages = []

    def invoke(self, messages):
        self.calls += 1
        self.seen_messages.append(messages)
        payload = self._payloads[min(self.calls - 1, len(self._payloads) - 1)]
        if isinstance(payload, str):
            return AIMessage(content=payload)
        return payload


class _RawModel:
    def __init__(self, content: str = ""):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content=self._content)


def _suggest(payloads, pending=None, context=None, **kwargs):
    model = _ScriptedModel(payloads)
    result = suggest_pending_inputs(
        pending=pending if pending is not None else _full_pending(),
        context=context or {
            "user_request": "a multi-tenant clinic platform under $450/month",
            "requirements": {"budget_constraints": "$450 per month"},
            "capacity": {"peak_read_qps": 12.0},
            "components": [{"component": "Amazon S3", "role": "object storage"}],
        },
        llm=model,
        raw_llm=_RawModel(),
        **kwargs,
    )
    return result, model


def _by_name(result, name: str):
    for item in result.suggestions:
        if item.name == name:
            return item
    return None


def _skipped(result, name: str):
    for item in result.skipped:
        if item.name == name:
            return item
    return None


class PlatformOwnedValueTests(unittest.TestCase):
    def test_artifact_facts_come_from_the_platform(self):
        result, model = _suggest([{"proposals": []}])
        runtime = _by_name(result, "runtime")
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.source, SOURCE_PLATFORM_DEFAULT)
        self.assertEqual(runtime.value, "python3.12")
        handler = _by_name(result, "handler")
        self.assertEqual(handler.value, "handler.handler")
        key_schema = _by_name(result, "key_schema")
        self.assertEqual(key_schema.value, "HASH:pk")
        attributes = _by_name(result, "attribute_definitions")
        self.assertEqual(attributes.value, "pk:S")

    def test_the_model_is_never_asked_about_artifact_facts(self):
        result, model = _suggest([{"proposals": []}])
        payload = model.seen_messages[0][1].content
        for name in ("runtime", "handler", "attribute_definitions", "key_schema"):
            self.assertNotIn(f'"name": "{name}"', payload)

    def test_artifact_facts_are_skipped_when_the_artifact_is_missing(self):
        result, model = _suggest(
            [{"proposals": []}], artifact_available=False
        )
        self.assertIsNone(_by_name(result, "runtime"))
        self.assertIn("packaged demo artifact", _skipped(result, "runtime").reason)


class HumanOnlyValueTests(unittest.TestCase):
    def test_bucket_name_is_never_proposed(self):
        result, model = _suggest([{"proposals": []}])
        self.assertIsNone(_by_name(result, "bucket_name"))
        skipped = _skipped(result, "bucket_name")
        self.assertIsNotNone(skipped)
        self.assertIn("globally unique", skipped.reason)

    def test_the_model_is_never_asked_for_a_bucket_name(self):
        result, model = _suggest([{"proposals": []}])
        payload = model.seen_messages[0][1].content
        self.assertNotIn('"name": "bucket_name"', payload)


class ProposalValidationTests(unittest.TestCase):
    def test_a_valid_proposal_is_returned_with_its_provenance(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "s3-amazon-s3",
                "name": "versioning",
                "value": "enabled",
                "rationale": "The request asks for recoverable uploads.",
            }],
        }])
        suggestion = _by_name(result, "versioning")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.value, "enabled")
        self.assertEqual(suggestion.source, SOURCE_AI_SUGGESTION)
        self.assertIn("recoverable", suggestion.rationale)

    def test_a_value_outside_the_allowlist_is_dropped(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "s3-amazon-s3",
                "name": "versioning",
                "value": "sometimes",
                "rationale": "r",
            }],
        }])
        self.assertIsNone(_by_name(result, "versioning"))
        skipped = _skipped(result, "versioning")
        self.assertIsNotNone(skipped)
        self.assertIn("allowlist", skipped.reason)

    def test_an_integer_outside_the_contract_range_is_dropped(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "lambda-aws-lambda",
                "name": "memory_mb",
                "value": "999999999",
                "rationale": "r",
            }],
        }])
        self.assertIsNone(_by_name(result, "memory_mb"))
        self.assertIsNotNone(_skipped(result, "memory_mb"))

    def test_a_proposal_for_an_unasked_input_is_ignored(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "s3-amazon-s3",
                "name": "encryption",
                "value": "aes256",
                "rationale": "r",
            }, {
                "resource_id": "s3-amazon-s3",
                "name": "not_a_real_input",
                "value": "whatever",
                "rationale": "r",
            }],
        }])
        self.assertEqual(_by_name(result, "encryption").value, "aes256")
        self.assertIsNone(_skipped(result, "not_a_real_input"))

    def test_one_correction_round_repairs_a_refused_proposal(self):
        result, model = _suggest([
            {
                "proposals": [{
                    "resource_id": "s3-amazon-s3",
                    "name": "versioning",
                    "value": "sometimes",
                    "rationale": "r",
                }],
            },
            {
                "proposals": [{
                    "resource_id": "s3-amazon-s3",
                    "name": "versioning",
                    "value": "enabled",
                    "rationale": "r",
                }],
            },
        ])
        self.assertEqual(model.calls, 2)
        self.assertEqual(_by_name(result, "versioning").value, "enabled")
        # The correction round states why the first value was refused.
        second = model.seen_messages[1][-1].content
        self.assertIn("REJECTED", second)
        self.assertIn("versioning", second)

    def test_a_refused_proposal_never_becomes_a_suggestion(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "s3-amazon-s3",
                "name": "encryption",
                "value": "rot13",
                "rationale": "r",
            }],
        }], max_attempts=1)
        self.assertIsNone(_by_name(result, "encryption"))
        self.assertEqual(model.calls, 1)


class _FailingModel:
    def invoke(self, messages):
        raise RuntimeError("provider unreachable")


class ScalarNormalizationTests(unittest.TestCase):
    def test_an_integer_value_is_normalized_to_the_plan_string(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "lambda-aws-lambda",
                "name": "memory_mb",
                "value": 1024,
                "rationale": "A modest memory size.",
            }],
        }])
        self.assertEqual(_by_name(result, "memory_mb").value, "1024")
        self.assertEqual(model.calls, 1)

    def test_a_boolean_value_is_normalized_to_the_contract_spelling(self):
        pending = _full_pending() + [
            _pending(
                "cognito_user_pool-amazon-cognito", "cognito_user_pool",
                "self_sign_up_enabled", "Amazon Cognito",
            ),
        ]
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "dynamodb-amazon-dynamodb",
                "name": "point_in_time_recovery",
                "value": True,
                "rationale": "Protect the table.",
            }, {
                "resource_id": "cognito_user_pool-amazon-cognito",
                "name": "self_sign_up_enabled",
                "value": False,
                "rationale": "Clinics create accounts.",
            }],
        }], pending=pending)
        self.assertEqual(_by_name(result, "point_in_time_recovery").value, "true")
        self.assertEqual(_by_name(result, "self_sign_up_enabled").value, "false")

    def test_a_float_whole_number_is_normalized_without_a_decimal(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "lambda-aws-lambda",
                "name": "timeout_seconds",
                "value": 30.0,
                "rationale": "Half a minute.",
            }],
        }])
        self.assertEqual(_by_name(result, "timeout_seconds").value, "30")


class ResilienceTests(unittest.TestCase):
    def test_a_malformed_answer_is_retried_then_salvaged(self):
        result, model = _suggest([
            {"proposals": "not a list"},
            {
                "proposals": [{
                    "resource_id": "s3-amazon-s3",
                    "name": "encryption",
                    "value": "aes256",
                    "rationale": "Encrypt at rest.",
                }],
            },
        ])
        self.assertEqual(model.calls, 2)
        self.assertEqual(_by_name(result, "encryption").value, "aes256")
        self.assertIn("could not be used", model.seen_messages[1][-1].content)

    def test_a_malformed_answer_never_raises(self):
        result, model = _suggest([
            {"proposals": "not a list"},
        ], max_attempts=1)
        self.assertEqual(model.calls, 1)
        self.assertIsNone(_by_name(result, "encryption"))
        self.assertTrue(any("unavailable" in note for note in result.notes))

    def test_one_bad_item_does_not_discard_the_rest(self):
        result, model = _suggest([{
            "proposals": [{
                "resource_id": "s3-amazon-s3",
                "name": "encryption",
                "value": "aes256",
                "rationale": "Encrypt at rest.",
            }, {
                "resource_id": "s3-amazon-s3",
                "name": "versioning",
            }],
        }])
        self.assertEqual(model.calls, 1)
        self.assertEqual(_by_name(result, "encryption").value, "aes256")

    def test_a_model_outage_degrades_to_the_platform_defaults(self):
        result = suggest_pending_inputs(
            pending=_full_pending(),
            context={"user_request": ""},
            llm=_FailingModel(),
            raw_llm=_FailingModel(),
        )
        self.assertEqual(_by_name(result, "runtime").value, "python3.12")
        self.assertEqual(_by_name(result, "key_schema").value, "HASH:pk")
        self.assertIsNone(_by_name(result, "memory_mb"))
        self.assertIsNotNone(_skipped(result, "memory_mb"))
        self.assertTrue(any("unavailable" in note for note in result.notes))

    def test_an_empty_answer_is_not_treated_as_a_failure(self):
        result, model = _suggest([{"proposals": []}])
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.notes, ())
        self.assertIsNotNone(_skipped(result, "memory_mb"))


class RequestIsContextTests(unittest.TestCase):
    def test_values_are_proposed_without_a_detailed_request(self):
        result, model = _suggest(
            [{
                "proposals": [{
                    "resource_id": "lambda-aws-lambda",
                    "name": "memory_mb",
                    "value": "512",
                    "rationale": "A safe default for a small API.",
                }],
            }],
            context={"user_request": ""},
        )
        self.assertEqual(_by_name(result, "memory_mb").value, "512")
        payload = model.seen_messages[0][1].content
        self.assertIn('"user_request": ""', payload)
        self.assertIn('"name": "memory_mb"', payload)

    def test_the_system_prompt_does_not_require_a_detailed_request(self):
        result, model = _suggest([{"proposals": []}])
        system = model.seen_messages[0][0].content
        self.assertIn("not a prerequisite", system)
        self.assertIn("Never leave an input unanswered", system)


class PromptContentTests(unittest.TestCase):
    def test_the_prompt_carries_the_request_and_the_accepted_values(self):
        result, model = _suggest([{"proposals": []}])
        payload = model.seen_messages[0][1].content
        self.assertIn("multi-tenant clinic platform", payload)
        self.assertIn("$450 per month", payload)
        self.assertIn("enabled", payload)  # versioning allowlist
        self.assertIn("PAY_PER_REQUEST", payload)  # billing mode allowlist
        self.assertIn("Amazon S3", payload)

    def test_the_system_prompt_forbids_invented_facts(self):
        result, model = _suggest([{"proposals": []}])
        system = model.seen_messages[0][0].content
        for phrase in ("Never propose names", "prices", "secrets"):
            self.assertIn(phrase, system)


if __name__ == "__main__":
    unittest.main()
