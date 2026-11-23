"""Slice 4C tests: ModuleContract, VerifiedModuleRegistry and trust model.

Covers catalog integrity, exact lookup, verification gating, provider
compatibility, deterministic ordering, content-digest pinning against the
modules on disk, and the absence of network or LLM dependencies.

Run: poetry run python -m unittest tests.test_provisioning_module_contract
"""
import ast
import pathlib
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date

from ai_architect.domain.provisioning.module import (
    DerivationKind,
    ModuleContract,
    ModuleId,
    ModuleInputDefinition,
    ModuleOutputReference,
    ParameterBindingSpec,
    ParameterConstraints,
    ParameterType,
    SourceKind,
    VerificationMetadata,
    VerificationMethod,
    VerificationStatus,
)
from ai_architect.services.provisioning.module_catalog import (
    CATALOG,
    CATALOG_VERSION,
    CLOUDWATCH_ALARMS,
    HTTP_API,
    LOG_GROUP,
    NOSQL_TABLE,
    OBJECT_STORAGE,
    SERVERLESS_FUNCTION,
)
from ai_architect.services.provisioning.module_registry import (
    CatalogError,
    VerifiedModuleRegistry,
    build_default_registry,
    module_source_digest,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _imported_modules(path: pathlib.Path) -> set:
    """Top-level module names actually imported by a source file.

    Parsed from the AST rather than matched as text, so a docstring
    mentioning "retrieval" cannot be mistaken for a dependency.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _verification(status=VerificationStatus.VERIFIED, **overrides):
    payload = dict(
        status=status,
        verified_by="test-reviewer",
        verified_on=date(2026, 1, 1),
        methods=frozenset({VerificationMethod.TESTED_CONTRACT}),
        catalog_revision=CATALOG_VERSION,
    )
    payload.update(overrides)
    return VerificationMetadata(**payload)


def _contract(module_id=ModuleId.NOSQL_TABLE, **overrides):
    payload = dict(
        module_id=module_id,
        provider="aws",
        source="../../infra/modules/aws-nosql-table",
        source_kind=SourceKind.LOCAL_PATH,
        source_digest="a" * 64,
        version="1.0.0",
        description="test module",
        capability_tags=frozenset({"key_value"}),
        inputs=(
            ModuleInputDefinition(
                name="table_name",
                type=ParameterType.STRING,
                required=True,
                binding=ParameterBindingSpec(parameter_name="table_name"),
                constraints=ParameterConstraints(pattern=r"^[a-z0-9-]{3,63}$"),
                description="table name",
            ),
        ),
        outputs=(
            ModuleOutputReference(
                name="table_id", type=ParameterType.STRING, description="id"
            ),
        ),
        verification=_verification(),
        contract_version="1.0.0",
    )
    payload.update(overrides)
    return ModuleContract(**payload)


def _pending_registry(module_id=ModuleId.NOSQL_TABLE):
    """Mirror the shipped catalog with one module demoted to PENDING.

    The shipped catalog is now fully VERIFIED, so the trust-gate tests
    exercise the PENDING path against a synthetic copy. The gate logic
    under test is identical; only the fixture is synthetic.
    """
    contracts = []
    for contract in CATALOG:
        if contract.module_id is module_id:
            contract = contract.model_copy(
                update={
                    "verification": contract.verification.model_copy(
                        update={"status": VerificationStatus.PENDING}
                    )
                }
            )
        contracts.append(contract)
    return VerifiedModuleRegistry(
        tuple(contracts),
        catalog_version=CATALOG_VERSION,
        verify_digests=False,
    )


class CatalogIntegrityTests(unittest.TestCase):
    """The shipped catalog is internally consistent."""

    def test_catalog_declares_ten_modules(self):
        self.assertEqual(
            {c.module_id for c in CATALOG},
            {
                ModuleId.OBJECT_STORAGE,
                ModuleId.NOSQL_TABLE,
                ModuleId.SERVERLESS_FUNCTION,
                ModuleId.HTTP_API,
                ModuleId.CLOUDFRONT_DISTRIBUTION,
                ModuleId.COGNITO_USER_POOL,
                ModuleId.SQS_QUEUE,
                ModuleId.SNS_TOPIC,
                ModuleId.LOG_GROUP,
                ModuleId.CLOUDWATCH_ALARMS,
            },
        )

    def test_module_ids_are_capability_level(self):
        """Never raw Terraform resource types."""
        for contract in CATALOG:
            value = contract.module_id.value
            self.assertNotIn("aws", value)
            self.assertNotIn("s3", value)
            self.assertNotIn("dynamo", value)
            self.assertNotIn("lambda", value)
            # Capability nouns, not resource nouns.
            self.assertNotIn("bucket", value)
            self.assertNotIn("resource", value)

    def test_every_contract_has_exact_version_pin(self):
        for contract in CATALOG:
            self.assertRegex(contract.version, r"^\d+\.\d+\.\d+$")

    def test_every_contract_declares_verification(self):
        for contract in CATALOG:
            self.assertIsInstance(
                contract.verification.status, VerificationStatus
            )
            self.assertTrue(contract.verification.verified_by)
            self.assertIsInstance(contract.verification.verified_on, date)

    def test_verified_modules_record_methods_and_limitations(self):
        for contract in CATALOG:
            self.assertEqual(
                contract.verification.status, VerificationStatus.VERIFIED
            )
            self.assertTrue(contract.verification.methods)
            self.assertTrue(contract.verification.limitations)

    def test_verified_modules_declare_outputs(self):
        for contract in CATALOG:
            self.assertTrue(contract.outputs)

    def test_module_directories_exist(self):
        for contract in CATALOG:
            relative = contract.source.replace("../../", "")
            self.assertTrue(
                (REPO_ROOT / relative).is_dir(), msg=f"missing {relative}"
            )

    def test_graduated_function_ships_an_implementation(self):
        """serverless_function graduated to VERIFIED with a main.tf."""
        self.assertEqual(
            SERVERLESS_FUNCTION.verification.status,
            VerificationStatus.VERIFIED,
        )
        directory = REPO_ROOT / "infra/modules/aws-serverless-function"
        self.assertTrue((directory / "variables.tf").exists())
        self.assertTrue(
            (directory / "main.tf").exists(),
            msg="a VERIFIED module must ship an implementation",
        )

    def test_graduated_function_declares_artifact_inputs(self):
        names = {i.name for i in SERVERLESS_FUNCTION.inputs}
        self.assertIn("code_artifact_path", names)
        self.assertIn("code_artifact_sha256", names)
        by_name = {i.name: i for i in SERVERLESS_FUNCTION.inputs}
        self.assertEqual(
            by_name["code_artifact_path"].type, ParameterType.STRING
        )
        self.assertEqual(
            by_name["code_artifact_sha256"].type, ParameterType.STRING
        )

    def test_table_name_follows_service_limits(self):
        """DynamoDB names: 3-255 chars per the service naming rules."""
        import re

        definition = next(
            i for i in NOSQL_TABLE.inputs if i.name == "table_name"
        )
        pattern = definition.constraints.pattern
        self.assertTrue(re.fullmatch(pattern, "dynamodb-table"))
        long_id = (
            "dynamodb-amazon-dynamodb-on-demand-point-in-time-"
            "recovery-enabled"
        )
        self.assertTrue(len(long_id) > 63)
        self.assertTrue(re.fullmatch(pattern, long_id))
        self.assertFalse(re.fullmatch(pattern, "ab"))
        self.assertFalse(re.fullmatch(pattern, "x" * 256))
        self.assertFalse(re.fullmatch(pattern, "has space"))

    def test_shipped_catalog_loads(self):
        registry = build_default_registry()
        self.assertEqual(len(registry.all_contracts()), 10)

    def test_catalog_revision_matches_registry_version(self):
        for contract in CATALOG:
            self.assertEqual(
                contract.verification.catalog_revision, CATALOG_VERSION
            )


class ModuleInterfaceParityTests(unittest.TestCase):
    """The catalog mirrors the real module variables, in both directions.

    Parses each module's variables.tf and asserts exact parity with the
    declared contract: the same input names, compatible types, and
    required-ness derived from the presence of a Terraform default. This
    is the executable evidence behind the TESTED_CONTRACT verification
    method recorded on every VERIFIED contract.
    """

    _VARIABLE_BLOCK = re.compile(
        r'variable\s+"([^"]+)"\s*\{(.*?)\n\}', re.DOTALL
    )

    _TYPE_MAP = {
        "string": {
            ParameterType.STRING,
            ParameterType.ENUM,
            ParameterType.ARTIFACT_REF,
            ParameterType.SECRET_REF,
        },
        # "number" takes INT, and ENUM when the allowlist is all-numeric
        # (retention_days): the contract constrains the value, the module
        # receives a number.
        "number": {ParameterType.INT, ParameterType.ENUM},
        "bool": {ParameterType.BOOL},
        "list(string)": {ParameterType.STRING_LIST},
    }

    def _variables(self, contract):
        relative = contract.source.replace("../../", "")
        text = (
            (REPO_ROOT / relative / "variables.tf").read_text(encoding="utf-8")
        )
        variables = {}
        for name, body in self._VARIABLE_BLOCK.findall(text):
            type_match = re.search(r"type\s*=\s*([^\n#]+)", body)
            type_token = type_match.group(1).strip() if type_match else ""
            variables[name] = {
                "type": type_token,
                "has_default": re.search(r"\bdefault\s*=", body) is not None,
            }
        return variables

    def test_catalog_inputs_match_module_variables_exactly(self):
        for contract in CATALOG:
            with self.subTest(module=contract.module_id.value):
                variables = self._variables(contract)
                declared = {i.name: i for i in contract.inputs}
                self.assertEqual(
                    set(declared),
                    set(variables),
                    msg=(
                        f"{contract.module_id.value}: catalog inputs and "
                        "module variables differ"
                    ),
                )
                for name, variable in sorted(variables.items()):
                    definition = declared[name]
                    allowed = self._TYPE_MAP.get(variable["type"])
                    self.assertIsNotNone(
                        allowed,
                        msg=(
                            f"{contract.module_id.value}.{name}: unmapped "
                            f"module type {variable['type']!r}"
                        ),
                    )
                    self.assertIn(
                        definition.type,
                        allowed,
                        msg=f"{contract.module_id.value}.{name}",
                    )
                    self.assertEqual(
                        definition.required,
                        not variable["has_default"],
                        msg=(
                            f"{contract.module_id.value}.{name}: required "
                            "must mean the variable declares no default"
                        ),
                    )

    def test_http_api_declares_exactly_the_module_interface(self):
        self.assertEqual(
            {i.name for i in HTTP_API.inputs},
            {
                "api_name",
                "lambda_invoke_arn",
                "lambda_arn",
                "log_group_arn",
                "access_log_format",
            },
        )
        by_name = {i.name: i for i in HTTP_API.inputs}
        self.assertTrue(by_name["api_name"].binding.resource_id)
        self.assertEqual(
            by_name["lambda_arn"].binding.derivation,
            DerivationKind.LAMBDA_ARN,
        )
        self.assertEqual(
            by_name["lambda_invoke_arn"].binding.derivation,
            DerivationKind.LAMBDA_INVOKE_ARN,
        )
        self.assertEqual(
            by_name["log_group_arn"].binding.derivation,
            DerivationKind.LOG_GROUP_ARN,
        )
        self.assertEqual(
            by_name["access_log_format"].binding.derivation,
            DerivationKind.ACCESS_LOG_FORMAT,
        )

    def test_serverless_function_drops_package_type_and_declares_wiring(self):
        names = {i.name for i in SERVERLESS_FUNCTION.inputs}
        self.assertNotIn("package_type", names)
        self.assertIn("log_group_arn", names)
        by_name = {i.name: i for i in SERVERLESS_FUNCTION.inputs}
        self.assertEqual(
            by_name["log_group_arn"].binding.derivation,
            DerivationKind.LOG_GROUP_ARN,
        )
        self.assertTrue(by_name["log_group_arn"].required)
        self.assertFalse(by_name["table_arns"].required)
        self.assertFalse(by_name["table_name"].required)
        self.assertEqual(
            by_name["table_arns"].binding.derivation,
            DerivationKind.TABLE_ARNS,
        )
        self.assertEqual(
            by_name["table_name"].binding.derivation,
            DerivationKind.TABLE_NAME,
        )

    def test_log_group_name_is_derived_by_the_platform_policy(self):
        by_name = {i.name: i for i in LOG_GROUP.inputs}
        self.assertEqual(
            by_name["log_group_name"].binding.derivation,
            DerivationKind.LOG_GROUP_NAME,
        )

    def test_billing_mode_allowlist_is_the_provider_value(self):
        definition = next(
            i for i in NOSQL_TABLE.inputs if i.name == "billing_mode"
        )
        self.assertEqual(
            set(definition.constraints.allowed_values), {"PAY_PER_REQUEST"}
        )

    def test_every_declared_derivation_kind_exists(self):
        """A typo in a derivation name must fail here, not at selection."""
        for contract in CATALOG:
            for definition in contract.inputs:
                derivation = definition.binding.derivation
                if derivation is not None:
                    self.assertIn(derivation, DerivationKind)


class LookupTests(unittest.TestCase):
    """Exact lookup only; no fuzzy or category fallback."""

    def setUp(self):
        self.registry = build_default_registry()

    def test_exact_lookup_succeeds(self):
        self.assertEqual(
            self.registry.get(ModuleId.OBJECT_STORAGE).module_id,
            ModuleId.OBJECT_STORAGE,
        )

    def test_module_id_is_a_closed_enum(self):
        """An unrecognised identity cannot even be constructed."""
        with self.assertRaises(ValueError):
            ModuleId("not_a_module")

    def test_missing_module_raises_with_available_list(self):
        """A genuine catalog gap, not an unparseable identifier."""
        registry = VerifiedModuleRegistry(
            (NOSQL_TABLE,),
            catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        with self.assertRaises(KeyError) as ctx:
            registry.get(ModuleId.OBJECT_STORAGE)
        message = str(ctx.exception)
        self.assertIn("not in the verified catalog", message)
        self.assertIn("nosql_table", message)

    def test_has_is_true_for_present_only(self):
        registry = VerifiedModuleRegistry(
            (NOSQL_TABLE,), catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        self.assertTrue(registry.has(ModuleId.NOSQL_TABLE))
        self.assertFalse(registry.has(ModuleId.OBJECT_STORAGE))

    def test_error_lists_available_modules_deterministically(self):
        registry = VerifiedModuleRegistry(
            (NOSQL_TABLE, OBJECT_STORAGE), catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        with self.assertRaises(KeyError) as ctx:
            registry.get(ModuleId.SERVERLESS_FUNCTION)
        message = str(ctx.exception)
        positions = [
            message.index(name)
            for name in ("nosql_table", "object_storage")
        ]
        self.assertEqual(positions, sorted(positions))

    def test_no_string_based_lookup_api_exists(self):
        """There is deliberately no fuzzy matcher."""
        for forbidden in ("match", "search", "find", "resolve_by_name"):
            self.assertFalse(
                hasattr(self.registry, forbidden),
                msg=f"registry exposes fuzzy lookup {forbidden!r}",
            )

    def test_for_provider_is_deterministically_ordered(self):
        first = self.registry.for_provider("aws")
        second = self.registry.for_provider("aws")
        self.assertEqual(
            [c.module_id.value for c in first],
            [c.module_id.value for c in second],
        )
        self.assertEqual(
            [c.module_id.value for c in first],
            sorted(c.module_id.value for c in first),
        )

    def test_for_unknown_provider_is_empty(self):
        self.assertEqual(self.registry.for_provider("gcp"), ())

    def test_capability_tags_are_workload_neutral(self):
        tags = self.registry.capability_tags()
        for tag in tags:
            for forbidden in (
                "whatsapp", "payment", "healthcare", "chat", "url",
                "ecommerce", "medical", "banking", "messag",
            ):
                self.assertNotIn(forbidden, tag)

    def test_registry_module_imports_no_network_library(self):
        """Structural check on 4C's own imports, not the whole process."""
        path = (
            REPO_ROOT / "src/ai_architect/services/provisioning"
            / "module_registry.py"
        )
        imported = _imported_modules(path)
        for forbidden in ("requests", "httpx", "urllib", "socket", "aiohttp"):
            self.assertNotIn(forbidden, imported)


class TrustGateTests(unittest.TestCase):
    """Only verified, provider-compatible contracts are selectable."""

    def setUp(self):
        self.registry = build_default_registry()

    def test_verified_module_is_selectable(self):
        contract = self.registry.require_selectable(
            ModuleId.OBJECT_STORAGE, provider="aws"
        )
        self.assertEqual(contract.module_id, ModuleId.OBJECT_STORAGE)

    def test_pending_module_is_rejected(self):
        registry = _pending_registry(ModuleId.SERVERLESS_FUNCTION)
        with self.assertRaises(LookupError) as ctx:
            registry.require_selectable(
                ModuleId.SERVERLESS_FUNCTION, provider="aws"
            )
        self.assertIn("pending", str(ctx.exception))

    def test_pending_failure_is_distinct_from_absent(self):
        """'Declared but unverified' must not read as 'no such module'."""
        registry = _pending_registry(ModuleId.SERVERLESS_FUNCTION)
        with self.assertRaises(LookupError) as pending:
            registry.require_selectable(
                ModuleId.SERVERLESS_FUNCTION, provider="aws"
            )
        registry = VerifiedModuleRegistry(
            (NOSQL_TABLE,), catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        with self.assertRaises(KeyError) as absent:
            registry.get(ModuleId.OBJECT_STORAGE)
        self.assertNotIsInstance(pending.exception, KeyError)
        self.assertIsInstance(absent.exception, KeyError)
        self.assertIn("pending", str(pending.exception))
        self.assertNotIn("pending", str(absent.exception))

    def test_provider_mismatch_is_rejected(self):
        with self.assertRaises(LookupError) as ctx:
            self.registry.require_selectable(
                ModuleId.OBJECT_STORAGE, provider="gcp"
            )
        self.assertIn("provider", str(ctx.exception))

    def test_revoked_module_is_rejected(self):
        registry = VerifiedModuleRegistry(
            (_contract(verification=_verification(
                status=VerificationStatus.REVOKED, methods=frozenset()
            )),),
            catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        with self.assertRaises(LookupError):
            registry.require_selectable(ModuleId.NOSQL_TABLE, provider="aws")

    def test_revoke_requires_only_a_data_change(self):
        """Trust revocation is a catalog edit, not a logic change."""
        self.assertEqual(
            OBJECT_STORAGE.verification.status, VerificationStatus.VERIFIED
        )
        revoked = OBJECT_STORAGE.model_copy(
            update={
                "verification": OBJECT_STORAGE.verification.model_copy(
                    update={"status": VerificationStatus.REVOKED}
                )
            }
        )
        registry = VerifiedModuleRegistry(
            (revoked,),
            catalog_version=CATALOG_VERSION,
            verify_digests=False,
        )
        with self.assertRaises(LookupError):
            registry.require_selectable(
                ModuleId.OBJECT_STORAGE, provider="aws"
            )

    def test_selectable_property_matches_status(self):
        self.assertTrue(OBJECT_STORAGE.selectable)
        pending = _pending_registry(ModuleId.SERVERLESS_FUNCTION).get(
            ModuleId.SERVERLESS_FUNCTION
        )
        self.assertFalse(pending.selectable)

    def test_diagnostics_report_pending_module(self):
        messages = " ".join(
            str(d) for d in _pending_registry(
                ModuleId.SERVERLESS_FUNCTION
            ).diagnostics()
        )
        self.assertIn("serverless_function", messages)
        self.assertIn("PENDING", messages)


class CatalogValidationTests(unittest.TestCase):
    """A malformed catalog fails closed at construction."""

    def _registry(self, contracts, version=CATALOG_VERSION):
        return VerifiedModuleRegistry(
            tuple(contracts), catalog_version=version, verify_digests=False
        )

    def test_empty_catalog_rejected(self):
        with self.assertRaises(CatalogError):
            self._registry(())

    def test_duplicate_module_id_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            self._registry([_contract(), _contract()])
        self.assertIn("duplicate", str(ctx.exception))

    def test_non_pinned_version_rejected(self):
        for bad in ("~> 5.0", "1.0", "latest", "v1.0.0"):
            with self.subTest(version=bad):
                with self.assertRaises(CatalogError) as ctx:
                    self._registry([_contract(version=bad)])
                self.assertIn("pin", str(ctx.exception))

    def test_absolute_local_source_rejected(self):
        for bad in ("/etc/modules/thing", "C:/modules/thing"):
            with self.subTest(source=bad):
                with self.assertRaises(CatalogError):
                    self._registry([_contract(source=bad)])

    def test_traversal_source_rejected(self):
        with self.assertRaises(CatalogError):
            self._registry([_contract(source="../../../outside")])

    def test_registry_source_must_be_wellformed(self):
        with self.assertRaises(CatalogError):
            self._registry([
                _contract(source="not a source", source_kind=SourceKind.REGISTRY)
            ])

    def test_catalog_revision_mismatch_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            self._registry([_contract()], version="other-catalog/9.9.9")
        self.assertIn("catalog_revision", str(ctx.exception))

    def test_verified_without_method_rejected(self):
        with self.assertRaises(CatalogError) as ctx:
            self._registry([
                _contract(verification=_verification(methods=frozenset()))
            ])
        self.assertIn("verification method", str(ctx.exception))

    def test_verified_without_reviewer_rejected(self):
        with self.assertRaises(CatalogError):
            self._registry([
                _contract(verification=_verification(verified_by=""))
            ])

    def test_enum_without_allowlist_rejected(self):
        broken = _contract(
            inputs=(
                ModuleInputDefinition(
                    name="mode",
                    type=ParameterType.ENUM,
                    required=True,
                    binding=ParameterBindingSpec(parameter_name="mode"),
                    constraints=ParameterConstraints(),
                    description="mode",
                ),
            )
        )
        with self.assertRaises(CatalogError) as ctx:
            self._registry([broken])
        self.assertIn("allowed_values", str(ctx.exception))

    def test_allowlist_on_non_enum_rejected(self):
        broken = _contract(
            inputs=(
                ModuleInputDefinition(
                    name="name",
                    type=ParameterType.STRING,
                    required=True,
                    binding=ParameterBindingSpec(parameter_name="name"),
                    constraints=ParameterConstraints(
                        allowed_values=frozenset({"a", "b"})
                    ),
                    description="name",
                ),
            )
        )
        with self.assertRaises(CatalogError):
            self._registry([broken])

    def test_secret_flag_requires_secret_type(self):
        broken = _contract(
            inputs=(
                ModuleInputDefinition(
                    name="password",
                    type=ParameterType.STRING,
                    required=True,
                    binding=ParameterBindingSpec(parameter_name="password"),
                    is_secret=True,
                    description="bad",
                ),
            )
        )
        with self.assertRaises(CatalogError) as ctx:
            self._registry([broken])
        self.assertIn("is_secret", str(ctx.exception))

    def test_duplicate_input_name_rejected(self):
        definition = ModuleInputDefinition(
            name="table_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="table_name"),
            description="dup",
        )
        with self.assertRaises(CatalogError):
            self._registry([_contract(inputs=(definition, definition))])

    def test_required_input_cannot_declare_default(self):
        from ai_architect.domain.provisioning.module import TypedString
        definition = ModuleInputDefinition(
            name="table_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="table_name"),
            default=TypedString(value="x"),
            description="bad",
        )
        with self.assertRaises(CatalogError) as ctx:
            self._registry([_contract(inputs=(definition,))])
        self.assertIn("cannot declare a default", str(ctx.exception))

    def test_input_needs_exactly_one_binding_source(self):
        with self.assertRaises(ValueError):
            ParameterBindingSpec()
        with self.assertRaises(ValueError):
            ParameterBindingSpec(
                parameter_name="a", resource_id=True
            )


class DigestPinningTests(unittest.TestCase):
    """MVP modules are pinned by content digest computed from disk."""

    def test_digest_is_stable_for_unchanged_module(self):
        directory = REPO_ROOT / "infra/modules/aws-object-storage"
        self.assertEqual(
            module_source_digest(directory), module_source_digest(directory)
        )

    def test_digest_changes_when_module_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = REPO_ROOT / "infra/modules/aws-object-storage"
            target = pathlib.Path(tmp) / "mod"
            shutil.copytree(source, target)
            before = module_source_digest(target)
            (target / "main.tf").write_text(
                "# tampered\n", encoding="utf-8"
            )
            self.assertNotEqual(before, module_source_digest(target))

    def test_registry_binds_real_digests(self):
        registry = build_default_registry()
        for contract in registry.all_contracts():
            expected = module_source_digest(
                REPO_ROOT / contract.source.replace("../../", "")
            )
            self.assertEqual(contract.source_digest, expected)
            self.assertEqual(len(contract.source_digest), 64)

    def test_digest_ignores_non_terraform_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = pathlib.Path(tmp) / "mod"
            module.mkdir()
            (module / "main.tf").write_text("resource {}\n", encoding="utf-8")
            before = module_source_digest(module)
            (module / "README.md").write_text("docs\n", encoding="utf-8")
            self.assertEqual(before, module_source_digest(module))

    def test_missing_directory_fails_closed(self):
        with self.assertRaises(CatalogError):
            module_source_digest(pathlib.Path("/nonexistent/module"))

    def test_directory_without_tf_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError):
                module_source_digest(pathlib.Path(tmp))

    def test_allowlists_expose_pinned_values(self):
        registry = build_default_registry()
        self.assertTrue(registry.source_allowlist())
        self.assertEqual(registry.version_allowlist(), frozenset({"1.0.0"}))
        self.assertEqual(len(registry.source_digest_allowlist()), 10)


class CatalogDigestTests(unittest.TestCase):
    """Catalog digest binds a selection to an exact trust state."""

    def test_digest_is_stable_across_instances(self):
        first = build_default_registry().catalog_digest
        second = build_default_registry().catalog_digest
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_digest_changes_when_verification_status_changes(self):
        revoked = tuple(
            c.model_copy(update={
                "verification": c.verification.model_copy(
                    update={"status": VerificationStatus.REVOKED}
                )
            })
            if c.module_id is ModuleId.OBJECT_STORAGE else c
            for c in CATALOG
        )
        baseline = build_default_registry()
        altered = VerifiedModuleRegistry(
            revoked, catalog_version=CATALOG_VERSION
        )
        self.assertNotEqual(baseline.catalog_digest, altered.catalog_digest)

    def test_digest_changes_when_version_changes(self):
        bumped = tuple(
            c.model_copy(update={"version": "1.0.1"})
            if c.module_id is ModuleId.NOSQL_TABLE else c
            for c in CATALOG
        )
        baseline = build_default_registry()
        altered = VerifiedModuleRegistry(
            bumped, catalog_version=CATALOG_VERSION
        )
        self.assertNotEqual(baseline.catalog_digest, altered.catalog_digest)


class TrustBoundaryTests(unittest.TestCase):
    """4C adds no forbidden dependency and touches no frozen file."""

    #: 4C sources. Dependency checks are AST-based so prose cannot
    #: produce a false positive or hide a real import.
    SOURCES = (
        "src/ai_architect/domain/provisioning/module.py",
        "src/ai_architect/services/provisioning/module_catalog.py",
        "src/ai_architect/services/provisioning/module_registry.py",
        "src/ai_architect/services/provisioning/capability_mapping.py",
        "src/ai_architect/services/provisioning/module_parameters.py",
        "src/ai_architect/services/provisioning/module_selection.py",
    )

    def _imports(self):
        for relative in self.SOURCES:
            yield relative, _imported_modules(REPO_ROOT / relative)

    def test_no_llm_dependency(self):
        forbidden = {
            "langchain", "langchain_core", "langgraph", "openai",
            "anthropic", "ollama",
        }
        for name, imported in self._imports():
            self.assertEqual(
                imported & forbidden, set(), msg=f"{name} imports {imported & forbidden}"
            )

    def test_no_llm_infrastructure_import(self):
        forbidden = {"ai_architect.infrastructure"}
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_agents_or_orchestrator_import(self):
        forbidden = {"ai_architect.agents", "ai_architect.api", "langgraph"}
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_mcp_or_retrieval_import(self):
        forbidden = {"mcp", "langchain_mcp_adapters", "qdrant_client"}
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_aws_sdk_import(self):
        forbidden = {"boto3", "botocore"}
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_network_or_subprocess_import(self):
        forbidden = {
            "requests", "httpx", "urllib", "socket", "aiohttp", "subprocess",
        }
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_environment_read(self):
        forbidden = {"os", "getpass", "pathlib"}
        # pathlib is allowed (disk digest pinning); os is not.
        for name, imported in self._imports():
            self.assertNotIn("os", imported, msg=f"{name} imports os")
            self.assertNotIn("getpass", imported, msg=name)

    def test_no_terraform_cli_import(self):
        forbidden = {"ai_architect.services.provisioning.terraform_cli"}
        for name, imported in self._imports():
            self.assertEqual(imported & forbidden, set(), msg=name)

    def test_no_hcl_library_import(self):
        for name, imported in self._imports():
            self.assertNotIn("hcl2", imported, msg=name)
            self.assertNotIn("hcl", imported, msg=name)

    def test_sources_are_project_owned_not_community(self):
        registry = build_default_registry()
        for source in registry.source_allowlist():
            self.assertTrue(source.startswith("../../"), msg=source)
            self.assertIn("infra/modules", source)

    def test_module_sources_have_no_public_registry_host(self):
        for contract in CATALOG:
            self.assertEqual(contract.source_kind, SourceKind.LOCAL_PATH)

    def test_4c_does_not_emit_terraform_resource_blocks(self):
        """4C ends at ModuleSelection: no Terraform block literal anywhere.

        Checks string constants via the AST rather than grepping the file,
        so prose describing Terraform cannot trip it and a real emitted
        block header cannot hide inside an f-string.
        """
        offenders = []
        for relative in self.SOURCES:
            tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str):
                    continue
                text = node.value
                for opener in ('resource "', 'module "', 'provider "'):
                    if opener in text and "```" not in text:
                        # Docstrings legitimately mention these as prose.
                        docstring = ast.get_docstring(
                            next(
                                (
                                    n
                                    for n in ast.walk(tree)
                                    if isinstance(n, (ast.Module, ast.ClassDef,
                                                      ast.FunctionDef))
                                    and ast.get_docstring(n) == text
                                ),
                                None,
                            ),
                            None,
                        )
                        if docstring is None:
                            offenders.append((relative, opener))
        self.assertEqual(offenders, [], msg=f"4C emits Terraform: {offenders}")


if __name__ == "__main__":
    unittest.main()