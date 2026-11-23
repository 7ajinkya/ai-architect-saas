"""Slice 4C tests: contract-driven typed parameter decoding.

Focus: the decoding boundary must be driven by the declared contract type,
must never reinterpret a string as HCL, and must fail closed on anything
that is not unambiguously representable.

Run: poetry run python -m unittest tests.test_provisioning_module_parameters
"""
import unittest

from ai_architect.domain.provisioning.models import (
    ParameterSource,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
)
from ai_architect.domain.provisioning.module import (
    BindingOrigin,
    DerivationKind,
    ModuleContract,
    ModuleErrorCode,
    ModuleInputDefinition,
    ModuleOutputReference,
    ParameterBindingSpec,
    ParameterConstraints,
    ParameterType,
    TypedArtifactRef,
    TypedAttributeList,
    TypedBool,
    TypedEnum,
    TypedInt,
    TypedKeySchema,
    TypedString,
    TypedStringList,
    VerificationMetadata,
    VerificationMethod,
    VerificationStatus,
)
from ai_architect.services.provisioning.module_catalog import CATALOG_VERSION
from ai_architect.services.provisioning.module_parameters import (
    bind_resource_inputs,
    derive,
    unmapped_parameters,
)
from datetime import date


def _resource(kind, resource_id, values, pending=()):
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
        pending_inputs=pending,
    )


def _definition(name, kind, *, required=True, constraints=None,
                parameter_name=None, resource_id=False, derivation=None,
                default=None):
    return ModuleInputDefinition(
        name=name,
        type=kind,
        required=required,
        binding=ParameterBindingSpec(
            parameter_name=parameter_name,
            resource_id=resource_id,
            derivation=derivation,
        ),
        constraints=constraints or ParameterConstraints(),
        default=default,
        description="test input",
    )


def _contract(inputs):
    return ModuleContract(
        module_id="object_storage",
        provider="aws",
        source="../../infra/modules/aws-object-storage",
        source_digest="a" * 64,
        version="1.0.0",
        description="test",
        inputs=tuple(inputs),
        outputs=(ModuleOutputReference(
            name="bucket_id", type=ParameterType.STRING, description="id"
        ),),
        verification=VerificationMetadata(
            status=VerificationStatus.VERIFIED,
            verified_by="tester",
            verified_on=date(2026, 1, 1),
            methods=frozenset({VerificationMethod.TESTED_CONTRACT}),
            catalog_revision=CATALOG_VERSION,
        ),
        contract_version="1.0.0",
    )


def _bind(definition, kind, values, pending=()):
    """Bind a single-input contract and return (bindings, diagnostics)."""
    contract = _contract([definition])
    resource = _resource(kind, "r-1", values, pending)
    return bind_resource_inputs(resource, contract)


