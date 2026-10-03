"""M15's cost measurement (spec B3): raw tokens, priced at two reference dates.

    fly ssh console -C "sh -c 'cd /app && python -m app.jobs.measure cost ...'"

Every figure is recomputed from the raw token columns of `spans`. The stored
`cost_usd` was fixed when each span was written, much of it with placeholder
rates, and is never read here. Prices come from `app.obs.pricing.cost_usd`, the
same function the tracer uses, at 15 Dec 2026 and 15 Jan 2027: either side of
the Flash price rise.

Each line of the report is labelled measured, estimate, unmeasured, list
price, or projection. The projection covers v1's pipeline in observe mode,
plus estimates for what M18 switches back on. v2's planner is unknown until
M19 and is not in it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import psycopg

from app.extraction.prompts import CLASSIFY_SYSTEM, EXTRACT_SYSTEM
from app.obs.pricing import PRICING_CHECKED_ON, cost_usd
from app.rag.chunk import MAX_CHARS
from app.rag.search import DEFAULT_LIMIT, SNIPPET_CHARS

REFERENCE_DATES = {
    "2026": datetime(2026, 12, 15, tzinfo=UTC),
    "2027": datetime(2027, 1, 15, tzinfo=UTC),
}

MIN_EXTRACTIONS = 20
"""Below this many extraction spans, an average is not called measured."""

EXTRACTION_OUTPUT_CAP = 4096
"""`max_output_tokens` on the extraction call: the most one can cost in output."""

CHARS_PER_TOKEN = 4
"""The usual rough ratio for English. Used only for estimates, never for
anything labelled measured."""

RECONCILE_TOLERANCE = 0.15

BUDGET_V1_MAX_2027 = 15.0
BUDGET_HOSTING_MAX = 10.0
"""The committed budget thresholds (M15 spec, B4)."""

CORPUS_CHUNKS_PER_MESSAGE = 343 / 272
"""From the v1 corpus (README, M10): 343 chunks from 272 messages."""

EMBEDDING_RATE_PER_MTOK = Decimal("0.20")
"""Gemini Embedding 2's published text rate on 2026-09-30. The configured
`gemini-embedding-001` was not listed on the pricing page that day, so the
listed rate is used: an estimate, and M18 has to check the model's status."""


class UnpricedModelError(RuntimeError):
    """A span's model has no rate. Summing its NULL as zero would understate
    the budget, so the report stops instead."""


@dataclass(frozen=True, slots=True)
class SpanRow:
    trace_id: str
    node: str
    model: str | None
    started_at: datetime
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    thinking_tokens: int
    retry_count: int


@dataclass(frozen=True, slots=True)
class EmbeddingEstimate:
    messages_per_day: float
    """Mail per day that ingestion's query (`DEFAULT_QUERY`) would embed."""

    chunks_per_message: float = CORPUS_CHUNKS_PER_MESSAGE
    tokens_per_chunk: float = MAX_CHARS / CHARS_PER_TOKEN

    def per_month(self) -> float:
        tokens = self.messages_per_day * 30 * self.chunks_per_message * self.tokens_per_chunk
        return float(Decimal(tokens) * EMBEDDING_RATE_PER_MTOK / Decimal(1_000_000))


def refuse_local_database(database_url: str) -> None:
    host = (urlsplit(database_url).hostname or "") if "://" in database_url else ""
    if host in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit(
            "measure cost reads the unattended window's spans, which live in production: "
            "run it on the instance through `fly ssh console` (docs/DEPLOY.md)."
        )


def load_spans(conn: psycopg.Connection, since: datetime, until: datetime) -> list[SpanRow]:
    rows = conn.execute(
        """
        SELECT trace_id::text, node, model, started_at, input_tokens, output_tokens,
               cached_tokens, thinking_tokens, retry_count
          FROM spans
         WHERE started_at >= %s AND started_at < %s
        """,
        (since, until),
    ).fetchall()
    return [SpanRow(*row) for row in rows]


