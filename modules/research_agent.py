# =============================================================================
# MODULE 2: RESEARCH AGENT  (v4 — Tavily + NewsAPI + Finnhub, refined filter)
# File: modules/research_agent.py
#
# PURPOSE: The "internet researcher" of our system.
# Accepts a research request (company, sector, or deal),
# fetches information from THREE sources in parallel,
# filters out irrelevant/low-quality/duplicate articles,
# and returns clean, scored, relevant results.
#
# SOURCES (in order of strength):
#   1. Tavily   : Google-quality web search WITH full article text (primary)
#   2. NewsAPI  : Broad Indian + global media coverage (secondary)
#   3. Finnhub  : US market financial news + merger feeds (tertiary)
#
# RESEARCH MODES: company / sector / deal
#
# CHANGES FROM V3:
#   1. Company mode requires the full distinctive name to appear
#      (kills false matches like "reduce reliance on China")
#   2. Tavily uses clean summary content first, raw page only as fallback
#      (removes navigation/menu junk from summaries)
#   3. Duplicate detection now checks URL as well as title
#      (catches the same article published under slightly different titles)
# =============================================================================


# --- IMPORTS ---

import os
import time
import requests
import finnhub
from tavily import TavilyClient
from datetime import datetime, timedelta
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load API keys from .env — must run before any os.getenv()
load_dotenv()


# =============================================================================
# CONFIGURATION
# =============================================================================

NEWS_API_KEY    = os.getenv("NEWS_API_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
TAVILY_API_KEY  = os.getenv("TAVILY_API_KEY")

MAX_ARTICLES        = 10    # results to fetch per source
MIN_CONTENT_LENGTH  = 80    # min characters of content to be useful
MIN_RELEVANCE_SCORE = 40    # min score (0-100) to pass the filter
REQUEST_DELAY       = 0.5   # polite pause between API calls (seconds)
REQUEST_TIMEOUT     = 10    # give up on a slow API after this many seconds

# Common/short words ignored during keyword matching
STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all",
    "can", "had", "her", "was", "one", "our", "out", "day",
    "get", "has", "him", "his", "how", "its", "let", "may",
    "new", "now", "old", "see", "two", "way", "who", "boy",
    "did", "she", "too", "use", "with", "this", "that", "from",
    "they", "been", "have", "more", "will", "also", "into",
    "than", "then", "when", "what", "said", "each", "which",
    "deal", "stock", "stocks"   # too generic in finance context
}


# =============================================================================
# HELPER: Clean Raw Text
# =============================================================================

def clean_text(text):
    """Cleans text — strips whitespace, collapses blank lines. Always a string."""
    if not text:
        return ""
    text = str(text).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


# =============================================================================
# HELPER: Extract Meaningful Words from a Keyword Phrase
# =============================================================================

def extract_meaningful_words(keyword):
    """
    Splits a phrase into meaningful words (4+ chars, not stop words).
    "Banking sector India" -> ["banking", "sector", "india"]
    """
    words = keyword.lower().split()
    return [w for w in words if len(w) > 3 and w not in STOP_WORDS]


# =============================================================================
# HELPER: Build Search Keywords
# =============================================================================

def build_keywords(research_request):
    """
    Builds keyword phrases based on research mode.

    For COMPANY mode, also stores the company's full name as a
    "_required_phrase" inside the request. The filter later REQUIRES
    this phrase to appear in an article — this is what stops false
    matches like "reduce reliance on China" from passing.

    Returns a deduplicated list of search phrases.
    """

    mode     = research_request.get("mode", "company")
    keywords = []

    if mode == "company":
        name = research_request.get("name", "")
        # The full company name must appear in any article we keep.
        # Stored on the request so the filter can read it later.
        research_request["_required_phrase"] = name.lower()
        keywords = [
            name,
            f"{name} stock",
            f"{name} earnings",
            f"{name} results",
            f"{name} news",
        ]

    elif mode == "sector":
        sector  = research_request.get("sector", "")
        country = research_request.get("country", "India")
        keywords = [
            f"{sector} sector {country}",
            f"{sector} industry {country}",
            f"{sector} stocks {country}",
            f"{country} {sector} outlook",
            f"{sector} companies {country}",
            sector,
        ]

    elif mode == "deal":
        description = research_request.get("description", "")
        companies   = research_request.get("companies", [])
        keywords = [description]
        if len(companies) >= 2:
            keywords += [
                f"{companies[0]} {companies[1]}",
                f"{companies[0]} {companies[1]} deal",
                f"{companies[0]} {companies[1]} acquisition",
                f"{companies[0]} {companies[1]} merger",
            ]
        elif len(companies) == 1:
            keywords += [
                f"{companies[0]} acquisition",
                f"{companies[0]} merger",
                f"{companies[0]} deal",
                f"{companies[0]} news",
            ]

    # Deduplicate while preserving order
    seen, clean = set(), []
    for kw in keywords:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            clean.append(kw)
    return clean


