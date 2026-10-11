# =============================================================================
# MODULE 3: FINANCIAL ANALYSIS (REBUILT)
# File: modules/financial_analysis.py
#
# SECTION 1: SECTOR INTELLIGENCE
#   - Identify the company's sector/industry and market (India vs US)
#   - Match it to our internal sector definitions (metrics + benchmarks)
#   - Research the sector outlook using Tavily
#
# More sections (Data Engine, Peer Engine, Analysis Engine, Thesis Generator)
# will be added in later sessions.
# =============================================================================

import yfinance as yf
from tavily import TavilyClient
import os
from dotenv import load_dotenv

# Version of this module's logic/output. Bump it (1 -> 2) whenever the logic
# or the shape of the output changes (comment-only edits don't count).
# main.py saves it with every report so old and new results can be told apart.
MODULE_VERSION = "1"

# Load API keys from the .env file in the project root
load_dotenv()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")


# -----------------------------------------------------------------------------
# SECTOR DEFINITIONS
#
# This dictionary is the "brain" of sector-specific analysis.
# Each entry defines:
#   - match_keywords: words to look for in yfinance's "industry"/"sector" fields
#                      to identify if a company belongs to this sector
#   - key_metrics: the sector-specific metrics we care about (used in later sections)
#   - benchmarks: threshold values, split by market (India / US), used for
#                  story detection and scoring (used in later sections)
#
# TO ADD A NEW SECTOR LATER:
#   Copy one of these blocks, change the name, match_keywords, key_metrics,
#   and benchmarks. Nothing else in the code needs to change.
# -----------------------------------------------------------------------------

SECTOR_DEFINITIONS = {

    "IT Services": {
        "match_keywords": [
            "information technology services", "software", "it services",
            "consulting", "computer systems"
        ],
        "key_metrics": [
            "revenue_per_employee", "attrition_rate", "operating_margin", "deal_wins_ttm"
        ],
        "benchmarks": {
            "India": {
                "operating_margin_good": 0.20,   # 20%+ is healthy for Indian IT
                "attrition_high": 0.15,          # above 15% is a concern
            },
            "US": {
                "operating_margin_good": 0.18,
                "attrition_high": 0.15,
            }
        }
    },

    "Banking": {
        "match_keywords": [
            "bank", "banks", "financial services", "diversified banks", "regional banks"
        ],
        "key_metrics": [
            "net_interest_margin", "gnpa_or_npl_ratio", "casa_ratio", "capital_adequacy_ratio"
        ],
        "benchmarks": {
            "India": {
                "nim_good": 0.03,        # NIM above 3% is healthy
                "gnpa_high": 0.03,        # GNPA above 3% is a concern
            },
            "US": {
                "nim_good": 0.03,
                "npl_high": 0.02,         # US banks typically run lower NPL than India
            }
        }
    },

    "FMCG": {
        "match_keywords": [
            "household", "personal products", "packaged foods", "beverages",
            "consumer goods", "tobacco"
        ],
        "key_metrics": [
            "gross_margin", "volume_growth", "advertising_pct_of_revenue"
        ],
        "benchmarks": {
            "India": {
                "gross_margin_good": 0.40,
            },
            "US": {
                "gross_margin_good": 0.45,
            }
        }
    },

    "Pharmaceuticals": {
        "match_keywords": [
            "pharmaceutical", "drug manufacturers", "biotechnology", "pharma"
        ],
        "key_metrics": [
            "rd_pct_of_revenue", "gross_margin", "export_revenue_pct"
        ],
        "benchmarks": {
            "India": {
                "rd_pct_good": 0.08,
            },
            "US": {
                "rd_pct_good": 0.15,    # US/global pharma typically spends more on R&D
            }
        }
    },

    "Energy": {
        "match_keywords": [
            "oil", "gas", "energy", "refining", "petroleum", "exploration"
        ],
        "key_metrics": [
            "roce", "refining_margin", "ev_ebitda"
        ],
        "benchmarks": {
            "India": {
                "roce_good": 0.12,
            },
            "US": {
                "roce_good": 0.10,
            }
        }
    },

    "Automobiles": {
        "match_keywords": [
            "auto", "automobile", "vehicles", "auto manufacturers", "auto parts"
        ],
        "key_metrics": [
            "volume_growth", "ebitda_margin", "ev_revenue_pct"
        ],
        "benchmarks": {
            "India": {
                "ebitda_margin_good": 0.10,
            },
            "US": {
                "ebitda_margin_good": 0.08,
            }
        }
    },
}


# -----------------------------------------------------------------------------
# GENERIC FALLBACK SECTOR
#
# If a company's industry doesn't match any of the sectors above, we use this.
# It contains no sector-specific metrics — only universal ones (handled in
# later sections). This ensures the module never crashes on an unrecognised
# sector; it just analyses with less depth.
# -----------------------------------------------------------------------------

GENERIC_SECTOR = {
    "name": "Generic",
    "key_metrics": [],
    "benchmarks": {
        "India": {},
        "US": {}
    }
}


# -----------------------------------------------------------------------------
# FUNCTION: detect_market
#
# Looks at the ticker's suffix to decide if this is an Indian or US stock.
#   .NS  -> NSE (India)
#   .BO  -> BSE (India)
#   (no suffix) -> assumed US
# -----------------------------------------------------------------------------

def detect_market(ticker):
    """
    Determines whether a ticker is Indian or US based on its suffix.

    Input:
        ticker (str): e.g. "INFY.NS", "RELIANCE.BO", "AAPL"

    Output:
        "India" or "US"
    """
    ticker_upper = ticker.upper()

    if ticker_upper.endswith(".NS") or ticker_upper.endswith(".BO"):
        return "India"
    else:
        return "US"


# -----------------------------------------------------------------------------
# FUNCTION: match_sector
#
# Takes the raw "sector" and "industry" strings from yfinance and tries to
# match them against our SECTOR_DEFINITIONS using keyword matching.
#
# Why keyword matching (not exact matching)?
# Yahoo Finance's industry strings vary a lot ("Software—Application" vs
# "Information Technology Services" vs "Software—Infrastructure"). Rather than
# listing every possible exact string, we check if any of our keywords
# APPEAR WITHIN the Yahoo Finance string.
# -----------------------------------------------------------------------------

def match_sector(yf_sector, yf_industry):
    """
    Matches Yahoo Finance's sector/industry strings to one of our
    internal SECTOR_DEFINITIONS entries.

    Input:
        yf_sector (str or None): e.g. "Technology"
        yf_industry (str or None): e.g. "Information Technology Services"

    Output:
        Tuple of (sector_name, sector_definition_dict)
        Returns ("Generic", GENERIC_SECTOR) if nothing matches.
    """
    # Combine sector and industry into one lowercase string for searching.
    # We lowercase everything so "Banks" and "banks" both match.
    search_text = f"{yf_sector or ''} {yf_industry or ''}".lower()

    # Loop through each sector definition and check if any of its
    # match_keywords appear inside search_text
    for sector_name, definition in SECTOR_DEFINITIONS.items():
        for keyword in definition["match_keywords"]:
            if keyword in search_text:
                # Found a match! Return immediately.
                return sector_name, definition

    # No match found in any sector definition
    return "Generic", GENERIC_SECTOR


# -----------------------------------------------------------------------------
# FUNCTION: get_sector_outlook
#
# Uses Tavily to search for the current outlook on a given sector, in a
# given market (India or US), and extracts a brief summary plus
# tailwinds/headwinds.
# -----------------------------------------------------------------------------

def get_sector_outlook(sector_name, market, fallback_industry=None):
    """
    Searches Tavily for the current outlook of a sector and extracts
    a structured summary.

    Input:
        sector_name (str): e.g. "IT Services", "Banking", or "Generic"
        market (str): "India" or "US"
        fallback_industry (str or None): Yahoo Finance's raw "industry" string
            (e.g. "Consumer Electronics"). Used ONLY when sector_name is
            "Generic" — gives Tavily something real to search for instead
            of the literal word "Generic".

    Output:
        A dictionary:
        {
            "summary": "...",
            "tailwinds": [...],
            "headwinds": [...],
            "sources": [...],
            "search_term_used": "..."   # NEW: shows what we actually searched for
        }
    """
    # Decide what term to actually search for.
    # If sector_name is "Generic" AND we have a fallback_industry, use that.
    # Otherwise, use sector_name as before.
    if sector_name == "Generic" and fallback_industry:
        search_term = fallback_industry
    else:
        search_term = sector_name

    print(f"\n[Sector Intelligence] Researching outlook for: {search_term} ({market})")

    # Build a search query using the chosen search_term
    query = f"{search_term} sector outlook {market} 2026 growth trends challenges"

    result = {
        "summary": None,
        "tailwinds": [],
        "headwinds": [],
        "sources": [],
        "search_term_used": search_term   # NEW: record what we searched for
    }

    # Check if we have a Tavily API key before trying
    if not TAVILY_API_KEY:
        result["summary"] = "Tavily API key not found — sector outlook unavailable."
        return result

    try:
        # Create a Tavily client using our API key
        client = TavilyClient(api_key=TAVILY_API_KEY)

        # Run the search. "search_depth=advanced" gives more thorough results.
        # "max_results=5" limits how many articles Tavily returns.
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=5
        )

        # Tavily returns a dictionary with a "results" list and sometimes
        # an "answer" field (a direct AI-generated summary of the search)
        if response.get("answer"):
            result["summary"] = response["answer"]

        # Collect source URLs and titles for transparency
        for item in response.get("results", []):
            result["sources"].append({
                "title": item.get("title"),
                "url": item.get("url")
            })

        # If Tavily didn't give us a direct "answer", build a basic summary
        # from the titles of the top results
        if not result["summary"] and result["sources"]:
            titles = [s["title"] for s in result["sources"][:3] if s["title"]]
            result["summary"] = "Recent coverage includes: " + "; ".join(titles)

        print(f"  [OK] Sector outlook retrieved ({len(result['sources'])} sources)")

    except Exception as e:
        # If Tavily fails (network issue, quota exceeded, etc.), don't crash —
        # just record the error so the rest of the module keeps working
        print(f"  [ERROR] Tavily search failed: {e}")
        result["summary"] = f"Sector outlook unavailable due to error: {e}"

    return result
# -----------------------------------------------------------------------------
# FUNCTION: run_sector_intelligence
#
# This is the main entry point for Section 1. It ties together:
#   1. Fetching sector/industry from yfinance
#   2. Detecting market (India/US)
#   3. Matching to our internal sector definitions
#   4. Getting the sector outlook via Tavily
# -----------------------------------------------------------------------------

def run_sector_intelligence(ticker):
    """
    Runs the full Section 1 process for a given ticker.

    Input:
        ticker (str): e.g. "INFY.NS", "AAPL"

    Output:
        A dictionary containing sector/market identification and outlook.
        Returns a dict with "success": False if data can't be fetched.
    """
    print("\n" + "=" * 60)
    print("SECTOR INTELLIGENCE")
    print("=" * 60)
    print(f"Ticker: {ticker}")

    # Step 1: Detect market based on ticker suffix
    market = detect_market(ticker)
    print(f"Market: {market}")

    # Step 2: Fetch sector/industry from Yahoo Finance
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
    except Exception as e:
        print(f"[ERROR] Could not fetch data from Yahoo Finance: {e}")
        return {"success": False, "error": str(e)}

    yf_sector = info.get("sector")
    yf_industry = info.get("industry")
    company_name = info.get("longName") or info.get("shortName") or ticker

    print(f"Sector (Yahoo): {yf_sector}")
    print(f"Industry (Yahoo): {yf_industry}")

    # Step 3: Match to our internal sector definitions
    matched_sector_name, sector_definition = match_sector(yf_sector, yf_industry)
    print(f"Matched Sector Definition: {matched_sector_name}")

    # Step 4: Get sector outlook via Tavily
    # If matched_sector_name is "Generic", pass Yahoo's raw industry string
    # as a fallback so Tavily has something real to search for.
    outlook = get_sector_outlook(matched_sector_name, market, fallback_industry=yf_industry)

    # Step 5: Assemble final result
    result = {
        "success": True,
        "ticker": ticker,
        "company_name": company_name,
        "market": market,
        "yf_sector": yf_sector,
        "yf_industry": yf_industry,
        "matched_sector": matched_sector_name,
        "sector_definition": sector_definition,
        "sector_outlook": outlook,
    }

    # Print a clean summary
    print("\n" + "-" * 40)
    print("SECTOR OUTLOOK")
    print("-" * 40)
    print(f"Summary: {outlook['summary']}")
    print(f"\nSources consulted:")
    for src in outlook["sources"]:
        print(f"  - {src['title']}")
        print(f"    {src['url']}")

    print("\n" + "=" * 60)

    return result


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Runs only when this file is executed directly.
# -----------------------------------------------------------------------------


# =============================================================================
# SECTION 2: DATA ENGINE
#
# Pulls three layers of financial data from yfinance:
#   Layer 1: Universal metrics (every company, every sector)
#   Layer 2: Sector-specific metrics (based on matched sector)
#   Layer 3: 5-year historical data (for trend detection in Section 4)
# =============================================================================


def safe_get(data_dict, key, default=None):
    """
    Safely retrieves a value from a dictionary without crashing.
    If the key doesn't exist or the dict is None, returns default (None).
    
    Why we need this:
    yfinance often returns incomplete data. Accessing a missing key
    normally causes a crash. This prevents that.
    """
    if data_dict is None:
        return default
    return data_dict.get(key, default)


def safe_divide(numerator, denominator, default=None):
    """
    Safely divides two numbers without crashing.
    Returns default if either value is None or denominator is zero.
    
    Why we need this:
    Several ratios (ROCE, Interest Coverage, etc.) involve division.
    If either number is missing, normal division would crash Python.
    """
    if numerator is None or denominator is None:
        return default
    if denominator == 0:
        return default
    try:
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default


