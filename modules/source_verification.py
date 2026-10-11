# source_verification.py
# ============================================================================
# MODULE: Source Verification  (STEPS 1-2 of build)
# ----------------------------------------------------------------------------
# This module fact-checks claims produced by other modules. It does NO new
# analysis; it only tests claims that already exist.
#
# THIS FILE CURRENTLY CONTAINS:
#   STEP 1 — CLAIM EXTRACTOR: reads a saved Macro output (a JSON fixture) so we
#            can develop against a STABLE input with zero Macro/Tavily cost.
#            Pre-selects fact-bearing fields (in Python, for precise
#            source_location traceability), runs ONE leashed Haiku pass per
#            field, then exact-match-dedups the results.
#   STEP 2 — BUDGET GATE + SEARCH PLANNING: chooses which claims are worth
#            spending Tavily credits on (top-5, HIGH-first) and builds one
#            search query per chosen claim. Spends ZERO credits — selection and
#            planning only.
#
# It does NOT search the web and does NOT produce verdicts yet — those are
# later steps (retrieve -> validate -> verdict -> package).
#
# Design principle (the spine of this whole module):
#   The extractor REPORTS what the upstream module asserted. It never edits,
#   resolves, combines, or corrects a claim. Preserving the module's exact
#   wording — including any contradictions — is the entire point.
# ============================================================================

import os
import json
from dotenv import load_dotenv
import anthropic
from tavily import TavilyClient

# Version of this module's logic/output. Bump it (1 -> 2) whenever the logic
# or the shape of the output changes (comment-only edits don't count).
# main.py saves it with every report so old and new results can be told apart.
MODULE_VERSION = "1"

# --- Connect to services ONCE when the module loads (same pattern as Macro) ---
load_dotenv()
_anthropic_client = anthropic.Anthropic()
_MODEL = "claude-haiku-4-5"

# Tavily is built defensively: if the key is missing, the client is None and
# retrieval degrades gracefully (returns "no evidence") instead of crashing.
_tavily_key = os.getenv("TAVILY_API_KEY")
_tavily_client = TavilyClient(api_key=_tavily_key) if _tavily_key else None

# How many results to pull per query. 5 costs the same 1 credit at basic depth
# as 3, and gives the validator more chances to find a clean matching source.
_RESULTS_PER_QUERY = 5

# --- Tolerance table (Decision 6) --------------------------------------------
# How close evidence must be to the claim to count as SUPPORTS. One place to
# tune. All values are v1 starting points, to be revised after real runs.
#   pp  = percentage points (absolute difference)
#   rel = relative percent difference
_TOLERANCES = {
    "revenue_share":   {"type": "pp",  "limit": 5},    # geographic / segment mix
    "market_share":    {"type": "pp",  "limit": 5},
    "growth_rate":     {"type": "pp",  "limit": 3},
    "margin":          {"type": "pp",  "limit": 2},
    "tariff_rate":     {"type": "pp",  "limit": 5},     # policy rates
    "commission_rate": {"type": "pp",  "limit": 5},
    "_default_number": {"type": "rel", "limit": 10},    # large raw magnitudes
}
# Approximate claims (the '~' hedge) get their tolerance multiplied by this.
_APPROX_MULTIPLIER = 1.5

# The five possible final statuses (Decision 4).
_STATUS_VERIFIED = "VERIFIED"
_STATUS_CONTRADICTED = "CONTRADICTED"
_STATUS_UNVERIFIED = "UNVERIFIED"
_STATUS_NOT_VERIFIED_BUDGET = "NOT_VERIFIED_BUDGET"
_STATUS_LOW_SKIPPED = "LOW_SKIPPED"

# --- Budget gate constants (Decision 4) --------------------------------------
# Max claims we will actually search per module run. A CEILING, not a target:
# if only 2 claims qualify, we spend 2. Tunable after watching real runs.
_MAX_CLAIMS_TO_VERIFY = 5

# Ordering of materiality bands, best first. LOW is never searched.
_MATERIALITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


# ============================================================================
# FIELD SELECTION — which parts of the Macro dict we show the extractor.
# ----------------------------------------------------------------------------
# Kept in ONE place so it is easy to tune. Anything not listed here is never
# shown to Haiku, so it can never become a claim. We skip analytical /
# predictive / qualitative fields (summary, customer_impact, direction, the
# verdict) and already-sourced data (fred_data comes from an API, not memory).
# ============================================================================

# Fields INSIDE each macro_points[i] that we DO show the extractor.
_MACRO_POINT_FIELDS = ["external_change", "company_impact"]

# Fields inside company_understanding that we DO show the extractor.
_COMPANY_UNDERSTANDING_FIELDS = [
    "business_model", "revenue_drivers", "cost_structure", "geographic_exposure",
]

# Fields inside each drivers[i] that we DO show the extractor.
_DRIVER_FIELDS = ["materiality_rationale"]


