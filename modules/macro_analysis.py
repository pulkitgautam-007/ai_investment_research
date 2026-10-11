# macro_analysis.py
# ============================================================================
# MODULE: Macro Analysis
# ----------------------------------------------------------------------------
# Assembles five previously-tested steps into ONE pipeline:
#   Step 1  get_fred_data(market)          -> hard economic numbers (string)
#   Step 2  generate_themes(...)           -> 3-4 material earnings drivers
#   Step 3  gather_macro_news(themes)      -> Tavily results for each driver
#   Step 4  run_reasoning(...)             -> 3-5 causal-chain macro points
#   Step 5  score_macro(points)            -> final sentiment + confidence
#
# Public entry point:
#   analyze_macro(company_name, sector, market, verbose=False) -> dict
#
# Design notes:
#   - Sequential (simple to build/debug); parallelize later only if needed.
#   - FRED is market-aware and degrades gracefully; Tavily+Haiku are the
#     company-specific backbone that works for both US and India.
#   - Every step has a safe fallback so one failure never kills the pipeline.
# ============================================================================

import os
import json
from dotenv import load_dotenv
from fredapi import Fred
from tavily import TavilyClient
import anthropic

# Version of this module's logic/output. Bump it (1 -> 2) whenever the logic
# or the shape of the output changes (comment-only edits don't count).
# main.py saves it with every report so old and new results can be told apart.
MODULE_VERSION = "1"

# --- Load keys and connect to all services ONCE, when the module loads ---
load_dotenv()

# Anthropic reads ANTHROPIC_API_KEY from the environment automatically.
_anthropic_client = anthropic.Anthropic()

# FRED and Tavily need their keys passed in explicitly. We build these clients
# defensively: if a key is missing, the client is set to None and the relevant
# step degrades gracefully instead of crashing at import time.
_fred_key = os.getenv("FRED_API_KEY")
_fred_client = Fred(api_key=_fred_key) if _fred_key else None

_tavily_key = os.getenv("TAVILY_API_KEY")
_tavily_client = TavilyClient(api_key=_tavily_key) if _tavily_key else None

# Shared constant used by several steps.
_MODEL = "claude-haiku-4-5"


# ============================================================================
# STEP 1 — FRED economic data (market-aware)
# ============================================================================
def get_fred_data(market, verbose=False):
    """Pull a small set of hard economic indicators for the given market.

    Returns a formatted multi-line string. Never raises: on any failure it
    returns a short note so the pipeline can continue on Tavily + Haiku.
    """
    if _fred_client is None:
        return "FRED data unavailable (no FRED_API_KEY configured)."

    # Choose indicators based on market. US series are fresh and monthly.
    # India series come from IMF/OECD/World Bank feeds and update slowly, so
    # we keep the set small and always show the date so staleness is visible.
    if market.lower() == "us":
        indicators = {
            "UNRATE": "US Unemployment Rate (%)",
            "CPIAUCSL": "US Consumer Price Index (inflation gauge)",
            "FEDFUNDS": "US Federal Funds Rate (%)",
            "GDP": "US GDP ($ Billions)",
            "DGS10": "US 10-Year Treasury Yield (%)",
        }
    else:
        # Treat anything non-US (India and others) with the international set.
        indicators = {
            "FPCPITOTLZGIND": "India CPI Inflation (annual %)",
            "INDIRLTLT01STM": "India 10-Year Govt Bond Yield (%)",
            "INDGDPRQPSMEI": "India GDP (index, quarterly)",
        }

    lines = []
    for series_id, label in indicators.items():
        try:
            data = _fred_client.get_series(series_id)
            if data is None or len(data.dropna()) == 0:
                # Series exists but has no usable values — skip it silently.
                continue
            data = data.dropna()
            latest_value = data.iloc[-1]
            latest_date = data.index[-1].strftime("%Y-%m-%d")
            lines.append(f"{label}: {latest_value:.2f} (as of {latest_date})")
        except Exception as e:
            # One bad series shouldn't stop the others.
            if verbose:
                print(f"  FRED: {label} failed — {e}")
            continue

    if not lines:
        return f"FRED data unavailable for market '{market}' this run."

    result = "\n".join(lines)
    if verbose:
        print("--- FRED DATA ---")
        print(result, "\n")
    return result