def to_pct(value, decimal_places=2):
    """
    Converts a decimal ratio to a rounded percentage.
    Example: 0.173 → 17.3
    
    Why we need this:
    yfinance gives margins/ratios as decimals (0.173 means 17.3%).
    We store them as percentages for readability.
    Returns None if value is None (never crashes).
    """
    if value is None:
        return None
    try:
        return round(float(value) * 100, decimal_places)
    except (TypeError, ValueError):
        return None


def to_round(value, decimal_places=2):
    """
    Safely rounds a number to the specified decimal places.
    Returns None if value is None.
    """
    if value is None:
        return None
    try:
        return round(float(value), decimal_places)
    except (TypeError, ValueError):
        return None


# -----------------------------------------------------------------------------
# FUNCTION: fetch_raw_yfinance_data
#
# Downloads all raw data objects from yfinance in one place.
# Every other function in Section 2 reads from these objects.
# We fetch once and reuse — avoids making multiple network calls.
# -----------------------------------------------------------------------------

def fetch_raw_yfinance_data(ticker):
    """
    Downloads all available yfinance data for a ticker in one network call.

    Returns a dictionary containing:
        info           → flat dict of current/trailing metrics
        financials_df  → income statement DataFrame (annual, ~4 years)
        balance_df     → balance sheet DataFrame (annual, ~4 years)
        cashflow_df    → cash flow statement DataFrame (annual, ~4 years)
    
    Returns None for any DataFrame that fails to load (handled gracefully).
    """
    print(f"\n[Data Engine] Fetching raw data for: {ticker}")

    try:
        stock = yf.Ticker(ticker)

        # .info → the big flat dictionary (most current metrics)
        info = stock.info

        # Quick check: if info is completely empty, the ticker is invalid
        if not info or len(info) < 5:
            print(f"  [ERROR] Yahoo Finance returned no data for {ticker}")
            return None

        print(f"  [OK] info dictionary loaded ({len(info)} fields)")

    except Exception as e:
        print(f"  [ERROR] Could not load info for {ticker}: {e}")
        return None

    # Load each financial statement separately
    # We wrap each in try/except because any one can fail independently

    try:
        financials_df = stock.financials   # Income statement
        print(f"  [OK] Income statement loaded")
    except Exception as e:
        print(f"  [WARN] Income statement unavailable: {e}")
        financials_df = None

    try:
        balance_df = stock.balance_sheet   # Balance sheet
        print(f"  [OK] Balance sheet loaded")
    except Exception as e:
        print(f"  [WARN] Balance sheet unavailable: {e}")
        balance_df = None

    try:
        cashflow_df = stock.cashflow       # Cash flow statement
        print(f"  [OK] Cash flow statement loaded")
    except Exception as e:
        print(f"  [WARN] Cash flow statement unavailable: {e}")
        cashflow_df = None

    return {
        "info": info,
        "financials_df": financials_df,
        "balance_df": balance_df,
        "cashflow_df": cashflow_df
    }


# -----------------------------------------------------------------------------
# HELPER: get_df_value
#
# Reads a single value from a financial statement DataFrame.
# DataFrames have rows (financial line items) and columns (years).
# We almost always want the most recent year = column index 0.
# -----------------------------------------------------------------------------

def get_df_value(df, row_name_options, col_index=0):
    """
    Safely reads a value from a financial statement DataFrame.

    Why row_name_options is a LIST:
    yfinance uses slightly different row names across versions and markets.
    For example, net income might be stored as "Net Income" in one version
    and "Net Income Common Stockholders" in another.
    We try each option until one works.

    Input:
        df              → the DataFrame (financials, balance sheet, or cashflow)
        row_name_options → list of possible row names to try
        col_index       → which year column to read (0 = most recent)

    Output:
        The value if found, or None if nothing matched.
    """
    if df is None or df.empty:
        return None

    for row_name in row_name_options:
        if row_name in df.index:
            try:
                value = df.loc[row_name].iloc[col_index]
                # Check for pandas NA/NaN values
                import math
                if value is not None and not math.isnan(float(value)):
                    return float(value)
            except (IndexError, TypeError, ValueError):
                continue

    return None


# -----------------------------------------------------------------------------
# FUNCTION: fetch_universal_metrics
#
# Calculates the core metrics that apply to EVERY company regardless of sector.
# These are the foundation of any investment analysis.
# -----------------------------------------------------------------------------

def fetch_universal_metrics(ticker, raw_data, market):
    """
    Pulls and calculates universal financial metrics from yfinance data.

    Universal metrics (calculated for every company, every sector):
        - Revenue (TTM and growth)
        - EPS and EPS growth
        - Profitability (gross margin, operating margin, net margin)
        - ROE, ROCE (manually calculated)
        - Debt/Equity, Interest Coverage
        - Free Cash Flow
        - Valuation (P/E, P/B, EV/EBITDA, EV/Revenue)

    Input:
        ticker   → stock ticker string
        raw_data → dictionary returned by fetch_raw_yfinance_data()
        market   → "India" or "US"

    Output:
        Dictionary of universal metrics with values and metadata.
    """
    print("\n[Data Engine] Calculating universal metrics...")

    info         = raw_data["info"]
    financials_df = raw_data["financials_df"]
    balance_df   = raw_data["balance_df"]
    cashflow_df  = raw_data["cashflow_df"]

    # ------------------------------------------------------------------
    # REVENUE
    # TTM = Trailing Twelve Months (most recent 12 months of revenue)
    # ------------------------------------------------------------------
    revenue_ttm = safe_get(info, "totalRevenue")

    # Revenue from 1 year ago — stored in the income statement DataFrame
    # column index 0 = most recent year, index 1 = one year prior
    revenue_prior = get_df_value(
        financials_df,
        ["Total Revenue", "Revenue"],
        col_index=1   # One year back
    )

    # Revenue growth = (current - prior) / prior × 100
    revenue_growth_pct = to_pct(
        safe_divide(
            (revenue_ttm - revenue_prior) if (revenue_ttm and revenue_prior) else None,
            revenue_prior
        )
    )

    # ------------------------------------------------------------------
    # EARNINGS PER SHARE (EPS)
    # EPS = Net Income / Total Shares Outstanding
    # Tells us how much profit each share "earns"
    # ------------------------------------------------------------------
    eps_trailing = safe_get(info, "trailingEps")
    eps_forward  = safe_get(info, "forwardEps")   # Analyst estimate for next year

    # EPS growth = (forward - trailing) / |trailing| × 100
    # We use abs() on trailing because if trailing EPS is negative,
    # the growth % direction would otherwise be inverted
    eps_growth_pct = to_pct(
        safe_divide(
            (eps_forward - eps_trailing) if (eps_forward and eps_trailing) else None,
            abs(eps_trailing) if eps_trailing else None
        )
    )

    # ------------------------------------------------------------------
    # PROFITABILITY MARGINS
    # All come directly from yfinance's info dict as decimals
    # We convert to percentages using to_pct()
    # ------------------------------------------------------------------
    gross_margin_pct     = to_pct(safe_get(info, "grossMargins"))
    operating_margin_pct = to_pct(safe_get(info, "operatingMargins"))
    net_margin_pct       = to_pct(safe_get(info, "profitMargins"))

    # ------------------------------------------------------------------
    # ROE (Return on Equity)
    # = Net Income / Shareholders' Equity
    # Measures how efficiently the company uses shareholder money
    # yfinance gives this directly
    # ------------------------------------------------------------------
    roe_pct = to_pct(safe_get(info, "returnOnEquity"))

    # ------------------------------------------------------------------
    # ROCE (Return on Capital Employed) — MANUALLY CALCULATED
    # = EBIT / Capital Employed
    # EBIT = Operating Income (Earnings Before Interest and Tax)
    # Capital Employed = Total Assets - Current Liabilities
    #
    # Why manually?
    # yfinance doesn't provide ROCE directly.
    # We have to build it from raw components ourselves.
    # ------------------------------------------------------------------

    # Get EBIT from the income statement DataFrame
    ebit = get_df_value(
        financials_df,
        ["Operating Income", "EBIT", "Ebit"],
        col_index=0
    )

    # Get Total Assets and Current Liabilities from the balance sheet
    total_assets = get_df_value(
        balance_df,
        ["Total Assets"],
        col_index=0
    )

    current_liabilities = get_df_value(
        balance_df,
        ["Current Liabilities", "Total Current Liabilities"],
        col_index=0
    )

    # Capital Employed = Total Assets - Current Liabilities
    capital_employed = None
    if total_assets is not None and current_liabilities is not None:
        capital_employed = total_assets - current_liabilities

    # ROCE = EBIT / Capital Employed
    roce_pct = to_pct(safe_divide(ebit, capital_employed))

    # ------------------------------------------------------------------
    # DEBT / EQUITY RATIO
    # = Total Debt / Shareholders' Equity
    # Higher = more leveraged = more financial risk
    # yfinance gives us both components
    # ------------------------------------------------------------------
    total_debt = safe_get(info, "totalDebt")
    book_value_per_share = safe_get(info, "bookValue")
    shares_outstanding   = safe_get(info, "sharesOutstanding")

    # Shareholders' equity = book value per share × shares outstanding
    shareholders_equity = None
    if book_value_per_share and shares_outstanding:
        shareholders_equity = book_value_per_share * shares_outstanding

    # Try to get equity directly from balance sheet as a fallback
    if shareholders_equity is None:
        shareholders_equity = get_df_value(
            balance_df,
            ["Stockholders Equity", "Total Stockholder Equity",
             "Total Equity Gross Minority Interest"],
            col_index=0
        )

    debt_to_equity = to_round(safe_divide(total_debt, shareholders_equity))

    # ------------------------------------------------------------------
    # INTEREST COVERAGE RATIO
    # = EBIT / Interest Expense
    # Answers: "Can the company comfortably pay its interest bills?"
    # Above 3x = generally safe. Below 1.5x = concern.
    # ------------------------------------------------------------------
    interest_expense = get_df_value(
        financials_df,
        ["Interest Expense", "Interest Expense Non Operating"],
        col_index=0
    )

    # Interest expense is often stored as a negative number in DataFrames
    # We take the absolute value to make the ratio positive
    if interest_expense is not None:
        interest_expense = abs(interest_expense)

    interest_coverage = to_round(safe_divide(ebit, interest_expense))

    # ------------------------------------------------------------------
    # FREE CASH FLOW (FCF)
    # = Operating Cash Flow - Capital Expenditures
    # "Real" money left over after running and maintaining the business.
    # FCF is harder to manipulate than net income — analysts trust it more.
    # ------------------------------------------------------------------
    fcf = safe_get(info, "freeCashflow")

    # If not in info dict, calculate manually from cash flow statement
    if fcf is None:
        operating_cf = get_df_value(
            cashflow_df,
            ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
            col_index=0
        )
        capex = get_df_value(
            cashflow_df,
            ["Capital Expenditure", "Purchase Of Plant Property And Equipment"],
            col_index=0
        )
        # Capex is usually stored as a negative number (cash going out)
        if operating_cf is not None and capex is not None:
            fcf = operating_cf + capex   # + because capex is already negative

    # ------------------------------------------------------------------
    # VALUATION RATIOS
    # These tell us how expensive or cheap the stock is
    # relative to its earnings, assets, and sales
    # ------------------------------------------------------------------
    pe_trailing  = to_round(safe_get(info, "trailingPE"))
    pe_forward   = to_round(safe_get(info, "forwardPE"))
    pb_ratio     = to_round(safe_get(info, "priceToBook"))
    ps_ratio     = to_round(safe_get(info, "priceToSalesTrailing12Months"))
    ev_ebitda    = to_round(safe_get(info, "enterpriseToEbitda"))
    ev_revenue   = to_round(safe_get(info, "enterpriseToRevenue"))

    # Current stock price and market cap
    current_price = (
        safe_get(info, "currentPrice") or
        safe_get(info, "regularMarketPrice") or
        safe_get(info, "previousClose")
    )
    market_cap    = safe_get(info, "marketCap")

    # ------------------------------------------------------------------
    # ASSEMBLE RESULT
    # ------------------------------------------------------------------
    result = {
        # Revenue
        "revenue_ttm":          revenue_ttm,
        "revenue_prior_year":   revenue_prior,
        "revenue_growth_pct":   revenue_growth_pct,

        # EPS
        "eps_trailing":         to_round(eps_trailing),
        "eps_forward":          to_round(eps_forward),
        "eps_growth_pct":       eps_growth_pct,

        # Margins
        "gross_margin_pct":     gross_margin_pct,
        "operating_margin_pct": operating_margin_pct,
        "net_margin_pct":       net_margin_pct,

        # Returns
        "roe_pct":              roe_pct,
        "roce_pct":             roce_pct,

        # Leverage
        "total_debt":           total_debt,
        "shareholders_equity":  shareholders_equity,
        "debt_to_equity":       debt_to_equity,
        "interest_coverage":    interest_coverage,

        # Cash flow
        "free_cash_flow":       fcf,

        # Valuation
        "current_price":        to_round(current_price),
        "market_cap":           market_cap,
        "pe_trailing":          pe_trailing,
        "pe_forward":           pe_forward,
        "pb_ratio":             pb_ratio,
        "ps_ratio":             ps_ratio,
        "ev_ebitda":            ev_ebitda,
        "ev_revenue":           ev_revenue,

        # Supporting values used in later sections
        "_ebit":                ebit,
        "_capital_employed":    capital_employed,
        "_interest_expense":    interest_expense,
        "_shareholders_equity": shareholders_equity,
    }

    print(f"  [OK] Universal metrics calculated")
    return result


# -----------------------------------------------------------------------------
# FUNCTION: fetch_sector_specific_metrics
#
# Calculates metrics that only apply to specific sectors.
# Uses the matched sector name from Section 1 to decide what to calculate.
# If the sector is Generic or unrecognised, returns an empty dict (no crash).
# -----------------------------------------------------------------------------