# =============================================================================
# HELPER: Build the Main Web-Search Query (one natural phrase for Tavily)
# =============================================================================

def build_search_query(research_request):
    """
    Builds ONE natural-language query for Tavily web search.
    Company : "Reliance Industries stock news latest financial results"
    Sector  : "Banking sector India outlook news investment"
    Deal    : "Adani acquisition deal latest news details"
    """
    mode = research_request.get("mode", "company")

    if mode == "company":
        name = research_request.get("name", "")
        return f"{name} stock news latest financial results"
    elif mode == "sector":
        sector  = research_request.get("sector", "")
        country = research_request.get("country", "India")
        return f"{sector} sector {country} outlook news investment"
    elif mode == "deal":
        description = research_request.get("description", "")
        return f"{description} latest news details"
    return research_request.get("name", "")


# =============================================================================
# FILTERING LAYER: Score a Single Article
# =============================================================================

def score_article(article, keywords):
    """
    Scores an article 0-100 for relevance.

    Scores against FOUR fields (all available on free tiers):
      title (strongest), description, content, url

    Per keyword:
      LEVEL 1 — full phrase match (title +30, content +10)
      LEVEL 2 — individual word match (proportional)

    Plus a small bonus from Tavily's own relevance score.
    """

    score = 0
    title       = article.get("title", "").lower()
    content     = article.get("content", "").lower()
    description = article.get("description", "").lower()
    url         = article.get("url", "").lower()

    full_text = content + " " + description
    url_words = url.replace("/", " ").replace("-", " ").replace(".", " ")

    for keyword in keywords:
        kw = keyword.lower()

        # LEVEL 1: full phrase
        if kw in title:
            score += 30
        elif kw in full_text:
            score += 10

        # LEVEL 2: individual words
        words = extract_meaningful_words(keyword)
        if not words:
            continue

        title_hits   = sum(1 for w in words if w in title)
        content_hits = sum(1 for w in words if w in full_text)
        url_hits     = sum(1 for w in words if w in url_words)

        if (title_hits / len(words)) >= 0.5:
            score += int((title_hits / len(words)) * 20)
        if (content_hits / len(words)) >= 0.5:
            score += int((content_hits / len(words)) * 10)
        if (url_hits / len(words)) >= 0.5:
            score += int((url_hits / len(words)) * 10)

    # BONUS: substantial content
    if len(content) + len(description) > MIN_CONTENT_LENGTH:
        score += 10

    # BONUS: Tavily's own relevance (0.0 - 1.0)
    tavily_score = article.get("tavily_score", 0)
    if tavily_score:
        score += int(tavily_score * 15)

    return min(score, 100)


# =============================================================================
# FILTERING LAYER: Filter and Score All Articles
# =============================================================================

