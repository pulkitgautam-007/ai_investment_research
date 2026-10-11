# ============================================================
# MODULE: Company Identifier
# PURPOSE: Takes a company name and finds its stock details
# FILE: modules/company_identifier.py
# ============================================================

import yfinance as yf          # The Yahoo Finance library we installed
import json                    # Built-in Python tool to format data nicely


def get_company_details(ticker_symbol: str, fallback_name: str = None, fallback_exchange: str = None) -> dict:
    """
    Fetch full company details for a KNOWN ticker symbol - no searching, no questions.

    Used two ways:
      1. By search_company(), after the user picks a match from the list.
      2. Directly by main.py when you run:  python main.py --ticker INFY.NS
         (this is what lets the scheduler run without a human present).

    Parameters:
        ticker_symbol:     e.g. "INFY.NS" or "AAPL"
        fallback_name:     name to use if yfinance has no company name
        fallback_exchange: exchange code to use if yfinance has none

    Returns:
        The same dictionary shape search_company() has always returned.
    """
    # Clean up what was typed: remove stray spaces, make it upper case ("infy.ns" -> "INFY.NS")
    ticker_symbol = ticker_symbol.strip().upper()

    print(f"\nFetching full details for {ticker_symbol}...")

    try:
        info = yf.Ticker(ticker_symbol).info   # Big dictionary of company data from yfinance

    except Exception as e:
        return {
            "success": False,
            "error": f"Could not fetch details for {ticker_symbol}: {str(e)}",
            "company_name": fallback_name or ticker_symbol
        }

    # A mistyped ticker often does NOT raise an error - yfinance just returns an
    # almost-empty dictionary. No company name at all = treat as "not found"
    # instead of pretending we succeeded.
    if not info or not (info.get("longName") or info.get("shortName")):
        return {
            "success": False,
            "error": f"No company found for ticker '{ticker_symbol}'. Check the spelling (Indian stocks need .NS or .BO).",
            "company_name": fallback_name or ticker_symbol
        }

    # Extract the fields we care about (same keys as before, so nothing downstream breaks)
    company_data = {
        "success":          True,
        "ticker":           ticker_symbol,
        "company_name":     info.get("longName") or fallback_name or info.get("shortName"),
        "exchange":         info.get("exchange",            fallback_exchange or "N/A"),
        "exchange_full":    info.get("fullExchangeName",    "N/A"),
        "currency":         info.get("currency",            "N/A"),
        "sector":           info.get("sector",              "N/A"),
        "industry":         info.get("industry",            "N/A"),
        "country":          info.get("country",             "N/A"),
        "website":          info.get("website",             "N/A"),
        "market_cap":       info.get("marketCap",           None),
        "employees":        info.get("fullTimeEmployees",   None),
        "description":      info.get("longBusinessSummary", "N/A"),
        "quote_type":       info.get("quoteType",           "N/A"),
    }

    return company_data


