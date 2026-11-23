"""Deterministic monthly cost calculator.

Consumes CapacityModelingResult + ComponentSelectionResult + normalized
PricedComponent facts and produces CostEstimationResult with pure
arithmetic. Every usage number carries its exact derivation formula;
anything not derivable becomes an explicit unpriced entry, never an
invented number.

Every applicable billing dimension of every selected component is
classified through the coverage registry (priceable / estimable /
unsupported) and reported in a per-dimension coverage report, so a
dimension the pipeline *expects* can never vanish silently just because
its price fact was not retrieved. A deterministic completeness verdict
(FULLY_PRICED / PARTIALLY_UNRESOLVED / UNSUPPORTED) gates the published
best-estimate total: while anything applicable is unresolved, no total
is published and the partial priced subtotal keeps its explicit
"subtotal, not total" meaning.
"""

from datetime import datetime

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.cost.models import (
    CostCompleteness,
    CostEstimationResult,
    CostLineItem,
    CoverageEntry,
    CoverageStatus,
    DimensionDisposition,
    EstimatedCostRange,
    PricedComponent,
    PricingModel,
    UnpricedComponent,
    UsageEstimate,
    UsageProvenance,
)
from ai_architect.services.cost import coverage_registry
from ai_architect.services.cost.dimensions import label_for_dimension

SECONDS_PER_MONTH = 2_592_000  # 30 days
BYTES_PER_GB = 1_000_000_000
HOURS_PER_MONTH = 730

# Coverage statuses that make an estimate incomplete. NOT_MODELED is the
# pre-registry spelling of UNSUPPORTED and counts identically.
_GAP_STATUS_VALUES = frozenset(
    {
        CoverageStatus.USAGE_UNKNOWN.value,
        CoverageStatus.PRICE_UNAVAILABLE.value,
        CoverageStatus.UNSUPPORTED.value,
        CoverageStatus.NOT_MODELED.value,
    }
)

# Gap statuses that are audited as outside the pipeline's pricing scope.
# With at least one estimated line present and no unresolvable gaps, these
# residuals still allow the best-estimate total to be published.
_SCOPE_RESIDUAL_STATUS_VALUES = frozenset(
    {
        CoverageStatus.UNSUPPORTED.value,
        CoverageStatus.NOT_MODELED.value,
    }
)

# Gap statuses that always suppress a published total: usage the pipeline
# could neither derive nor estimate, and dimensions with no retrieved
# price. UNSUPPORTED/NOT_MODELED residuals never suppress it by
# themselves (see the completeness verdict in calculate()).
_UNRESOLVABLE_GAP_STATUS_VALUES = frozenset(
    {
        CoverageStatus.USAGE_UNKNOWN.value,
        CoverageStatus.PRICE_UNAVAILABLE.value,
    }
)

ESTIMATED_SCENARIO_NOTE = (
    "Estimated lines use the typical usage scenario for the best-estimate "
    "total; the low/high range is illustrative given the stated assumptions "
    "and is not a contractual bound."
)


def _match_tier(tiers, usage: float):
    """Select the tier containing usage; deterministic by begin bound."""
    ordered = sorted(tiers, key=lambda t: t.begin)
    chosen = ordered[0]
    for tier in ordered:
        if usage >= tier.begin:
            chosen = tier
    return chosen


def _tiered_cost(tiers, usage: float) -> tuple[float, float, int]:
    """Marginal tiered cost.

    Returns (total, first-tier unit price, number of contributing tiers)
    so lines can record exactly how multi-tier totals were built.
    """
    ordered = sorted(tiers, key=lambda t: t.begin)
    total = 0.0
    contributing = 0
    for tier in ordered:
        end = tier.end if tier.end is not None else float("inf")
        billable = max(0.0, min(usage, end) - tier.begin)
        if billable > 0:
            contributing += 1
        total += billable * tier.unit_price_usd
    return total, ordered[0].unit_price_usd, contributing