# ============================================================================
# STEP 2 — Theme generator (material earnings drivers)
# ============================================================================
_VALID_CATEGORIES = {"demand", "cost", "competitive", "regulatory", "technology"}


def _themes_fallback(company_name, sector, market):
    return {
        "company_understanding": {
            "business_model": "N/A", "revenue_drivers": "N/A",
            "cost_structure": "N/A", "geographic_exposure": "N/A",
        },
        "drivers": [
            {
                "theme": "General macro outlook",
                "category": "Demand",
                "query": f"{company_name} {sector} {market} earnings outlook 2026",
                "materiality_rationale": "Fallback query — driver generation failed validation.",
            }
        ],
    }


def generate_themes(company_name, sector, market, verbose=False):
    """Ask Haiku for the 3-4 most material external earnings drivers.

    Returns a dict with 'company_understanding' and 'drivers'. Falls back to
    a safe single-driver dict if the model output fails validation.
    """
    worked_example = """
Example output (exact JSON shape, for a company in this space):
{
  "company_understanding": {
    "business_model": "Exports IT and consulting services, billed largely in USD",
    "revenue_drivers": "Enterprise digital transformation, cloud, AI/consulting deals",
    "cost_structure": "Labor-heavy; majority of costs are INR salaries",
    "geographic_exposure": "Revenue concentrated in North America and Europe"
  },
  "drivers": [
    {
      "theme": "Enterprise AI Spending Reallocation",
      "category": "Demand",
      "query": "enterprise AI budget allocation IT consulting services outlook 2026",
      "materiality_rationale": "AI budgets are becoming a large share of enterprise tech spend and can meaningfully shift demand between growth and legacy service lines."
    }
  ]
}
"""

    system_prompt = (
        "You are an equity analyst identifying the external DRIVERS most likely to "
        "materially affect a specific company's earnings over the next 12-24 months. "
        "You do NOT catalogue generic macro categories. You first understand the company's "
        "economics, then surface only drivers that pass a strict Materiality Test.\n\n"
        "A driver qualifies ONLY if it clears ALL of these:\n"
        "1. It can materially affect revenue, margin, cash flow, market share, or valuation "
        "within 12-24 months.\n"
        "2. Its potential impact is HIGH IN MAGNITUDE, not trivial.\n"
        "3. It affects THIS company more distinctly than the broad market — favor "
        "company/sector-specific drivers over ambient economy-wide ones.\n"
        "4. It is DISTINCT from the other selected drivers, not a restatement.\n"
        "5. It is relevant to a real investor's decision.\n\n"
        "Do NOT force traditional macro themes (interest rates, inflation, GDP) unless they "
        "are genuinely among the most material drivers for THIS company. "
        "You respond with ONLY valid JSON. No markdown, no preamble, no explanation."
    )

    user_prompt = f"""
Identify the most material external earnings drivers for this company.

Company: {company_name}
Sector: {sector}
Market: {market}

PROCESS:
- First, briefly understand the company's economics (business model, revenue drivers,
  cost structure, geographic exposure).
- Internally consider a broad set of possible external drivers.
- Then return ONLY the 3-4 that pass the Materiality Test. Return fewer than 4 if fewer
  genuinely qualify — do NOT pad to a quota.

FOR EACH DRIVER:
- theme: a short name for the driver
- category: one of Demand, Cost, Competitive, Regulatory, Technology
- query: a short, search-engine-ready phrase (include 2026 where useful)
- materiality_rationale: ONE sentence proving it is high-magnitude AND company-specific.

Return the EXACT JSON structure shown in the example. Nothing else.

{worked_example}

Now produce the JSON for {company_name}.
"""

    if verbose:
        print(f"Generating material earnings drivers for {company_name}...")

    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text.strip()
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(cleaned)

        cu = result.get("company_understanding", {})
        for field in ["business_model", "revenue_drivers", "cost_structure", "geographic_exposure"]:
            if not cu.get(field):
                raise ValueError(f"company_understanding missing '{field}'.")

        drivers = result.get("drivers")
        if not isinstance(drivers, list) or not (3 <= len(drivers) <= 4):
            raise ValueError(f"Expected 3-4 drivers, got {len(drivers) if isinstance(drivers, list) else 'none'}.")

        for d in drivers:
            for field in ["theme", "category", "query", "materiality_rationale"]:
                if not d.get(field):
                    raise ValueError(f"A driver is missing '{field}'.")
            if d["category"].lower() not in _VALID_CATEGORIES:
                raise ValueError(f"Invalid category: {d['category']}")

        if verbose:
            print(f"  Themes OK — {len(drivers)} drivers.\n")
        return result

    except Exception as e:
        if verbose:
            print(f"  Theme generation failed ({e}). Using fallback.\n")
        return _themes_fallback(company_name, sector, market)