def _collect_fields(macro_output):
    """Walk the Macro dict and gather (source_location, text) pairs to extract from.

    Returns a list of dicts: {"source_location": str, "text": str}
    Each source_location is a precise, human-readable pointer we STAMP ourselves
    from Python (never trusting Haiku to report where a claim came from).
    """
    fields = []

    # --- macro_points: a list; index each one so locations are unique ---
    for i, point in enumerate(macro_output.get("macro_points", [])):
        for field_name in _MACRO_POINT_FIELDS:
            text = point.get(field_name, "")
            if text and text.strip():
                fields.append({
                    "source_location": f"macro_points[{i}].{field_name}",
                    "text": text.strip(),
                })

    # --- company_understanding: a single dict of named fields ---
    cu = macro_output.get("company_understanding", {})
    for field_name in _COMPANY_UNDERSTANDING_FIELDS:
        text = cu.get(field_name, "")
        if text and text.strip():
            fields.append({
                "source_location": f"company_understanding.{field_name}",
                "text": text.strip(),
            })

    # --- drivers: a list; index each one ---
    for i, driver in enumerate(macro_output.get("drivers", [])):
        for field_name in _DRIVER_FIELDS:
            text = driver.get(field_name, "")
            if text and text.strip():
                fields.append({
                    "source_location": f"drivers[{i}].{field_name}",
                    "text": text.strip(),
                })

    return fields


# ============================================================================
# THE LEASHED EXTRACTION PASS — one call per field.
# ============================================================================
_EXTRACTION_SYSTEM_PROMPT = (
    "You are a claim-extraction engine for an investment research system. Your ONE job "
    "is to read a short piece of text written by another analysis module and pull out the "
    "atomic FACTUAL claims inside it. You do NOT verify them. You do NOT judge whether they "
    "are true. You do NOT add, improve, combine, resolve, or correct anything. You report "
    "ONLY what the text actually asserts.\n\n"

    "For EACH candidate statement, decide one of three actions:\n"
    "  EXTRACT   — it is a checkable factual claim (see the 5-part test).\n"
    "  SKIP      — it is analysis, opinion, prediction, or too vague to test.\n"
    "  AMBIGUOUS — it contains conflicting values that CANNOT be separated without "
    "guessing the author's intent.\n\n"

    "A statement is EXTRACT only if ALL FIVE hold:\n"
    "  1. FACTUAL — presented as fact, not opinion or analysis.\n"
    "  2. PAST OR PRESENT — not a prediction or future expectation.\n"
    "  3. EXTERNALLY CHECKABLE — another source could confirm or contradict it.\n"
    "  4. SPECIFIC ENOUGH TO TEST — has a measurable value, date, named event, "
    "requirement, or status.\n"
    "  5. RELEVANT — about this company or its market, not a generic economic truth.\n\n"

    "CRITICAL RULES:\n"
    "  - NEVER infer missing context. If two numbers conflict and the text does NOT state "
    "a distinguishing period/methodology/definition, mark the statement AMBIGUOUS. Do NOT "
    "invent a reason they could both be true.\n"
    "  - If two numbers ARE distinguished in the text (different periods, methodologies, "
    "definitions, or populations), extract them as SEPARATE claims, and set "
    "relationship_flag to 'POTENTIAL_CONFLICT' on each.\n"
    "  - A reported action or announcement with a specific, dated, named commitment IS "
    "extractable (claim_nature='reported_status') — you are recording that it was "
    "announced, not that it will happen. Vague talks or intentions with no concrete terms "
    "are SKIP.\n"
    "  - A number is NOT automatically factual: an impact estimate or projection (e.g. "
    "'could reduce margin by 200 basis points') is a PREDICTION — SKIP it.\n"
    "  - SCALE WORDS ARE PART OF THE VALUE. If a number carries a scale word (million, "
    "billion, thousand, lakh, crore), the 'value' field MUST reflect the FULL number. "
    "'12.4 million units' -> value 12400000, not 12.4. '100M chips' -> value 100000000. "
    "Keep original_text exactly as written ('12.4M units'), but make value the fully "
    "expanded number.\n"
    "  - Preserve hedges. If the text says '~20%', keep precision='approximate' and record "
    "original_text exactly as written.\n\n"

    "MATERIALITY: for each EXTRACTED claim, tag how much the analysis would change if the "
    "claim were FALSE — assessed against the claim AS STATED, never against whether you "
    "think it is true:\n"
    "  HIGH   — if false, a meaningful part of the analysis becomes unreliable.\n"
    "  MEDIUM — if false, a supporting point weakens but the analysis survives.\n"
    "  LOW    — if false, the analysis is essentially unchanged.\n\n"

    "You respond with ONLY valid JSON. No markdown, no preamble, no explanation."
)

_EXTRACTION_WORKED_EXAMPLE = """
Example. For this input text:
"Apple's China market share rose from 15% in Q2 2025 to 19% in Q2 2026, and Apple could gain further share as AI features roll out."

Correct output:
{
  "claims": [
    {
      "action": "EXTRACT",
      "claim": "Apple's China market share was 15% in Q2 2025",
      "metric": "market_share",
      "value": 15,
      "unit": "%",
      "original_text": "15% in Q2 2025",
      "precision": "exact",
      "period": "Q2 2025",
      "claim_nature": "measurement",
      "materiality": "HIGH",
      "relationship_flag": "POTENTIAL_CONFLICT"
    },
    {
      "action": "EXTRACT",
      "claim": "Apple's China market share was 19% in Q2 2026",
      "metric": "market_share",
      "value": 19,
      "unit": "%",
      "original_text": "19% in Q2 2026",
      "precision": "exact",
      "period": "Q2 2026",
      "claim_nature": "measurement",
      "materiality": "HIGH",
      "relationship_flag": "POTENTIAL_CONFLICT"
    },
    {
      "action": "SKIP",
      "claim": "Apple could gain further share as AI features roll out",
      "reason": "prediction / forward-looking"
    }
  ]
}
"""

