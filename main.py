"""
main.py - the orchestrator ("general contractor") of the AI Investment Research System.

It does no research itself. It runs the modules in order, passes each one's
output to the next, keeps going if one fails, builds the report, and saves the
whole run to the database.

How to run (from F:\\Python\\ai_investment_research):
    python main.py                      -> asks you for a company name
    python main.py Infosys              -> searches, then you pick from a list
    python main.py --ticker INFY.NS     -> no questions at all (scheduler-ready)
    python main.py --ticker AAPL --verbose   -> same, with detailed module output
"""

import argparse      # reads what you type after "python main.py"
import sys           # lets us hand an exit code (0 = ok, 1 = problem) back to Windows
import time          # a stopwatch, to time each step
import traceback     # prints the full error details when something fails

from modules.company_identifier import search_company, get_company_details
from modules import financial_analysis, macro_analysis, technical_analysis, source_verification
from modules.financial_analysis import detect_market
from modules.report_generation import build_report, save_report
from modules.database import save_run


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def clean(value):
    """Treat missing-looking values ("N/A", "", None) as truly empty (None)."""
    if value is None or str(value).strip() in ("", "N/A"):
        return None
    return value


def short_error(exc):
    """One-line error text for the database, e.g. 'KeyError: price'."""
    return f"{type(exc).__name__}: {exc}"


def run_step(label, func, *args, **kwargs):
    """
    Run one module inside a "circuit breaker" and time it.

    Returns three things: (result, error_text, seconds)
      - success: (the module's output, None, seconds)
      - failure: (None, "short error text", seconds)  and the full error is printed
    A module that returns nothing (None) is treated as a failure.
    """
    print(f"\n>>> {label}: starting...")
    start = time.perf_counter()
    try:
        result = func(*args, **kwargs)
        if result is None:
            raise ValueError("module returned nothing (None)")
        seconds = time.perf_counter() - start
        print(f">>> {label}: done in {seconds:.1f}s")
        return result, None, seconds
    except Exception as exc:
        seconds = time.perf_counter() - start
        print(f">>> {label}: FAILED after {seconds:.1f}s - {short_error(exc)}")
        traceback.print_exc()          # full details on screen, for debugging
        return None, short_error(exc), seconds


# -----------------------------------------------------------------------------
# Step wrappers
# -----------------------------------------------------------------------------

def run_financial(ticker):
    """Run the 5-step Financial pipeline and bundle the results into one dict."""
    sector_result = financial_analysis.run_sector_intelligence(ticker)
    data_result = financial_analysis.run_data_engine(ticker, sector_result)
    peer_result = financial_analysis.run_peer_engine(ticker, sector_result, data_result)
    analysis_result = financial_analysis.run_analysis_engine(ticker, sector_result, data_result, peer_result)
    thesis_result = financial_analysis.run_thesis_generator(analysis_result, sector_result)
    return {
        "ticker": ticker,
        "sector_result": sector_result,
        "data_result": data_result,
        "peer_result": peer_result,
        "analysis_result": analysis_result,
        "thesis_result": thesis_result,
    }


def identify_company(args):
    """Decision 5: --ticker (no questions), else a typed name, else ask."""
    if args.ticker:
        return get_company_details(args.ticker)

    name = " ".join(args.company).strip() if args.company else ""
    if not name:
        name = input("Enter company name: ").strip()
    if not name:
        return {"success": False, "error": "No company name entered."}
    return search_company(name)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the full investment research pipeline for one company."
    )
    parser.add_argument("company", nargs="*",
                        help='Company name, e.g. Infosys or "Reliance Industries"')
    parser.add_argument("--ticker",
                        help="Exact ticker, e.g. INFY.NS or AAPL (skips the pick-a-number step)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed output from the modules")
    return parser.parse_args()


# -----------------------------------------------------------------------------
# The orchestrator
# -----------------------------------------------------------------------------