# ============================================================================
# STEP 3 — Tavily macro-news fetcher
# ============================================================================
def gather_macro_news(themes, verbose=False):
    """Run each driver's search query through Tavily (basic depth, 3 results).

    Takes the dict returned by generate_themes. Returns a list of result dicts
    (query, title, url, content). Never raises; returns whatever it collected.
    """
    if _tavily_client is None:
        if verbose:
            print("  Tavily unavailable (no TAVILY_API_KEY).")
        return []

    # Pull the search queries out of the themes structure.
    queries = [d["query"] for d in themes.get("drivers", []) if d.get("query")]

    all_results = []
    for query in queries:
        if verbose:
            print(f"  Searching: {query}")
        try:
            response = _tavily_client.search(
                query=query,
                search_depth="basic",   # 1 credit — explicit, no auto-upgrade
                max_results=3,
            )
            for r in response.get("results", []):
                all_results.append({
                    "query": query,
                    "title": r.get("title", "No title"),
                    "url": r.get("url", "No URL"),
                    "content": r.get("content", ""),
                })
        except Exception as e:
            if verbose:
                print(f"    FAILED: {e}")
            continue

    if verbose:
        print(f"  Collected {len(all_results)} results.\n")
    return all_results


# ============================================================================
# STEP 4 — Reasoning engine (5-step causal chain)
# ============================================================================
_VALID_LEVERS = {"revenue", "margin", "market share", "valuation"}
_VALID_DIRECTIONS = {"tailwind", "headwind", "opportunity", "structural risk"}


def _reasoning_fallback():
    return {
        "macro_points": [
            {
                "factor": "Analysis unavailable",
                "external_change": "Reasoning engine returned an invalid response.",
                "customer_impact": "N/A",
                "company_impact": "N/A",
                "financial_lever": "N/A",
                "direction": "N/A",
                "summary": "Macro reasoning could not be completed this run.",
            }
        ]
    }


def _format_news_for_prompt(news_results):
    """Turn the Tavily result list into a compact text block for the prompt."""
    if not news_results:
        return "(no macro news retrieved this run)"
    lines = []
    for r in news_results:
        snippet = r["content"][:300].strip()
        lines.append(f"- {r['title']}: {snippet}")
    return "\n".join(lines)