_VALID_ACTIONS = {"EXTRACT", "SKIP", "AMBIGUOUS"}
_VALID_MATERIALITY = {"HIGH", "MEDIUM", "LOW"}


def _extract_from_text(source_location, text, verbose=False):
    """Run ONE leashed Haiku pass over a single field's text.

    Returns a list of raw claim dicts (each still carrying its 'action').
    Never raises: on any failure returns [] so one bad field can't kill the run.
    """
    user_prompt = f"""
Extract the atomic factual claims from the text below.

TEXT (from {source_location}):
"{text}"

Return ONLY JSON in this exact shape:
{{
  "claims": [
    {{
      "action": "EXTRACT" | "SKIP" | "AMBIGUOUS",
      "claim": "the claim or statement, quoted faithfully from the text",
      "metric": "short label e.g. revenue_share, market_share, growth_rate, margin, tariff_rate, raw_number, date, event",
      "value": number or null,
      "unit": "% | USD | units | null",
      "original_text": "the exact snippet as written, including any ~",
      "precision": "exact" | "approximate",
      "period": "the stated period e.g. 'Q2 2025', 'FY2025', 'January 2026', or 'unknown'",
      "claim_nature": "measurement" | "event" | "reported_status",
      "materiality": "HIGH" | "MEDIUM" | "LOW",
      "relationship_flag": "NONE" | "POTENTIAL_CONFLICT"
    }}
  ]
}}

For SKIP or AMBIGUOUS items, include "action", "claim", and a short "reason";
the other fields may be omitted.

{_EXTRACTION_WORKED_EXAMPLE}

Now extract from the text above. Return only the JSON.
"""

    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            temperature=0.2,
            system=_EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(cleaned)

        claims = result.get("claims", [])
        if not isinstance(claims, list):
            raise ValueError("'claims' was not a list.")

        # Validate each claim's action; drop anything malformed rather than crash.
        valid_claims = []
        for c in claims:
            action = c.get("action", "")
            if action not in _VALID_ACTIONS:
                if verbose:
                    print(f"    (dropped a claim with bad action: {action!r})")
                continue
            # Stamp the source_location OURSELVES — never trust the model for it.
            c["source_location"] = source_location
            valid_claims.append(c)

        if verbose:
            kept = sum(1 for c in valid_claims if c["action"] == "EXTRACT")
            print(f"  {source_location}: {len(valid_claims)} statements ({kept} EXTRACT)")
        return valid_claims

    except Exception as e:
        if verbose:
            print(f"  {source_location}: extraction failed ({e}) — skipped.")
        return []


# ============================================================================
# EXACT-MATCH DEDUP — collapse identical facts so we don't spend budget twice.
# ----------------------------------------------------------------------------
# Two EXTRACT claims are "the same" ONLY if metric + value + period all match
# exactly. This is deliberately strict: it will NOT guess that near-duplicates
# are equal (that semantic dedup is deferred). The first occurrence is kept as
# primary; each later duplicate's source_location is recorded in
# 'also_asserted_at' so the audit trail survives — we never silently drop a
# claim. SKIP / AMBIGUOUS claims are left untouched. Valueless claims (value is
# None) are NOT deduped, because metric+period alone is too blunt a key.
# ============================================================================
def _dedup_extracted(claims, verbose=False):
    """Collapse exact-duplicate EXTRACT claims; preserve their locations."""
    seen = {}          # key -> the primary claim dict we keep
    deduped = []       # the output list, in original order

    for c in claims:
        # Only EXTRACT claims are deduped; everything else passes straight through.
        if c.get("action") != "EXTRACT":
            deduped.append(c)
            continue

        # Only dedup claims that carry a REAL value. When value is None the key
        # collapses to metric+period, which is too blunt and wrongly merges
        # distinct valueless facts (e.g. two different regulatory statuses). We
        # only ever merge PROVABLE duplicates, so valueless claims pass through.
        if c.get("value") is None:
            deduped.append(c)
            continue

        # The identity of a claim: same metric, value, and period = same fact.
        key = (c.get("metric"), c.get("value"), c.get("period"))

        if key in seen:
            # We've seen this exact fact — fold this one into the primary.
            primary = seen[key]
            primary.setdefault("also_asserted_at", []).append(c.get("source_location"))
            if verbose:
                print(f"  dedup: {c.get('source_location')} folded into "
                      f"{primary.get('claim_id', '(pending id)')} "
                      f"({c.get('metric')}={c.get('value')})")
        else:
            # First time we've seen this fact — keep it as the primary.
            seen[key] = c
            deduped.append(c)

    return deduped