class ScalarDecodingTests(unittest.TestCase):
    """21. Each declared type decodes with exactly one interpretation."""

    def test_string_decodes_and_is_length_bounded(self):
        bindings, diagnostics = _bind(
            _definition(
                "name", ParameterType.STRING,
                constraints=ParameterConstraints(
                    pattern=r"^[a-z0-9-]{3,20}$"
                ),
                parameter_name="name",
            ),
            ResourceKind.S3, {"name": "my-bucket"},
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedString)
        self.assertEqual(bindings[0].value.value, "my-bucket")

    def test_string_rejects_pattern_violation(self):
        _, diagnostics = _bind(
            _definition(
                "name", ParameterType.STRING,
                constraints=ParameterConstraints(pattern=r"^[a-z]+$"),
                parameter_name="name",
            ),
            ResourceKind.S3, {"name": "NOT-LOWER"},
        )
        self.assertEqual(
            {d.code for d in diagnostics},
            {ModuleErrorCode.MODULE_INPUT_TYPE_INVALID},
        )

    def test_string_respects_max_length(self):
        _, diagnostics = _bind(
            _definition(
                "name", ParameterType.STRING,
                constraints=ParameterConstraints(max_length=4),
                parameter_name="name",
            ),
            ResourceKind.S3, {"name": "far-too-long"},
        )
        self.assertEqual(len(diagnostics), 1)

    def test_enum_decodes_only_allowlisted_values(self):
        definition = _definition(
            "mode", ParameterType.ENUM,
            constraints=ParameterConstraints(
                allowed_values=frozenset({"a", "b"})
            ),
            parameter_name="mode",
        )
        bindings, diagnostics = _bind(
            definition, ResourceKind.S3, {"mode": "a"}
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedEnum)

        _, rejected = _bind(definition, ResourceKind.S3, {"mode": "c"})
        self.assertEqual(
            {d.code for d in rejected},
            {ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST},
        )

    def test_enum_is_case_sensitive(self):
        """No fuzzy case folding: 'Enabled' is not 'enabled'."""
        definition = _definition(
            "mode", ParameterType.ENUM,
            constraints=ParameterConstraints(allowed_values=frozenset({"enabled"})),
            parameter_name="mode",
        )
        _, diagnostics = _bind(
            definition, ResourceKind.S3, {"mode": "Enabled"}
        )
        self.assertEqual(len(diagnostics), 1)

    def test_bool_accepts_only_fixed_literals(self):
        definition = _definition(
            "flag", ParameterType.BOOL, parameter_name="flag"
        )
        for raw, expected in (
            ("true", True), ("TRUE", True), ("enabled", True),
            ("false", False), ("Disabled", False),
        ):
            with self.subTest(raw=raw):
                bindings, diagnostics = _bind(
                    definition, ResourceKind.S3, {"flag": raw}
                )
                self.assertEqual(diagnostics, ())
                self.assertIsInstance(bindings[0].value, TypedBool)
                self.assertEqual(bindings[0].value.value, expected)

    def test_bool_rejects_truthiness_coercion(self):
        """'1', 'yes', 'on' are refused, not coerced."""
        definition = _definition(
            "flag", ParameterType.BOOL, parameter_name="flag"
        )
        for raw in ("1", "yes", "on", "0", "no", "off", ""):
            with self.subTest(raw=raw):
                _, diagnostics = _bind(
                    definition, ResourceKind.S3, {"flag": raw}
                )
                self.assertEqual(len(diagnostics), 1, msg=raw)

    def test_int_requires_plain_digits_and_respects_bounds(self):
        definition = _definition(
            "size", ParameterType.INT,
            constraints=ParameterConstraints(minimum=128, maximum=10240),
            parameter_name="size",
        )
        bindings, diagnostics = _bind(
            definition, ResourceKind.S3, {"size": "512"}
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedInt)
        self.assertEqual(bindings[0].value.value, 512)

        for raw in ("0", "99999", "-5", "512.0", "0x200", "5e2", " "):
            with self.subTest(raw=raw):
                _, bad = _bind(definition, ResourceKind.S3, {"size": raw})
                self.assertEqual(len(bad), 1, msg=raw)

    def test_string_list_splits_and_validates_each_item(self):
        definition = _definition(
            "items", ParameterType.STRING_LIST,
            constraints=ParameterConstraints(pattern=r"^[a-z]+$"),
            parameter_name="items",
        )
        bindings, diagnostics = _bind(
            definition, ResourceKind.S3, {"items": "one,two"}
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedStringList)
        self.assertEqual(bindings[0].value.value, ("one", "two"))

    def test_string_list_rejects_empty(self):
        definition = _definition(
            "items", ParameterType.STRING_LIST, parameter_name="items"
        )
        _, diagnostics = _bind(definition, ResourceKind.S3, {"items": ",,,"})
        self.assertEqual(len(diagnostics), 1)

    def test_artifact_ref_is_typed_and_never_inline(self):
        definition = _definition(
            "code", ParameterType.ARTIFACT_REF,
            constraints=ParameterConstraints(
                pattern=r"^[A-Za-z][A-Za-z0-9._/-]{0,255}$"
            ),
            parameter_name="code",
        )
        bindings, diagnostics = _bind(
            definition, ResourceKind.LAMBDA, {"code": "build/app-abc123.zip"}
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedArtifactRef)


