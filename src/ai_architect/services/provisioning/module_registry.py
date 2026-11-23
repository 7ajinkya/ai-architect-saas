"""Closed-world verified module registry.

Runtime trust enforcement for Slice 4C. This is the *second* of the two
distinct concerns the design separates:

- **Knowledge / schema retrieval** (authoritative, advisory, agent-facing)
  is a future Terraform Registry integration. It informs proposals.
- **Runtime trust / allowlisting** (deterministic, enforcing) is this
  module. It decides what may actually be invoked.

Keeping them apart is what allows the deterministic boundary to stay
network-free and LLM-free while the agent still benefits from current
provider schemas later.

Properties:

- Exact ``ModuleId`` lookup only. No fuzzy, substring or category fallback.
- ``source`` and ``version`` are read from frozen ``ModuleContract`` values.
  They are never derived from any proposal or parameter.
- The catalog is validated once at construction. A malformed catalog fails
  closed immediately rather than degrading at selection time.
- Every accessor returns deterministically ordered tuples.
- Local modules are pinned by a content digest computed over their ``.tf``
  files, so "verified" is checkable against what is actually on disk.
"""

import hashlib
import json
import pathlib
import re

from ai_architect.domain.provisioning.module import (
    MODULE_ROOT,
    ModuleContract,
    ModuleDiagnostic,
    ModuleDiagnosticCategory,
    ModuleErrorCode,
    ModuleId,
    ParameterType,
    SourceKind,
    TypedValue,
    VerificationStatus,
)