# ============================================================================
# STEP 1 PUBLIC ENTRY — extract all claims from a Macro output dict.
# ============================================================================
def extract_claims(macro_output, source_module="macro", verbose=False):
    """Extract every claim from a Macro output dict.

    Returns a list of claim dicts, each with a stable claim_id and its action
    (EXTRACT / SKIP / AMBIGUOUS). No searching, no verdicts — Step 1 only.
    """
    fields = _collect_fields(macro_output)
    if verbose:
        print(f"Selected {len(fields)} fields to extract from.\n")

    all_claims = []
    for field in fields:
        claims = _extract_from_text(field["source_location"], field["text"], verbose=verbose)
        all_claims.extend(claims)

    # Assign stable IDs across ALL statements FIRST (Convention A: skips get IDs
    # too, so every extractor decision is permanently addressable). We ID before
    # dedup so the primary claim already has its id when duplicates fold into it.
    for i, c in enumerate(all_claims, start=1):
        c["claim_id"] = f"{source_module}_{i:03d}"

    # Collapse exact-duplicate EXTRACT claims (keeps budget from being spent
    # twice on the same fact); duplicates' locations are preserved.
    if verbose:
        print("\nRunning exact-match dedup...")
    all_claims = _dedup_extracted(all_claims, verbose=verbose)

    return all_claims


# ============================================================================
# STEP 2a — BUDGET GATE: choose which claims get searched.
# ----------------------------------------------------------------------------
# Sort EXTRACT claims HIGH-first (preserving the extractor's original order
# within each band — the locked tie-break), then take the top N. Claims beyond
# the cap are over budget (later tagged NOT_VERIFIED_BUDGET): worth checking,
# but the budget stopped us. LOW claims are never searched. Nothing is deleted —
# every claim keeps a visible status so the audit trail stays honest.
# ============================================================================
def apply_budget_gate(claims, max_claims=_MAX_CLAIMS_TO_VERIFY, verbose=False):
    """Split EXTRACT claims into (to_search, over_budget, low). Returns all three.

    Only EXTRACT claims are considered. HIGH before MEDIUM; LOW never searched.
    Original order is preserved within each band via a STABLE sort.
    """
    # Pull just the EXTRACT claims; the rest (SKIP/AMBIGUOUS) aren't searchable.
    extract_claims_list = [c for c in claims if c.get("action") == "EXTRACT"]

    # LOW is never searched — separate those out immediately.
    searchable = [c for c in extract_claims_list if c.get("materiality") != "LOW"]
    low = [c for c in extract_claims_list if c.get("materiality") == "LOW"]

    # Sort by materiality band FIRST, then numeric-first within the band. A claim
    # carrying a real value (a checkable number) is both more material and more
    # verifiable than a vague valueless status, so it should win a search slot.
    # Python's sort is stable, so ties beyond these keys keep extractor order —
    # preserving the locked "extractor order within band" rule as the final tie-break.
    def _sort_key(c):
        band = _MATERIALITY_ORDER.get(c.get("materiality"), 99)
        has_value = 0 if c.get("value") is not None else 1  # 0 sorts before 1
        return (band, has_value)

    searchable.sort(key=_sort_key)

    to_search = searchable[:max_claims]
    over_budget = searchable[max_claims:]

    if verbose:
        print(f"\nBudget gate: {len(searchable)} searchable "
              f"({sum(1 for c in searchable if c.get('materiality') == 'HIGH')} HIGH), "
              f"cap {max_claims}.")
        print(f"  -> {len(to_search)} will be searched, "
              f"{len(over_budget)} over budget, {len(low)} LOW (never searched).")

    return to_search, over_budget, low


# ============================================================================
# STEP 2b — SEARCH PLANNING: build one query per claim (NO credits spent).
# ----------------------------------------------------------------------------
# Deterministic Python string assembly — no LLM. Cheap, repeatable, and easy to
# eyeball before we spend a single Tavily credit. We attach the query to each
# claim as 'search_query' so the next step (retrieve) just reads it.
# ============================================================================
def plan_searches(claims, company_name, verbose=False):
    """Attach a 'search_query' string to each claim. Returns the same list."""
    for c in claims:
        # Start from the faithful claim text, which already names the subject.
        claim_text = c.get("claim", "")

        # Build a compact, source-seeking query: company + claim subject + period.
        # Only prepend the company name if the claim doesn't already start with it,
        # so we don't get "Apple Apple's China market share...".
        if claim_text.lower().startswith(company_name.lower()):
            parts = [claim_text]
        else:
            parts = [company_name, claim_text]

        period = c.get("period", "unknown")
        if period and period != "unknown":
            parts.append(period)

        # A light nudge toward authoritative sources for numeric facts.
        if c.get("value") is not None:
            parts.append("official figures")

        query = " ".join(parts)

        # Tavily queries work best short; trim to a sane length.
        c["search_query"] = query[:200]

        if verbose:
            print(f"  {c.get('claim_id')}: {c['search_query']}")

    return claims