def fetch_sector_specific_metrics(ticker, raw_data, sector_name, market):
    """
    Calculates sector-specific metrics based on the matched sector.

    Each sector block is independent — only the matching one runs.
    Adding a new sector later = adding a new elif block here.

    Input:
        ticker      → stock ticker
        raw_data    → dict from fetch_raw_yfinance_data()
        sector_name → matched sector name from Section 1
        market      → "India" or "US"

    Output:
        Dictionary of sector-specific metrics (empty dict if Generic).
    """
    print(f"\n[Data Engine] Calculating sector-specific metrics for: {sector_name}")

    info          = raw_data["info"]
    financials_df = raw_data["financials_df"]
    balance_df    = raw_data["balance_df"]
    cashflow_df   = raw_data["cashflow_df"]

    metrics = {}

    # ------------------------------------------------------------------
    # IT SERVICES
    # Key metrics: Operating Margin, Revenue per Employee
    # ------------------------------------------------------------------
    if sector_name == "IT Services":

        # Operating margin — how much operating profit per rupee of revenue
        metrics["operating_margin_pct"] = to_pct(safe_get(info, "operatingMargins"))

        # Revenue per employee — efficiency metric for IT companies
        # Higher = more productive workforce
        revenue      = safe_get(info, "totalRevenue")
        employees    = safe_get(info, "fullTimeEmployees")
        rev_per_emp  = safe_divide(revenue, employees)
        metrics["revenue_per_employee"] = to_round(rev_per_emp)
        metrics["total_employees"]      = employees

    # ------------------------------------------------------------------
    # BANKING
    # Key metrics: NIM, GNPA/NPL, CASA ratio, Capital Adequacy
    # Note: These are NOT available in yfinance for most Indian banks.
    # We fetch what we can and flag the rest as unavailable.
    # The Peer Engine (Section 3) + Analysis Engine (Section 4) will
    # use the Tavily fallback for these when missing.
    # ------------------------------------------------------------------
    elif sector_name == "Banking":

        # Net Interest Margin — not directly available in yfinance
        # We flag it as None and note it needs manual/alternative sourcing
        metrics["net_interest_margin_pct"] = None
        metrics["nim_note"] = "NIM not available via yfinance — use screener.in or annual report"

        # For US banks, yfinance sometimes has net interest income
        net_interest_income = get_df_value(
            financials_df,
            ["Net Interest Income"],
            col_index=0
        )
        metrics["net_interest_income"] = net_interest_income

        # Capital Adequacy / Equity ratios as proxies
        metrics["equity_to_assets_pct"] = to_pct(
            safe_divide(
                get_df_value(
                    balance_df,
                    ["Stockholders Equity", "Total Stockholder Equity"],
                    col_index=0
                ),
                get_df_value(balance_df, ["Total Assets"], col_index=0)
            )
        )

    # ------------------------------------------------------------------
    # FMCG
    # Key metrics: Gross Margin, Advertising % of Revenue
    # ------------------------------------------------------------------
    elif sector_name == "FMCG":

        metrics["gross_margin_pct"] = to_pct(safe_get(info, "grossMargins"))

        # Advertising spend as % of revenue
        # yfinance doesn't break this out, so we flag it
        metrics["advertising_pct_note"] = "Ad spend % not available via yfinance — use annual report"

    # ------------------------------------------------------------------
    # PHARMACEUTICALS
    # Key metrics: R&D spend %, Gross Margin
    # ------------------------------------------------------------------
    elif sector_name == "Pharmaceuticals":

        metrics["gross_margin_pct"] = to_pct(safe_get(info, "grossMargins"))

        # R&D as % of revenue
        rd_expense = get_df_value(
            financials_df,
            ["Research And Development", "Research Development"],
            col_index=0
        )
        revenue = safe_get(info, "totalRevenue")
        metrics["rd_expense"]     = rd_expense
        metrics["rd_pct_revenue"] = to_pct(safe_divide(rd_expense, revenue))

    # ------------------------------------------------------------------
    # ENERGY
    # Key metrics: ROCE, EV/EBITDA (calculated in universal metrics)
    # Most energy-specific metrics (refining margins) not in yfinance
    # ------------------------------------------------------------------
    elif sector_name == "Energy":

        metrics["ev_ebitda"]     = to_round(safe_get(info, "enterpriseToEbitda"))
        metrics["refining_margin_note"] = "Refining margins not available via yfinance — use company reports"

    # ------------------------------------------------------------------
    # AUTOMOBILES
    # Key metrics: EBITDA margin, Revenue growth (volume proxy)
    # ------------------------------------------------------------------
    elif sector_name == "Automobiles":

        # EBITDA margin
        ebitda   = safe_get(info, "ebitda")
        revenue  = safe_get(info, "totalRevenue")
        metrics["ebitda_margin_pct"] = to_pct(safe_divide(ebitda, revenue))
        metrics["volume_growth_note"] = "Volume growth not available via yfinance — use company press releases"

    # ------------------------------------------------------------------
    # GENERIC / UNRECOGNISED SECTOR
    # No sector-specific metrics — universal metrics only
    # ------------------------------------------------------------------
    else:
        print(f"  [Note] No sector-specific metrics defined for '{sector_name}' — universal metrics only")

    print(f"  [OK] Sector-specific metrics calculated ({len(metrics)} fields)")
    return metrics


# -----------------------------------------------------------------------------
# FUNCTION: fetch_historical_data
#
# Pulls 4-5 years of annual data for key metrics.
# Used by the Analysis Engine (Section 4) to detect trend breaks and
# compare current performance against the company's own history.
# -----------------------------------------------------------------------------

def fetch_historical_data(raw_data):
    """
    Extracts multi-year historical data from financial statement DataFrames.

    Why 4-5 years?
    A single year's number tells you nothing. 5 years of data tells you
    whether metrics are improving, declining, or stable — the trend.

    Output:
        Dictionary with lists of annual values for key metrics.
        Each list is ordered most recent → oldest.
        Example: revenue_history = [1200Cr, 1050Cr, 900Cr, 780Cr]
    """
    print("\n[Data Engine] Extracting historical data...")

    financials_df = raw_data["financials_df"]
    balance_df    = raw_data["balance_df"]
    cashflow_df   = raw_data["cashflow_df"]

    history = {
        "years":              [],   # Year labels e.g. ["2024", "2023", "2022", "2021"]
        "revenue":            [],
        "net_income":         [],
        "operating_income":   [],
        "gross_profit":       [],
        "operating_cashflow": [],
        "capex":              [],
        "total_debt":         [],
        "shareholders_equity":[],
    }

    # Helper to extract a full row from a DataFrame as a list
    def extract_row(df, row_name_options):
        """Returns a list of values for a row across all available years."""
        if df is None or df.empty:
            return []
        for row_name in row_name_options:
            if row_name in df.index:
                try:
                    import math
                    values = []
                    for v in df.loc[row_name]:
                        try:
                            fv = float(v)
                            values.append(None if math.isnan(fv) else fv)
                        except (TypeError, ValueError):
                            values.append(None)
                    return values
                except Exception:
                    continue
        return []

    # Extract year labels from the DataFrame column headers
    if financials_df is not None and not financials_df.empty:
        history["years"] = [str(col)[:4] for col in financials_df.columns]

    # Income statement items
    history["revenue"] = extract_row(
        financials_df, ["Total Revenue", "Revenue"]
    )
    history["net_income"] = extract_row(
        financials_df, ["Net Income", "Net Income Common Stockholders"]
    )
    history["operating_income"] = extract_row(
        financials_df, ["Operating Income", "EBIT", "Ebit"]
    )
    history["gross_profit"] = extract_row(
        financials_df, ["Gross Profit"]
    )

    # Cash flow items
    history["operating_cashflow"] = extract_row(
        cashflow_df, ["Operating Cash Flow",
                      "Cash Flow From Continuing Operating Activities"]
    )
    history["capex"] = extract_row(
        cashflow_df, ["Capital Expenditure",
                      "Purchase Of Plant Property And Equipment"]
    )

    # Balance sheet items
    history["total_debt"] = extract_row(
        balance_df, ["Total Debt", "Long Term Debt And Capital Lease Obligation"]
    )
    history["shareholders_equity"] = extract_row(
        balance_df, ["Stockholders Equity", "Total Stockholder Equity",
                     "Total Equity Gross Minority Interest"]
    )

    # Derive net margin history if we have both revenue and net income
    if history["revenue"] and history["net_income"]:
        history["net_margin_pct"] = []
        for rev, ni in zip(history["revenue"], history["net_income"]):
            margin = to_pct(safe_divide(ni, rev))
            history["net_margin_pct"].append(margin)
    else:
        history["net_margin_pct"] = []

    # Derive operating margin history
    if history["revenue"] and history["operating_income"]:
        history["operating_margin_pct"] = []
        for rev, oi in zip(history["revenue"], history["operating_income"]):
            margin = to_pct(safe_divide(oi, rev))
            history["operating_margin_pct"].append(margin)
    else:
        history["operating_margin_pct"] = []

    years_available = len(history["years"])
    print(f"  [OK] Historical data extracted ({years_available} years available)")

    return history


# -----------------------------------------------------------------------------
# FUNCTION: print_data_engine_summary
#
# Prints a readable summary of Section 2's output to the terminal.
# For testing and verification — not part of the final report output.
# -----------------------------------------------------------------------------

def print_data_engine_summary(universal, sector_specific, historical, sector_name, market):
    """Prints a clean terminal summary of all Data Engine outputs."""

    currency = "₹" if market == "India" else "$"

    def fmt_number(n):
        """Formats large numbers into readable Cr/B/M strings."""
        if n is None:
            return "N/A"
        n = float(n)
        if market == "India":
            if abs(n) >= 1e12:
                return f"{currency}{n/1e7:.0f} Cr"     # Crores (Indian standard)
            elif abs(n) >= 1e9:
                return f"{currency}{n/1e7:.0f} Cr"
            elif abs(n) >= 1e7:
                return f"{currency}{n/1e7:.1f} Cr"
            else:
                return f"{currency}{n:,.0f}"
        else:
            if abs(n) >= 1e12:
                return f"{currency}{n/1e12:.2f}T"
            elif abs(n) >= 1e9:
                return f"{currency}{n/1e9:.2f}B"
            elif abs(n) >= 1e6:
                return f"{currency}{n/1e6:.2f}M"
            else:
                return f"{currency}{n:,.0f}"

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "N/A"

    print("\n" + "=" * 60)
    print("DATA ENGINE SUMMARY")
    print("=" * 60)

    print(f"\n── UNIVERSAL METRICS ──────────────────────────────")
    print(f"  Revenue (TTM):         {fmt_number(universal.get('revenue_ttm'))}")
    print(f"  Revenue Growth YoY:    {fmt(universal.get('revenue_growth_pct'), '%')}")
    print(f"  EPS (Trailing):        {fmt(universal.get('eps_trailing'))}")
    print(f"  EPS (Forward):         {fmt(universal.get('eps_forward'))}")
    print(f"  EPS Growth:            {fmt(universal.get('eps_growth_pct'), '%')}")
    print(f"  Gross Margin:          {fmt(universal.get('gross_margin_pct'), '%')}")
    print(f"  Operating Margin:      {fmt(universal.get('operating_margin_pct'), '%')}")
    print(f"  Net Margin:            {fmt(universal.get('net_margin_pct'), '%')}")
    print(f"  ROE:                   {fmt(universal.get('roe_pct'), '%')}")
    print(f"  ROCE:                  {fmt(universal.get('roce_pct'), '%')}")
    print(f"  Debt / Equity:         {fmt(universal.get('debt_to_equity'))}")
    print(f"  Interest Coverage:     {fmt(universal.get('interest_coverage'), 'x')}")
    print(f"  Free Cash Flow:        {fmt_number(universal.get('free_cash_flow'))}")
    print(f"  P/E (Trailing):        {fmt(universal.get('pe_trailing'))}")
    print(f"  P/E (Forward):         {fmt(universal.get('pe_forward'))}")
    print(f"  P/B Ratio:             {fmt(universal.get('pb_ratio'))}")
    print(f"  EV/EBITDA:             {fmt(universal.get('ev_ebitda'))}")

    print(f"\n── SECTOR-SPECIFIC METRICS ({sector_name}) ─────────────")
    if sector_specific:
        for key, value in sector_specific.items():
            if not key.endswith("_note"):   # Skip note fields in summary
                label = key.replace("_", " ").title()
                print(f"  {label:<28} {fmt(value)}")
        # Print notes separately
        for key, value in sector_specific.items():
            if key.endswith("_note"):
                print(f"  [Note] {value}")
    else:
        print("  No sector-specific metrics available.")

    print(f"\n── HISTORICAL DATA ({len(historical.get('years', []))} years) ────────────────────")
    if historical.get("years"):
        print(f"  Years:            {historical['years']}")
        print(f"  Revenue:          {[fmt_number(r) for r in historical['revenue']]}")
        print(f"  Net Margin %:     {historical.get('net_margin_pct', [])}")
        print(f"  Operating Margin: {historical.get('operating_margin_pct', [])}")
    else:
        print("  No historical data available.")

    print("\n" + "=" * 60)


# -----------------------------------------------------------------------------
# FUNCTION: run_data_engine
#
# Main entry point for Section 2.
# Calls fetch_raw_yfinance_data(), then all three extraction functions,
# then prints a summary.
# Takes the Section 1 result as input so it knows the sector and market.
# -----------------------------------------------------------------------------