class InjectionResistanceTests(unittest.TestCase):
    """22, 24. No HCL, JSON or YAML can be smuggled through a parameter."""

    PAYLOADS = (
        '${var.secret}',
        'pk" evil',
        "pk\n  provisioner \"local-exec\" {",
        "<<EOF\nboom\nEOF",
        "{ resource = 1 }",
        "`whoami`",
        "a\\nb",
        "x$y",
        "{}[]",
    )

    def test_string_rejects_every_payload(self):
        definition = _definition(
            "name", ParameterType.STRING, parameter_name="name"
        )
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                _, diagnostics = _bind(
                    definition, ResourceKind.S3, {"name": payload}
                )
                self.assertEqual(len(diagnostics), 1, msg=payload)

    def test_artifact_ref_rejects_every_payload(self):
        definition = _definition(
            "code", ParameterType.ARTIFACT_REF, parameter_name="code"
        )
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                _, diagnostics = _bind(
                    definition, ResourceKind.LAMBDA, {"code": payload}
                )
                self.assertEqual(len(diagnostics), 1, msg=payload)

    def test_structured_tokens_reject_every_payload(self):
        definition = _definition(
            "attributes", ParameterType.ATTRIBUTE_LIST,
            parameter_name="attributes",
        )
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                _, diagnostics = _bind(
                    definition, ResourceKind.DYNAMODB, {"attributes": payload}
                )
                self.assertEqual(len(diagnostics), 1, msg=payload)

    def test_json_and_yaml_are_not_parsed(self):
        definition = _definition(
            "attributes", ParameterType.ATTRIBUTE_LIST,
            parameter_name="attributes",
        )
        for payload in ('{"pk": "S"}', "pk: S\n", "- pk: S\n", "pk: S # x"):
            with self.subTest(payload=payload):
                _, diagnostics = _bind(
                    definition, ResourceKind.DYNAMODB, {"attributes": payload}
                )
                self.assertEqual(len(diagnostics), 1, msg=payload)

    def test_no_default_value_is_invented_for_a_missing_optional(self):
        definition = _definition(
            "maybe", ParameterType.STRING, required=False,
            parameter_name="maybe",
        )
        bindings, diagnostics = _bind(
            definition, ResourceKind.S3, {"other": "x"}
        )
        self.assertEqual(diagnostics, ())
        self.assertEqual(bindings, ())

    def test_missing_required_parameter_is_diagnosed(self):
        definition = _definition(
            "needed", ParameterType.STRING, parameter_name="needed"
        )
        _, diagnostics = _bind(definition, ResourceKind.S3, {})
        self.assertEqual(
            {d.code for d in diagnostics},
            {ModuleErrorCode.MODULE_INPUT_REQUIRED},
        )


class StructuredProjectionTests(unittest.TestCase):
    """23. Frozen 4A structured parameters decode via a fixed grammar."""

    def _ddb_definition(self, name, derivation):
        return _definition(name, ParameterType.STRING, derivation=derivation)

    def _ddb_resource(self, attributes="pk:S,sk:N", schema="HASH:pk,RANGE:sk"):
        return _resource(
            ResourceKind.DYNAMODB, "dynamodb-table",
            {
                "attribute_definitions": attributes,
                "key_schema": schema,
            },
        )

    def test_attribute_list_decodes_and_sorts(self):
        definition = _definition(
            "attributes", ParameterType.ATTRIBUTE_LIST,
            parameter_name="attribute_definitions",
        )
        resource = self._ddb_resource(attributes="sk:N,pk:S")
        bindings, diagnostics = bind_resource_inputs(
            resource, _contract([definition])
        )
        self.assertEqual(diagnostics, ())
        self.assertIsInstance(bindings[0].value, TypedAttributeList)
        self.assertEqual(
            [a.name for a in bindings[0].value.value], ["pk", "sk"]
        )

    def test_attribute_list_rejects_duplicate_names(self):
        definition = _definition(
            "attributes", ParameterType.ATTRIBUTE_LIST,
            parameter_name="attribute_definitions",
        )
        _, diagnostics = bind_resource_inputs(
            self._ddb_resource(attributes="pk:S,pk:N"), _contract([definition])
        )
        self.assertEqual(
            {d.code for d in diagnostics},
            {ModuleErrorCode.MODULE_INPUT_INCONSISTENT},
        )

    def test_attribute_list_rejects_bad_types(self):
        definition = _definition(
            "attributes", ParameterType.ATTRIBUTE_LIST,
            parameter_name="attribute_definitions",
        )
        for attributes in ("pk:X", "pk", "pk:", ":S", "1pk:S"):
            with self.subTest(attributes=attributes):
                _, diagnostics = bind_resource_inputs(
                    self._ddb_resource(attributes=attributes),
                    _contract([definition]),
                )
                self.assertEqual(len(diagnostics), 1, msg=attributes)

    def test_key_schema_decodes(self):
        definition = _definition(
            "key_schema", ParameterType.KEY_SCHEMA,
            parameter_name="key_schema",
        )
        _, diagnostics = bind_resource_inputs(
            self._ddb_resource(), _contract([definition])
        )
        self.assertEqual(diagnostics, ())

    def test_key_schema_requires_a_hash_key(self):
        definition = _definition(
            "key_schema", ParameterType.KEY_SCHEMA,
            parameter_name="key_schema",
        )
        _, diagnostics = bind_resource_inputs(
            self._ddb_resource(schema="RANGE:sk"), _contract([definition])
        )
        self.assertEqual(len(diagnostics), 1)

    def test_key_schema_rejects_duplicate_role(self):
        definition = _definition(
            "key_schema", ParameterType.KEY_SCHEMA,
            parameter_name="key_schema",
        )
        _, diagnostics = bind_resource_inputs(
            self._ddb_resource(schema="HASH:pk,HASH:sk"),
            _contract([definition]),
        )
        self.assertEqual(
            {d.code for d in diagnostics},
            {ModuleErrorCode.MODULE_INPUT_INCONSISTENT},
        )

    def test_derived_key_name_and_type(self):
        contract = _contract([
            _definition("hash_key_name", ParameterType.STRING,
                        derivation=DerivationKind.HASH_KEY_NAME),
            _definition("hash_key_type", ParameterType.ENUM,
                        derivation=DerivationKind.HASH_KEY_TYPE),
        ])
        bindings, diagnostics = bind_resource_inputs(
            self._ddb_resource(), contract
        )
        self.assertEqual(diagnostics, ())
        values = {b.input_name: b.value.value for b in bindings}
        self.assertEqual(values, {"hash_key_name": "pk", "hash_key_type": "S"})

    def test_derived_type_requires_declared_attribute(self):
        contract = _contract([
            _definition("hash_key_type", ParameterType.ENUM,
                        derivation=DerivationKind.HASH_KEY_TYPE),
        ])
        _, diagnostics = bind_resource_inputs(
            self._ddb_resource(schema="HASH:ghost"), contract
        )
        self.assertEqual(
            {d.code for d in diagnostics},
            {ModuleErrorCode.MODULE_INPUT_INCONSISTENT},
        )

    def test_optional_range_key_unbound_when_absent(self):
        contract = _contract([
            _definition("range_key_name", ParameterType.STRING,
                        required=False,
                        derivation=DerivationKind.RANGE_KEY_NAME),
        ])
        bindings, diagnostics = bind_resource_inputs(
            self._ddb_resource(schema="HASH:pk"), contract
        )
        self.assertEqual(diagnostics, ())
        self.assertEqual(bindings, ())

    def test_resource_id_derivation_is_validated(self):
        contract = _contract([
            _definition("table_name", ParameterType.STRING, resource_id=True),
        ])
        bindings, diagnostics = bind_resource_inputs(
            self._ddb_resource(), contract
        )
        self.assertEqual(diagnostics, ())
        self.assertEqual(bindings[0].value.value, "dynamodb-table")
        self.assertEqual(bindings[0].origin, BindingOrigin.DERIVED)

    def test_missing_structural_parameter_is_diagnosed(self):
        resource = _resource(
            ResourceKind.DYNAMODB, "dynamodb-table", {"billing_mode": "x"}
        )
        contract = _contract([
            _definition("hash_key_name", ParameterType.STRING,
                        derivation=DerivationKind.HASH_KEY_NAME),
        ])
        _, diagnostics = bind_resource_inputs(resource, contract)
        self.assertEqual(len(diagnostics), 1)