def run_reasoning(company_name, sector, market, fred_data, news_results, verbose=False):
    """Force Haiku through the 5-step causal chain using FRED + Tavily inputs.

    Returns a dict with 'macro_points' (3-5 items). Safe fallback on failure.
    """
    tavily_news = _format_news_for_prompt(news_results)

    worked_example = """
Example (for a different company, showing the exact JSON shape and reasoning depth):
{
  "macro_points": [
    {
      "factor": "Currency depreciation",
      "external_change": "The rupee weakened ~10% versus the dollar over 12 months",
      "customer_impact": "Overseas clients' dollar contracts now convert into more rupees",
      "company_impact": "The company bills clients in dollars but pays most costs in rupees",
      "financial_lever": "margin",
      "direction": "tailwind",
      "summary": "A weaker rupee lifts reported margins on dollar-denominated revenue."
    }
  ]
}
"""

    system_prompt = (
        "You are a macro-economic analyst. Given economic data and news, you reason about "
        "the impact on ONE specific company using a strict 5-step causal chain for each point: "
        "(1) what external change is happening, (2) how it affects customer/market behavior, "
        "(3) how that flows to the company, (4) which financial lever it hits "
        "(revenue, margin, market share, or valuation), (5) whether it is a tailwind, headwind, "
        "opportunity, or structural risk. You surface ONLY genuinely meaningful factors — never "
        "pad to a quota. You respond with ONLY valid JSON. No markdown, no preamble, no explanation."
    )

    user_prompt = f"""
Analyze the macro environment for this specific company.

Company: {company_name}
Sector: {sector}
Market: {market}

--- ECONOMIC DATA (FRED) ---
{fred_data}

--- MACRO NEWS (web) ---
{tavily_news}

RULES:
- Produce between 3 and 5 macro points — ONLY factors that genuinely matter for {company_name}.
- For EACH point, complete all 5 causal fields: external_change, customer_impact,
  company_impact, financial_lever, direction.
- financial_lever must be EXACTLY ONE of: revenue, margin, market share, valuation.
  Choose the SINGLE most affected lever. Do NOT combine or list multiple levers.
- direction must be one of: tailwind, headwind, opportunity, structural risk.
- Reasoning must be SPECIFIC to {company_name}, not generic economics.
- Ignore noisy or irrelevant news items; do not force a point from weak data.
- Return the EXACT JSON structure shown in the example. Nothing else.

{worked_example}

Now produce the JSON for {company_name}.
"""

    if verbose:
        print(f"Running 5-step macro reasoning for {company_name}...")

    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=2000,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text.strip()
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(cleaned)

        points = result.get("macro_points")
        if not isinstance(points, list) or not (3 <= len(points) <= 5):
            raise ValueError(f"Expected 3-5 macro points, got {len(points) if isinstance(points, list) else 'none'}.")

        required = ["factor", "external_change", "customer_impact",
                    "company_impact", "financial_lever", "direction", "summary"]
        for p in points:
            for field in required:
                if not p.get(field):
                    raise ValueError(f"A point is missing '{field}'.")
            # Haiku sometimes returns multiple levers ("revenue, margin").
            # Take the first valid one rather than discarding the whole analysis.
            lever_raw = p["financial_lever"].lower()
            lever_match = next((v for v in _VALID_LEVERS if v in lever_raw), None)
            if lever_match is None:
                raise ValueError(f"Invalid financial_lever: {p['financial_lever']}")
            p["financial_lever"] = lever_match
            if p["direction"].lower() not in _VALID_DIRECTIONS:
                raise ValueError(f"Invalid direction: {p['direction']}")

        if verbose:
            print(f"  Reasoning OK — {len(points)} points.\n")
        return result

    except Exception as e:
        if verbose:
            print(f"  Reasoning failed ({e}). Using fallback.\n")
        return _reasoning_fallback()


# ============================================================================
# STEP 5 — Scoring / final verdict
# ============================================================================
_VALID_SENTIMENTS = {"positive", "neutral", "negative"}


def _scoring_fallback():
    return {
        "overall_sentiment": "Neutral",
        "confidence_pct": 0,
        "explanation": "Scoring engine returned an invalid response; verdict unavailable this run.",
    }


def _format_points_for_scoring(reasoning):
    """Turn the reasoning dict into the numbered text block the scorer expects."""
    points = reasoning.get("macro_points", [])
    lines = []
    for i, p in enumerate(points, start=1):
        lines.append(
            f"{i}. {p['factor']} [{p['direction'].upper()}, {p['financial_lever']}]: {p['summary']}"
        )
    return "\n".join(lines)


def score_macro(company_name, reasoning, verbose=False):
    """Ask Haiku to weigh the reasoned points by magnitude into one verdict.

    Returns a dict: overall_sentiment, confidence_pct, explanation.
    """
    macro_points = _format_points_for_scoring(reasoning)

    worked_example = """
Example output (exact JSON shape, nothing else):
{
  "overall_sentiment": "Positive",
  "confidence_pct": 65,
  "explanation": "Two to three sentences that WEIGH the drivers by magnitude, not just count them, and state which forces dominate the verdict."
}
"""

    system_prompt = (
        "You are a macro-economic analyst producing a final verdict for one company. "
        "Given a set of reasoned macro drivers (each tagged tailwind or headwind, with a "
        "financial lever and a sense of magnitude), you WEIGH them by materiality — a large "
        "high-magnitude driver outweighs several minor ones. You do NOT simply count tailwinds "
        "versus headwinds. You then output one overall sentiment, a confidence percentage, and "
        "a short explanation naming which drivers dominate the verdict.\n\n"
        "overall_sentiment must be exactly one of: Positive, Neutral, Negative.\n"
        "confidence_pct is an integer 0-100 reflecting how clear-cut the balance of drivers is "
        "(a lopsided balance = high confidence; a close or uncertain balance = lower confidence).\n"
        "You respond with ONLY valid JSON. No markdown, no preamble, no explanation outside the JSON."
    )

    user_prompt = f"""
Produce the final macro verdict for {company_name}, weighing these reasoned drivers.

--- REASONED MACRO DRIVERS ---
{macro_points}

RULES:
- WEIGH by magnitude, do not just count. A single large driver can outweigh several small ones.
- overall_sentiment: exactly one of Positive, Neutral, Negative.
- confidence_pct: integer 0-100, based on how clear-cut the balance is.
- explanation: 2-3 sentences naming which drivers dominate and why.
- Return the EXACT JSON structure shown. Nothing else.

{worked_example}

Now produce the JSON verdict for {company_name}.
"""

    if verbose:
        print(f"Producing final macro verdict for {company_name}...")

    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=600,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text.strip()
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(cleaned)

        sentiment = result.get("overall_sentiment", "")
        if sentiment.lower() not in _VALID_SENTIMENTS:
            raise ValueError(f"Invalid sentiment: {sentiment}")

        conf = result.get("confidence_pct")
        if not isinstance(conf, (int, float)) or not (0 <= conf <= 100):
            raise ValueError(f"Invalid confidence_pct: {conf}")

        if not result.get("explanation"):
            raise ValueError("Missing explanation.")

        if verbose:
            print("  Scoring OK.\n")
        return result

    except Exception as e:
        if verbose:
            print(f"  Scoring failed ({e}). Using fallback.\n")
        return _scoring_fallback()