# ============================================================================
# STEP 3 — RETRIEVE EVIDENCE: run each claim's query through Tavily.
# ----------------------------------------------------------------------------
# THIS STEP SPENDS CREDITS: one basic-depth search (1 credit) per claim.
# It attaches the raw search results to each claim as 'evidence_results'. It
# does NOT judge them — that's the validator's job (Step 4). A search that
# returns nothing yields a clean empty list (a meaningful result: possibly
# unverifiable), never a crash.
# ============================================================================
def retrieve_evidence(claims, verbose=False):
    """For each claim, run its search_query through Tavily and attach results.

    Adds 'evidence_results' to each claim: a list of
    {title, url, content, score} dicts (possibly empty). Returns the same list.
    Never raises: a failed search yields [] for that claim.
    """
    if _tavily_client is None:
        if verbose:
            print("  Tavily unavailable (no TAVILY_API_KEY) — no evidence retrieved.")
        for c in claims:
            c["evidence_results"] = []
        return claims

    for c in claims:
        query = c.get("search_query", "")
        if not query:
            c["evidence_results"] = []
            continue

        if verbose:
            print(f"  Searching [{c.get('claim_id')}]: {query}")

        try:
            response = _tavily_client.search(
                query=query,
                search_depth="basic",       # 1 credit — explicit, never auto-upgrade
                max_results=_RESULTS_PER_QUERY,
            )
            results = []
            for r in response.get("results", []):
                results.append({
                    "title": r.get("title", "No title"),
                    "url": r.get("url", "No URL"),
                    "content": r.get("content", ""),
                    "score": r.get("score"),   # Tavily's own relevance score
                })
            c["evidence_results"] = results

            if verbose:
                print(f"    -> {len(results)} results")

        except Exception as e:
            # A failed search is "no evidence", not a crash.
            if verbose:
                print(f"    -> search FAILED ({e}) — treating as no evidence.")
            c["evidence_results"] = []

    return claims

# ============================================================================
# STEP 4 — VALIDATE: judge each claim against its single best-matching result.
# ----------------------------------------------------------------------------
# A leashed Haiku pass that (1) picks the ONE retrieved result that best matches
# the claim, (2) checks the four gates (entity, metric, period, unit), and only
# then (3) judges the evidence relationship (SUPPORTS/CONTRADICTS/NON_COMPARABLE)
# using our tolerance table. The relationship maps to a final status.
#
# THE ABSOLUTE LEASH: the model may judge ONLY what the provided evidence text
# says. It must NOT use its own knowledge. If the evidence doesn't state it, it
# is not verified — this is what prevents circular verification.
#
# HARD RULE: VERIFIED or CONTRADICTED requires a real source_url + evidence, or
# we downgrade to UNVERIFIED. A verdict can never be sourceless.
# ============================================================================
_VALIDATION_SYSTEM_PROMPT = (
    "You are an evidence-validation engine for an investment research fact-checker. "
    "You are given ONE factual claim and a small set of web search results. Your job is "
    "to decide whether the PROVIDED EVIDENCE supports, contradicts, or cannot test the "
    "claim.\n\n"

    "THE ABSOLUTE RULE: judge ONLY what is written in the provided evidence text. NEVER "
    "use your own knowledge or memory. If the evidence does not actually state the fact, "
    "the claim is NOT verified — no matter how true it may seem to you. Confirming a claim "
    "from your own memory instead of the evidence is the single worst error you can make.\n\n"

    "PROCESS, in strict order:\n"
    "1. SELECT the SINGLE result that best addresses this exact claim. If none of the "
    "results actually address it, say so (best_result_index = null).\n"
    "2. Run the FOUR GATES against that one result. ALL must pass before any value "
    "comparison:\n"
    "   - ENTITY: is it about the same company/subject?\n"
    "   - METRIC: is it the SAME measure? (revenue share != market share != shipment "
    "growth != employee share. These are DIFFERENT metrics — do not treat them as equal.) "
    "BEWARE the subtle trap: two figures can both be a percentage about the same region "
    "(e.g. 'Greater China revenue share' vs 'China smartphone market share') and still be "
    "DIFFERENT metrics. A revenue-mix percentage is NOT a market-share percentage. If the "
    "evidence measures a different metric than the claim — even if both are percentages "
    "about the same place — the metric gate FAILS and the result is NON_COMPARABLE.\n"
    "   - PERIOD: is it the same time period? If the claim has a period and the evidence "
    "clearly differs, that is NON_COMPARABLE. If the evidence states no period, treat "
    "period as unknown — NOT automatically comparable.\n"
    "   - UNIT: same unit/definition? (a percentage is not a count; USD is not units.)\n"
    "3. Only if all four gates pass, compare the values and decide the relationship:\n"
    "   - SUPPORTS: the evidence value matches the claim within tolerance.\n"
    "   - CONTRADICTS: the evidence value is clearly outside tolerance.\n"
    "   - NON_COMPARABLE: a gate failed (different metric, period, unit, or entity), so "
    "the evidence does not actually test the claim.\n\n"
        "CONFLICTING EVIDENCE: after choosing your best result, glance at the OTHER results. "
    "If they disagree with your chosen one about the same fact (some support, some "
    "contradict, or the numbers scatter), the evidence is genuinely mixed — do NOT return "
    "a confident VERIFIED. Either lower confidence to LOW, or if the claim as worded is "
    "only partly borne out, prefer NON_COMPARABLE. A claim is only VERIFIED when the "
    "evidence is consistent, not when one cherry-picked snippet happens to fit.\n\n"
    "NUMERIC vs NON-NUMERIC claims:\n"
    "   - If the claim has a numeric value, DO NOT decide SUPPORTS/CONTRADICTS yourself. "
    "Instead, extract the EVIDENCE VALUE: the single number the chosen source states for "
    "this exact metric/period (as a plain number, same unit as the claim). A separate "
    "system compares it to the claim within tolerance. If the four gates fail, or the "
    "source states no comparable number, set evidence_value to null.\n"
    "   - If the claim is NON-numeric (an event, announcement, or status), THEN you decide "
    "the relationship: SUPPORTS if the evidence confirms the event/status happened as "
    "described, CONTRADICTS if it states otherwise, NON_COMPARABLE if the evidence does not "
    "address it. Leave evidence_value null for these.\n\n"

    "You respond with ONLY valid JSON. No markdown, no preamble, no explanation."
)