def filter_articles(raw_articles, keywords, research_request=None):
    """
    Runs all raw articles through gates and returns clean results.

    Gate 1 : has a title
    Gate 2 : not a duplicate (checks BOTH title AND url)
    Gate 3 : has enough content
    Gate 4a: (company mode only) must contain the full company name
    Gate 4b: relevance score >= MIN_RELEVANCE_SCORE
    Gate 5 : sort by score (best first)
    """

    if research_request is None:
        research_request = {}

    # The required phrase only exists for company mode (set in build_keywords)
    required_phrase = research_request.get("_required_phrase", "")

    print(f"\n  [Filter] Starting with {len(raw_articles)} raw articles...")

    rej_no_title = rej_dup = rej_short = rej_low = rej_no_name = 0
    filtered = []
    seen_titles = set()
    seen_urls   = set()

    for article in raw_articles:

        # GATE 1: title
        title = clean_text(article.get("title", ""))
        if not title or title.lower() in ("none", "n/a"):
            rej_no_title += 1
            continue

        # GATE 2: duplicate — check title AND url
        title_key = "".join(c for c in title.lower() if c.isalnum() or c.isspace()).strip()

        # Normalize URL: strip protocol, www, and trailing slash for comparison
        url = article.get("url", "").lower()
        url_key = (url.replace("https://", "").replace("http://", "")
                      .replace("www.", "").rstrip("/"))

        if title_key in seen_titles or (url_key and url_key in seen_urls):
            rej_dup += 1
            continue
        seen_titles.add(title_key)
        if url_key:
            seen_urls.add(url_key)

        # GATE 3: content length
        content     = clean_text(article.get("content", ""))
        description = clean_text(article.get("description", ""))
        if len(content) + len(description) < MIN_CONTENT_LENGTH:
            rej_short += 1
            continue

        # GATE 4a: company mode must contain the full company name
        if required_phrase:
            haystack = (title + " " + content + " " + description).lower()
            if required_phrase not in haystack:
                rej_no_name += 1
                continue

        # GATE 4b: relevance score
        score = score_article(article, keywords)
        if score < MIN_RELEVANCE_SCORE:
            rej_low += 1
            continue

        # passed — enrich and keep
        article["relevance_score"] = score
        article["title"]           = title
        article["content"]         = content
        article["description"]     = description
        filtered.append(article)

    # GATE 5: sort by score
    filtered.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    print(f"  [Filter] Results breakdown:")
    print(f"           PASSED        : {len(filtered)}")
    print(f"           x No title    : {rej_no_title}")
    print(f"           x Duplicate   : {rej_dup}")
    print(f"           x Too short   : {rej_short}")
    if required_phrase:
        print(f"           x Name absent : {rej_no_name}")
    print(f"           x Low score   : {rej_low}")

    return filtered


# =============================================================================
# SOURCE ADAPTER 1: Tavily  (PRIMARY — web search with full content)
# =============================================================================

def fetch_from_tavily(keywords, research_request):
    """
    Searches the web via Tavily and returns articles.

    Tavily gives two content fields per result:
      - content     : a clean AI-extracted summary (we prefer this)
      - raw_content  : the full raw page (often full of menu/nav junk)
    We use the clean summary first and only fall back to raw_content
    if the summary is empty.
    """

    print("\n  [Tavily] Starting web search...")

    if not TAVILY_API_KEY:
        print("  [Tavily] WARNING: No API key found in .env. Skipping.")
        return []

    articles = []

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        query  = build_search_query(research_request)
        print(f"  [Tavily] Query: '{query}'")

        response = client.search(
            query               = query,
            search_depth        = "advanced",
            max_results         = MAX_ARTICLES,
            include_raw_content  = True,
            topic               = "news",
            days                = 30,
        )

        results = response.get("results", [])
        print(f"  [Tavily] Returned {len(results)} results")

        for item in results:
            # Prefer the clean summary; fall back to a trimmed slice of raw page
            clean_summary = item.get("content", "")
            raw           = item.get("raw_content", "") or ""
            full_content  = clean_summary if clean_summary else raw[:2000]

            url = item.get("url", "")
            article = {
                "source"       : "Tavily",
                "source_name"  : url.split("/")[2] if url else "Web",
                "title"        : clean_text(item.get("title", "")),
                "description"  : clean_text(clean_summary),
                "content"      : clean_text(full_content),
                "url"          : url,
                "published_at" : item.get("published_date", ""),
                "tavily_score" : item.get("score", 0),
                "category"     : "",
            }
            if article["title"]:
                articles.append(article)

    except Exception as e:
        print(f"  [Tavily] Error: {e}")

    print(f"  [Tavily] Articles collected: {len(articles)}")
    return articles


# =============================================================================
# SOURCE ADAPTER 2: NewsAPI  (SECONDARY)
# =============================================================================