# Versions must be an exact pin. A range would reintroduce the schema
# drift the catalog exists to centralise.
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# A local module source must be a relative path inside the project. An
# absolute path, a network reference or a traversal is refused outright.
_LOCAL_SOURCE = re.compile(r"^\.\./\.\./[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_LOCAL_SOURCE_PREFIX_RE = re.compile(r"^\.\./\.\./")
# Any ".." segment after the fixed prefix is traversal, not a path. The
# character class above already permits dots, so this has to be checked
# separately or "../../../etc" would validate.
_LOCAL_TRAVERSAL = re.compile(r"(?:^|/)\.\.(?:/|$)")
_REGISTRY_SOURCE = re.compile(
    r"^[a-z0-9-]+(\.[a-z0-9-]+)*/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+$"
)

_TF_SUFFIX = ".tf"


class CatalogError(Exception):
    """The catalog itself is invalid. Fail closed at construction."""


def _canonical(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _module_tf_files(directory: pathlib.Path) -> list:
    """A module directory's top-level ``.tf`` files, sorted by path."""
    if not directory.is_dir():
        raise CatalogError(f"module directory not found: {directory}")
    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix == _TF_SUFFIX
    )
    if not files:
        raise CatalogError(f"module has no .tf files: {directory}")
    return files


def _digest_entries(entries) -> str:
    """sha256 over ``(basename, content)`` pairs in the given order."""
    payload = [
        {"path": name, "content": content} for name, content in entries
    ]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def module_source_digest(directory: pathlib.Path) -> str:
    """sha256 over a module's ``.tf`` files, sorted by relative path.

    This is the MVP pinning mechanism: a local module has no published
    version to pin, so its content is pinned instead.
    """
    files = _module_tf_files(directory)
    return _digest_entries(
        (path.name, path.read_text(encoding="utf-8")) for path in files
    )


def local_source_tree_path(source: str) -> str:
    """Repository-relative path behind a local module ``source``.

    ``source`` strings are written render-relative (``../../infra/...``,
    the prefix of the path every generated tree used to sit below); the
    repository-relative path used for digest binding and for in-tree
    module copies is that string with the fixed prefix stripped
    explicitly rather than guessed.
    """
    return _LOCAL_SOURCE_PREFIX_RE.sub("", source)


class VerifiedModuleRegistry:
    """Deterministic, closed-world allowlist of verified modules."""

    def __init__(
        self,
        contracts: tuple,
        *,
        catalog_version: str,
        repository_root: pathlib.Path = None,
        verify_digests: bool = True,
    ):
        self._repository_root = repository_root or pathlib.Path(__file__).resolve().parents[4]
        self._catalog_version = catalog_version
        self._validate(contracts)
        self._by_id = {contract.module_id: contract for contract in contracts}
        self._contracts = tuple(
            sorted(contracts, key=lambda contract: contract.module_id.value)
        )
        if verify_digests:
            self._bind_digests()

    # ---------------------------------------------------------------- load

    def _validate(self, contracts: tuple) -> None:
        if not contracts:
            raise CatalogError("catalog must declare at least one module")

        seen = set()
        for contract in contracts:
            if contract.module_id in seen:
                raise CatalogError(
                    f"duplicate module_id in catalog: {contract.module_id.value}"
                )
            seen.add(contract.module_id)

            if not _EXACT_VERSION.match(contract.version):
                raise CatalogError(
                    f"{contract.module_id.value}: version {contract.version!r} is "
                    f"not an exact x.y.z pin"
                )
            if contract.verification.catalog_revision != self._catalog_version:
                raise CatalogError(
                    f"{contract.module_id.value}: catalog_revision does not match "
                    f"the registry catalog_version"
                )
            if not contract.provider.strip():
                raise CatalogError(
                    f"{contract.module_id.value}: provider must be declared"
                )

            if contract.source_kind is SourceKind.LOCAL_PATH:
                relative = local_source_tree_path(contract.source)
                if not _LOCAL_SOURCE.match(contract.source):
                    raise CatalogError(
                        f"{contract.module_id.value}: local module source must be a "
                        f"project-relative path, got {contract.source!r}"
                    )
                if _LOCAL_TRAVERSAL.search(relative):
                    raise CatalogError(
                        f"{contract.module_id.value}: local module source must not "
                        f"contain a traversal segment, got {contract.source!r}"
                    )
            elif not _REGISTRY_SOURCE.match(contract.source):
                raise CatalogError(
                    f"{contract.module_id.value}: registry module source is malformed: "
                    f"{contract.source!r}"
                )

            if contract.verification.status is VerificationStatus.VERIFIED:
                if not contract.verification.methods:
                    raise CatalogError(
                        f"{contract.module_id.value}: a verified contract must "
                        f"record at least one verification method"
                    )
                if not contract.verification.verified_by:
                    raise CatalogError(
                        f"{contract.module_id.value}: a verified contract must "
                        f"record who verified it"
                    )

            self._validate_inputs(contract)

    def _validate_inputs(self, contract: ModuleContract) -> None:
        names = set()
        for definition in contract.inputs:
            if definition.name in names:
                raise CatalogError(
                    f"{contract.module_id.value}: duplicate input {definition.name!r}"
                )
            names.add(definition.name)

            if definition.type is ParameterType.ENUM:
                if not definition.constraints.allowed_values:
                    raise CatalogError(
                        f"{contract.module_id.value}.{definition.name}: an enum "
                        f"input must declare allowed_values"
                    )
            if definition.type in (
                ParameterType.STRING, ParameterType.ARTIFACT_REF,
                ParameterType.SECRET_REF,
            ):
                if definition.constraints.allowed_values:
                    raise CatalogError(
                        f"{contract.module_id.value}.{definition.name}: allowed_values "
                        f"is only valid for enum inputs"
                    )
            if definition.is_secret and definition.type is not ParameterType.SECRET_REF:
                raise CatalogError(
                    f"{contract.module_id.value}.{definition.name}: is_secret requires "
                    f"type secret_ref"
                )
            if definition.default is not None:
                if definition.required:
                    raise CatalogError(
                        f"{contract.module_id.value}.{definition.name}: a required "
                        f"input cannot declare a default"
                    )
                if definition.default.type is not definition.type:
                    raise CatalogError(
                        f"{contract.module_id.value}.{definition.name}: default type "
                        f"does not match declared type"
                    )

    def _bind_digests(self) -> None:
        """Pin local modules by content digest from disk.

        ``source`` is expressed relative to the *future Terraform working
        directory* (the generated tree lives two levels below the
        repository root, so ``../../`` reaches ``infra/modules``). Digest
        binding has to resolve that against the repository instead, so the
        render-relative prefix is stripped explicitly rather than guessed.
        """
        for contract in self._contracts:
            if contract.source_kind is not SourceKind.LOCAL_PATH:
                continue
            relative = local_source_tree_path(contract.source)
            directory = (self._repository_root / relative).resolve()
            digest = module_source_digest(directory)
            self._by_id[contract.module_id] = contract.model_copy(
                update={"source_digest": digest}
            )
        self._contracts = tuple(
            sorted(
                self._by_id.values(),
                key=lambda contract: contract.module_id.value,
            )
        )

    # ------------------------------------------------------------- lookups

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    @property
    def catalog_digest(self) -> str:
        """Content hash over the whole catalog.

        Bound into every ``ModuleSelection`` so a selection can be traced
        to the exact trust state that produced it, and so changing the
        catalog invalidates prior selections by inspection.
        """
        payload = [
            {
                "module_id": contract.module_id.value,
                "provider": contract.provider,
                "source": contract.source,
                "source_kind": contract.source_kind.value,
                "source_digest": contract.source_digest,
                "version": contract.version,
                "contract_version": contract.contract_version,
                "status": contract.verification.status.value,
                "inputs": sorted(
                    (i.name, i.type.value, i.required) for i in contract.inputs
                ),
            }
            for contract in self._contracts
        ]
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

    def all_contracts(self) -> tuple:
        return self._contracts

    def get(self, module_id: ModuleId) -> ModuleContract:
        """Exact lookup. Unknown identity is an explicit failure."""
        contract = self._by_id.get(module_id)
        if contract is None:
            available = ", ".join(
                sorted(c.module_id.value for c in self._contracts)
            )
            raise KeyError(
                f"module {module_id.value!r} is not in the verified catalog; "
                f"available: {available}"
            )
        return contract

    def has(self, module_id: ModuleId) -> bool:
        return module_id in self._by_id

    def module_tree(self, module_id: ModuleId) -> dict:
        """The pinned ``.tf`` files of a local module: name -> content.

        Content is read exactly as ``module_source_digest`` reads it
        (UTF-8 text; platform newline normalization happens in the text
        layer) and is refused unless it still matches the contract's
        pinned digest, so a renderer can only ever copy bytes the
        registry trust state covers.
        """
        contract = self.get(module_id)
        if contract.source_kind is not SourceKind.LOCAL_PATH:
            raise CatalogError(
                f"{module_id.value}: module content is only readable for "
                f"local modules, got {contract.source_kind.value!r}"
            )
        relative = local_source_tree_path(contract.source)
        directory = (self._repository_root / relative).resolve()
        files = _module_tf_files(directory)
        entries = [
            (path.name, path.read_text(encoding="utf-8")) for path in files
        ]
        if _digest_entries(entries) != contract.source_digest:
            raise CatalogError(
                f"{module_id.value}: module content on disk no longer "
                f"matches the pinned digest"
            )
        return dict(entries)

    def for_provider(self, provider: str) -> tuple:
        """Deterministically ordered contracts for one provider."""
        return tuple(
            contract for contract in self._contracts
            if contract.provider == provider
        )

    def capability_tags(self) -> frozenset:
        tags = set()
        for contract in self._contracts:
            tags.update(contract.capability_tags)
        return frozenset(tags)

    def source_allowlist(self) -> frozenset:
        """Exact trusted ``source`` strings, for the future guard."""
        return frozenset(c.source for c in self._contracts)

    def version_allowlist(self) -> frozenset:
        return frozenset(c.version for c in self._contracts)

    def source_digest_allowlist(self) -> frozenset:
        return frozenset(c.source_digest for c in self._contracts)

    # ------------------------------------------------------- trust queries

    def require_selectable(
        self, module_id: ModuleId, *, provider: str
    ) -> ModuleContract:
        """Return a contract only if it is genuinely usable.

        Raises ``KeyError`` for an absent module and ``LookupError`` for a
        module that exists but is not verified or not provider-compatible.
        Callers translate these into typed diagnostics.
        """
        contract = self.get(module_id)

        if contract.provider != provider:
            raise LookupError(
                f"module {module_id.value!r} targets provider "
                f"{contract.provider!r}, not {provider!r}"
            )

        status = contract.verification.status
        if status is not VerificationStatus.VERIFIED:
            raise LookupError(
                f"module {module_id.value!r} is declared but its verification "
                f"status is {status.value!r}; it cannot be selected"
            )
        return contract

    def diagnostics(self) -> tuple:
        """Non-fatal catalog observations, for reporting only."""
        findings = []
        for contract in self._contracts:
            verification = contract.verification
            if verification.status is VerificationStatus.PENDING:
                findings.append(
                    ModuleDiagnostic(
                        category=ModuleDiagnosticCategory.NOT_VERIFIED,
                        code=ModuleErrorCode.MODULE_NOT_VERIFIED,
                        message=(
                            f"Module {contract.module_id.value!r} is declared but "
                            f"PENDING verification and cannot be selected."
                        ),
                    )
                )
            if verification.expires_on is not None:
                findings.append(
                    ModuleDiagnostic(
                        category=ModuleDiagnosticCategory.CATALOG,
                        code=ModuleErrorCode.CATALOG_INVALID,
                        message=(
                            f"Module {contract.module_id.value!r} verification "
                            f"expires on {verification.expires_on.isoformat()}."
                        ),
                    )
                )
            if not contract.outputs:
                findings.append(
                    ModuleDiagnostic(
                        category=ModuleDiagnosticCategory.CATALOG,
                        code=ModuleErrorCode.CATALOG_INVALID,
                        message=(
                            f"Module {contract.module_id.value!r} declares no outputs; "
                            f"downstream wiring cannot reference it."
                        ),
                    )
                )
        return tuple(findings)


def build_default_registry(
    repository_root: pathlib.Path = None, *, verify_digests: bool = True
) -> VerifiedModuleRegistry:
    """Registry backed by the project-owned catalog."""
    from ai_architect.services.provisioning.module_catalog import (
        CATALOG,
        CATALOG_VERSION,
    )

    return VerifiedModuleRegistry(
        CATALOG,
        catalog_version=CATALOG_VERSION,
        repository_root=repository_root,
        verify_digests=verify_digests,
    )