def _tolerance_for(claim):
    """Return a short human-readable tolerance hint for the prompt."""
    metric = claim.get("metric", "")
    rule = _TOLERANCES.get(metric, _TOLERANCES["_default_number"])
    limit = rule["limit"]
    if claim.get("precision") == "approximate":
        limit = limit * _APPROX_MULTIPLIER
    if rule["type"] == "pp":
        return f"within {limit} percentage points"
    return f"within {limit}% relative"

def _compare_values(claim, evidence_value):
    """Deterministically decide SUPPORTS / CONTRADICTS / NON_COMPARABLE for a
    NUMERIC claim, using our tolerance table. Python does the arithmetic — the
    LLM is not trusted with the comparison. Returns a relationship string.

    NON_COMPARABLE if either value is missing (can't compare what we don't have).
    """
    claim_value = claim.get("value")
    if claim_value is None or evidence_value is None:
        return "NON_COMPARABLE"

    try:
        claim_value = float(claim_value)
        evidence_value = float(evidence_value)
    except (TypeError, ValueError):
        return "NON_COMPARABLE"

    # Look up the tolerance for this metric; apply the approximate multiplier.
    rule = _TOLERANCES.get(claim.get("metric"), _TOLERANCES["_default_number"])
    limit = rule["limit"]
    if claim.get("precision") == "approximate":
        limit = limit * _APPROX_MULTIPLIER

    if rule["type"] == "pp":
        # Percentage-point metrics: absolute difference in points.
        difference = abs(claim_value - evidence_value)
        within = difference <= limit
    else:
        # Large raw magnitudes: relative percent difference vs the claim value.
        if claim_value == 0:
            within = (evidence_value == 0)
        else:
            rel_diff = abs(claim_value - evidence_value) / abs(claim_value) * 100
            within = rel_diff <= limit

    return "SUPPORTS" if within else "CONTRADICTS"

def _validate_one(claim, verbose=False):
    """Validate a single claim against its retrieved evidence. Returns a
    'verification' dict. Never raises."""
    results = claim.get("evidence_results", [])

    # No evidence at all -> honest UNVERIFIED, no source.
    if not results:
        return {
            "status": _STATUS_UNVERIFIED,
            "evidence_relationship": None,
            "evidence": None,
            "source_url": None,
            "search_quality": "LOW",
            "confidence": "LOW",
            "skip_reason": None,
        }

    # Build a compact, numbered evidence block for the prompt.
    evidence_lines = []
    for i, r in enumerate(results):
        snippet = r.get("content", "")[:400].replace("\n", " ")
        evidence_lines.append(f"[{i}] {r.get('title','')} — {snippet} (url: {r.get('url','')})")
    evidence_block = "\n".join(evidence_lines)

    tolerance_hint = _tolerance_for(claim)

    user_prompt = f"""
CLAIM TO CHECK:
"{claim.get('claim')}"
  metric: {claim.get('metric')}   value: {claim.get('value')} {claim.get('unit')}
  period: {claim.get('period')}   precision: {claim.get('precision')}
  claim_nature: {claim.get('claim_nature')}
  tolerance for this claim: {tolerance_hint}

SEARCH RESULTS (judge ONLY from this text):
{evidence_block}

Return ONLY JSON in this exact shape:
{{
  "best_result_index": <number 0-{len(results)-1}, or null if none address the claim>,
  "gate_entity": "pass" | "fail",
  "gate_metric": "pass" | "fail",
  "gate_period": "pass" | "fail" | "unknown",
  "gate_unit": "pass" | "fail",
  "evidence_value": <the number the source states for this metric/period, or null>,
  "evidence_relationship": "SUPPORTS" | "CONTRADICTS" | "NON_COMPARABLE (only decide this for NON-numeric claims; for numeric claims leave it as null)",
  "evidence_quote": "the exact sentence/phrase from the chosen result that decides it",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reasoning": "one short sentence, grounded ONLY in the evidence text"
}}
"""

    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=800,
            temperature=0.2,
            system=_VALIDATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        v = json.loads(cleaned)

        idx = v.get("best_result_index")

        # Decide the evidence relationship.
        # - NUMERIC claims: Python computes it deterministically from the value
        #   the LLM found (the LLM is NOT trusted with the arithmetic).
        # - NON-numeric claims (value is None): the LLM's relationship stands.
        if claim.get("value") is not None:
            evidence_value = v.get("evidence_value")
            # If gates failed, the LLM should have nulled the value; treat a
            # failed metric/entity/unit gate as NON_COMPARABLE regardless.
            gate_fail = "fail" in (v.get("gate_entity"), v.get("gate_metric"),
                                   v.get("gate_unit"))
            if gate_fail:
                rel = "NON_COMPARABLE"
            else:
                rel = _compare_values(claim, evidence_value)
        else:
            rel = v.get("evidence_relationship")

        # Map relationship -> final status.
        if rel == "SUPPORTS":
            status = _STATUS_VERIFIED
        elif rel == "CONTRADICTS":
            status = _STATUS_CONTRADICTED
        else:  # NON_COMPARABLE or anything unexpected
            status = _STATUS_UNVERIFIED

        # Resolve the chosen source URL/snippet (only if a real result was picked).
        source_url = None
        evidence_text = None
        if isinstance(idx, int) and 0 <= idx < len(results):
            source_url = results[idx].get("url")
            evidence_text = v.get("evidence_quote") or results[idx].get("content", "")[:300]

        # HARD RULE: VERIFIED/CONTRADICTED must have a real source + evidence.
        # If not, downgrade to UNVERIFIED — a verdict can never be sourceless.
        if status in (_STATUS_VERIFIED, _STATUS_CONTRADICTED):
            if not source_url or not evidence_text:
                if verbose:
                    print(f"    downgraded {status} -> UNVERIFIED (no real source).")
                status = _STATUS_UNVERIFIED
                source_url = None
                evidence_text = None

        # Simple search_quality: strong if a clear source was chosen and gates held.
        gates = [v.get("gate_entity"), v.get("gate_metric"),
                 v.get("gate_unit")]
        if source_url and all(g == "pass" for g in gates):
            search_quality = "HIGH"
        elif source_url:
            search_quality = "MEDIUM"
        else:
            search_quality = "LOW"

        if verbose:
            print(f"    {claim.get('claim_id')}: {status} "
                  f"(rel={rel}, quality={search_quality})")

        return {
            "status": status,
            "evidence_relationship": rel,
            "evidence": evidence_text,
            "source_url": source_url,
            "source_type": None,   # (source-type classification deferred)
            "search_quality": search_quality,
            "confidence": v.get("confidence", "LOW"),
            "skip_reason": None,
            "gates": {
                "entity": v.get("gate_entity"),
                "metric": v.get("gate_metric"),
                "period": v.get("gate_period"),
                "unit": v.get("gate_unit"),
            },
            "reasoning": v.get("reasoning"),
        }

    except Exception as e:
        if verbose:
            print(f"    {claim.get('claim_id')}: validation failed ({e}) -> UNVERIFIED.")
        return {
            "status": _STATUS_UNVERIFIED,
            "evidence_relationship": None,
            "evidence": None,
            "source_url": None,
            "search_quality": "LOW",
            "confidence": "LOW",
            "skip_reason": None,
        }