class UnmappedParameterTests(unittest.TestCase):
    """20. A parameter the contract ignores is reported, never dropped."""

    def test_unmapped_parameter_is_reported(self):
        contract = _contract([
            _definition("mode", ParameterType.STRING, parameter_name="mode"),
        ])
        resource = _resource(
            ResourceKind.S3, "s3-bucket", {"mode": "x", "leftover": "y"}
        )
        self.assertEqual(unmapped_parameters(resource, contract), ("leftover",))

    def test_structural_parameters_count_as_consumed(self):
        contract = _contract([
            _definition("hash_key_name", ParameterType.STRING,
                        derivation=DerivationKind.HASH_KEY_NAME),
        ])
        resource = self._resource = _resource(
            ResourceKind.DYNAMODB, "dynamodb-table",
            {"attribute_definitions": "pk:S", "key_schema": "HASH:pk"},
        )
        self.assertEqual(unmapped_parameters(resource, contract), ())

    def test_bound_parameters_are_not_reported(self):
        contract = _contract([
            _definition("mode", ParameterType.STRING, parameter_name="mode"),
        ])
        resource = _resource(ResourceKind.S3, "s3-bucket", {"mode": "x"})
        self.assertEqual(unmapped_parameters(resource, contract), ())


class OriginTests(unittest.TestCase):
    """Provenance of a binding is explicit."""

    def test_parameter_backed_input_records_its_parameter(self):
        definition = _definition(
            "mode", ParameterType.STRING, parameter_name="mode"
        )
        bindings, _ = _bind(definition, ResourceKind.S3, {"mode": "x"})
        self.assertEqual(bindings[0].from_parameter, "mode")
        self.assertEqual(bindings[0].origin, BindingOrigin.REQUIRED_INPUT)

    def test_derived_input_records_no_parameter(self):
        definition = _definition(
            "name", ParameterType.STRING, resource_id=True
        )
        bindings, _ = _bind(definition, ResourceKind.S3, {})
        self.assertIsNone(bindings[0].from_parameter)
        self.assertEqual(bindings[0].origin, BindingOrigin.DERIVED)


if __name__ == "__main__":
    unittest.main()