def run_data_engine(ticker, sector_intelligence_result):
    """
    Orchestrates the full Section 2 data pull.

    Input:
        ticker                    → stock ticker string
        sector_intelligence_result → dictionary returned by run_sector_intelligence()

    Output:
        Dictionary containing:
            universal        → universal metrics dict
            sector_specific  → sector-specific metrics dict
            historical       → historical data dict
        Returns None if raw data fetch fails.
    """
    print("\n" + "=" * 60)
    print("DATA ENGINE")
    print("=" * 60)

    # Extract what we need from the Section 1 result
    sector_name = sector_intelligence_result.get("matched_sector", "Generic")
    market      = sector_intelligence_result.get("market", "US")

    # Step 1: Fetch all raw data from yfinance in one call
    raw_data = fetch_raw_yfinance_data(ticker)

    if raw_data is None:
        print("[ERROR] Data Engine could not fetch raw data. Aborting.")
        return None

    # Step 2: Calculate universal metrics (every company)
    universal = fetch_universal_metrics(ticker, raw_data, market)

    # Step 3: Calculate sector-specific metrics (based on matched sector)
    sector_specific = fetch_sector_specific_metrics(
        ticker, raw_data, sector_name, market
    )

    # Step 4: Extract historical data (for trend analysis in Section 4)
    historical = fetch_historical_data(raw_data)

    # Step 5: Print summary to terminal
    print_data_engine_summary(universal, sector_specific, historical, sector_name, market)

    return {
        "universal":       universal,
        "sector_specific": sector_specific,
        "historical":      historical,
    }

    # -----------------------------------------------------------------------------
# STANDALONE TEST
# Runs only when this file is executed directly.
# -----------------------------------------------------------------------------

# =============================================================================
# SECTION 3: PEER ENGINE
#
# Dynamically finds the top 5 peers for a given company using Yahoo Finance's
# industry classification, sorted by market cap.
# Falls back to Tavily if fewer than 3 peers are found via the screener.
# Fetches key comparison metrics for each peer.
# =============================================================================


# -----------------------------------------------------------------------------
# FUNCTION: find_peers_via_screener
#
# Uses yfinance's built-in screener to find companies in the same industry.
# Filters by market (India vs US) and sorts by market cap descending.
# -----------------------------------------------------------------------------

def find_peers_via_screener(yf_industry, yf_sector, market, exclude_ticker, exclude_company_name=None):
    """
    Searches Yahoo Finance using a proper equity query — filtering by
    sector AND region/exchange directly — instead of filtering today's
    "most active" stocks (which was the old, broken approach).

    Input:
        yf_industry    → Yahoo Finance industry string (e.g. "Information Technology Services")
        yf_sector      → Yahoo Finance sector string (e.g. "Technology")
        market         → "India" or "US"
        exclude_ticker → the company we're analysing (remove it from peer list)

    Output:
        List of peer dictionaries, each containing ticker and basic info.
        Empty list if nothing found.
    """
    print(f"\n[Peer Engine] Searching for peers in: {yf_industry} ({market})")

    if not yf_sector:
        print("  [WARN] No sector available to search with — skipping screener")
        return []

    peers = []

    try:
        # Build a proper equity query.
        # EquityQuery lets us combine conditions with AND/OR logic.
        # We filter by:
        #   1. sector  → matches Yahoo's broad sector classification
        #   2. region  → "in" for India, "us" for United States
        #
        # Why filter by sector (not industry)?
        # Yahoo's screener query field accepts sector names reliably,
        # but exact industry string matching via query is inconsistent.
        # We fetch a wider net by sector, then narrow by industry in
        # Python afterwards (same as before) — but this time the
        # STARTING universe is correct (all stocks in that sector/region),
        # not just today's random "most active" list.

        region_code = "in" if market == "India" else "us"

        query = yf.EquityQuery(
            "and",
            [
                yf.EquityQuery("eq", ["sector", yf_sector]),
                yf.EquityQuery("eq", ["region", region_code]),
            ]
        )

        # Run the screener with our custom query, sorted by market cap descending
        screen_result = yf.screen(
            query,
            sortField="intradaymarketcap",
            sortAsc=False,
            size=100   # Get up to 100 candidates from this sector/region
        )

        quotes = screen_result.get("quotes", [])

        if not quotes:
            print("  [WARN] Screener returned no results for this sector/region")
            return []

        print(f"  Screener returned {len(quotes)} candidates in {yf_sector} ({region_code.upper()}) — filtering...")

        for quote in quotes:
            ticker = quote.get("symbol", "")

            # Skip the company we're analysing — check both ticker AND name,
            # since the same company can appear under a different exchange
            # suffix (e.g. we're analysing INFY.NS but INFY.BO shows up too)
            if ticker.upper() == exclude_ticker.upper():
                continue

            if exclude_company_name:
                candidate_name = (quote.get("longName") or quote.get("shortName") or "").lower()
                exclude_name_clean = exclude_company_name.lower().replace("limited", "").replace("ltd", "").replace("inc.", "").replace("inc", "").strip()
                candidate_name_clean = candidate_name.replace("limited", "").replace("ltd", "").replace("inc.", "").replace("inc", "").strip()

                if exclude_name_clean and exclude_name_clean in candidate_name_clean:
                    continue

            # Double-check market suffix matches expectation
            if market == "India":
                if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
                    continue
            else:
                if "." in ticker:
                    continue

            # Narrow further by industry where available (more precise match)
            quote_industry = quote.get("industry", "") or ""
            quote_sector   = quote.get("sector", "") or ""

            # Use partial matching instead of exact string equality.
            # Yahoo's industry field is often blank or inconsistently filled
            # for screener results, even for genuine peers. We check if the
            # two industry strings share meaningful overlapping words instead
            # of requiring an exact match.
            industry_match = False
            if yf_industry and quote_industry:
                target_words = set(yf_industry.lower().split())
                candidate_words = set(quote_industry.lower().split())
                # Remove common filler words that don't indicate real similarity
                filler_words = {"and", "of", "the", "&", "-", "services", "products"}
                target_words -= filler_words
                candidate_words -= filler_words

                # If at least 2 meaningful words overlap, count it as a match
                # (e.g. "Information Technology Services" vs "Software—Application"
                # would share "information"/"technology" type overlap in some cases)
                overlap = target_words & candidate_words
                industry_match = len(overlap) >= 1

            peers.append({
                "ticker":       ticker,
                "name":         quote.get("longName") or quote.get("shortName") or ticker,
                "market_cap":   quote.get("marketCap") or quote.get("intradaymarketcap"),
                "industry":     quote_industry,   # may be empty from screener — verified below
                "sector":       quote_sector,
                "match_type":   "industry" if industry_match else "sector",
            })


        # ------------------------------------------------------------------
        # VERIFY INDUSTRY FOR TOP CANDIDATES
        #
        # The screener's bulk results often have an empty "industry" field,
        # even for genuine peers. This means our industry_match check was
        # silently failing for everyone, letting irrelevant sector-mates
        # (like LG Electronics India under "Technology") slip into the
        # final top-5 purely by market cap.
        #
        # Fix: take the top 15 candidates by market cap, look up each one's
        # REAL industry via yfinance .info (same reliable source we used in
        # Section 1), and re-check the match. This costs a few extra API
        # calls but ensures the final 5 are genuine industry peers wherever
        # possible.
        # ------------------------------------------------------------------

        # Sort by market cap first so we check the most relevant (largest) candidates
        peers.sort(key=lambda x: -(x.get("market_cap") or 0))

        # Only verify the top 15 — checking all 100 would be slow and unnecessary
        candidates_to_verify = peers[:15]

        print(f"  Verifying real industry for top {len(candidates_to_verify)} candidates...")

        verified_peers = []
        for p in candidates_to_verify:
            try:
                detail_info = yf.Ticker(p["ticker"]).info
                real_industry = detail_info.get("industry", "") or ""

                # Re-run the same fuzzy match logic using the VERIFIED industry
                real_match = False
                if yf_industry and real_industry:
                    target_words = set(yf_industry.lower().split())
                    candidate_words = set(real_industry.lower().split())
                    filler_words = {"and", "of", "the", "&", "-", "services", "products"}
                    target_words -= filler_words
                    candidate_words -= filler_words
                    overlap = target_words & candidate_words
                    real_match = len(overlap) >= 1

                p["industry"] = real_industry
                p["match_type"] = "industry" if real_match else "sector"
                verified_peers.append(p)

            except Exception:
                # If verification fails, keep the original sector-only classification
                verified_peers.append(p)

        peers = verified_peers

        # ------------------------------------------------------------------
        # DE-DUPLICATE BY COMPANY NAME
        #
        # Why we need this:
        # The same company often appears twice — once for each exchange
        # listing (e.g. TCS.NS and TCS.BO are both Tata Consultancy Services).
        # We keep only ONE listing per company — whichever has the higher
        # market cap (usually the more liquid/primary listing).
        #
        # We also exclude the company itself if its name matches the
        # company we're analysing (catches cases like INFY.BO showing up
        # as a "peer" of INFY.NS).
        # ------------------------------------------------------------------

        deduplicated = {}   # key = normalised company name, value = peer dict

        for p in peers:
            # Normalise the name: lowercase, strip common suffixes like
            # "Limited", "Ltd", "Inc", "Corp" so name matching is more reliable
            name_normalised = (
                p["name"].lower()
                .replace("limited", "")
                .replace("ltd.", "")
                .replace("ltd", "")
                .replace("inc.", "")
                .replace("inc", "")
                .replace("corp.", "")
                .replace("corp", "")
                .replace(",", "")
                .strip()
            )

            if name_normalised in deduplicated:
                # We've seen this company before — keep whichever has higher market cap
                existing_mc = deduplicated[name_normalised].get("market_cap") or 0
                new_mc = p.get("market_cap") or 0
                if new_mc > existing_mc:
                    deduplicated[name_normalised] = p
            else:
                deduplicated[name_normalised] = p

        peers = list(deduplicated.values())

        # ------------------------------------------------------------------
        # STRICT INDUSTRY FILTER
        #
        # Only keep peers that passed the industry verification check.
        # Sector-only matches are dropped entirely — they are too
        # loosely related to be meaningful comparisons.
        #
        # We deliberately accept a smaller peer list (even 1-2 peers)
        # over a full list of 5 that includes irrelevant companies.
        # Wrong peers corrupt scores more than fewer peers do.
        # ------------------------------------------------------------------
        peers = [p for p in peers if p.get("match_type") == "industry"]

        # Sort verified industry peers by market cap descending
        peers.sort(key=lambda x: -(x.get("market_cap") or 0))

        # Take top 5
        peers = peers[:5]

        if not peers:
            print("  [NOTE] No peers passed the industry verification filter.")
            print("         Consider providing manual peers for comparison.")

        print(f"  [OK] Found {len(peers)} peers via screener")
        for i, p in enumerate(peers, 1):
            mc = p.get("market_cap")
            mc_str = f"${mc/1e9:.1f}B" if mc else "N/A"
            print(f"    {i}. {p['ticker']:<15} {p['name'][:30]:<30} ({p['match_type']}) MarketCap: {mc_str}")

        # Check confidence: if NONE of the peers are exact industry matches,
        # this comparison is lower quality — flag it so later sections
        # (Analysis Engine, reports) can show appropriate caution
        exact_industry_matches = sum(1 for p in peers if p["match_type"] == "industry")
        if peers and exact_industry_matches == 0:
            print(f"  [WARN] No exact industry matches found — all {len(peers)} peers are "
                  f"broad sector matches only. Comparison confidence is LOW.")

    except Exception as e:
        print(f"  [ERROR] Screener search failed: {e}")
        peers = []

    return peers


# -----------------------------------------------------------------------------
# FUNCTION: find_peers_via_tavily
#
# Fallback peer finder using Tavily search.
# Called when screener returns fewer than 3 peers.
# Searches for competitor names and tries to extract NSE/BSE tickers.
# -----------------------------------------------------------------------------

def find_peers_via_tavily(company_name, yf_industry, market, exclude_ticker):
    """
    Uses Tavily to find competitor company names when the screener fails.

    This is a best-effort fallback. Tavily returns text from articles —
    we extract company names and attempt to find their tickers via yfinance.

    Input:
        company_name   → e.g. "Infosys Limited"
        yf_industry    → e.g. "Information Technology Services"
        market         → "India" or "US"
        exclude_ticker → ticker to exclude

    Output:
        List of peer dictionaries (same format as find_peers_via_screener).
        May be empty if nothing useful is found.
    """
    print(f"\n[Peer Engine] Screener insufficient — trying Tavily fallback...")

    if not TAVILY_API_KEY:
        print("  [WARN] No Tavily API key — cannot use fallback")
        return []

    peers = []

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)

        # Build a targeted search query
        exchange = "NSE India" if market == "India" else "US stock market"
        query = f"top competitors of {company_name} in {yf_industry} listed on {exchange} 2025"

        response = client.search(
            query=query,
            search_depth="basic",   # Basic is fine for this — saves quota
            max_results=5
        )

        # Collect all text content from results
        all_text = ""
        for item in response.get("results", []):
            all_text += " " + (item.get("content") or item.get("title") or "")

        if not all_text.strip():
            print("  [WARN] Tavily returned no useful content")
            return []

        # Extract potential company names by looking for capitalised words
        # that appear near keywords like "competitor", "rival", "peer"
        # This is a simple heuristic — not perfect but useful as fallback
        import re

        # Look for NSE ticker patterns (e.g. TCS, WIPRO, HCLTECH)
        # NSE tickers are typically 2-10 uppercase letters
        if market == "India":
            # Search for patterns that look like NSE tickers in the text
            potential_tickers = re.findall(r'\b([A-Z]{2,10})(?:\.NS|\.BO)?\b', all_text)
            # Add .NS suffix and try to validate via yfinance
            suffix = ".NS"
        else:
            # US tickers: 1-5 uppercase letters
            potential_tickers = re.findall(r'\b([A-Z]{1,5})\b', all_text)
            suffix = ""

        # Deduplicate while preserving order
        seen = set()
        unique_tickers = []
        for t in potential_tickers:
            if t not in seen and t != exclude_ticker.replace(".NS","").replace(".BO",""):
                seen.add(t)
                unique_tickers.append(t)

        print(f"  Found {len(unique_tickers)} potential tickers in Tavily results — validating...")

        # Try each potential ticker against yfinance to validate it's real
        validated = 0
        for raw_ticker in unique_tickers[:20]:   # Try first 20 candidates
            if validated >= 5:
                break

            full_ticker = raw_ticker + suffix if suffix else raw_ticker

            # Skip common English words that look like tickers
            skip_words = {
                "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT",
                "HAVE", "WILL", "ARE", "WAS", "HAS", "ITS", "CAN",
                "ALL", "NEW", "TOP", "KEY", "CEO", "CFO", "IPO",
                "GDP", "RBI", "NSE", "BSE", "USA", "USD", "INR"
            }
            if raw_ticker in skip_words:
                continue

            try:
                test_stock = yf.Ticker(full_ticker)
                test_info  = test_stock.info

                # Valid stock will have a longName or shortName
                name = test_info.get("longName") or test_info.get("shortName")
                mc   = test_info.get("marketCap")

                if name and mc:
                    peers.append({
                        "ticker":     full_ticker,
                        "name":       name,
                        "market_cap": mc,
                        "industry":   test_info.get("industry", ""),
                        "sector":     test_info.get("sector", ""),
                        "match_type": "tavily_fallback",
                    })
                    validated += 1
                    print(f"    + Validated: {full_ticker} — {name}")

            except Exception:
                continue   # Invalid ticker — skip silently

        # Sort by market cap
        peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
        print(f"  [OK] Tavily fallback found {len(peers)} validated peers")

    except Exception as e:
        print(f"  [ERROR] Tavily peer fallback failed: {e}")

    return peers