def validate_claims(claims, verbose=False):
    """Validate each searched claim; attach a 'verification' dict to each."""
    for c in claims:
        c["verification"] = _validate_one(c, verbose=verbose)
    return claims

# ============================================================================
# STEP 5 — PACKAGE: assemble everything into the locked output schema.
# ----------------------------------------------------------------------------
# Pure assembly, no LLM, no credits. Takes every claim (searched, over-budget,
# skipped) and emits the frozen Decision-7 schema Report Generation will read.
# Enforces the hard rule: VERIFIED/CONTRADICTED must carry a real source_url.
# ============================================================================
def _build_extracted_block(claim):
    """The 'extracted' half of a claim record (what the extractor produced)."""
    return {
        "claim": claim.get("claim"),
        "metric": claim.get("metric"),
        "value": claim.get("value"),
        "unit": claim.get("unit"),
        "precision": claim.get("precision"),
        "period": claim.get("period"),
        "claim_nature": claim.get("claim_nature"),
        "materiality": claim.get("materiality"),
        "relationship_flag": claim.get("relationship_flag", "NONE"),
        "also_asserted_at": claim.get("also_asserted_at", []),
    }


def _empty_verification(status, skip_reason=None):
    """A verification block for claims that were never searched."""
    return {
        "status": status,
        "evidence_relationship": None,
        "evidence": None,
        "source_url": None,
        "source_type": None,
        "search_quality": None,
        "confidence": None,
        "skip_reason": skip_reason,
    }


