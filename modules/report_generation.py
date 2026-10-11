# -*- coding: utf-8 -*-
# =============================================================================
# MODULE 7: REPORT GENERATION
#
# Reads the dictionaries produced by the analysis modules and weaves them into
# one readable Markdown report saved in outputs/.
# This module does NO analysis and NO data fetching -- only formatting.
#
# Sections: Macro + Source Verification, Financial, Technical.
# =============================================================================

import json
import os
from datetime import datetime


# =============================================================================
# SHARED HELPERS
# =============================================================================

def load_fixture(path):
    """Read a JSON file off disk, return it as a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def make_bar(score, out_of, width=10):
    """Fixed-width visual bar showing what fraction of out_of the score fills."""
    if not out_of or out_of <= 0 or score is None:
        return "░" * width
    fraction = max(0.0, min(1.0, score / out_of))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


# =============================================================================
# MACRO + SOURCE VERIFICATION SECTION
# =============================================================================

def attach_verdicts(macro, verification):
    """Match each verification verdict to the Macro point it is about."""
    macro_points = macro.get("macro_points", [])
    claims = verification.get("claims_checked", [])

    joined = []
    for index, point in enumerate(macro_points):
        prefix = f"macro_points[{index}]"
        matching = []
        for claim in claims:
            location = claim.get("source_location", "")
            if location.startswith(prefix):
                matching.append(claim)
        joined.append({"point": point, "verdicts": matching})
    return joined


STATUS_DISPLAY = {
    "VERIFIED":            ("✅ VERIFIED",            "checked and supported by a source"),
    "CONTRADICTED":        ("❌ CONTRADICTED",        "a source disagrees with this"),
    "UNVERIFIED":          ("⚠️ UNVERIFIED",          "searched, but evidence was insufficient"),
    "NOT_VERIFIED_BUDGET": ("⏳ NOT CHECKED (budget)", "worth checking, but the search cap was reached"),
}
ASSESSED_STATUSES = {"VERIFIED", "CONTRADICTED", "UNVERIFIED", "NOT_VERIFIED_BUDGET"}


def render_point(number, pair):
    """Turn ONE joined macro point into a Markdown block."""
    point = pair.get("point", {})
    verdicts = pair.get("verdicts", [])

    factor    = point.get("factor", "(unnamed factor)")
    direction = point.get("direction", "n/a")
    lever     = point.get("financial_lever", "n/a")
    summary   = point.get("summary", "")
    external  = point.get("external_change", "")
    company   = point.get("company_impact", "")

    lines = []
    lines.append(f"### {number}. {factor}")
    lines.append(f"**Direction:** {direction} · **Financial lever:** {lever}")
    lines.append("")
    if summary:
        lines.append(summary)
        lines.append("")
    if external:
        lines.append(f"**What's changing:** {external}")
    if company:
        lines.append(f"**Why it matters for the company:** {company}")
    lines.append("")

    assessed, skipped = [], []
    for v in verdicts:
        status = v.get("verification", {}).get("status", "")
        (assessed if status in ASSESSED_STATUSES else skipped).append(v)

    lines.append("**Fact-check of this point's claims:**")
    lines.append("")
    if assessed:
        for v in assessed:
            extracted = v.get("extracted", {})
            verification = v.get("verification", {})
            claim_text = extracted.get("claim", "(no claim text)")
            status = verification.get("status", "")
            badge, plain = STATUS_DISPLAY.get(status, (f"[{status}]", ""))
            lines.append(f"- {badge} — \"{claim_text}\"")
            if plain:
                lines.append(f"    - _{plain}_")
            evidence = verification.get("evidence")
            source_url = verification.get("source_url")
            if evidence:
                lines.append(f"    - Evidence: {evidence}")
            if source_url:
                lines.append(f"    - Source: {source_url}")
    else:
        lines.append("- _No claims from this point were independently assessed._")
    lines.append("")

    if skipped:
        lines.append("<details>")
        lines.append(f"<summary>{len(skipped)} statement(s) not fact-checked "
                     f"(low materiality or opinion)</summary>")
        lines.append("")
        for v in skipped:
            claim_text = v.get("extracted", {}).get("claim", "(no claim text)")
            lines.append(f"- \"{claim_text}\"")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def render_macro_section(macro, verification):
    """Build the whole Macro + Source Verification section."""
    lines = []
    lines.append("## Macro Analysis (with independent fact-check)")
    lines.append("")
    lines.append("_Each macro factor is followed by an independent fact-check of its "
                 "checkable claims. A badge shows whether each claim was verified "
                 "against a real source._")
    lines.append("")

    joined = attach_verdicts(macro, verification)
    if not joined:
        lines.append("_No macro factors were available to report._")
        lines.append("")
    else:
        for i, pair in enumerate(joined):
            lines.append(render_point(i + 1, pair))
    return "\n".join(lines)


# =============================================================================
# FINANCIAL SECTION
# =============================================================================

SCORECARD_DIMENSIONS = [
    ("growth",             "Growth"),
    ("quality",            "Quality"),
    ("financial_strength", "Financial Strength"),
    ("valuation",          "Valuation"),
    ("sector_outlook",     "Sector Outlook"),
]


def render_scorecard(scorecard):
    """Render the 5-dimension scorecard as a table with visual bars."""
    lines = []
    lines.append("### Scorecard")
    lines.append("")
    lines.append("| Dimension | Score | | Detail |")
    lines.append("|---|---|---|---|")

    for key, label in SCORECARD_DIMENSIONS:
        dim = scorecard.get(key)
        if not dim:
            continue
        score = dim.get("score")
        out_of = dim.get("out_of", 0)
        reason = dim.get("reason", "")
        bar = make_bar(score, out_of)
        lines.append(f"| {label} | {score}/{out_of} | `{bar}` | {reason} |")

    total = scorecard.get("total")
    rating = scorecard.get("rating", "")
    total_bar = make_bar(total, 100)
    lines.append(f"| **Total** | **{total}/100** | `{total_bar}` | **{rating}** |")
    lines.append("")
    return "\n".join(lines)


def render_thesis(thesis_result):
    """Render the investment thesis, bull case, bear case, and key risks."""
    lines = []

    thesis = thesis_result.get("investment_thesis", "")
    if thesis:
        lines.append("### Investment Thesis (analytical stance)")
        lines.append("")
        lines.append(thesis)
        lines.append("")

    bull = thesis_result.get("bull_case", [])
    lines.append("### Bull Case")
    lines.append("")
    if bull:
        for point in bull:
            lines.append(f"- {point}")
    else:
        lines.append("- _None identified._")
    lines.append("")

    bear = thesis_result.get("bear_case", [])
    lines.append("### Bear Case")
    lines.append("")
    if bear:
        for point in bear:
            lines.append(f"- {point}")
    else:
        lines.append("- _None identified._")
    lines.append("")

    risks = thesis_result.get("key_risks", [])
    lines.append("### Key Risks")
    lines.append("")
    if risks:
        for risk in risks:
            lines.append(f"- {risk}")
    else:
        lines.append("- _None flagged._")
    lines.append("")
    return "\n".join(lines)


def render_key_stories(key_stories):
    """Render the flagged stories (anomalies worth attention)."""
    lines = []
    lines.append("### Notable Signals")
    lines.append("")
    if not key_stories:
        lines.append("_No significant signals detected — metrics are stable and "
                     "broadly in line with expectations._")
        lines.append("")
        return "\n".join(lines)

    signal_icon = {"bullish": "🟢", "bearish": "🔴", "watch": "🟡"}
    for s in key_stories:
        icon = signal_icon.get(s.get("signal", ""), "•")
        title = s.get("title", "(untitled)")
        context = s.get("context", "")
        lines.append(f"- {icon} **{title}** — {context}")
    lines.append("")
    return "\n".join(lines)


def render_peer_table(peer_result):
    """Render the peer comparison table, carrying its confidence flag honestly."""
    lines = []
    lines.append("### Peer Comparison")
    lines.append("")

    confidence = peer_result.get("peer_confidence", "none")
    peers = peer_result.get("peers", [])

    if confidence == "low":
        lines.append("> ⚠️ **Low-confidence comparison.** Few clean industry peers were "
                     "found, so these figures are broad matches only — treat them with "
                     "caution.")
        lines.append("")
    elif confidence == "none" or not peers:
        lines.append("_No peer comparison available._")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Company | Net Margin | ROE | Rev Growth | P/E | Debt/Equity |")
    lines.append("|---|---|---|---|---|---|")

    def cell(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "N/A"

    for p in peers:
        name = p.get("name", p.get("ticker", "?"))
        lines.append(
            f"| {name} "
            f"| {cell(p.get('net_margin_pct'), '%')} "
            f"| {cell(p.get('roe_pct'), '%')} "
            f"| {cell(p.get('revenue_growth_pct'), '%')} "
            f"| {cell(p.get('pe_trailing'))} "
            f"| {cell(p.get('debt_to_equity'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_lenses_drawer(lenses):
    """Render the six lenses inside a click-to-expand drawer."""
    lines = []
    if not lenses:
        return ""
    lines.append("<details>")
    lines.append("<summary>Six analytical lenses (click to expand)</summary>")
    lines.append("")
    for lens_name, lens_data in lenses.items():
        label = lens_name.replace("_", " ").title()
        finding = lens_data.get("finding", "")
        # Clean upstream artifacts: drop trailing "of None" / "of none" phrasing
        finding = finding.replace("peer average of None", "peer average (not available)")
        finding = finding.replace("average of None", "average (not available)")
        lines.append(f"- **{label}:** {finding}")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def render_financial_section(financial):
    """Build the whole Financial section from a financial fixture dict."""
    lines = []
    lines.append("## Financial Analysis")
    lines.append("")

    analysis = financial.get("analysis_result", {})
    scorecard = analysis.get("scorecard", {})
    key_stories = analysis.get("key_stories", [])
    lenses = analysis.get("lenses", {})
    thesis_result = financial.get("thesis_result", {})
    peer_result = financial.get("peer_result", {})

    if scorecard:
        lines.append(render_scorecard(scorecard))
    if thesis_result:
        lines.append(render_thesis(thesis_result))
    lines.append(render_key_stories(key_stories))
    lines.append(render_peer_table(peer_result))
    lines.append(render_lenses_drawer(lenses))

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# TECHNICAL SECTION
# =============================================================================

# Which character readings are "interesting" enough to surface as a chip.
# Normal / null stay silent; anything else gets flagged at a glance (A-plus).
VOLATILITY_INTERESTING = {"subdued", "elevated"}
VOLUME_INTERESTING = {"low", "high"}


def render_technical_section(technical):
    """
    Build the Technical section from a technical fixture dict.
    Prints the ready-made context sentence, shows the MA numbers compactly,
    surfaces character chips ONLY when not normal, and reports signals
    (an empty signals list is a valid 'nothing unusual' finding).
    """
    lines = []
    lines.append("## Technical Analysis")
    lines.append("")

    # Graceful degradation: if the module couldn't run, say so and stop.
    if not technical or not technical.get("available"):
        lines.append("_Technical data was unavailable for this stock._")
        lines.append("")
        lines.append("---")
        lines.append("")
        return "\n".join(lines)

    context = technical.get("context", {})
    sentence = context.get("sentence", "")
    latest_price = context.get("latest_price")
    ma_50 = context.get("ma_50")
    ma_200 = context.get("ma_200")

    # The ready-made, never-judging context sentence, printed as-is.
    if sentence:
        lines.append(sentence)
        lines.append("")

    # The supporting numbers, shown compactly.
    number_bits = []
    if latest_price is not None:
        number_bits.append(f"**Price:** {latest_price}")
    if ma_50 is not None:
        number_bits.append(f"**50-day avg:** {ma_50}")
    if ma_200 is not None:
        number_bits.append(f"**200-day avg:** {ma_200}")
    if number_bits:
        lines.append(" · ".join(number_bits))
        lines.append("")

    # A-plus: surface character chips ONLY when not normal/null.
    vol_char = context.get("volatility_character")
    volu_char = context.get("volume_character")
    chips = []
    if vol_char in VOLATILITY_INTERESTING:
        chips.append(f"**Volatility:** {vol_char}")
    if volu_char in VOLUME_INTERESTING:
        chips.append(f"**Volume:** {volu_char}")
    if chips:
        lines.append("> " + " · ".join(chips) +
                     " — _a departure from this stock's usual behaviour._")
        lines.append("")

    # Signals. An empty list is a valid, meaningful 'nothing unusual' result.
    signals = technical.get("signals", [])
    lines.append("**Signals:**")
    lines.append("")
    if signals:
        for s in signals:
            # Signals are untouched upstream; render defensively.
            if isinstance(s, dict):
                text = s.get("description") or s.get("sentence") or str(s)
            else:
                text = str(s)
            lines.append(f"- {text}")
    else:
        lines.append("_No conditional signals are active — recent price action is "
                     "unremarkable._")
    lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# ASSEMBLY + SAVE
# =============================================================================

def build_report(macro=None, verification=None, financial=None, technical=None):
    """
    Stitch a title block plus whichever sections have data into one Markdown
    document. Any section whose inputs are missing is simply skipped.
    """
    company_name = "Unknown Company"
    if macro:
        company_name = macro.get("company_name", company_name)
    elif financial:
        company_name = financial.get("ticker", company_name)
    elif technical:
        company_name = technical.get("ticker", company_name)

    generated_on = datetime.now().strftime("%d %B %Y, %H:%M")

    lines = []
    lines.append(f"# Investment Research Report — {company_name}")
    lines.append("")
    lines.append(f"**Report generated:** {generated_on}  ")
    lines.append("")
    lines.append("_This report lays out each module's findings. It does not issue a "
                 "buy or sell recommendation — the reader draws their own conclusion._")
    lines.append("")
    lines.append("---")
    lines.append("")

    if macro and verification:
        lines.append(render_macro_section(macro, verification))
    if financial:
        lines.append(render_financial_section(financial))
    if technical:
        lines.append(render_technical_section(technical))

    if not (macro or financial or technical):
        lines.append("_No module outputs were available to build a report._")
        lines.append("")

    return "\n".join(lines)


def save_report(report_text, company_name):
    """Write the report to a timestamped .md file in outputs/."""
    os.makedirs("outputs", exist_ok=True)
    safe_name = company_name.lower().replace(" ", "_").replace(".", "")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"report_{safe_name}_{stamp}.md"
    path = os.path.join("outputs", filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_text)
    return path


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == "__main__":
    print("=== Building COMBINED report: Macro + Verification + Financial + Technical ===\n")

    macro = load_fixture("macro_fixture_infosys.json")
    verification = load_fixture("source_verification_infosys_output.json")
    financial = load_fixture("financial_fixture_infosys.json")
    technical = load_fixture("technical_fixture_infosys.json")

    report_text = build_report(
        macro=macro,
        verification=verification,
        financial=financial,
        technical=technical,
    )

    company_name = macro.get("company_name", "company")
    path = save_report(report_text, company_name)

    print(f"Report written to: {path}")
    print(f"Length: {len(report_text)} characters")