# -----------------------------------------------------------------------------
# FUNCTION: fetch_peer_metrics
#
# Given a list of peer tickers, fetches key comparison metrics for each.
# We fetch a small, fast subset — not the full Data Engine.
# -----------------------------------------------------------------------------

def fetch_peer_metrics(peer_list, market):
    """
    Fetches key financial metrics for each peer company.

    We fetch only what's needed for comparison — not the full analysis.
    This keeps the peer fetch fast (one .info call per peer).

    Metrics fetched per peer:
        - Market Cap
        - Revenue (TTM)
        - Revenue Growth YoY
        - Net Margin
        - ROE
        - Debt/Equity
        - P/E (Trailing)
        - EV/EBITDA

    Input:
        peer_list → list of peer dicts from find_peers_via_screener/tavily
        market    → "India" or "US"

    Output:
        List of peer dicts with metrics added.
    """
    print(f"\n[Peer Engine] Fetching metrics for {len(peer_list)} peers...")

    enriched_peers = []

    for peer in peer_list:
        ticker = peer["ticker"]
        print(f"  Fetching: {ticker}...", end=" ")

        try:
            stock = yf.Ticker(ticker)
            info  = stock.info

            # Revenue growth: need current and prior year revenue
            revenue_current = safe_get(info, "totalRevenue")

            # Get prior year revenue from financials DataFrame
            try:
                fin_df = stock.financials
                revenue_prior = get_df_value(
                    fin_df,
                    ["Total Revenue", "Revenue"],
                    col_index=1
                )
            except Exception:
                revenue_prior = None

            # Calculate revenue growth
            rev_growth = None
            if revenue_current and revenue_prior and revenue_prior != 0:
                rev_growth = to_pct(
                    safe_divide(revenue_current - revenue_prior, abs(revenue_prior))
                )

            # Get shareholders equity for D/E calculation
            book_value   = safe_get(info, "bookValue")
            shares_out   = safe_get(info, "sharesOutstanding")
            equity = book_value * shares_out if book_value and shares_out else None

            # D/E ratio
            total_debt = safe_get(info, "totalDebt")
            de_ratio   = to_round(safe_divide(total_debt, equity))

            # Assemble peer metrics
            peer_metrics = {
                "ticker":           ticker,
                "name":             peer.get("name", ticker),
                "market_cap":       safe_get(info, "marketCap"),
                "revenue_ttm":      revenue_current,
                "revenue_growth_pct": rev_growth,
                "net_margin_pct":   to_pct(safe_get(info, "profitMargins")),
                "roe_pct":          to_pct(safe_get(info, "returnOnEquity")),
                "debt_to_equity":   de_ratio,
                "pe_trailing":      to_round(safe_get(info, "trailingPE")),
                "ev_ebitda":        to_round(safe_get(info, "enterpriseToEbitda")),
                "match_type":       peer.get("match_type", "screener"),
            }

            enriched_peers.append(peer_metrics)
            print("OK")

        except Exception as e:
            print(f"FAILED ({e})")
            # Add the peer with None metrics rather than skipping entirely
            enriched_peers.append({
                "ticker":   ticker,
                "name":     peer.get("name", ticker),
                "error":    str(e),
            })

    return enriched_peers


# -----------------------------------------------------------------------------
# FUNCTION: calculate_peer_averages
#
# Calculates the average value of each metric across all peers.
# Used by the Analysis Engine (Section 4) to compare our company vs peers.
# -----------------------------------------------------------------------------

