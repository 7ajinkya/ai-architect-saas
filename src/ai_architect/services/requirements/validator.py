from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
    ValidationResult,
    ValidationStatus,
)


class RequirementsValidator:
    """Deterministically validates extracted requirements."""

    def validate(
        self,
        extraction: RequirementsExtraction,
    ) -> ValidationResult:
        blocking_clarifications: list[ClarificationCandidate] = []

        for candidate in extraction.clarification_candidates:
            if self._is_blocking(candidate, extraction):
                blocking_clarifications.append(candidate)

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

    def _is_blocking(
        self,
        candidate: ClarificationCandidate,
        extraction: RequirementsExtraction,
    ) -> bool:
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