def _fact_for(priced: PricedComponent, usage_key: str, model: PricingModel):
    for fact in priced.facts:
        if fact.usage_key == usage_key and fact.pricing_model == model:
            return fact
    return None


class CostCalculator:
    """Pure monthly-cost arithmetic over live pricing facts."""

    def calculate(
        self,
        capacity: CapacityModelingResult,
        selection: ComponentSelectionResult,
        priced: list[PricedComponent],
        region_code: str,
        usage_estimates: list[UsageEstimate] | None = None,
    ) -> CostEstimationResult:
        calc = capacity.calculation
        proposal = capacity.proposal
        peak_factor = proposal.peak_factor
        avg_write_qps = calc.peak_write_qps / peak_factor
        avg_read_qps = calc.peak_read_qps / peak_factor
        avg_msg_bytes = proposal.average_message_size_bytes

        lines: list[CostLineItem] = []
        estimated_lines: list[CostLineItem] = []
        assumptions: list[str] = []
        warnings: list[str] = []
        unpriced: list[UnpricedComponent] = []
        coverage: list[CoverageEntry] = []
        as_of = None
        estimated_low_total = 0.0
        estimated_high_total = 0.0
        # Components whose priced lines share the identical
        # (avg_write_qps + avg_read_qps) request-volume derivation.
        # Used only for disclosure; quantities are never adjusted here.
        shared_volume_components: set[str] = set()

        # Estimates are keyed by component-dimension; a duplicate key is
        # rejected (first wins) rather than silently priced twice.
        estimates_by_key: dict[tuple[str, str], UsageEstimate] = {}
        for estimate in usage_estimates or []:
            key = (estimate.component_name, estimate.dimension)
            if key in estimates_by_key:
                warnings.append(
                    f"Duplicate usage estimate for {key[0]}/{key[1]}; "
                    "the first estimate was kept."
                )
                continue
            estimates_by_key[key] = estimate
        applied_estimates: set[tuple[str, str]] = set()
        priced_line_keys: set[tuple[str, str]] = set()

        by_name = {p.component_name: p for p in priced}
        selection_names = {
            comp.name for comp in selection.selected_components
        }

        for comp in selection.selected_components:
            facts_holder = by_name.get(comp.name)
            if facts_holder is None or facts_holder.unavailable_reason:
                unpriced.append(
                    UnpricedComponent(
                        component_name=comp.name,
                        reason=(
                            facts_holder.unavailable_reason
                            if facts_holder and facts_holder.unavailable_reason
                            else "No pricing facts were retrieved for this component."
                        ),
                    )
                )
                coverage.extend(
                    self._blocked_entries(
                        comp.name, facts_holder, region_code,
                        priceable_reason=None,
                    )
                )
                continue

            on_demand_facts = [
                fact
                for fact in facts_holder.facts
                if fact.pricing_model == PricingModel.ON_DEMAND
            ]
            for fact in on_demand_facts:
                if fact.retrieved_at is not None and (
                    as_of is None or fact.retrieved_at > as_of
                ):
                    as_of = fact.retrieved_at

            service = coverage_registry.resolve_service(
                comp.name, facts_holder.service_code
            )

            def price_fact(fact, dim_spec) -> None:
                """Price one on-demand fact, or record why it cannot be.

                A priced fact becomes a line and a priced coverage row; a
                declined fact first tries an estimator-supplied usage
                estimate for the same component-dimension (which produces
                an estimated line priced at the retrieved fact), and
                otherwise becomes an explicit unpriced entry plus a
                usage-unknown row carrying the identical reason. When the
                registry declares the dimension estimable, the registry's
                assumption-input context is appended to the reason; a
                retrieved fact never flips a declared dimension silently.
                """
                nonlocal estimated_low_total, estimated_high_total
                key = (comp.name, fact.usage_key)
                line, declined_reason = self._price_on_demand(
                    comp_name=comp.name,
                    fact=fact,
                    avg_write_qps=avg_write_qps,
                    avg_read_qps=avg_read_qps,
                    avg_msg_bytes=avg_msg_bytes,
                    calc=calc,
                    peak_factor=peak_factor,
                    region_code=region_code,
                    assumptions=assumptions,
                )
                disposition = (
                    dim_spec.disposition if dim_spec is not None else None
                )
                if line is not None:
                    lines.append(line)
                    priced_line_keys.add(key)
                    coverage.append(
                        CoverageEntry(
                            component_name=comp.name,
                            dimension=fact.usage_key,
                            label=label_for_dimension(fact.usage_key),
                            status=CoverageStatus.PRICED,
                            reason="Included in the estimated monthly total.",
                            disposition=disposition,
                        )
                    )
                    if fact.usage_key in (
                        "requests",
                        "rest_requests",
                        "http_requests",
                        "queries",
                    ):
                        shared_volume_components.add(comp.name)
                    return
                estimate = estimates_by_key.get(key)
                if estimate is not None:
                    estimated = self._price_estimated(
                        comp_name=comp.name,
                        fact=fact,
                        estimate=estimate,
                        region_code=region_code,
                        assumptions=assumptions,
                        warnings=warnings,
                    )
                    if estimated is not None:
                        est_line, low_cost, high_cost = estimated
                        applied_estimates.add(key)
                        estimated_lines.append(est_line)
                        estimated_low_total += low_cost
                        estimated_high_total += high_cost
                        coverage.append(
                            CoverageEntry(
                                component_name=comp.name,
                                dimension=fact.usage_key,
                                label=label_for_dimension(fact.usage_key),
                                status=CoverageStatus.ESTIMATED,
                                reason=(
                                    "Priced from an estimated usage "
                                    f"quantity ({estimate.provenance}); "
                                    f"typical {estimate.typical_quantity:,.3f} "
                                    f"{fact.unit} (low "
                                    f"{estimate.low_quantity:,.3f} / high "
                                    f"{estimate.high_quantity:,.3f}). "
                                    "Included in the best-estimate total "
                                    "and range, not the deterministic "
                                    "priced subtotal."
                                ),
                                disposition=disposition,
                            )
                        )
                        return
                reason = declined_reason or (
                    "No usage derivation rule for usage key "
                    f"{fact.usage_key!r}; not priced rather than estimated."
                )
                if (
                    dim_spec is not None
                    and disposition == DimensionDisposition.ESTIMABLE
                ):
                    reason = (
                        f"{reason} "
                        + coverage_registry.estimable_reason(
                            dim_spec, price_fact_retrieved=True
                        )
                    )
                elif (
                    dim_spec is not None
                    and disposition == DimensionDisposition.UNSUPPORTED
                ):
                    reason = f"{reason} {dim_spec.reason}"
                unpriced.append(
                    UnpricedComponent(component_name=comp.name, reason=reason)
                )
                coverage.append(
                    CoverageEntry(
                        component_name=comp.name,
                        dimension=fact.usage_key,
                        label=label_for_dimension(fact.usage_key),
                        status=CoverageStatus.USAGE_UNKNOWN,
                        reason=reason,
                        disposition=disposition,
                    )
                )

            if not on_demand_facts:
                # Reserved-only or factless component: exactly one
                # component-level unpriced entry with the explicit reason
                # (unchanged wording), plus coverage rows for the expected
                # dimensions. No dimension-level unpriced entry is
                # duplicated on top of it.
                if facts_holder.facts:
                    reason = (
                        "Only reserved pricing facts were retrieved; no "
                        "on-demand baseline is available for this component, "
                        "and no rate is invented."
                    )
                    priceable_reason = lambda dim: (  # noqa: E731
                        "Only reserved pricing facts were retrieved for "
                        "this component; no on-demand rate is available "
                        f"for '{dim.dimension}', and no rate is invented."
                    )
                else:
                    reason = "No pricing facts were retrieved for this component."
                    priceable_reason = None
                unpriced.append(
                    UnpricedComponent(component_name=comp.name, reason=reason)
                )
                coverage.extend(
                    self._blocked_entries(
                        comp.name, facts_holder, region_code,
                        priceable_reason=priceable_reason,
                    )
                )
                continue

            if service is None:
                # No registry entry for this component name: the legacy
                # per-fact path (still fail-closed, still disclosed).
                for fact in on_demand_facts:
                    price_fact(fact, None)
                continue

            resolved = self._resolve_configuration(facts_holder)
            applicable, excluded = coverage_registry.coverage_for(
                service.service_code, **resolved
            )
            expected_keys = {dim.dimension for dim in service.dimensions}
            facts_by_key = {}
            for fact in on_demand_facts:
                facts_by_key.setdefault(fact.usage_key, fact)

            for dim in applicable:
                fact = facts_by_key.pop(dim.dimension, None)
                if fact is not None:
                    price_fact(fact, dim)
                elif dim.disposition == DimensionDisposition.UNSUPPORTED:
                    coverage.append(
                        CoverageEntry(
                            component_name=comp.name,
                            dimension=dim.dimension,
                            label=dim.label,
                            status=CoverageStatus.UNSUPPORTED,
                            reason=dim.reason,
                            disposition=dim.disposition,
                        )
                    )
                elif dim.disposition == DimensionDisposition.ESTIMABLE:
                    # The pipeline knows the dimension is billed but no
                    # retrieved price fact covers it; without a fact
                    # there is no rate to price an estimate with, so
                    # this stays explicitly unresolved (and disclosed).
                    dim_reason = coverage_registry.estimable_reason(
                        dim, price_fact_retrieved=False
                    )
                    unpriced.append(
                        UnpricedComponent(
                            component_name=comp.name, reason=dim_reason
                        )
                    )
                    coverage.append(
                        CoverageEntry(
                            component_name=comp.name,
                            dimension=dim.dimension,
                            label=dim.label,
                            status=CoverageStatus.USAGE_UNKNOWN,
                            reason=dim_reason,
                            disposition=dim.disposition,
                        )
                    )
                else:
                    # Priceable dimension with no retrieved fact: the
                    # pipeline derives the usage but has no catalog
                    # price for it. Explicit, never a silent omission.
                    dim_reason = coverage_registry.price_unavailable_reason(
                        dim, region_code
                    )
                    unpriced.append(
                        UnpricedComponent(
                            component_name=comp.name, reason=dim_reason
                        )
                    )
                    coverage.append(
                        CoverageEntry(
                            component_name=comp.name,
                            dimension=dim.dimension,
                            label=dim.label,
                            status=CoverageStatus.PRICE_UNAVAILABLE,
                            reason=dim_reason,
                            disposition=dim.disposition,
                        )
                    )

            for dim in excluded:
                coverage.append(
                    CoverageEntry(
                        component_name=comp.name,
                        dimension=dim.dimension,
                        label=dim.label,
                        status=CoverageStatus.NOT_APPLICABLE,
                        reason=coverage_registry.not_applicable_reason(
                            dim,
                            variant=resolved["variant"],
                            billing_model=resolved["billing_model"],
                        ),
                        disposition=dim.disposition,
                    )
                )

            for fact in on_demand_facts:
                if fact.usage_key in expected_keys:
                    continue
                price_fact(fact, None)

        totals: dict[str, float] = {}
        for line in lines:
            key = str(line.pricing_model)
            totals[key] = totals.get(key, 0.0) + line.monthly_cost_usd

        estimated_totals: dict[str, float] = {}
        for line in estimated_lines:
            key = str(line.pricing_model)
            estimated_totals[key] = (
                estimated_totals.get(key, 0.0) + line.monthly_cost_usd
            )

        reserved_present = any(
            t != PricingModel.ON_DEMAND.value for t in totals
        )
        if not reserved_present:
            warnings.append(
                "No reserved pricing applies to the priced usage-based SKUs; "
                "reserved scenarios from the Price List API cover "
                "provisioned resources (e.g. EC2/RDS/ElastiCache instances) "
                "whose instance specifications are not determined by the "
                "pipeline, so only the on-demand baseline is totaled."
            )

        if len(shared_volume_components) >= 2:
            names = ", ".join(sorted(shared_volume_components))
            warnings.append(
                "Shared request-volume assumption: the priced request-based "
                f"components ({names}) each use the identical monthly volume "
                "(avg_write_qps + avg_read_qps) * 2592000 s/month. "
                "Cache-hit, origin-forward, fan-out, and per-layer topology "
                "effects are not modeled in this slice, so edge (e.g. "
                "CloudFront), origin (e.g. API Gateway), queue, and DNS "
                "volumes are assumed equal rather than derived per layer."
            )

        # Deterministic completeness verdict and total publication.
        #   No estimated lines: the E1 rule verbatim -- any coverage gap
        #     or component-level unpriced entry makes the result partial,
        #     and only a result with nothing priceable at all is
        #     UNSUPPORTED.
        #   With estimated lines: the best-estimate total is published
        #     unless a gap is unresolvable (usage_unknown /
        #     price_unavailable) or a component-level unpriced entry
        #     exists. Audited out-of-scope residuals (unsupported, legacy
        #     not_modeled) allow publication with the residual disclosed.
        coverage.sort(key=lambda entry: (entry.component_name, entry.dimension))
        unresolved_dimension_count = sum(
            1 for entry in coverage if entry.status in _GAP_STATUS_VALUES
        )
        residual_statuses = {
            entry.status
            for entry in coverage
            if entry.status in _GAP_STATUS_VALUES
        }
        has_unresolvable_gaps = bool(unpriced) or bool(
            residual_statuses & _UNRESOLVABLE_GAP_STATUS_VALUES
        )
        if not estimated_lines:
            if unresolved_dimension_count or unpriced:
                completeness = (
                    CostCompleteness.PARTIALLY_UNRESOLVED
                    if lines
                    else CostCompleteness.UNSUPPORTED
                )
            else:
                completeness = CostCompleteness.FULLY_PRICED
        elif has_unresolvable_gaps:
            completeness = CostCompleteness.PARTIALLY_UNRESOLVED
        elif residual_statuses & _SCOPE_RESIDUAL_STATUS_VALUES:
            completeness = CostCompleteness.ESTIMATED_WITH_RESIDUAL_UNKNOWNS
        else:
            completeness = CostCompleteness.ESTIMATED_WITH_ASSUMPTIONS

        if estimated_lines:
            warnings.append(ESTIMATED_SCENARIO_NOTE)

        # Leftover estimates (component not selected, dimension already
        # priced, or no retrieved price fact to price them with) are
        # disclosed explicitly, never silently dropped.
        for key in sorted(set(estimates_by_key) - applied_estimates):
            comp_name, dimension = key
            if key in priced_line_keys:
                warnings.append(
                    f"Usage estimate for {comp_name}/{dimension} ignored: "
                    "the dimension was already priced deterministically; "
                    "known facts are never overwritten."
                )
            elif comp_name in selection_names:
                warnings.append(
                    f"Usage estimate for {comp_name}/{dimension} ignored: "
                    "no retrieved on-demand price fact covers the "
                    "dimension, so nothing could be priced; it stays "
                    "unresolved and disclosed."
                )
            else:
                warnings.append(
                    f"Usage estimate for {comp_name}/{dimension} ignored: "
                    "the component is not part of the selection."
                )

        fully_priced = completeness == CostCompleteness.FULLY_PRICED
        publish_total = completeness in (
            CostCompleteness.FULLY_PRICED,
            CostCompleteness.ESTIMATED_WITH_ASSUMPTIONS,
            CostCompleteness.ESTIMATED_WITH_RESIDUAL_UNKNOWNS,
        )
        if publish_total:
            priced_subtotal = sum(totals.values())
            best_estimate = priced_subtotal + sum(estimated_totals.values())
            estimated_range = EstimatedCostRange(
                low_usd=priced_subtotal + estimated_low_total,
                high_usd=priced_subtotal + estimated_high_total,
            )
        else:
            best_estimate = None
            estimated_range = None

        return CostEstimationResult(
            lines=lines,
            totals_usd_per_month=totals,
            assumptions=assumptions,
            warnings=warnings,
            unpriced=unpriced,
            coverage=coverage,
            fully_priced=fully_priced,
            completeness=completeness,
            estimated_lines=estimated_lines,
            estimates=[
                estimates_by_key[key] for key in sorted(applied_estimates)
            ],
            estimated_totals_usd_per_month=estimated_totals,
            best_estimate_total_usd_per_month=best_estimate,
            estimated_range_usd_per_month=estimated_range,
            unresolved_dimension_count=unresolved_dimension_count,
            pricing_as_of=as_of or datetime.utcnow(),
        )

    @staticmethod
    def _resolve_configuration(
        facts_holder: PricedComponent,
    ) -> dict[str, str | None]:
        """Resolved variant/billing model for one priced component.

        Compatibility delegate: the implementation lives in
        coverage_registry.resolve_configuration so the calculator and
        the usage estimator classify dimensions identically.
        """
        return coverage_registry.resolve_configuration(facts_holder)

    @staticmethod
    def _blocked_entries(
        comp_name: str,
        facts_holder: PricedComponent | None,
        region_code: str,
        priceable_reason=None,
    ) -> list[CoverageEntry]:
        """Coverage rows for a component with no usable on-demand rate.

        Every expected dimension is still disclosed: unsupported
        dimensions keep their audited reason, estimable dimensions stay
        usage-unknown, and priceable dimensions report why no price fact
        covers them. Rows only -- the component-level unpriced entry is
        the authoritative gap signal for this component, so no
        dimension-level unpriced entry is duplicated next to it.
        """
        if facts_holder is None or not facts_holder.service_code:
            return []
        service = coverage_registry.resolve_service(
            comp_name, facts_holder.service_code
        )
        if service is None:
            return []
        resolved = CostCalculator._resolve_configuration(facts_holder)
        applicable, excluded = coverage_registry.coverage_for(
            service.service_code, **resolved
        )
        entries: list[CoverageEntry] = []
        for dim in applicable:
            if dim.disposition == DimensionDisposition.UNSUPPORTED:
                status = CoverageStatus.UNSUPPORTED
                reason = dim.reason
            elif dim.disposition == DimensionDisposition.ESTIMABLE:
                status = CoverageStatus.USAGE_UNKNOWN
                reason = coverage_registry.estimable_reason(
                    dim, price_fact_retrieved=False
                )
            elif priceable_reason is None:
                status = CoverageStatus.PRICE_UNAVAILABLE
                reason = coverage_registry.price_unavailable_reason(
                    dim, region_code
                )
            else:
                status = CoverageStatus.PRICE_UNAVAILABLE
                reason = priceable_reason(dim)
            entries.append(
                CoverageEntry(
                    component_name=comp_name,
                    dimension=dim.dimension,
                    label=dim.label,
                    status=status,
                    reason=reason,
                    disposition=dim.disposition,
                )
            )
        for dim in excluded:
            entries.append(
                CoverageEntry(
                    component_name=comp_name,
                    dimension=dim.dimension,
                    label=dim.label,
                    status=CoverageStatus.NOT_APPLICABLE,
                    reason=coverage_registry.not_applicable_reason(
                        dim,
                        variant=resolved["variant"],
                        billing_model=resolved["billing_model"],
                    ),
                    disposition=dim.disposition,
                )
            )
        return entries

    def _price_estimated(
        self,
        comp_name: str,
        fact,
        estimate: UsageEstimate,
        region_code: str,
        assumptions: list[str],
        warnings: list[str],
    ) -> tuple[CostLineItem, float, float] | None:
        """Price an estimated usage quantity at the retrieved fact's rate.

        Returns (typical-scenario line, low-scenario cost, high-scenario
        cost), or None when the estimate is rejected (unit mismatch or
        unsupported provenance); the caller then falls back to the
        explicit unresolved path and the warning explains the
        rejection. Known facts are never overwritten: callers only
        reach this method after the deterministic derivation declined.
        """
        if estimate.unit != fact.unit:
            warnings.append(
                f"Usage estimate for {comp_name}/{fact.usage_key} was "
                f"rejected: unit mismatch (estimate '{estimate.unit}' vs "
                f"retrieved price fact '{fact.unit}'); the dimension "
                "stays unresolved."
            )
            return None
        if estimate.provenance not in (
            UsageProvenance.DETERMINISTIC_DERIVATION,
            UsageProvenance.ESTIMATOR_ASSUMPTION,
        ):
            warnings.append(
                f"Usage estimate for {comp_name}/{fact.usage_key} was "
                f"rejected: provenance '{estimate.provenance}' is not an "
                "estimation provenance accepted by the calculator."
            )
            return None

        typical_cost, _first_price, tier_count = _tiered_cost(
            fact.tiers, estimate.typical_quantity
        )
        low_cost, _low_price, _low_tiers = _tiered_cost(
            fact.tiers, estimate.low_quantity
        )
        high_cost, _high_price, _high_tiers = _tiered_cost(
            fact.tiers, estimate.high_quantity
        )
        tier = _match_tier(fact.tiers, estimate.typical_quantity)
        formula = (
            "estimated monthly usage (typical scenario "
            f"{estimate.typical_quantity:,.3f} {fact.unit}; low "
            f"{estimate.low_quantity:,.3f} / high "
            f"{estimate.high_quantity:,.3f}) priced at the retrieved "
            "on-demand rate"
        )
        if tier_count > 1:
            formula += (
                "; monthly total summed marginally across "
                f"{tier_count} applicable tiers"
            )
        line = CostLineItem(
            component_name=comp_name,
            pricing_model=PricingModel.ON_DEMAND,
            unit=tier.unit,
            monthly_usage=estimate.typical_quantity,
            unit_price_usd=tier.unit_price_usd,
            monthly_cost_usd=typical_cost,
            usage_formula=formula,
            pricing_source=fact.source,
            pricing_retrieved_at=fact.retrieved_at,
            region_code=region_code,
            usage_provenance=estimate.provenance,
        )
        assumption = (
            f"{comp_name}: '{fact.usage_key}' monthly usage is estimated "
            f"({estimate.provenance}); the typical scenario "
            f"({estimate.typical_quantity:,.3f} {fact.unit}) drives the "
            "best-estimate total, with low/high scenarios at "
            f"{estimate.low_quantity:,.3f}/{estimate.high_quantity:,.3f} "
            f"{fact.unit}."
        )
        if estimate.assumptions:
            assumption += (
                " Assumptions: " + "; ".join(estimate.assumptions)
            )
        assumptions.append(assumption)
        return line, low_cost, high_cost

    def _price_on_demand(
        self,
        comp_name: str,
        fact,
        avg_write_qps: float,
        avg_read_qps: float,
        avg_msg_bytes: float,
        calc,
        peak_factor: float,
        region_code: str,
        assumptions: list[str],
    ) -> tuple[CostLineItem | None, str | None]:
        """Price one on-demand fact, or return why usage is not derivable.

        Returns (line, declined_reason); exactly one side is set. No
        amount is invented: a declined dimension's reason is turned into
        an explicit unpriced entry and a usage-unknown coverage row by
        the caller.
        """
        key = fact.usage_key
        if key in ("requests", "rest_requests", "http_requests"):
            monthly = (avg_write_qps + avg_read_qps) * SECONDS_PER_MONTH
            formula = (
                "(peak_write_qps + peak_read_qps) / peak_factor "
                f"({avg_write_qps + avg_read_qps:.3f} avg total QPS) "
                f"* {SECONDS_PER_MONTH} s/month"
            )
        elif key == "storage_gbmo":
            monthly = (
                calc.storage_growth_bytes_per_day * 30 / BYTES_PER_GB
            )
            formula = (
                "storage_growth_bytes_per_day "
                f"({calc.storage_growth_bytes_per_day:.1f} B/day) * 30 days "
                "/ 1e9 B/GB = one month of accumulated growth"
            )
            assumptions.append(
                f"{comp_name}: storage priced as one month of accumulated "
                "daily growth; pre-existing stored volume is not modeled."
            )
        elif key in ("wru", "rru"):
            per_request = (
                max(1, -(-int(avg_msg_bytes) // 1024))
                if key == "wru"
                else max(1, -(-int(avg_msg_bytes) // 4096))
            )
            base = avg_write_qps if key == "wru" else avg_read_qps
            monthly = base * per_request * SECONDS_PER_MONTH
            formula = (
                f"{'avg_write_qps' if key == 'wru' else 'avg_read_qps'} "
                f"({base:.3f}) * {per_request} request unit(s) per request "
                f"(1KB{' write' if key == 'wru' else ', 4KB read'} rounding of "
                f"{avg_msg_bytes:.0f} B average, strongly consistent reads) "
                f"* {SECONDS_PER_MONTH} s/month"
            )
        elif key == "transfer_gb":
            avg_egress_bps = calc.egress_bytes_per_second / peak_factor
            monthly = avg_egress_bps * SECONDS_PER_MONTH / BYTES_PER_GB
            formula = (
                "egress_bytes_per_second "
                f"({calc.egress_bytes_per_second:.1f} peak B/s) "
                f"/ peak_factor ({peak_factor:.3f}) "
                f"= {avg_egress_bps:.1f} avg B/s "
                f"* {SECONDS_PER_MONTH} s/month / 1e9 B/GB"
            )
        elif key == "queries":
            monthly = (avg_write_qps + avg_read_qps) * SECONDS_PER_MONTH
            formula = (
                "one DNS query per request: "
                "(avg_write_qps + avg_read_qps) "
                f"({avg_write_qps + avg_read_qps:.3f}) "
                f"* {SECONDS_PER_MONTH} s/month"
            )
            assumptions.append(
                f"{comp_name}: one DNS query per request is assumed; "
                "caching resolvers would lower this."
            )
        elif key == "zones":
            monthly = 1.0
            formula = "1 hosted zone (stated; zone count is not modeled)"
            assumptions.append(
                f"{comp_name}: exactly 1 hosted zone assumed; "
                "additional zones are not modeled."
            )
        else:
            return None, (
                f"No usage derivation rule for usage key {key!r}; "
                "not priced rather than estimated."
            )

        total, _first_price, tier_count = _tiered_cost(fact.tiers, monthly)
        tier = _match_tier(fact.tiers, monthly)
        unit_price = tier.unit_price_usd
        if tier_count > 1:
            formula += (
                f"; monthly total summed marginally across "
                f"{tier_count} applicable tiers"
            )
        return (
            CostLineItem(
                component_name=comp_name,
                pricing_model=PricingModel.ON_DEMAND,
                unit=tier.unit,
                monthly_usage=monthly,
                unit_price_usd=unit_price,
                monthly_cost_usd=total,
                usage_formula=formula,
                pricing_source=fact.source,
                pricing_retrieved_at=fact.retrieved_at,
                region_code=region_code,
                usage_provenance=(
                    UsageProvenance.STATED_ASSUMPTION
                    if key in ("zones", "queries")
                    else UsageProvenance.DETERMINISTIC_DERIVATION
                ),
            ),
            None,
        )
