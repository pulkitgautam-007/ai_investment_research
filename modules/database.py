"""
database.py - Database & Storage module.

Saves each research run to a local SQLite database (data/research.db):
  reports         - one row per run (catalogue card + headline numbers)
  module_outputs  - each module's full raw output (the working papers)

Agreed design rules:
  - Timestamps stored in UTC (ISO-8601). Convert to local time only for display.
  - Report paths stored relative to the project folder.
  - Headline numbers are derived HERE, from the same dicts saved as JSON.
  - One transaction per run: all rows are saved together, or none are.
  - A database failure prints a warning and returns None. It never crashes the caller.
  - No buy/sell or rating column, by design.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "research.db"

MODULE_NAMES = ["macro", "verification", "financial", "technical"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name        TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    market              TEXT,
    sector              TEXT,
    created_at          TEXT NOT NULL,
    report_path         TEXT,
    financial_score     REAL,
    price               REAL,
    currency            TEXT,
    verified_count      INTEGER,
    contradicted_count  INTEGER,
    unverified_count    INTEGER,
    not_checked_count   INTEGER,
    low_skipped_count   INTEGER
);

CREATE TABLE IF NOT EXISTS module_outputs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER NOT NULL,
    module_name     TEXT NOT NULL,
    module_version  TEXT,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    output_json     TEXT,
    error_message   TEXT,
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE,
    UNIQUE (report_id, module_name)
);

CREATE INDEX IF NOT EXISTS idx_reports_ticker_created
    ON reports (ticker, created_at);
"""


# ---------------------------------------------------------------
# Connection and setup
# ---------------------------------------------------------------

def get_connection():
    """Open the database file, creating the data folder if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the tables and index if they do not exist yet. Safe to run repeatedly."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------

def _utc_now():
    """Current time in UTC, e.g. 2026-10-05T09:02:18Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative_path(report_path):
    """Turn a report path into a project-relative path, e.g. outputs/report_x.md"""
    if not report_path:
        return None
    path = Path(report_path).resolve()
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(report_path)


def _derive_currency(ticker, market):
    """
    RULE-BASED, NOT MEASURED. The system covers only India and US,
    so the currency is inferred from the ticker suffix or market name.
    """
    t = (ticker or "").upper()
    if t.endswith(".NS") or t.endswith(".BO"):
        return "INR"
    m = (market or "").lower()
    if "india" in m or "nse" in m or "bse" in m:
        return "INR"
    if m in ("us", "usa", "united states") or "nyse" in m or "nasdaq" in m:
        return "USD"
    if t and "." not in t:
        return "USD"
    return None


def _safe_get(data, *keys):
    """Walk down nested dicts; return None instead of crashing if anything is missing."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _to_number(value):
    """Return the value only if it is a real number; otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _check_verification_counts(summary):
    """
    Sanity check on Source Verification's summary.
    total_extracted counts only MATERIAL claims (those eligible for checking).
    Low-materiality claims (low_skipped) are counted separately, outside it.
    Rule: verified + contradicted + unverified + not_verified_budget == total_extracted
    """
    parts = ["verified", "contradicted", "unverified", "not_verified_budget"]
    values = [summary.get(p) for p in parts]
    total = summary.get("total_extracted")
    if not all(isinstance(v, int) for v in values) or not isinstance(total, int):
        print("WARNING [database]: verification summary incomplete - count check skipped.")
        return
    if sum(values) != total:
        print(f"WARNING [database]: material-claim counts add up to {sum(values)}, "
              f"but total_extracted is {total}.")


def _extract_headlines(financial, technical, verification, ticker, market):
    """Pull the headline numbers out of the module outputs."""
    headlines = {
        "financial_score": _to_number(_safe_get(financial, "analysis_result", "scorecard", "total")),
        "price": None,
        "currency": None,
        "verified_count": None,
        "contradicted_count": None,
        "unverified_count": None,
        "not_checked_count": None,
        "low_skipped_count": None,
    }

    price = _to_number(_safe_get(technical, "context", "latest_price"))
    if price is None:
        price = _to_number(_safe_get(financial, "data_result", "universal", "current_price"))
    headlines["price"] = price
    if price is not None:
        headlines["currency"] = _derive_currency(ticker, market)

    summary = _safe_get(verification, "summary")
    if isinstance(summary, dict):
        headlines["verified_count"] = _to_number(summary.get("verified"))
        headlines["contradicted_count"] = _to_number(summary.get("contradicted"))
        headlines["unverified_count"] = _to_number(summary.get("unverified"))
        headlines["not_checked_count"] = _to_number(summary.get("not_verified_budget"))
        headlines["low_skipped_count"] = _to_number(summary.get("low_skipped"))
        _check_verification_counts(summary)

    return headlines


