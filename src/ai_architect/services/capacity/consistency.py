"""Deterministic workload-consistency guardrail for capacity proposals.

Compares the average total request throughput implied by a
CapacityModelProposal against the user's stated request rate, when the
requirements state one in ``traffic_characteristics``.

There is deliberately no structured numeric RPS field on
RequirementsExtraction (``traffic_characteristics`` is free text), so
this module parses that single field conservatively: a number is only
honored when it is directly adjacent to an explicit rate unit
(per-second, per-minute, or per-day phrasing). Anything else returns
None and the check is skipped (fail-open) rather than risking a
false-positive failure on an unparseable requirement.

The comparison is one-sided by construction: derived average total
throughput can never legitimately exceed a stated rate, whether that
rate was meant as average or peak. A derived average far below a
stated peak is normal (peaks exceed averages) and is not flagged.
"""

import re

SECONDS_PER_DAY = 86_400

# Derived average more than this multiple of the stated rate is a
# material mismatch. Wide enough to absorb legitimate
# average-vs-peak and rounding differences, tight enough to catch the
# observed 9.84x double-counted-reads failure.
MATERIAL_MISMATCH_RATIO = 3.0

_MULTIPLIERS = {"k": 1_000.0, "m": 1_000_000.0}

# <number>[k|m] <rate unit>. The unit must be explicit and adjacent;
# bare numbers ("1,000", "3x") never match.
_RATE_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<mult>[kKmM])?"
    r"\s*(?P<unit>"
    r"rps"
    r"|reqs?/s"
    r"|r/s"
    r"|requests?\s*/\s*s(?:ec(?:ond)?s?)?"
    r"|requests?\s+per\s+sec(?:ond)?s?"
    r")\b",
    re.IGNORECASE,
)

_PER_MINUTE_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<mult>[kKmM])?"
    r"\s*(?P<unit>"
    r"reqs?\s*/\s*min(?:ute)?s?"
    r"|requests?\s*/\s*min(?:ute)?s?"
    r"|requests?\s+per\s+min(?:ute)?s?"
    r")\b",
    re.IGNORECASE,
)

_PER_DAY_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<mult>[kKmM])?"
    r"\s*(?P<unit>"
    r"reqs?\s*/\s*day"
    r"|requests?\s*/\s*day"
    r"|requests?\s+per\s+day"
    r")\b",
    re.IGNORECASE,
)


def _to_float(num: str, mult: str | None) -> float:
    value = float(num.replace(",", ""))
    if mult:
        value *= _MULTIPLIERS[mult.lower()]
    return value


def parse_stated_rps(text: str | None) -> float | None:
    """Extract a stated request rate in requests/second, if present.

    Only honors an explicit rate unit adjacent to the number. Returns
    None when the text states no parseable rate, so callers skip the
    check instead of failing on prose they cannot read.
    """
    if not text or not text.strip():
        return None
    match = _RATE_RE.search(text)
    if match:
        return _to_float(match.group("num"), match.group("mult"))
    match = _PER_MINUTE_RE.search(text)
    if match:
        return _to_float(match.group("num"), match.group("mult")) / 60.0
    match = _PER_DAY_RE.search(text)
    if match:
        return _to_float(match.group("num"), match.group("mult")) / SECONDS_PER_DAY
    return None


def derived_avg_total_rps(proposal) -> float:
    """Average total (read + write) throughput implied by a proposal."""
    return (
        proposal.daily_active_users
        * proposal.messages_per_user_per_day
        * (1.0 + proposal.read_write_ratio)
        / SECONDS_PER_DAY
    )


def check_workload_consistency(extraction, proposal) -> float | None:
    """Fail loudly on a material stated-vs-derived workload mismatch.

    Returns the stated RPS when one was parsed (whether or not it
    matched), or None when the requirements state no parseable rate.
    Raises ValueError with a diagnostic naming the stated rate, the
    derived average, their ratio, and the three driving proposal
    inputs, when derived average exceeds stated by more than
    MATERIAL_MISMATCH_RATIO.
    """
    traffic = getattr(extraction, "traffic_characteristics", "") or ""
    stated = parse_stated_rps(traffic)
    if stated is None or stated <= 0:
        return None
    derived = derived_avg_total_rps(proposal)
    ratio = derived / stated
    if ratio <= MATERIAL_MISMATCH_RATIO:
        return stated
    raise ValueError(
        "Capacity workload is inconsistent with the stated request "
        f"rate: requirements state ~{stated:.1f} requests/second "
        f"({traffic.strip()[:120]!r}), but the proposal implies "
        f"{derived:.1f} average requests/second "
        f"({ratio:.2f}x the stated rate, limit "
        f"{MATERIAL_MISMATCH_RATIO:.1f}x). Proposal inputs: "
        f"daily_active_users={proposal.daily_active_users}, "
        "messages_per_user_per_day="
        f"{proposal.messages_per_user_per_day} (WRITE-only; reads "
        "are derived via read_write_ratio and must not be included), "
        f"read_write_ratio={proposal.read_write_ratio}."
    )