def fetch_from_newsapi(keywords, research_request):
    """
    Fetches news from NewsAPI across thousands of media sources.
    Free tier truncates 'content' — that's why Tavily is primary.
    Still valuable for broad Indian media coverage.
    """

    print("\n  [NewsAPI] Starting fetch...")

    if not NEWS_API_KEY:
        print("  [NewsAPI] WARNING: No API key found in .env. Skipping.")
        return []

    articles = []
    end_date   = datetime.now()
    start_date = end_date - timedelta(days=30)
    from_date  = start_date.strftime("%Y-%m-%d")
    to_date    = end_date.strftime("%Y-%m-%d")

    for keyword in keywords[:3]:   # top 3 keywords (respect rate limits)

        url = "https://newsapi.org/v2/everything"
        params = {
            "q": keyword, "from": from_date, "to": to_date,
            "language": "en", "sortBy": "relevancy",
            "pageSize": MAX_ARTICLES, "apiKey": NEWS_API_KEY
        }

        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json().get("articles", [])
            print(f"  [NewsAPI] '{keyword}' -> {len(raw)} results")

            for item in raw:
                article = {
                    "source"       : "NewsAPI",
                    "source_name"  : item.get("source", {}).get("name", "Unknown"),
                    "title"        : clean_text(item.get("title", "")),
                    "description"  : clean_text(item.get("description", "")),
                    "content"      : clean_text(item.get("content", "")),
                    "url"          : item.get("url", ""),
                    "published_at" : item.get("publishedAt", ""),
                    "tavily_score" : 0,
                    "category"     : "",
                }
                if article["title"]:
                    articles.append(article)

            time.sleep(REQUEST_DELAY)

        except requests.exceptions.HTTPError as e:
            print(f"  [NewsAPI] HTTP Error for '{keyword}': {e}")
        except requests.exceptions.ConnectionError:
            print(f"  [NewsAPI] Connection error — check internet")
        except requests.exceptions.Timeout:
            print(f"  [NewsAPI] Timeout for '{keyword}'")
        except Exception as e:
            print(f"  [NewsAPI] Unexpected error: {e}")

    print(f"  [NewsAPI] Total collected: {len(articles)}")
    return articles


# =============================================================================
# SOURCE ADAPTER 3: Finnhub  (TERTIARY — US market + merger feeds)
# =============================================================================

def fetch_from_finnhub(keywords, research_request):
    """
    Fetches financial news from Finnhub (free tier: general + merger news).
    US-focused, but adds value for deal mode and global market context.
    """

    print("\n  [Finnhub] Starting fetch...")

    if not FINNHUB_API_KEY:
        print("  [Finnhub] WARNING: No API key found in .env. Skipping.")
        return []

    client = finnhub.Client(api_key=FINNHUB_API_KEY)
    articles = []
    mode = research_request.get("mode", "company")

    try:
        if mode == "deal":
            merger  = client.general_news("merger",  min_id=0)
            general = client.general_news("general", min_id=0)
            items   = merger + general
            print(f"  [Finnhub] merger: {len(merger)}, general: {len(general)}")
        else:
            items = client.general_news("general", min_id=0)
            print(f"  [Finnhub] general news: {len(items)}")

        for item in items[:MAX_ARTICLES * 3]:
            raw_time = item.get("datetime", 0)
            try:
                pub = str(datetime.fromtimestamp(raw_time))
            except Exception:
                pub = ""

            article = {
                "source"       : "Finnhub",
                "source_name"  : clean_text(item.get("source", "Unknown")),
                "title"        : clean_text(item.get("headline", "")),
                "description"  : clean_text(item.get("summary", "")),
                "content"      : clean_text(item.get("summary", "")),
                "url"          : item.get("url", ""),
                "published_at" : pub,
                "tavily_score" : 0,
                "category"     : item.get("category", ""),
            }
            if article["title"]:
                articles.append(article)

        time.sleep(REQUEST_DELAY)

    except finnhub.FinnhubAPIException as e:
        print(f"  [Finnhub] API Error: {e}")
    except Exception as e:
        print(f"  [Finnhub] Unexpected error: {e}")

    print(f"  [Finnhub] Articles collected: {len(articles)}")
    return articles


# =============================================================================
# RESEARCH MODE BUILDERS
# =============================================================================

def research_company(name, ticker):
    """Create a company research request."""
    return {
        "mode": "company", "name": name, "ticker": ticker,
        "description": f"Investment research on {name} ({ticker})"
    }


def research_sector(sector, country="India"):
    """Create a sector research request."""
    return {
        "mode": "sector", "sector": sector, "country": country,
        "name": f"{sector} Sector ({country})",
        "description": f"Sector research on {sector} in {country}"
    }


def research_deal(description, companies=None):
    """Create a deal tracking research request."""
    return {
        "mode": "deal", "description": description,
        "companies": companies or [], "name": description,
    }


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================

