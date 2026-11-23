from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
    ValidationResult,
    ValidationStatus,
)
from ai_architect.services.region.normalizer import (
    RegionNormalizationError,
    resolve_effective_region,
)


class RequirementsValidator:
    """Deterministically validates extracted requirements."""

    # MVP is single-cloud: exactly one of these must be explicitly provided.
    SUPPORTED_PROVIDERS = ("aws", "azure", "gcp")

    def validate(
        self,
        extraction: RequirementsExtraction,
    ) -> ValidationResult:
        blocking_clarifications: list[ClarificationCandidate] = []

        for candidate in extraction.clarification_candidates:
            if self._is_blocking(candidate, extraction):
                blocking_clarifications.append(candidate)

        provider_clarification = self._missing_provider_clarification(
            extraction,
            blocking_clarifications,
        )
        if provider_clarification is not None:
            blocking_clarifications.append(provider_clarification)

        region_clarification = self._missing_region_clarification(
            extraction,
            blocking_clarifications,
        )
        if region_clarification is not None:
            blocking_clarifications.append(region_clarification)

        status = (
            ValidationStatus.NEEDS_CLARIFICATION
            if blocking_clarifications
            else ValidationStatus.READY
        )

        return ValidationResult(
            status=status,
            blocking_clarifications=blocking_clarifications,
            accepted_assumptions=list(extraction.assumptions),
        )

    def _missing_provider_clarification(
        self,
        extraction: RequirementsExtraction,
        already_blocking: list[ClarificationCandidate],
    ) -> ClarificationCandidate | None:
        """Deterministic policy: a valid single-cloud provider is required.

        The LLM may identify the gap, but this rule owns the decision that
        it blocks progression. No default is ever invented; the existing
        HITL loop must ask the user. An unsupported value blocks exactly
        like a missing one.
        """
        provider = (extraction.cloud_provider or "").strip().lower()

        if provider in self.SUPPORTED_PROVIDERS:
            return None

        if any(
            c.requirement_key == "cloud_provider"
            for c in already_blocking
        ):
            return None

        return ClarificationCandidate(
            requirement_key="cloud_provider",
            requirement="cloud_provider",
            question=(
                "Which cloud provider should this architecture target? "
                "(aws, azure, or gcp)"
            ),
            reason=(
                "The MVP is single-cloud: Component Selection requires "
                "exactly one supported provider."
            ),
            impact=RequirementImpact.ARCHITECTURE,
        )

    def _missing_region_clarification(
        self,
        extraction: RequirementsExtraction,
        already_blocking: list[ClarificationCandidate],
    ) -> ClarificationCandidate | None:
        """Deterministic policy: Cost requires a resolvable region.

        The LLM may identify the gap, but this rule owns the decision
        that it blocks progression — mirroring resolve_effective_region
        so READY always implies Cost Estimation can resolve a region.
        No default is ever invented; the existing HITL loop must ask
        the user. Never duplicates a region question the model already
        raised. Region normalization is AWS-scoped, so a stated
        non-AWS provider keeps its existing behavior here and the
        provider-scoped stages still fail closed on their own terms.
        """
        if any(
            c.requirement_key == "region"
            for c in already_blocking
        ):
            return None

        provider = (extraction.cloud_provider or "").strip().lower()
        if provider and provider not in self.SUPPORTED_PROVIDERS:
            # Unsupported provider: the provider rule already blocks, and
            # region semantics for it are undefined here.
            return None
        if provider and provider != "aws":
            # Region normalization is implemented for AWS only; do not
            # second-guess another provider's region handling.
            return None

        try:
            resolve_effective_region(extraction)
        except RegionNormalizationError:
            return ClarificationCandidate(
                requirement_key="region",
                requirement="geographic deployment region",
                question=(
                    "Which geographic region(s) should the system be "
                    "deployed in?"
                ),
                reason=(
                    "Cost estimation requires a resolvable AWS deployment "
                    "region for region-specific pricing."
                ),
                impact=RequirementImpact.ARCHITECTURE,
            )
        return None

    def _is_blocking(
        self,
        candidate: ClarificationCandidate,
        extraction: RequirementsExtraction,
    ) -> bool:
        # A candidate that does not map to a canonical extraction field
        # cannot be applied by answer_clarification (setattr), so it must
        # not block: this also keeps yes/no confirmations of obvious
        # characteristics out of the blocking path.
        if candidate.requirement_key not in RequirementsExtraction.model_fields:
            return False

        if candidate.impact == RequirementImpact.ARCHITECTURE:
            return True

        if candidate.impact == RequirementImpact.COMPLIANCE:
            return self._has_regulated_context(extraction)

        return False

    def _has_regulated_context(
        self,
        extraction: RequirementsExtraction,
    ) -> bool:
        text = " ".join(
            [
                extraction.application_description,
                extraction.deployment_scope,
                extraction.region,
                extraction.compliance_requirements,
            ]
        ).lower()

        regulated_signals = {
            "gdpr",
            "hipaa",
            "pci",
            "regulated",
            "data residency",
            "residency",
            "healthcare",
            "financial",
            "banking",
        }

        return any(signal in text for signal in regulated_signals)