# ---------------------------------------------------------------
# Saving a run
# ---------------------------------------------------------------

def save_run(company_name, ticker, market=None, sector=None, report_path=None,
             macro=None, verification=None, financial=None, technical=None,
             module_errors=None, module_versions=None, verbose=False):
    """
    Save one research run. Every module input is optional.
    module_errors:   dict like {"technical": "error text"} for modules that failed.
    module_versions: dict like {"technical": "1"} - each module's MODULE_VERSION.
                     Saved only for modules that ran (success or failed);
                     a skipped module gets NULL because it never ran.
    Returns the new report id, or None if saving failed (a warning is printed).
    """
    module_errors = module_errors or {}
    module_versions = module_versions or {}
    outputs = {
        "macro": macro,
        "verification": verification,
        "financial": financial,
        "technical": technical,
    }
    conn = None
    try:
        init_db()
        headlines = _extract_headlines(financial, technical, verification, ticker, market)
        conn = get_connection()
        saved_statuses = []

        with conn:
            cursor = conn.execute(
                """INSERT INTO reports (
                       company_name, ticker, market, sector, created_at, report_path,
                       financial_score, price, currency,
                       verified_count, contradicted_count, unverified_count,
                       not_checked_count, low_skipped_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_name, ticker, market, sector, _utc_now(), _relative_path(report_path),
                 headlines["financial_score"], headlines["price"], headlines["currency"],
                 headlines["verified_count"], headlines["contradicted_count"],
                 headlines["unverified_count"], headlines["not_checked_count"],
                 headlines["low_skipped_count"]),
            )
            report_id = cursor.lastrowid

            for name in MODULE_NAMES:
                output = outputs[name]
                if output is not None:
                    status = "success"
                    output_json = json.dumps(output, ensure_ascii=False, default=str)
                    error = None
                elif name in module_errors:
                    status = "failed"
                    output_json = None
                    error = str(module_errors[name])[:1000]
                else:
                    status = "skipped"
                    output_json = None
                    error = None

                # A version only means something if the module actually ran
                # (success or failed). A skipped module gets NULL.
                version = module_versions.get(name) if status != "skipped" else None

                conn.execute(
                    """INSERT INTO module_outputs (
                           report_id, module_name, module_version, status,
                           output_json, error_message)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (report_id, name, version, status, output_json, error),
                )
                saved_statuses.append(f"{name}={status}")

        if verbose:
            print(f"[database] Saved run #{report_id} for {ticker}: " + ", ".join(saved_statuses))
        return report_id

    except Exception as exc:
        print(f"WARNING [database]: could not save run for {ticker} - {exc}. "
              f"The report file is unaffected.")
        return None
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------
# Reading data back
# ---------------------------------------------------------------

def get_reports(ticker=None, limit=20):
    """Most recent reports first. Optionally only one ticker."""
    init_db()
    conn = get_connection()
    try:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM reports WHERE ticker = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_score_history(ticker):
    """Financial score over time for one ticker, oldest first, with the module version."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT r.id AS report_id, r.created_at, r.financial_score,
                      m.module_version, m.status AS financial_status
               FROM reports r
               LEFT JOIN module_outputs m
                      ON m.report_id = r.id AND m.module_name = 'financial'
               WHERE r.ticker = ?
               ORDER BY r.created_at ASC, r.id ASC""",
            (ticker,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_module_statuses(report_id):
    """Which modules succeeded, failed, or were skipped for one run."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT module_name, module_version, status, error_message "
            "FROM module_outputs WHERE report_id = ? ORDER BY id",
            (report_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_module_output(report_id, module_name):
    """Return a module's full saved output as a dict, or None if not available."""
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT output_json FROM module_outputs WHERE report_id = ? AND module_name = ?",
            (report_id, module_name),
        ).fetchone()
        if row is None or row["output_json"] is None:
            return None
        return json.loads(row["output_json"])
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at: {DB_PATH}")
    recent = get_reports(limit=5)
    print(f"Reports stored: {len(recent)} (showing up to 5 most recent)")
    for r in recent:
        print(f"  #{r['id']}  {r['created_at']}  {r['ticker']}  score={r['financial_score']}")