def _price(span: SpanRow, at: datetime) -> float:
    if span.model is None:
        return 0.0
    cost = cost_usd(
        span.model,
        at=at,
        input_tokens=span.input_tokens,
        output_tokens=span.output_tokens,
        cached_tokens=span.cached_tokens,
        thinking_tokens=span.thinking_tokens,
    )
    if cost is None:
        if span.input_tokens or span.output_tokens or span.thinking_tokens:
            raise UnpricedModelError(
                f"No rate for model {span.model!r}: add it to app/obs/pricing.py"
            )
        return 0.0
    return float(cost)


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _prompt_tokens(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN


def cost_report(
    spans: list[SpanRow],
    *,
    inbound_per_day: float,
    hosting_usd: float,
    database_usd: float,
    embeddings: EmbeddingEstimate,
    billed_gemini_usd: float | None,
    extraction_model: str,
) -> dict[str, Any]:
    classify = [s for s in spans if s.node == "classify"]
    extract = [s for s in spans if s.node == "extract"]
    classified_traces = {s.trace_id for s in classify}
    extracted_traces = {s.trace_id for s in extract}

    meeting_rate = len(extracted_traces) / len(classified_traces) if classified_traces else 0.0
    extractions_per_meeting = len(extract) / len(extracted_traces) if extracted_traces else 1.0
    measured = len(extract) >= MIN_EXTRACTIONS

    # The bound for too few extractions: this mailbox's own message size (seen
    # through classify, which reads the same email) with the extraction prompt
    # in place of the classify prompt, and the whole output cap spent.
    mean_classify_input = _mean(s.input_tokens for s in classify) or 0.0
    bound_input = max(
        mean_classify_input - _prompt_tokens(CLASSIFY_SYSTEM) + _prompt_tokens(EXTRACT_SYSTEM),
        _prompt_tokens(EXTRACT_SYSTEM),
    )
    mean_extract_input = _mean(s.input_tokens for s in extract) if measured else bound_input
    search_extra_input = (mean_extract_input or 0.0) + DEFAULT_LIMIT * (
        SNIPPET_CHARS / CHARS_PER_TOKEN
    )

    triage: dict[str, Any] = {"label": "measured" if classify else "unmeasured"}
    extraction: dict[str, Any] = {
        "label": "measured"
        if measured
        else f"unmeasured ({len(extract)} of {MIN_EXTRACTIONS} extractions); upper bound used"
    }
    projection: dict[str, dict[str, float]] = {
        "v1_observe_mode": {},
        "m18_embeddings": {},
        "m18_search_context": {},
        "v1_total": {},
    }

    for year, at in REFERENCE_DATES.items():
        per_triage = _mean(_price(s, at) for s in classify) or 0.0
        triage[year] = per_triage

        if measured:
            per_extraction = _mean(_price(s, at) for s in extract) or 0.0
            extraction[year] = per_extraction
        else:
            bound = cost_usd(
                extraction_model,
                at=at,
                input_tokens=round(bound_input),
                output_tokens=EXTRACTION_OUTPUT_CAP,
            )
            if bound is None:
                raise UnpricedModelError(f"No rate for model {extraction_model!r}")
            per_extraction = float(bound)
            extraction[f"{year}_upper_bound"] = per_extraction

        search = cost_usd(extraction_model, at=at, input_tokens=round(search_extra_input))
        per_search = float(search) if search is not None else 0.0

        meetings_per_month = 30 * inbound_per_day * meeting_rate * extractions_per_meeting
        v1 = 30 * inbound_per_day * per_triage + meetings_per_month * per_extraction
        embed = embeddings.per_month()
        search_month = meetings_per_month * per_search
        projection["v1_observe_mode"][year] = v1
        projection["m18_embeddings"][year] = embed
        projection["m18_search_context"][year] = search_month
        projection["v1_total"][year] = v1 + embed + search_month

    priced_now = sum(_price(s, s.started_at) for s in spans)
    reconciliation: dict[str, Any] = {
        "priced_at_actual_dates": priced_now,
        "billed": billed_gemini_usd,
        "within_tolerance": None,
    }
    if billed_gemini_usd:
        difference = abs(priced_now - billed_gemini_usd) / billed_gemini_usd
        reconciliation["difference"] = difference
        reconciliation["within_tolerance"] = difference <= RECONCILE_TOLERANCE

    fixed = hosting_usd + database_usd
    return {
        "pricing_checked_on": PRICING_CHECKED_ON.isoformat(),
        "spans": len(spans),
        "classifications": len(classify),
        "extractions": len(extract),
        "meeting_rate": meeting_rate,
        "meeting_rate_label": "measured" if classify else "unmeasured",
        "extractions_per_meeting": extractions_per_meeting,
        "triage_per_message": triage,
        "extraction_per_extraction": extraction,
        "retries": sum(s.retry_count for s in spans),
        "hosting_and_database_list_price": {"hosting": hosting_usd, "database": database_usd},
        "projection": projection,
        "labels": {
            "v1_observe_mode": "projection: measured per-message costs x measured volume x 30",
            "m18_embeddings": f"estimate: at ${EMBEDDING_RATE_PER_MTOK}/M tokens (see module doc)",
            "m18_search_context": (
                "estimate: one search per extraction, prompt re-sent; "
                "returns with M19's planner, not M18"
            ),
            "v2_planner": "unknown until M19; not included",
        },
        "reconciliation": reconciliation,
        "decision": {
            "v1_total_2027": projection["v1_total"]["2027"],
            "v1_threshold": BUDGET_V1_MAX_2027,
            "hosting_and_database": fixed,
            "hosting_threshold": BUDGET_HOSTING_MAX,
            # No classifications means nothing was measured: no decision,
            # rather than a "holds" that rests on zeros.
            "budget_holds": None
            if not classify
            else projection["v1_total"]["2027"] <= BUDGET_V1_MAX_2027
            and fixed <= BUDGET_HOSTING_MAX,
        },
    }


def to_markdown(report: dict[str, Any], *, since: datetime, until: datetime) -> str:
    def money(value: float) -> str:
        return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"

    projection = report["projection"]
    extraction = report["extraction_per_extraction"]
    decision = report["decision"]
    rows = [
        f"# Cost, {since:%Y-%m-%d} to {until:%Y-%m-%d}",
        "",
        f"Rates checked {report['pricing_checked_on']}. {report['spans']} spans, "
        f"{report['classifications']} classifications, {report['extractions']} extractions, "
        f"{report['retries']} retries.",
        "",
        "| Line | 2026 rates | 2027 rates | Label |",
        "|---|---|---|---|",
        f"| Triage per message | {money(report['triage_per_message'].get('2026', 0))} | "
        f"{money(report['triage_per_message'].get('2027', 0))} | "
        f"{report['triage_per_message']['label']} |",
    ]
    if "2026" in extraction:
        rows.append(
            f"| Extraction per call | {money(extraction['2026'])} | {money(extraction['2027'])} "
            f"| {extraction['label']} |"
        )
    else:
        rows.append(
            f"| Extraction per call (bound) | {money(extraction['2026_upper_bound'])} | "
            f"{money(extraction['2027_upper_bound'])} | {extraction['label']} |"
        )
    rows += [
        f"| Meeting rate | {report['meeting_rate']:.3f} | | {report['meeting_rate_label']} |",
        f"| v1 per month, observe mode | {money(projection['v1_observe_mode']['2026'])} | "
        f"{money(projection['v1_observe_mode']['2027'])} | projection |",
        f"| + embeddings when M18 re-enables ingestion | "
        f"{money(projection['m18_embeddings']['2026'])} | "
        f"{money(projection['m18_embeddings']['2027'])} | estimate |",
        f"| + search_context when M19's planner brings it back | "
        f"{money(projection['m18_search_context']['2026'])} | "
        f"{money(projection['m18_search_context']['2027'])} | estimate |",
        f"| **v1 total per month** | **{money(projection['v1_total']['2026'])}** | "
        f"**{money(projection['v1_total']['2027'])}** | projection |",
        f"| Hosting + database | {money(decision['hosting_and_database'])} | "
        f"{money(decision['hosting_and_database'])} | list price |",
        "",
        "v2's planner is not included: unknown until M19.",
        "",
    ]
    reconciliation = report["reconciliation"]
    if reconciliation["billed"]:
        state = "within" if reconciliation["within_tolerance"] else "OUTSIDE"
        rows.append(
            f"Reconciliation: spans priced at their own dates "
            f"{money(reconciliation['priced_at_actual_dates'])} vs billed "
            f"{money(reconciliation['billed'])}: {state} 15%."
        )
    else:
        rows.append("Reconciliation: no billed figure given (--billed-gemini-usd).")
    if decision["budget_holds"] is None:
        rows += ["", "**Budget: not decided.** No classifications in the window."]
        return "\n".join(rows) + "\n"
    verdict = "holds" if decision["budget_holds"] else "does NOT hold"
    rows += [
        "",
        f"**Budget {verdict}**: v1 total at 2027 rates {money(decision['v1_total_2027'])} "
        f"(limit {money(decision['v1_threshold'])}); hosting + database "
        f"{money(decision['hosting_and_database'])} "
        f"(limit {money(decision['hosting_threshold'])}).",
    ]
    return "\n".join(rows) + "\n"