def main():
    args = parse_args()
    run_start = time.perf_counter()

    print("=" * 60)
    print("AI INVESTMENT RESEARCH - full run")
    print("=" * 60)

    # --- 1. Identify the company (the only fatal step) -----------------------
    company = identify_company(args)
    if not company.get("success"):
        print(f"\nRun stopped: company could not be identified.")
        print(f"Reason: {company.get('error', 'unknown')}")
        return 1

    ticker = company["ticker"]
    company_name = company["company_name"]
    market = detect_market(ticker)                                          # Decision 2
    sector = clean(company.get("industry")) or clean(company.get("sector"))  # Decision 3
    macro_sector = sector or "Unknown"

    print(f"\nCompany : {company_name}")
    print(f"Ticker  : {ticker}")
    print(f"Market  : {market}")
    print(f"Sector  : {macro_sector}")

    outputs = {}    # module name -> output dict (only for successes)
    errors = {}     # module name -> short error text (only for failures)
    timings = {}    # step name   -> seconds

    # --- 2. Research modules, one at a time (Decision 1) ---------------------
    outputs["financial"], err, timings["financial"] = run_step(
        "Financial analysis", run_financial, ticker)
    if err:
        errors["financial"] = err

    outputs["macro"], err, timings["macro"] = run_step(
        "Macro analysis", macro_analysis.analyze_macro,
        company_name, macro_sector, market, verbose=args.verbose)
    if err:
        errors["macro"] = err

    outputs["technical"], err, timings["technical"] = run_step(
        "Technical analysis", technical_analysis.analyze_technical,
        ticker, verbose=args.verbose)
    if err:
        errors["technical"] = err

    # --- 3. Verification: only if Macro produced something to check ---------
    if outputs["macro"] is not None:
        outputs["verification"], err, timings["verification"] = run_step(
            "Source verification", source_verification.verify_macro_claims,
            outputs["macro"], verbose=args.verbose)
        if err:
            errors["verification"] = err
    else:
        outputs["verification"] = None
        print("\n>>> Source verification: skipped (Macro did not succeed)")

    # --- 4. Report: only if at least one research module succeeded (Decision 4)
    report_path = None
    research_ok = any(outputs[m] is not None for m in ("financial", "macro", "technical"))

    if not research_ok:
        print("\n>>> Report: NOT built - all three research modules failed "
              "(an empty report would be misleading).")
    else:
        print("\n>>> Report: building...")
        start = time.perf_counter()
        try:
            report_text = build_report(
                macro=outputs["macro"],
                verification=outputs["verification"],
                financial=outputs["financial"],
                technical=outputs["technical"],
            )
            report_path = save_report(report_text, company_name)
            print(f">>> Report: saved to {report_path}")
        except Exception as exc:
            print(f">>> Report: FAILED - {short_error(exc)}")
            traceback.print_exc()
        timings["report"] = time.perf_counter() - start

    # --- 5. Save the run to the database (always) ----------------------------
    versions = {                                                            # Decision 8
        "financial": financial_analysis.MODULE_VERSION,
        "macro": macro_analysis.MODULE_VERSION,
        "technical": technical_analysis.MODULE_VERSION,
        "verification": source_verification.MODULE_VERSION,
    }
    start = time.perf_counter()
    report_id = save_run(
        company_name, ticker,
        market=market,
        sector=sector,
        report_path=report_path,
        macro=outputs["macro"],
        verification=outputs["verification"],
        financial=outputs["financial"],
        technical=outputs["technical"],
        module_errors=errors,
        module_versions=versions,
        verbose=args.verbose,
    )
    timings["database"] = time.perf_counter() - start

    # --- 6. Summary ----------------------------------------------------------
    total = time.perf_counter() - run_start
    print("\n" + "=" * 60)
    print(f"RUN SUMMARY - {company_name} ({ticker})")
    print("=" * 60)
    print(f"{'Step':<15}{'Status':<10}{'Seconds':>8}")
    print("-" * 33)
    for name in ("financial", "macro", "technical", "verification"):
        if outputs.get(name) is not None:
            status = "success"
        elif name in errors:
            status = "FAILED"
        else:
            status = "skipped"
        secs = timings.get(name)
        secs_text = f"{secs:.1f}" if secs is not None else "-"
        print(f"{name:<15}{status:<10}{secs_text:>8}")
    print("-" * 33)
    print(f"Report      : {report_path or 'not created'}")
    print(f"Database id : {report_id if report_id is not None else 'NOT saved'}")
    print(f"Total time  : {total:.1f}s")
    print("Reminder    : check Tavily and Anthropic dashboards for credits used.")

    return 0 if report_path else 1


if __name__ == "__main__":
    sys.exit(main())