def package_results(macro_output, searched, over_budget, low, skipped_ambiguous,
                    verbose=False):
    """Assemble all claim groups into the locked output schema.

    - searched:          claims that went through validation (have 'verification')
    - over_budget:       EXTRACT claims the cap excluded -> NOT_VERIFIED_BUDGET
    - low:               LOW-materiality EXTRACT claims never searched -> LOW_SKIPPED
    - skipped_ambiguous: SKIP / AMBIGUOUS claims from extraction
    """
    claims_checked = []

    # --- searched claims: already carry a 'verification' dict from Step 4 ---
    for c in searched:
        v = c.get("verification", {})

        # HARD RULE (belt-and-suspenders): a VERIFIED/CONTRADICTED verdict must
        # have a real source_url + evidence, or we downgrade to UNVERIFIED. The
        # validator already enforces this, but packaging is the final gate.
        if v.get("status") in (_STATUS_VERIFIED, _STATUS_CONTRADICTED):
            if not v.get("source_url") or not v.get("evidence"):
                if verbose:
                    print(f"  package: downgraded {c.get('claim_id')} "
                          f"{v.get('status')} -> UNVERIFIED (no source).")
                v["status"] = _STATUS_UNVERIFIED
                v["source_url"] = None
                v["evidence"] = None

        claims_checked.append({
            "claim_id": c.get("claim_id"),
            "source_location": c.get("source_location"),
            "extracted": _build_extracted_block(c),
            "verification": v,
        })

    # --- over-budget: worth checking, but the cap stopped us ---
    for c in over_budget:
        claims_checked.append({
            "claim_id": c.get("claim_id"),
            "source_location": c.get("source_location"),
            "extracted": _build_extracted_block(c),
            "verification": _empty_verification(
                _STATUS_NOT_VERIFIED_BUDGET, skip_reason="BUDGET_LIMIT"),
        })

    # --- LOW: deliberately not worth spending on ---
    for c in low:
        claims_checked.append({
            "claim_id": c.get("claim_id"),
            "source_location": c.get("source_location"),
            "extracted": _build_extracted_block(c),
            "verification": _empty_verification(
                _STATUS_LOW_SKIPPED, skip_reason="LOW_MATERIALITY"),
        })

    # --- SKIP / AMBIGUOUS: never became verifiable claims at all ---
    for c in skipped_ambiguous:
        reason = "AMBIGUOUS" if c.get("action") == "AMBIGUOUS" else "NOT_FACTUAL"
        claims_checked.append({
            "claim_id": c.get("claim_id"),
            "source_location": c.get("source_location"),
            "extracted": _build_extracted_block(c),
            "verification": _empty_verification(
                _STATUS_LOW_SKIPPED, skip_reason=reason),
        })

    # --- summary counts ---
    def count(status):
        return sum(1 for c in claims_checked
                   if c["verification"]["status"] == status)

    summary = {
        "total_extracted": len(searched) + len(over_budget) + len(low),
        "verified": count(_STATUS_VERIFIED),
        "contradicted": count(_STATUS_CONTRADICTED),
        "unverified": count(_STATUS_UNVERIFIED),
        "not_verified_budget": count(_STATUS_NOT_VERIFIED_BUDGET),
        "low_skipped": count(_STATUS_LOW_SKIPPED),
    }

    return {
        "company_name": macro_output.get("company_name"),
        "source_module": "macro",
        "summary": summary,
        "claims_checked": claims_checked,
    }

    
# ============================================================================
# PUBLIC ENTRY POINT — the one function Report Generation calls.
# ----------------------------------------------------------------------------
# Takes a Macro output dict, runs the full pipeline, returns the locked schema.
# Reads company_name FROM THE DATA (no hardcoding). Spends Tavily credits (one
# search per gated claim) and Haiku calls (extraction + validation).
# ============================================================================
def verify_macro_claims(macro_output, verbose=False):
    """Run the full Source Verification pipeline on one Macro output dict."""
    company_name = macro_output.get("company_name", "the company")

    # Step 1 — extract every claim (EXTRACT / SKIP / AMBIGUOUS).
    all_claims = extract_claims(macro_output, source_module="macro", verbose=verbose)

    # Separate the rejected (SKIP/AMBIGUOUS) from the real EXTRACT claims.
    skipped_ambiguous = [c for c in all_claims if c.get("action") != "EXTRACT"]

    # Step 2a — budget gate (returns searched / over-budget / low, all EXTRACT).
    to_search, over_budget, low = apply_budget_gate(all_claims, verbose=verbose)

    # Step 2b — plan the queries (no credits).
    plan_searches(to_search, company_name=company_name, verbose=verbose)

    # Step 3 — retrieve evidence (SPENDS CREDITS).
    retrieve_evidence(to_search, verbose=verbose)

    # Step 4 — validate against evidence.
    validate_claims(to_search, verbose=verbose)

    # Step 5 — package into the locked schema.
    return package_results(
        macro_output, to_search, over_budget, low, skipped_ambiguous,
        verbose=verbose)

# ============================================================================
# TEST BLOCK — runs only when this file is executed directly.
# Loads the saved Apple fixture and runs Steps 1-2 on it (no Macro re-run,
# no Tavily credits spent).
# ============================================================================
if __name__ == "__main__":
    fixture_path = "macro_fixture_infosys.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        macro_output = json.load(f)

    print("=" * 70)
    print("  SOURCE VERIFICATION — FULL PIPELINE (public entry point)")
    print("=" * 70 + "\n")

    result = verify_macro_claims(macro_output, verbose=True)

    # --- Summary ---
    s = result["summary"]
    print("\n" + "=" * 70)
    print(f"  RESULT: {result['company_name']} ({result['source_module']})")
    print("=" * 70)
    print(f"  total extracted     : {s['total_extracted']}")
    print(f"  VERIFIED            : {s['verified']}")
    print(f"  CONTRADICTED        : {s['contradicted']}")
    print(f"  UNVERIFIED          : {s['unverified']}")
    print(f"  NOT_VERIFIED_BUDGET : {s['not_verified_budget']}")
    print(f"  LOW_SKIPPED         : {s['low_skipped']}")

    # --- The verdicts that were actually checked ---
    print("\n" + "=" * 70)
    print("  CHECKED CLAIMS (searched)")
    print("=" * 70)
    for c in result["claims_checked"]:
        v = c["verification"]
        if v["status"] in (_STATUS_VERIFIED, _STATUS_CONTRADICTED, _STATUS_UNVERIFIED):
            e = c["extracted"]
            print(f"\n  [{c['claim_id']}] {e['claim']}")
            print(f"     STATUS     : {v['status']}  (rel: {v['evidence_relationship']})")
            print(f"     confidence : {v['confidence']}   search_quality: {v['search_quality']}")
            if v.get("source_url"):
                print(f"     source     : {v['source_url']}")

    # --- Save the full schema so we can inspect the real output shape ---
    out_path = "source_verification_infosys_output.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nFull structured output saved to: {out_path}")