# ============================================================================
# PUBLIC ENTRY POINT — orchestrates all five steps in sequence
# ============================================================================
def analyze_macro(company_name, sector, market, verbose=False):
    """Run the full macro pipeline for one company and return one clean dict.

    Returns:
      {
        "company_name": str,
        "sector": str,
        "market": str,
        "verdict": { overall_sentiment, confidence_pct, explanation },
        "macro_points": [ ...the reasoned causal chains... ],
        "drivers": [ ...the themes that were searched... ],
        "company_understanding": { ... },
        "fred_data": str,
      }
    """
    if verbose:
        print("=" * 60)
        print(f"  MACRO ANALYSIS: {company_name} ({market})")
        print("=" * 60 + "\n")

    # Step 1: hard economic numbers.
    fred_data = get_fred_data(market, verbose=verbose)

    # Step 2: material earnings drivers.
    themes = generate_themes(company_name, sector, market, verbose=verbose)

    # Step 3: news for each driver.
    news_results = gather_macro_news(themes, verbose=verbose)

    # Step 4: reason about impact on THIS company.
    reasoning = run_reasoning(
        company_name, sector, market, fred_data, news_results, verbose=verbose
    )

    # Step 5: weigh into a final verdict.
    verdict = score_macro(company_name, reasoning, verbose=verbose)

    # Assemble one clean structured result for Report Generation to read.
    return {
        "company_name": company_name,
        "sector": sector,
        "market": market,
        "verdict": verdict,
        "macro_points": reasoning.get("macro_points", []),
        "drivers": themes.get("drivers", []),
        "company_understanding": themes.get("company_understanding", {}),
        "fred_data": fred_data,
    }


# ============================================================================
# TEST BLOCK — runs only when you execute this file directly.
# When another module imports analyze_macro, this block does NOT run.
# ============================================================================
if __name__ == "__main__":
    result = analyze_macro(
        company_name="Apple",
        sector="Technology Hardware",
        market="US",
        verbose=True,
    )

    print("=" * 60)
    print(f"  FINAL MACRO VERDICT: {result['company_name']}")
    print("=" * 60)
    v = result["verdict"]
    print(f"  Sentiment  : {v['overall_sentiment']}")
    print(f"  Confidence : {v['confidence_pct']}%")
    print(f"  Explanation: {v['explanation']}")
    print("=" * 60)
    print(f"\n  Reasoned macro points ({len(result['macro_points'])}):")
    for i, p in enumerate(result["macro_points"], start=1):
        print(f"\n    {i}. {p['factor']} [{p['direction'].upper()} -> {p['financial_lever']}]")
        print(f"       external_change : {p['external_change']}")
        print(f"       customer_impact : {p['customer_impact']}")
        print(f"       company_impact  : {p['company_impact']}")
        print(f"       summary         : {p['summary']}")

    print(f"\n  Company understanding:")
    cu = result["company_understanding"]
    for k, v in cu.items():
        print(f"    {k}: {v}")