def mad_filter(values):
    """
    # MAD = Median Absolute Deviation outlier filter

    Removes data artifacts (like a peer showing -98.75% revenue
    growth from a bad yfinance bulk result) before averaging.

    How it works:
      1. Find the median of the values
      2. Measure each value's distance from that median
      3. The median of those distances = MAD (typical spread)
      4. Any value more than 3x MAD from the median is excluded

    Input:  list of floats
    Output: list of floats with outliers removed
            (returns the original list if filtering is not possible)
    """
    # Need at least 3 values for outlier detection to be meaningful
    if len(values) < 3:
        return values

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        median = sorted_vals[n // 2]
    else:
        median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2

    deviations = sorted(abs(v - median) for v in values)
    if n % 2 == 1:
        mad = deviations[n // 2]
    else:
        mad = (deviations[n // 2 - 1] + deviations[n // 2]) / 2

    # If MAD is 0 (values nearly identical), nothing to filter
    if mad == 0:
        return values

    threshold = 3 * mad
    cleaned = [v for v in values if abs(v - median) <= threshold]

    # Safety net: never return an empty list
    return cleaned if cleaned else values


def calculate_peer_averages(peer_metrics_list):
    """
    Calculates average values for each metric across all peers.

    Why averages?
    A single peer comparison can be misleading. Averaging across 5 peers
    gives a more reliable "industry norm" to benchmark against.

    Input:
        peer_metrics_list → list of peer metric dicts

    Output:
        Dictionary of average values per metric.
        Only includes metrics where at least 2 peers have valid data.
    """
    # These are the numeric metrics we want to average
    numeric_metrics = [
        "net_margin_pct", "roe_pct", "debt_to_equity",
        "pe_trailing", "ev_ebitda", "revenue_growth_pct"
    ]

    averages = {}

    for metric in numeric_metrics:
        # Collect all valid (non-None) values for this metric
        values = []
        for peer in peer_metrics_list:
            v = peer.get(metric)
            if v is not None:
                try:
                    values.append(float(v))
                except (TypeError, ValueError):
                    continue

        # Only calculate average if we have at least 2 data points
        if len(values) >= 2:
            # Remove data artifacts before averaging (see mad_filter)
            clean_values = mad_filter(values)
            averages[metric] = to_round(sum(clean_values) / len(clean_values))
        else:
            averages[metric] = None

    return averages


# -----------------------------------------------------------------------------
# FUNCTION: print_peer_comparison_table
#
# Prints a formatted comparison table showing our company vs all peers.
# -----------------------------------------------------------------------------

def print_peer_comparison_table(company_ticker, company_universal,
                                 peer_metrics_list, peer_averages, market):
    """
    Prints a side-by-side comparison table of key metrics.

    Our company is shown first, then each peer, then the peer average.
    """
    currency = "₹" if market == "India" else "$"

    def fmt_mc(n):
        """Format market cap."""
        if n is None:
            return "N/A"
        n = float(n)
        if market == "India":
            return f"₹{n/1e7:.0f}Cr"
        else:
            return f"${n/1e9:.1f}B"

    def fmt_val(v, suffix=""):
        if v is None:
            return "N/A"
        return f"{v}{suffix}"

    print("\n" + "=" * 60)
    print("PEER COMPARISON TABLE")
    print("=" * 60)

    # Header row
    header = f"{'Metric':<22} {'This Co':>10}"
    for p in peer_metrics_list[:5]:
        ticker_short = p['ticker'].replace('.NS','').replace('.BO','')[:6]
        header += f" {ticker_short:>10}"
    header += f" {'PeerAvg':>10}"
    print(header)
    print("─" * (22 + 11 + len(peer_metrics_list[:5]) * 11 + 11))

    # Helper to print one metric row
    def print_row(label, company_val, peer_key, suffix=""):
        row = f"  {label:<20} {fmt_val(company_val, suffix):>10}"
        for p in peer_metrics_list[:5]:
            row += f" {fmt_val(p.get(peer_key), suffix):>10}"
        avg = peer_averages.get(peer_key)
        row += f" {fmt_val(avg, suffix):>10}"
        print(row)

    print_row("Net Margin %",    company_universal.get("net_margin_pct"),    "net_margin_pct",    "%")
    print_row("ROE %",           company_universal.get("roe_pct"),           "roe_pct",           "%")
    print_row("Revenue Growth%", company_universal.get("revenue_growth_pct"),"revenue_growth_pct","%")
    print_row("P/E (Trailing)",  company_universal.get("pe_trailing"),       "pe_trailing")
    print_row("EV/EBITDA",       company_universal.get("ev_ebitda"),         "ev_ebitda")
    print_row("Debt/Equity",     company_universal.get("debt_to_equity"),    "debt_to_equity")

    print("─" * (22 + 11 + len(peer_metrics_list[:5]) * 11 + 11))
    print()


# -----------------------------------------------------------------------------
# FUNCTION: run_peer_engine
#
# Main entry point for Section 3.
# Orchestrates peer finding, metric fetching, and comparison table.
# Takes outputs from Sections 1 and 2 as inputs.
# -----------------------------------------------------------------------------

def run_peer_engine(ticker, sector_intelligence_result, data_engine_result, manual_peers=None):
    """
    Orchestrates the full Section 3 peer analysis.

    Input:
        ticker                    → stock ticker string
        sector_intelligence_result → output from run_sector_intelligence()
        data_engine_result         → output from run_data_engine()

    Output:
        Dictionary containing:
            peers         → list of peer metric dicts
            peer_averages → dict of average metrics across peers
            peer_count    → number of peers found
    """
    print("\n" + "=" * 60)
    print("PEER ENGINE")
    print("=" * 60)

    # Extract what we need from previous sections
    market       = sector_intelligence_result.get("market", "US")
    yf_industry  = sector_intelligence_result.get("yf_industry", "")
    yf_sector    = sector_intelligence_result.get("yf_sector", "")
    company_name = sector_intelligence_result.get("company_name", ticker)
    universal    = data_engine_result.get("universal", {})

    print(f"Company: {company_name}")
    print(f"Industry: {yf_industry} | Market: {market}")

    # Step 1: Check if user provided manual peers
    if manual_peers:
        print(f"\n[Peer Engine] Using {len(manual_peers)} manually specified peers:")
        peers = []
        for manual_ticker in manual_peers:
            manual_ticker = manual_ticker.strip().upper()
            print(f"  Validating: {manual_ticker}...", end=" ")
            try:
                # Verify the manually provided ticker is real
                test_stock = yf.Ticker(manual_ticker)
                test_info  = test_stock.info
                name = test_info.get("longName") or test_info.get("shortName")
                mc   = test_info.get("marketCap")

                if name:
                    peers.append({
                        "ticker":     manual_ticker,
                        "name":       name,
                        "market_cap": mc,
                        "industry":   test_info.get("industry", ""),
                        "sector":     test_info.get("sector", ""),
                        "match_type": "manual",
                    })
                    print(f"OK — {name}")
                else:
                    print(f"FAILED — ticker not recognised, skipping")

            except Exception as e:
                print(f"FAILED — {e}, skipping")

    else:
        # Step 2: Auto-discover peers via Yahoo Finance screener
        peers = find_peers_via_screener(
            yf_industry, yf_sector, market, ticker,
            exclude_company_name=company_name
        )

        # Step 3: If fewer than 3 peers found, try Tavily fallback
        # Note: Tavily fallback now only runs when NO manual peers given
        # and screener found fewer than 3 verified industry peers
        if len(peers) < 3:
            print(f"\n  Only {len(peers)} verified industry peers found.")
            print(f"  Tip: You can provide manual peers for better comparison.")
            print(f"  Activating Tavily fallback...")
            tavily_peers = find_peers_via_tavily(
                company_name, yf_industry, market, ticker
            )

            # Only add Tavily peers that aren't already in our list
            existing_tickers = {p["ticker"] for p in peers}
            for tp in tavily_peers:
                if tp["ticker"] not in existing_tickers:
                    # Verify Tavily-found peers against industry too
                    try:
                        tv_info = yf.Ticker(tp["ticker"]).info
                        tv_industry = tv_info.get("industry", "") or ""
                        target_words = set(yf_industry.lower().split()) if yf_industry else set()
                        candidate_words = set(tv_industry.lower().split())
                        filler = {"and", "of", "the", "&", "-", "services", "products"}
                        overlap = (target_words - filler) & (candidate_words - filler)
                        if len(overlap) >= 1:
                            tp["match_type"] = "industry"
                            tp["industry"] = tv_industry
                            peers.append(tp)
                            existing_tickers.add(tp["ticker"])
                    except Exception:
                        continue

            peers.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)
            peers = peers[:5]

    # Step 3: Check peer comparison confidence BEFORE fetching metrics
    # (based on whether peers matched on exact industry or just broad sector)
    # Manual peers are user-chosen and validated, so trust them fully
    if manual_peers and peers:
        peer_confidence = "high"
    else:
        exact_industry_count = sum(1 for p in peers if p.get("match_type") == "industry")
        peer_confidence = "high" if exact_industry_count >= 2 else (
            "low" if peers else "none"
        )

    # Step 4: Fetch detailed metrics for each peer
    if peers:
        peer_metrics = fetch_peer_metrics(peers, market)
    else:
        print("  [WARN] No peers found via any method.")
        peer_metrics = []

    # Step 5: Calculate peer averages
    peer_averages = calculate_peer_averages(peer_metrics)

    # Step 5: Print comparison table
    if peer_metrics:
        print_peer_comparison_table(
            ticker, universal, peer_metrics, peer_averages, market
        )
    else:
        print("  No peer comparison available.")

    result = {
        "peers":           peer_metrics,
        "peer_averages":   peer_averages,
        "peer_count":      len(peer_metrics),
        "peer_confidence": peer_confidence,   # "high", "low", or "none"
    }

    if peer_confidence == "low":
        print(f"  [NOTE] Peer confidence is LOW — comparisons are based on broad "
              f"sector matches rather than precise industry matches. Treat peer-based "
              f"scores with caution.")

    print(f"[Peer Engine] Complete — {len(peer_metrics)} peers analysed.")
    return result
    

# =============================================================================
# SECTION 4: ANALYSIS ENGINE
#
# Reads outputs from Sections 1-3 and:
#   - Detects which metrics tell a meaningful story (story detector)
#   - Runs six analytical lenses
#   - Scores the company across 5 dimensions (100 points total)
# =============================================================================


# -----------------------------------------------------------------------------
# KNOWN DATA QUALITY ISSUES
#
# Some metrics are unreliable for certain markets due to yfinance quirks.
# We exclude these from scoring/story detection rather than show wrong data.
# -----------------------------------------------------------------------------

UNRELIABLE_METRICS = {
    "India": ["ev_ebitda"],   # Currency mixing issue confirmed in testing
}


def is_metric_reliable(metric_name, market):
    """
    Checks if a metric is known to be unreliable for a given market.
    Returns False if the metric should be excluded from analysis.
    """
    unreliable_list = UNRELIABLE_METRICS.get(market, [])
    return metric_name not in unreliable_list


# -----------------------------------------------------------------------------
# FUNCTION: detect_trend_break
#
# Filter 1: Has a metric been moving consistently in one direction
# and then reversed in the most recent period?
# -----------------------------------------------------------------------------

def detect_trend_break(metric_name, history_list, label):
    """
    Checks a list of historical values (most recent first) for a trend break.

    Input:
        metric_name  → internal name, e.g. "net_margin_pct"
        history_list → list of values, MOST RECENT FIRST
                       e.g. [16.4, 16.4, 17.1, 16.4] (current → oldest)
        label        → human-readable name, e.g. "Net Margin"

    Output:
        A story dict if a trend break is found, otherwise None.

    Logic:
        Compare the most recent value to the trend of the prior 2-3 values.
        If prior values were consistently moving one way (up or down) and
        the latest value breaks that direction by a meaningful amount, flag it.
    """
    # Remove None values but keep order
    clean = [v for v in history_list if v is not None]

    if len(clean) < 3:
        return None   # Not enough data to detect a trend

    latest = clean[0]
    prior_values = clean[1:4]   # Up to 3 prior years

    if len(prior_values) < 2:
        return None

    # Calculate the average of prior years (excluding latest)
    prior_avg = sum(prior_values) / len(prior_values)

    # Calculate % change from prior average to latest
    if prior_avg == 0:
        return None

    change_pct = ((latest - prior_avg) / abs(prior_avg)) * 100

    # Threshold: a move of more than 15% relative to the prior average
    # is considered a meaningful break
    if abs(change_pct) >= 15:
        direction = "declining" if change_pct < 0 else "improving"
        signal = "bearish" if change_pct < 0 else "bullish"

        return {
            "title": f"{label} {direction.capitalize()}",
            "metric": metric_name,
            "type": "trend_break",
            "current": latest,
            "context": (
                f"{label} moved to {latest} from a prior average of "
                f"{round(prior_avg, 2)} — a {round(abs(change_pct), 1)}% "
                f"{direction} shift"
            ),
            "signal": signal,
        }

    return None


# -----------------------------------------------------------------------------
# FUNCTION: detect_threshold_cross
#
# Filter 2: Has a metric crossed a meaningful danger/opportunity threshold?
# -----------------------------------------------------------------------------

def detect_threshold_cross(metric_name, current_value, label, market):
    """
    Checks if a metric has crossed a known important threshold.

    These thresholds are general investing rules of thumb, not sector-specific
    (sector-specific thresholds are handled separately using SECTOR_DEFINITIONS
    benchmarks in the lens functions below).

    Output:
        A story dict if a threshold is crossed, otherwise None.
    """
    if current_value is None:
        return None

    # Define universal thresholds: (metric_name, comparison, value, message, signal)
    threshold_rules = {
        "debt_to_equity": [
            (current_value > 1.5, "bearish", f"Debt/Equity at {current_value} — above the 1.5 caution level, indicating elevated leverage"),
            (current_value < 0.1, "bullish", f"Debt/Equity at {current_value} — company is nearly debt-free, a position of financial strength"),
        ],
        "roe_pct": [
            (current_value > 25, "bullish", f"ROE at {current_value}% — exceptional capital efficiency, above the 25% high-performance mark"),
            (current_value < 8, "bearish", f"ROE at {current_value}% — below the 8% level generally considered weak"),
        ],
        "interest_coverage": [
            (current_value < 2, "bearish", f"Interest Coverage at {current_value}x — below the 2x safety margin, signaling debt servicing risk"),
        ],
        "net_margin_pct": [
            (current_value < 0, "bearish", f"Net Margin at {current_value}% — company is currently unprofitable"),
        ],
    }

    rules = threshold_rules.get(metric_name, [])

    for condition, signal, message in rules:
        if condition:
            return {
                "title": f"{label} Threshold Alert",
                "metric": metric_name,
                "type": "threshold_cross",
                "current": current_value,
                "context": message,
                "signal": signal,
            }

    return None


# -----------------------------------------------------------------------------
# FUNCTION: detect_peer_divergence
#
# Filter 3: Is this metric significantly better or worse than peer average?
# -----------------------------------------------------------------------------

def detect_peer_divergence(metric_name, company_value, peer_avg, label, higher_is_better=True):
    """
    Compares a company metric to the peer average and flags significant gaps.

    Input:
        higher_is_better → True for metrics like ROE, Margin (higher = better)
                            False for metrics like P/E, Debt/Equity
                            (where "better" depends on context — we treat
                             these more carefully, see usage below)

    Output:
        A story dict if divergence > 20%, otherwise None.
    """
    if company_value is None or peer_avg is None or peer_avg == 0:
        return None

    diff_pct = ((company_value - peer_avg) / abs(peer_avg)) * 100

    if abs(diff_pct) < 20:
        return None   # Not a significant enough gap

    is_better = diff_pct > 0 if higher_is_better else diff_pct < 0
    signal = "bullish" if is_better else "bearish"
    direction = "above" if diff_pct > 0 else "below"

    return {
        "title": f"{label} {'Outperforming' if is_better else 'Underperforming'} Peers",
        "metric": metric_name,
        "type": "peer_divergence",
        "current": company_value,
        "context": (
            f"{label} of {company_value} is {round(abs(diff_pct), 1)}% {direction} "
            f"the peer average of {round(peer_avg, 2)}"
        ),
        "signal": signal,
    }


# -----------------------------------------------------------------------------
# FUNCTION: detect_growth_quality_divergence
#
# Filter 4: Is revenue growing while margins are falling (or vice versa)?
# This divergence between related metrics is always worth flagging.
# -----------------------------------------------------------------------------

def detect_growth_quality_divergence(revenue_growth_pct, net_margin_history):
    """
    Checks if growth and profitability are moving in opposite directions.

    Input:
        revenue_growth_pct → current YoY revenue growth %
        net_margin_history → list of net margin %, most recent first

    Output:
        A story dict if a divergence is found, otherwise None.
    """
    clean_margins = [m for m in net_margin_history if m is not None]

    if revenue_growth_pct is None or len(clean_margins) < 2:
        return None

    latest_margin = clean_margins[0]
    prior_margin = clean_margins[1]
    margin_change = latest_margin - prior_margin

    # Case 1: Strong revenue growth but declining margins
    if revenue_growth_pct > 10 and margin_change < -2:
        return {
            "title": "Growth at the Cost of Profitability",
            "metric": "growth_vs_quality",
            "type": "divergence",
            "current": revenue_growth_pct,
            "context": (
                f"Revenue growing {revenue_growth_pct}% YoY, but net margin "
                f"fell from {prior_margin}% to {latest_margin}% — growth may "
                f"be coming at the expense of profitability"
            ),
            "signal": "watch",
        }

    # Case 2: Weak/flat revenue growth but improving margins
    if revenue_growth_pct < 3 and margin_change > 2:
        return {
            "title": "Margin Expansion Despite Slow Growth",
            "metric": "growth_vs_quality",
            "type": "divergence",
            "current": revenue_growth_pct,
            "context": (
                f"Revenue growth is muted at {revenue_growth_pct}% YoY, but "
                f"net margin improved from {prior_margin}% to {latest_margin}% "
                f"— efficiency gains are offsetting slow top-line growth"
            ),
            "signal": "watch",
        }

    return None


# -----------------------------------------------------------------------------
# FUNCTION: run_story_detection
#
# Master function that runs all four story detection filters across all
# available metrics and returns only the stories that were actually flagged.
# -----------------------------------------------------------------------------

def run_story_detection(universal, historical, peer_averages, market):
    """
    Runs all story detection filters and collects flagged stories.

    Input:
        universal      → universal metrics dict (Section 2)
        historical     → historical data dict (Section 2)
        peer_averages  → peer average metrics dict (Section 3)
        market         → "India" or "US"

    Output:
        List of story dicts — only metrics with something meaningful happening.
    """
    print("\n[Analysis Engine] Running story detection...")

    stories = []

    # ------------------------------------------------------------------
    # TREND BREAK CHECKS (using historical data)
    # ------------------------------------------------------------------
    trend_checks = [
        ("net_margin_pct", historical.get("net_margin_pct", []), "Net Margin"),
        ("operating_margin_pct", historical.get("operating_margin_pct", []), "Operating Margin"),
    ]

    for metric_name, history, label in trend_checks:
        if is_metric_reliable(metric_name, market):
            story = detect_trend_break(metric_name, history, label)
            if story:
                stories.append(story)

    # ------------------------------------------------------------------
    # THRESHOLD CROSS CHECKS (using current universal metrics)
    # ------------------------------------------------------------------
    threshold_checks = [
        ("debt_to_equity", universal.get("debt_to_equity"), "Debt/Equity"),
        ("roe_pct", universal.get("roe_pct"), "ROE"),
        ("interest_coverage", universal.get("interest_coverage"), "Interest Coverage"),
        ("net_margin_pct", universal.get("net_margin_pct"), "Net Margin"),
    ]

    for metric_name, value, label in threshold_checks:
        if is_metric_reliable(metric_name, market):
            story = detect_threshold_cross(metric_name, value, label, market)
            if story:
                stories.append(story)

    # ------------------------------------------------------------------
    # PEER DIVERGENCE CHECKS (using Section 3 peer averages)
    # ------------------------------------------------------------------
    peer_checks = [
        ("net_margin_pct", universal.get("net_margin_pct"), peer_averages.get("net_margin_pct"), "Net Margin", True),
        ("roe_pct", universal.get("roe_pct"), peer_averages.get("roe_pct"), "ROE", True),
        ("revenue_growth_pct", universal.get("revenue_growth_pct"), peer_averages.get("revenue_growth_pct"), "Revenue Growth", True),
        ("pe_trailing", universal.get("pe_trailing"), peer_averages.get("pe_trailing"), "P/E Ratio", False),
        ("debt_to_equity", universal.get("debt_to_equity"), peer_averages.get("debt_to_equity"), "Debt/Equity", False),
    ]

    for metric_name, company_val, peer_avg, label, higher_better in peer_checks:
        if is_metric_reliable(metric_name, market):
            story = detect_peer_divergence(metric_name, company_val, peer_avg, label, higher_better)
            if story:
                stories.append(story)

    # ------------------------------------------------------------------
    # GROWTH VS QUALITY DIVERGENCE CHECK
    # ------------------------------------------------------------------
    divergence_story = detect_growth_quality_divergence(
        universal.get("revenue_growth_pct"),
        historical.get("net_margin_pct", [])
    )
    if divergence_story:
        stories.append(divergence_story)

    print(f"  [OK] {len(stories)} stories detected")
    return stories


# -----------------------------------------------------------------------------
# FUNCTION: run_six_lenses
#
# Produces a structured finding for each of the six analytical lenses.
# Unlike story detection (which only flags anomalies), the six lenses
# always produce a finding — but the finding is SHORT and synthesised,
# not a data dump.
# -----------------------------------------------------------------------------

def run_six_lenses(universal, peer_averages, sector_outlook_score, market):
    """
    Runs the six analytical lenses and produces one short finding per lens.

    Output:
        Dictionary with one entry per lens: growth, profitability,
        balance_sheet, cash_flow, valuation, competitive_position.
        Each entry has a "finding" (1-2 sentence summary).
    """
    print("\n[Analysis Engine] Running six analytical lenses...")

    lenses = {}

    # ------------------------------------------------------------------
    # LENS 1: GROWTH
    # ------------------------------------------------------------------
    rev_growth = universal.get("revenue_growth_pct")
    eps_growth = universal.get("eps_growth_pct")
    peer_growth = peer_averages.get("revenue_growth_pct")

    if rev_growth is None:
        growth_finding = "Revenue growth data unavailable."
    elif peer_growth and rev_growth > peer_growth * 1.2:
        growth_finding = f"Revenue growing {rev_growth}% YoY, outpacing peer average of {peer_growth}%."
    elif peer_growth and rev_growth < peer_growth * 0.8:
        growth_finding = f"Revenue growing {rev_growth}% YoY, trailing peer average of {peer_growth}%."
    else:
        growth_finding = f"Revenue growing {rev_growth}% YoY, broadly in line with peers."

    lenses["growth"] = {"finding": growth_finding}

    # ------------------------------------------------------------------
    # LENS 2: PROFITABILITY
    # ------------------------------------------------------------------
    net_margin = universal.get("net_margin_pct")
    roe = universal.get("roe_pct")
    roce = universal.get("roce_pct")

    if net_margin is None:
        profit_finding = "Profitability data unavailable."
    else:
        quality_desc = "strong" if (roe and roe > 20) else "moderate" if (roe and roe > 10) else "weak"
        profit_finding = f"Net margin at {net_margin}%, ROE at {roe}% — {quality_desc} capital efficiency."

    lenses["profitability"] = {"finding": profit_finding}

    # ------------------------------------------------------------------
    # LENS 3: BALANCE SHEET
    # ------------------------------------------------------------------
    de_ratio = universal.get("debt_to_equity")
    int_coverage = universal.get("interest_coverage")

    if de_ratio is None:
        bs_finding = "Balance sheet data unavailable."
    elif de_ratio < 0.3:
        bs_finding = f"Debt/Equity at {de_ratio} — conservatively financed with low leverage risk."
    elif de_ratio < 1.0:
        bs_finding = f"Debt/Equity at {de_ratio} — moderate, manageable leverage."
    else:
        bs_finding = f"Debt/Equity at {de_ratio} — elevated leverage, worth monitoring."

    lenses["balance_sheet"] = {"finding": bs_finding}

    # ------------------------------------------------------------------
    # LENS 4: CASH FLOW
    # ------------------------------------------------------------------
    fcf = universal.get("free_cash_flow")
    net_income_proxy = universal.get("revenue_ttm")  # proxy check only

    if fcf is None:
        cf_finding = "Cash flow data unavailable."
    elif fcf > 0:
        cf_finding = "Company is generating positive free cash flow, indicating genuine cash-generating ability beyond accounting profit."
    else:
        cf_finding = "Free cash flow is negative — the business is currently consuming cash rather than generating it."

    lenses["cash_flow"] = {"finding": cf_finding}

    # ------------------------------------------------------------------
    # LENS 5: VALUATION
    # ------------------------------------------------------------------
    pe = universal.get("pe_trailing")
    peer_pe = peer_averages.get("pe_trailing")

    if pe is None:
        val_finding = "Valuation data unavailable."
    elif peer_pe and pe < peer_pe * 0.8:
        val_finding = f"P/E of {pe} trades at a discount to peer average of {peer_pe} — potentially undervalued, or market pricing in lower growth expectations."
    elif peer_pe and pe > peer_pe * 1.2:
        val_finding = f"P/E of {pe} trades at a premium to peer average of {peer_pe} — market expects above-peer performance."
    else:
        val_finding = f"P/E of {pe} is broadly in line with the peer average of {peer_pe}."

    lenses["valuation"] = {"finding": val_finding}

    # ------------------------------------------------------------------
    # LENS 6: COMPETITIVE POSITION
    # ------------------------------------------------------------------
    margin_vs_peer = None
    roe_vs_peer = None

    if net_margin and peer_averages.get("net_margin_pct"):
        margin_vs_peer = net_margin - peer_averages["net_margin_pct"]
    if roe and peer_averages.get("roe_pct"):
        roe_vs_peer = roe - peer_averages["roe_pct"]

    wins = sum([1 for x in [margin_vs_peer, roe_vs_peer] if x and x > 0])

    if margin_vs_peer is None and roe_vs_peer is None:
        comp_finding = "Insufficient peer data for competitive positioning."
    elif wins == 2:
        comp_finding = "Outperforms peer average on both margin and returns — a position of competitive strength."
    elif wins == 0:
        comp_finding = "Trails peer average on both margin and returns — competitive position appears weaker than peers."
    else:
        comp_finding = "Mixed competitive position — ahead of peers on some measures, behind on others."

    lenses["competitive_position"] = {"finding": comp_finding}

    print(f"  [OK] Six lenses completed")
    return lenses


# -----------------------------------------------------------------------------
# FUNCTION: score_sector_outlook
#
# Converts the Tavily-researched sector outlook summary into a 0-15 score
# using simple keyword counting.
# -----------------------------------------------------------------------------

def score_sector_outlook(outlook_summary):
    """
    Scores sector outlook out of 15 using keyword sentiment counting.

    This is a simple, rules-based approach — not true sentiment analysis.
    It will be replaced/enhanced when Module 8 adds LLM-based analysis.

    Output:
        Integer score from 0 to 15, plus the reasoning.
    """
    if not outlook_summary:
        return 7, "No sector outlook data available — neutral score assigned."

    text = outlook_summary.lower()

    positive_words = [
        "growth", "growing", "opportunity", "expansion", "strong",
        "rising", "robust", "outpace", "demand", "positive", "boost"
    ]
    negative_words = [
        "slowdown", "risk", "challenge", "decline", "headwind",
        "tariff", "weak", "pressure", "concern", "uncertainty", "disruption"
    ]

    pos_count = sum(1 for word in positive_words if word in text)
    neg_count = sum(1 for word in negative_words if word in text)

    # Base score of 7.5 (neutral midpoint), adjusted by word balance
    net_signal = pos_count - neg_count

    # Each net positive/negative word shifts score by ~1.5 points, capped at 0-15
    score = 7.5 + (net_signal * 1.5)
    score = max(0, min(15, score))
    score = round(score)

    if net_signal > 1:
        reasoning = f"Outlook commentary skews positive ({pos_count} positive vs {neg_count} negative signals found)."
    elif net_signal < -1:
        reasoning = f"Outlook commentary skews negative ({neg_count} negative vs {pos_count} positive signals found)."
    else:
        reasoning = "Outlook commentary is mixed/neutral."

    return score, reasoning


# -----------------------------------------------------------------------------
# FUNCTION: calculate_scorecard
#
# Assigns the final 100-point score across 5 dimensions.
# -----------------------------------------------------------------------------

def calculate_scorecard(universal, peer_averages, sector_outlook_result, market, peer_count=0):
    """
    Calculates the 5-dimension scorecard (100 points total).

    Scoring approach for each dimension is rules-based, comparing the
    company's metrics against both absolute thresholds and peer averages.

    Output:
        Dictionary with score + reason for each dimension, plus total.
    """
    print("\n[Analysis Engine] Calculating scorecard...")

    scorecard = {}

    # ------------------------------------------------------------------
    # GROWTH (25 points)
    # Absolute growth component (up to 15 points), then a
    # peer-relative component (up to 10 points) below.
    # ------------------------------------------------------------------
    rev_growth  = universal.get("revenue_growth_pct") or 0
    eps_growth  = universal.get("eps_growth_pct") or 0
    peer_growth = peer_averages.get("revenue_growth_pct")

    growth_score = 0
    if rev_growth >= 20:
        growth_score += 15
    elif rev_growth >= 10:
        growth_score += 12
    elif rev_growth >= 5:
        growth_score += 8
    elif rev_growth >= 0:
        growth_score += 5
    else:
        growth_score += 0

    # Relative to peers component (up to 10 points)
    # Weight this component down when peer sample is small
    peer_weight = 1.0 if peer_count >= 3 else (0.5 if peer_count >= 1 else 0.0)

    if peer_growth is not None and peer_count >= 1:
        if rev_growth > peer_growth:
            growth_score += int(10 * peer_weight)
        elif rev_growth > peer_growth * 0.8:
            growth_score += int(6 * peer_weight)
        else:
            growth_score += int(2 * peer_weight)
        # Always add a small base if peer data exists but weight is low
        if peer_weight < 1.0:
            growth_score += int(5 * (1 - peer_weight))
    else:
        growth_score += 5   # Neutral if no peer data at all

    growth_score = min(25, growth_score)
    scorecard["growth"] = {
        "score": growth_score,
        "out_of": 25,
        "reason": f"Revenue growth of {rev_growth}% " + (
            f"vs peer average of {peer_growth}%" if peer_growth else "(no peer comparison available)"
        )
    }

    # ------------------------------------------------------------------
    # QUALITY (25 points)
    # Based on margins, ROE, ROCE
    # ------------------------------------------------------------------
    net_margin = universal.get("net_margin_pct") or 0
    roe = universal.get("roe_pct") or 0
    roce = universal.get("roce_pct")

    quality_score = 0
    # Net margin component (up to 10 points)
    if net_margin >= 20:
        quality_score += 10
    elif net_margin >= 12:
        quality_score += 7
    elif net_margin >= 5:
        quality_score += 4
    else:
        quality_score += 1

    # ROE component (up to 10 points)
    if roe >= 25:
        quality_score += 10
    elif roe >= 15:
        quality_score += 7
    elif roe >= 8:
        quality_score += 4
    else:
        quality_score += 1

    # ROCE component (up to 5 points) — skip if not available (e.g. banks)
    if roce is not None:
        if roce >= 20:
            quality_score += 5
        elif roce >= 12:
            quality_score += 3
        else:
            quality_score += 1
    else:
        quality_score += 3   # Neutral default if ROCE not applicable (banks)

    quality_score = min(25, quality_score)
    scorecard["quality"] = {
        "score": quality_score,
        "out_of": 25,
        "reason": f"Net margin {net_margin}%, ROE {roe}%" + (
            f", ROCE {roce}%" if roce is not None else " (ROCE not applicable)"
        )
    }

    # ------------------------------------------------------------------
    # FINANCIAL STRENGTH (20 points)
    # Based on Debt/Equity, Interest Coverage, Free Cash Flow
    # ------------------------------------------------------------------
    de_ratio = universal.get("debt_to_equity")
    int_coverage = universal.get("interest_coverage")
    fcf = universal.get("free_cash_flow")

    strength_score = 0
    # Debt/Equity component (up to 10 points)
    if de_ratio is not None:
        if de_ratio < 0.3:
            strength_score += 10
        elif de_ratio < 0.7:
            strength_score += 7
        elif de_ratio < 1.5:
            strength_score += 4
        else:
            strength_score += 1
    else:
        strength_score += 5

    # Interest coverage component (up to 5 points)
    if int_coverage is not None:
        if int_coverage > 10:
            strength_score += 5
        elif int_coverage > 3:
            strength_score += 3
        else:
            strength_score += 1
    else:
        strength_score += 3   # Neutral default

    # Free cash flow component (up to 5 points)
    if fcf is not None:
        strength_score += 5 if fcf > 0 else 0
    else:
        strength_score += 2

    strength_score = min(20, strength_score)
    scorecard["financial_strength"] = {
        "score": strength_score,
        "out_of": 20,
        "reason": f"Debt/Equity {de_ratio}, FCF {'positive' if fcf and fcf > 0 else 'negative/unavailable'}"
    }

    # ------------------------------------------------------------------
    # VALUATION (15 points)
    # Based on P/E vs peers
    # Note: EV/EBITDA excluded for India due to known data quality issue
    # ------------------------------------------------------------------
    pe = universal.get("pe_trailing")
    peer_pe = peer_averages.get("pe_trailing")

    valuation_score = 7   # Default neutral
    if pe is not None and peer_pe is not None and peer_pe > 0:
        discount_pct = ((peer_pe - pe) / peer_pe) * 100
        if discount_pct > 20:
            valuation_score = 14   # Significant discount = attractive
        elif discount_pct > 0:
            valuation_score = 10
        elif discount_pct > -20:
            valuation_score = 7
        else:
            valuation_score = 3    # Significant premium = expensive

    valuation_score = min(15, valuation_score)
    scorecard["valuation"] = {
        "score": valuation_score,
        "out_of": 15,
        "reason": f"P/E {pe} vs peer average {peer_pe}" if peer_pe else f"P/E {pe} (no peer comparison)"
    }

    # ------------------------------------------------------------------
    # SECTOR OUTLOOK (15 points)
    # Based on keyword sentiment from Section 1's Tavily research
    # ------------------------------------------------------------------
    outlook_summary = sector_outlook_result.get("summary", "")
    outlook_score, outlook_reason = score_sector_outlook(outlook_summary)

    scorecard["sector_outlook"] = {
        "score": outlook_score,
        "out_of": 15,
        "reason": outlook_reason
    }

    # ------------------------------------------------------------------
    # TOTAL
    # ------------------------------------------------------------------
    total = (
        scorecard["growth"]["score"] +
        scorecard["quality"]["score"] +
        scorecard["financial_strength"]["score"] +
        scorecard["valuation"]["score"] +
        scorecard["sector_outlook"]["score"]
    )

    if total >= 80:
        rating = "Strong — high conviction candidate"
    elif total >= 60:
        rating = "Positive — worth watching"
    elif total >= 40:
        rating = "Neutral — sector/macro dependent"
    elif total >= 20:
        rating = "Caution warranted"
    else:
        rating = "Significant concerns"

    scorecard["total"] = total
    scorecard["rating"] = rating

    print(f"  [OK] Scorecard calculated — Total: {total}/100 ({rating})")
    return scorecard


# -----------------------------------------------------------------------------
# FUNCTION: print_analysis_summary
#
# Prints the key stories, six lenses, and scorecard to the terminal.
# -----------------------------------------------------------------------------

def print_analysis_summary(stories, lenses, scorecard):
    """Prints a clean, readable summary of the Analysis Engine output."""

    print("\n" + "=" * 60)
    print("ANALYSIS ENGINE SUMMARY")
    print("=" * 60)

    # Key stories
    print(f"\n── KEY STORIES ({len(stories)} found) ─────────────────────")
    if stories:
        for s in stories:
            signal_icon = {"bullish": "✓ BULLISH", "bearish": "⚠ BEARISH", "watch": "⚠ WATCH"}.get(s["signal"], "•")
            print(f"\n  {signal_icon}  {s['title']}")
            print(f"            {s['context']}")
    else:
        print("  No significant stories detected — metrics are stable and in line with expectations.")

    # Six lenses
    print(f"\n── SIX LENSES ──────────────────────────────────────")
    for lens_name, lens_data in lenses.items():
        label = lens_name.replace("_", " ").title()
        print(f"  {label:<22} {lens_data['finding']}")

    # Scorecard
    print(f"\n── SCORECARD ───────────────────────────────────────")
    print(f"  Growth              {scorecard['growth']['score']:>3} / {scorecard['growth']['out_of']}   {scorecard['growth']['reason']}")
    print(f"  Quality             {scorecard['quality']['score']:>3} / {scorecard['quality']['out_of']}   {scorecard['quality']['reason']}")
    print(f"  Financial Strength  {scorecard['financial_strength']['score']:>3} / {scorecard['financial_strength']['out_of']}   {scorecard['financial_strength']['reason']}")
    print(f"  Valuation           {scorecard['valuation']['score']:>3} / {scorecard['valuation']['out_of']}   {scorecard['valuation']['reason']}")
    print(f"  Sector Outlook      {scorecard['sector_outlook']['score']:>3} / {scorecard['sector_outlook']['out_of']}   {scorecard['sector_outlook']['reason']}")
    print(f"  {'─'*56}")
    print(f"  TOTAL               {scorecard['total']:>3} / 100   {scorecard['rating']}")

    print("\n" + "=" * 60)


# -----------------------------------------------------------------------------
# FUNCTION: run_analysis_engine
#
# Main entry point for Section 4.
# Takes outputs from Sections 1, 2, 3 and produces stories + lenses + scorecard.
# -----------------------------------------------------------------------------

def run_analysis_engine(ticker, sector_intelligence_result, data_engine_result, peer_engine_result):
    """
    Orchestrates the full Section 4 analysis.

    Input:
        ticker                     → stock ticker
        sector_intelligence_result → output from run_sector_intelligence()
        data_engine_result         → output from run_data_engine()
        peer_engine_result         → output from run_peer_engine()

    Output:
        Dictionary containing:
            key_stories → list of flagged story dicts
            lenses      → six lens findings
            scorecard   → 5-dimension score + total
    """
    print("\n" + "=" * 60)
    print("ANALYSIS ENGINE")
    print("=" * 60)

    market         = sector_intelligence_result.get("market", "US")
    sector_outlook = sector_intelligence_result.get("sector_outlook", {})
    universal      = data_engine_result.get("universal", {})
    historical     = data_engine_result.get("historical", {})
    peer_averages  = peer_engine_result.get("peer_averages", {})

    # Step 1: Story detection
    stories = run_story_detection(universal, historical, peer_averages, market)

    # Step 2: Six lenses
    lenses = run_six_lenses(universal, peer_averages, None, market)

    # Step 3: Scorecard
    peer_count = peer_engine_result.get("peer_count", 0)
    scorecard = calculate_scorecard(universal, peer_averages, sector_outlook, market, peer_count=peer_count)

    # Step 4: Print summary
    print_analysis_summary(stories, lenses, scorecard)

    return {
        "key_stories": stories,
        "lenses": lenses,
        "scorecard": scorecard,
    }

# =============================================================================
# SECTION 5: THESIS GENERATOR
#
# Reads the outputs of Sections 1-4 (scorecard, key_stories, lenses) and
# synthesises them into:
#   - Bull Case      (strongest arguments for)
#   - Bear Case      (strongest arguments against)
#   - Key Risks      (specific things to watch)
#   - Investment Thesis (analytical stance, not buy/sell advice)
#
# This is RULES-BASED synthesis. It does no new data fetching and no new
# math -- it only reorganises and interprets what Sections 1-4 produced.
# The LLM prose upgrade is deferred to Module 8.
# =============================================================================


def build_bull_case(key_stories, scorecard):
    """
    Assembles the strongest arguments FOR the company.

    Sources:
      - Every story whose signal is "bullish"
      - Scorecard dimensions scoring >= 70% of their maximum

    Returns a list of plain-English bullet strings.
    """
    bull_points = []

    # Bullish stories first
    for story in key_stories:
        if story.get("signal") == "bullish":
            bull_points.append(story.get("context", story.get("title", "")))

    # Strong scorecard dimensions
    dimension_labels = {
        "growth": "Growth",
        "quality": "Quality",
        "financial_strength": "Financial Strength",
        "valuation": "Valuation",
        "sector_outlook": "Sector Outlook",
    }
    for key, label in dimension_labels.items():
        dim = scorecard.get(key)
        if not dim:
            continue
        score = dim.get("score", 0)
        out_of = dim.get("out_of", 1)
        if out_of > 0 and (score / out_of) >= 0.70:
            bull_points.append(
                f"{label} scores strongly ({score}/{out_of}): {dim.get('reason', '')}"
            )

    if not bull_points:
        bull_points.append("No clearly bullish signals detected in the current data.")

    return bull_points


def build_bear_case(key_stories, scorecard):
    """
    Assembles the strongest arguments AGAINST the company.

    Sources:
      - Every story whose signal is "bearish"
      - Scorecard dimensions scoring < 50% of their maximum

    Returns a list of plain-English bullet strings.
    """
    bear_points = []

    for story in key_stories:
        if story.get("signal") == "bearish":
            bear_points.append(story.get("context", story.get("title", "")))

    dimension_labels = {
        "growth": "Growth",
        "quality": "Quality",
        "financial_strength": "Financial Strength",
        "valuation": "Valuation",
        "sector_outlook": "Sector Outlook",
    }
    for key, label in dimension_labels.items():
        dim = scorecard.get(key)
        if not dim:
            continue
        score = dim.get("score", 0)
        out_of = dim.get("out_of", 1)
        if out_of > 0 and (score / out_of) < 0.50:
            bear_points.append(
                f"{label} is weak ({score}/{out_of}): {dim.get('reason', '')}"
            )

    if not bear_points:
        bear_points.append(
            "No clearly bearish signals detected -- but absence of visible "
            "negatives is not the same as absence of risk (see Key Risks)."
        )

    return bear_points


def build_key_risks(key_stories, scorecard, market):
    """
    Surfaces specific things to watch. Distinct from the bear case:
    the bear case is "arguments against"; risks are "what could go wrong
    or what the data cannot tell us reliably".

    Returns a list of plain-English bullet strings.
    """
    risks = []

    # 1. Any scorecard dimension below 50% is a concrete risk area
    dimension_labels = {
        "growth": "growth",
        "quality": "profitability/quality",
        "financial_strength": "balance-sheet strength",
        "valuation": "valuation",
        "sector_outlook": "sector outlook",
    }
    for key, label in dimension_labels.items():
        dim = scorecard.get(key)
        if not dim:
            continue
        score = dim.get("score", 0)
        out_of = dim.get("out_of", 1)
        if out_of > 0 and (score / out_of) < 0.50:
            risks.append(f"Weak {label} ({score}/{out_of}) is a drag on the overall picture.")

    # 2. "Strong overall but a hidden soft spot" -- flag the lowest dimension
    #    even when the total score is healthy, so a good score never masks a gap.
    total = scorecard.get("total", 0)
    if total >= 60:
        weakest_key = None
        weakest_ratio = 1.0
        for key in dimension_labels:
            dim = scorecard.get(key)
            if not dim:
                continue
            out_of = dim.get("out_of", 1)
            if out_of > 0:
                ratio = dim.get("score", 0) / out_of
                if ratio < weakest_ratio:
                    weakest_ratio = ratio
                    weakest_key = key
        if weakest_key and weakest_ratio < 0.60:
            dim = scorecard.get(weakest_key)
            risks.append(
                f"Despite a healthy overall score, {dimension_labels[weakest_key]} "
                f"is the soft spot ({dim.get('score')}/{dim.get('out_of')}) and is "
                f"the thing most worth monitoring."
            )

    # 3. Known data-quality caveat for Indian stocks
    if market == "India":
        risks.append(
            "Data caveat: EV/EBITDA is excluded for Indian stocks due to a known "
            "currency-mixing issue in the source data, so any EV/EBITDA figure "
            "shown should be ignored."
        )

    if not risks:
        risks.append("No specific elevated risks flagged by the current data.")

    return risks


def build_investment_thesis(scorecard, key_stories, lenses):
    """
    Produces an ANALYTICAL STANCE (not buy/sell/hold advice).
    Describes what the data supports and names the central tension.

    Returns a single multi-sentence string.
    """
    total = scorecard.get("total", 0)
    rating = scorecard.get("rating", "")

    bull_count = sum(1 for s in key_stories if s.get("signal") == "bullish")
    bear_count = sum(1 for s in key_stories if s.get("signal") == "bearish")

    # Polish: pluralise correctly and hide missing-peer valuation text
    def _plural(count, word):
        return f"{count} {word}" if count == 1 else f"{count} {word}s"

    # Opening stance keyed to the score band
    if total >= 80:
        stance = (
            "The data supports a strongly constructive view: the company scores "
            "highly across most dimensions."
        )
    elif total >= 60:
        stance = (
            "The data supports a broadly constructive view, though with clear "
            "areas to watch rather than an unqualified positive."
        )
    elif total >= 40:
        stance = (
            "The data supports a neutral, wait-and-see stance: strengths and "
            "weaknesses roughly offset, and the picture is sector- and macro-dependent."
        )
    else:
        stance = (
            "The data supports a cautious stance: weaknesses currently outweigh "
            "demonstrated strengths."
        )

    # Name the central tension using the bull/bear balance
    if bull_count > 0 and bear_count == 0:
        balance = (
            f" The signal set is one-sided ({bull_count} supportive, none negative), "
            f"so the main question is whether any single soft dimension undermines the case."
        )
    elif bear_count > 0 and bull_count == 0:
        balance = (
            f" The signal set leans negative ({bear_count} concerns, none supportive), "
            f"so the burden of proof is on a turnaround."
        )
    elif bull_count > 0 and bear_count > 0:
        balance = (
            f" The central tension is between {_plural(bull_count, 'supportive signal')} "
            f"and {_plural(bear_count, 'concern')}, which the reader must weigh directly."
        )
    else:
        balance = (
            " No strong directional stories were detected, so the stance rests on "
            "the scorecard dimensions rather than standout signals."
        )

    # Add the valuation angle from the valuation lens, if present.
    # Guard: if peer data was missing, the finding contains 'None' or
    # 'no peer' and would read awkwardly, so we drop the clause.
    val_lens = lenses.get("valuation", {})
    val_finding = val_lens.get("finding", "")
    finding_lower = val_finding.lower()
    peer_data_missing = ("none" in finding_lower) or ("no peer" in finding_lower)
    if val_finding and not peer_data_missing:
        valuation = " Valuation context: " + val_finding
    else:
        valuation = ""

    return stance + balance + valuation


def run_thesis_generator(analysis_result, sector_intelligence_result):
    """
    Orchestrates Section 5. Reads the Section 4 analysis_result and the
    Section 1 sector result, builds all four components, prints a summary,
    and returns them as a dictionary.
    """
    print("\n" + "=" * 60)
    print("THESIS GENERATOR")
    print("=" * 60)

    key_stories = analysis_result.get("key_stories", [])
    scorecard = analysis_result.get("scorecard", {})
    lenses = analysis_result.get("lenses", {})
    market = sector_intelligence_result.get("market", "US")

    bull_case = build_bull_case(key_stories, scorecard)
    bear_case = build_bear_case(key_stories, scorecard)
    key_risks = build_key_risks(key_stories, scorecard, market)
    thesis = build_investment_thesis(scorecard, key_stories, lenses)

    # Print a clean summary
    print("\n-- BULL CASE " + "-" * 33)
    for point in bull_case:
        print("  + " + point)

    print("\n-- BEAR CASE " + "-" * 33)
    for point in bear_case:
        print("  - " + point)

    print("\n-- KEY RISKS " + "-" * 33)
    for risk in key_risks:
        print("  ! " + risk)

    print("\n-- INVESTMENT THESIS (analytical stance) " + "-" * 5)
    print("  " + thesis)

    print("\n[Thesis Generator] Complete.")

    return {
        "bull_case": bull_case,
        "bear_case": bear_case,
        "key_risks": key_risks,
        "investment_thesis": thesis,
    }


if __name__ == "__main__":
    test_ticker = "AAPL"  # Change this to test other tickers

    # Optional: provide manual peers if you want to control comparison
    # Set to None to use automatic peer discovery
    # Example: manual_peers = ["TCS.NS", "WIPRO.NS", "HCLTECH.NS"]
    manual_peers = None

    print("Running standalone test with ticker: " + test_ticker)
    if manual_peers:
        print("Manual peers provided: " + str(manual_peers))

    sector_result = run_sector_intelligence(test_ticker)
    if not sector_result.get("success"):
        print("[TEST FAILED] Section 1 failed.")
    else:
        data_result = run_data_engine(test_ticker, sector_result)
        if not data_result:
            print("[TEST FAILED] Section 2 failed.")
        else:
            peer_result = run_peer_engine(
                test_ticker, sector_result, data_result,
                manual_peers=manual_peers
            )
            analysis_result = run_analysis_engine(
                test_ticker, sector_result, data_result, peer_result
            )
            # Section 5: Thesis Generator
            thesis_result = run_thesis_generator(
                analysis_result, sector_result
            )

            print("[TEST PASSED] All four sections completed!")
            print("[TEST PASSED] Peers found: " + str(peer_result["peer_count"]))
            print("[TEST PASSED] Peer confidence: " + str(peer_result["peer_confidence"]))
            print("[TEST PASSED] Total score: " + str(analysis_result["scorecard"]["total"]) + "/100")
            print("[TEST PASSED] Rating: " + analysis_result["scorecard"]["rating"])
            