def run_research(research_request):
    """
    Orchestrates the full pipeline:
    1. Build keywords  (also sets _required_phrase for company mode)
    2. Fetch from Tavily + NewsAPI + Finnhub in parallel
    3. Combine raw articles
    4. Filter and score (passing the request for the name-check gate)
    5. Return structured results
    """

    print("\n" + "="*60)
    print("RESEARCH AGENT: Starting")
    print("="*60)
    print(f"Mode       : {research_request.get('mode', '').upper()}")
    print(f"Target     : {research_request.get('name', research_request.get('description', ''))}")
    print(f"Started at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-"*60)

    # Step 1: keywords (this also sets _required_phrase on the request)
    keywords = build_keywords(research_request)
    print(f"\nSearch keywords ({len(keywords)}):")
    for i, kw in enumerate(keywords, 1):
        print(f"  {i}. {kw}")

    # Step 2 & 3: parallel fetch from all three sources
    print("\nLaunching parallel fetch from all three sources...")
    all_raw = []
    source_counts = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(fetch_from_tavily,  keywords, research_request): "Tavily",
            executor.submit(fetch_from_newsapi, keywords, research_request): "NewsAPI",
            executor.submit(fetch_from_finnhub, keywords, research_request): "Finnhub",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                all_raw.extend(result)
                source_counts[name] = len(result)
                print(f"  OK {name}: {len(result)} raw articles")
            except Exception as e:
                print(f"  FAIL {name}: {e}")
                source_counts[name] = 0

    print(f"\nTotal raw articles from all sources: {len(all_raw)}")

    # Step 4: filter (pass the request so the name-check gate works)
    filtered = filter_articles(all_raw, keywords, research_request)

    # Step 5: assemble results
    results = {
        "mode"            : research_request.get("mode"),
        "target"          : research_request.get("name", research_request.get("description")),
        "research_request": research_request,
        "keywords_used"   : keywords,
        "timestamp"       : datetime.now().isoformat(),
        "stats"           : {
            "total_raw"      : len(all_raw),
            "total_filtered" : len(filtered),
            "by_source"      : source_counts,
        },
        "articles"        : filtered,
        "status"          : "completed" if filtered else "no_results",
    }

    print("\n" + "="*60)
    print("RESEARCH AGENT: Complete")
    print("="*60)
    print(f"  By source     : {source_counts}")
    print(f"  Raw collected : {len(all_raw)}")
    print(f"  After filter  : {len(filtered)}")
    print(f"  Status        : {results['status']}")
    if filtered:
        top = filtered[0].get("relevance_score", 0)
        avg = sum(a.get("relevance_score", 0) for a in filtered) // len(filtered)
        print(f"  Top score     : {top}/100")
        print(f"  Average score : {avg}/100")
    print("="*60)

    return results


# =============================================================================
# DISPLAY HELPER
# =============================================================================

def display_results(research_results):
    """Prints a clean human-readable summary of the top articles."""

    if not research_results:
        print("No results to display.")
        return

    articles = research_results.get("articles", [])
    stats    = research_results.get("stats", {})

    print("\n" + "="*60)
    print("RESULTS SUMMARY")
    print(f"Target : {research_results.get('target', '')}")
    print(f"Mode   : {research_results.get('mode', '').upper()}")
    print(f"Found  : {stats.get('total_filtered', 0)} relevant "
          f"(from {stats.get('total_raw', 0)} raw)")
    print("="*60)

    if not articles:
        print("\nNo relevant articles found.")
        return

    for i, a in enumerate(articles[:10], 1):
        score = a.get("relevance_score", 0)
        label = "HIGH" if score >= 70 else "MED" if score >= 50 else "LOW"
        print(f"\n[{i}] {label} ({score}/100)")
        print(f"    Source : {a.get('source')} - {a.get('source_name')}")
        print(f"    Title  : {a.get('title', '')[:90]}")
        print(f"    Date   : {str(a.get('published_at', ''))[:16]}")
        desc = a.get("description", "")
        if desc:
            print(f"    Summary: {desc[:150]}...")

    if len(articles) > 10:
        print(f"\n  ... and {len(articles) - 10} more in full results")
    print("\n" + "="*60)


# =============================================================================
# TEST BLOCK — runs only when executing this file directly
# =============================================================================

if __name__ == "__main__":

    import json

    print("RESEARCH AGENT v4 — Test Mode (Tavily + NewsAPI + Finnhub)")
    print("="*60)

    # TEST 1: Company
    print("\n>>> TEST 1: COMPANY RESEARCH")
    r1 = run_research(research_company("Reliance Industries", "RELIANCE.NS"))
    display_results(r1)

    # TEST 2: Sector
    print("\n>>> TEST 2: SECTOR RESEARCH")
    r2 = run_research(research_sector("Banking", "India"))
    display_results(r2)

    # TEST 3: Deal
    print("\n>>> TEST 3: DEAL TRACKING")
    r3 = run_research(research_deal("Adani acquisition deal", ["Adani Group"]))
    display_results(r3)

    # Save all
    os.makedirs("outputs", exist_ok=True)
    out = {"company_research": r1, "sector_research": r2, "deal_tracking": r3}
    path = "outputs/research_agent_test.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nFull results saved to: {path}")