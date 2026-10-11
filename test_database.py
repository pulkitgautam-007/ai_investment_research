"""Throwaway test for modules/database.py - uses Infosys fixtures, spends no credits."""
import json
from pathlib import Path

from modules import database

# Use a separate scratch database so test rows never touch the real one
TEST_DB = Path("data") / "test_research.db"
database.DB_PATH = TEST_DB.resolve()
if TEST_DB.exists():
    TEST_DB.unlink()


def load(name):
    with open(name, encoding="utf-8") as f:
        return json.load(f)


macro = load("macro_fixture_infosys.json")
verification = load("source_verification_infosys_output.json")
financial = load("financial_fixture_infosys.json")
technical = load("technical_fixture_infosys.json")
ticker = financial["ticker"]

results = []


def check(label, condition):
    results.append(bool(condition))
    print(("PASS  " if condition else "FAIL  ") + label)


print("=" * 60)
print("TEST 1: full run with all four modules")
rid1 = database.save_run(
    company_name=macro.get("company_name", "Infosys"),
    ticker=ticker,
    market=macro.get("market"),
    sector=macro.get("sector"),
    report_path="outputs/report_test_infosys.md",
    macro=macro, verification=verification, financial=financial, technical=technical,
    verbose=True,
)
check("save_run returned a report id", isinstance(rid1, int))
row = database.get_reports(ticker=ticker)[0]
check("financial_score equals scorecard total",
      row["financial_score"] == financial["analysis_result"]["scorecard"]["total"])
check("price equals technical latest_price",
      row["price"] == technical["context"]["latest_price"])
check("currency is INR", row["currency"] == "INR")
check("created_at is UTC (ends with Z)", row["created_at"].endswith("Z"))
check("report_path is project-relative",
      row["report_path"] == "outputs/report_test_infosys.md")
s = verification["summary"]
check("all five verification counts match the summary",
      (row["verified_count"], row["contradicted_count"], row["unverified_count"],
       row["not_checked_count"], row["low_skipped_count"])
      == (s["verified"], s["contradicted"], s["unverified"],
          s["not_verified_budget"], s["low_skipped"]))
statuses = database.get_module_statuses(rid1)
check("all 4 modules recorded as success",
      len(statuses) == 4 and all(st["status"] == "success" for st in statuses))
check("financial JSON comes back identical",
      database.get_module_output(rid1, "financial") == financial)

print("=" * 60)
print("TEST 2: partial run - technical failed, macro and verification skipped")
rid2 = database.save_run(
    company_name="Infosys", ticker=ticker,
    financial=financial,
    module_errors={"technical": "Simulated price feed timeout"},
    verbose=True,
)
check("partial run saved", isinstance(rid2, int))
st2 = {st["module_name"]: st for st in database.get_module_statuses(rid2)}
check("technical marked failed with its error message",
      st2["technical"]["status"] == "failed" and "timeout" in st2["technical"]["error_message"])
check("macro marked skipped", st2["macro"]["status"] == "skipped")
row2 = database.get_reports(ticker=ticker)[0]
check("price fell back to financial current_price",
      row2["price"] == financial["data_result"]["universal"]["current_price"])
check("verification counts are NULL (not zero)", row2["verified_count"] is None)

print("=" * 60)
print("TEST 3: score history")
history = database.get_score_history(ticker)
check("history has 2 entries", len(history) == 2)
for h in history:
    print(f"   {h['created_at']}  score={h['financial_score']}  version={h['module_version']}")

print("=" * 60)
print("TEST 4: database failure must not crash")
real_path = database.DB_PATH
database.DB_PATH = Path(__file__).resolve() / "impossible.db"
rid_fail = database.save_run(company_name="Infosys", ticker=ticker)
check("bad database returns None instead of crashing", rid_fail is None)
database.DB_PATH = real_path

print("=" * 60)
print(f"RESULT: {sum(results)}/{len(results)} checks passed")