def search_company(company_name: str) -> dict:
    """
    Search for a company by name and return its key details.
    
    Parameters:
        company_name: The name you type, e.g. "Apple" or "Reliance Industries"
    
    Returns:
        A dictionary with company details, or an error message
    """
    
    print(f"\n🔍 Searching for: {company_name}")
    print("-" * 50)
    
    # Step 1: Use yfinance to search for the ticker symbol
    # yf.Search() looks up companies matching your search term
    try:
        search_results = yf.Search(company_name, max_results=5)
        quotes = search_results.quotes   # This gives us a list of matching companies
        
    except Exception as e:
        # If the search fails (e.g. no internet), we catch the error here
        return {
            "success": False,
            "error": f"Search failed: {str(e)}",
            "company_name": company_name
        }
    
    # Step 2: Check if we got any results at all
    if not quotes:
        return {
            "success": False,
            "error": "No companies found with that name. Try a different spelling.",
            "company_name": company_name
        }
    
    # Step 3: Show the user the top matches so they can pick the right one
    print(f"\n📋 Found {len(quotes)} possible matches:\n")
    
    for i, quote in enumerate(quotes):
        # Each quote has fields like symbol, shortname, exchange, quoteType
        symbol    = quote.get("symbol", "N/A")
        name      = quote.get("shortname", quote.get("longname", "N/A"))
        exchange  = quote.get("exchange", "N/A")
        type_     = quote.get("quoteType", "N/A")
        
        print(f"  [{i+1}] {symbol:12} | {name:40} | {exchange:10} | {type_}")
    
    # Step 4: Ask the user to pick which match is correct
    print(f"\n  [0] None of these — search again")
    
    while True:   # Keep asking until we get a valid answer
        try:
            choice = int(input(f"\n👉 Enter the number of the correct company (1-{len(quotes)}) or 0 to exit: "))
            if 0 <= choice <= len(quotes):
                break    # Valid choice — exit the loop
            else:
                print(f"   ⚠️  Please enter a number between 0 and {len(quotes)}")
        except ValueError:
            # If the user types letters instead of a number
            print("   ⚠️  Please type a number, not letters.")
    
    # If user chose 0, they want to try again
    if choice == 0:
        return {
            "success": False,
            "error": "User chose to search again.",
            "company_name": company_name
        }
    
    # Step 5: Get the selected company's ticker symbol
    selected_quote  = quotes[choice - 1]    # Lists count from 0, so choice 1 = index 0
    ticker_symbol   = selected_quote.get("symbol", "")
    
        # Steps 6 & 7: Fetch full details for the chosen ticker.
    # This work now lives in get_company_details() so main.py can also call it
    # directly with a known ticker (no list, no question).
    return get_company_details(
        ticker_symbol,
        fallback_name=selected_quote.get("shortname", company_name),
        fallback_exchange=selected_quote.get("exchange", "N/A"),
    )

def format_market_cap(value) -> str:
    """
    Converts a raw market cap number into a human-readable string.
    E.g. 3000000000000 → "$3.00 Trillion"
    """
    if value is None:
        return "N/A"
    
    if value >= 1_000_000_000_000:      # Trillion (12 zeros)
        return f"${value / 1_000_000_000_000:.2f} Trillion"
    elif value >= 1_000_000_000:        # Billion (9 zeros)
        return f"${value / 1_000_000_000:.2f} Billion"
    elif value >= 1_000_000:            # Million (6 zeros)
        return f"${value / 1_000_000:.2f} Million"
    else:
        return f"${value:,}"            # Just add commas for smaller numbers


def display_company_summary(company_data: dict):
    """
    Prints a clean, formatted summary of the company to the screen.
    """
    if not company_data.get("success"):
        print(f"\n❌ Error: {company_data.get('error', 'Unknown error')}")
        return
    
    print("\n" + "=" * 60)
    print("  ✅ COMPANY IDENTIFIED")
    print("=" * 60)
    print(f"  Company Name  : {company_data['company_name']}")
    print(f"  Ticker Symbol : {company_data['ticker']}")
    print(f"  Exchange      : {company_data['exchange_full']} ({company_data['exchange']})")
    print(f"  Currency      : {company_data['currency']}")
    print(f"  Sector        : {company_data['sector']}")
    print(f"  Industry      : {company_data['industry']}")
    print(f"  Country       : {company_data['country']}")
    print(f"  Market Cap    : {format_market_cap(company_data['market_cap'])}")
    print(f"  Employees     : {company_data['employees']:,}" if company_data['employees'] else f"  Employees     : N/A")
    print(f"  Website       : {company_data['website']}")
    print("=" * 60)
    
    # Print a short version of the description (first 300 characters)
    desc = company_data.get("description", "N/A")
    if desc != "N/A" and len(desc) > 300:
        print(f"\n  About: {desc[:300]}...")
    elif desc != "N/A":
        print(f"\n  About: {desc}")
    
    print()


# ============================================================
# STANDALONE TEST — runs only when you execute THIS file directly
# ============================================================
if __name__ == "__main__":
    # This block only runs if you run: python modules/company_identifier.py
    # It does NOT run when other files import this module
    
    print("=" * 60)
    print("  COMPANY IDENTIFIER — TEST MODE")
    print("=" * 60)
    
    while True:
        company_input = input("\n🏢 Enter a company name to search (or 'quit' to exit): ").strip()
        
        if company_input.lower() in ("quit", "exit", "q"):
            print("\n👋 Goodbye!\n")
            break
        
        if not company_input:
            print("   ⚠️  Please enter a company name.")
            continue
        
        # Run the search
        result = search_company(company_input)
        
        # Display the result
        display_company_summary(result)