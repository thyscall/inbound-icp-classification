import csv
import json
import logging
import os
import re
import sys

import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator
from firecrawl import FirecrawlApp
from google import genai
from tabulate import tabulate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RAW_COMPANIES_FILE = "raw-inbound-companies.txt"
INBOUND_CSV = "inbound-companies.csv"
CLASSIFIED_CSV = "classified-companies.csv"
ICP_RESEARCH_FILE = "sonar-icp-deep-research.txt"

CLASSIFIED_COLUMNS = [
    "company_name",
    "company_domain",
    "firm_type",
    "firm_reason",
    "icp_fit",
    "icp_reason",
    "firm_user_estimate",
    "firm_user_estimate_reason",
]


class CompanyClassification(BaseModel):
    company_name: str
    company_domain: str
    firm_type: str
    firm_reason: str
    icp_fit: str
    icp_reason: str
    firm_user_estimate: int
    firm_user_estimate_reason: str

    @field_validator("firm_type")
    @classmethod
    def validate_firm_type(cls, v: str) -> str:
        allowed = {"Law", "Financial", "Healthcare", "Accounting", "Creative"}
        if v not in allowed:
            raise ValueError(f"firm_type must be one of {allowed}, got '{v}'")
        return v

    @field_validator("icp_fit")
    @classmethod
    def validate_icp_fit(cls, v: str) -> str:
        allowed = {"Strong", "Weak", "Not a fit"}
        if v not in allowed:
            raise ValueError(f"icp_fit must be one of {allowed}, got '{v}'")
        return v

    @field_validator("firm_reason", "icp_reason", "firm_user_estimate_reason")
    @classmethod
    def strip_commas_from_reasons(cls, v: str) -> str:
        v = re.sub(r"(?<=\d),(?=\d{3})", "", v)
        v = v.replace(",", ";").replace("  ", " ").strip()
        return v


class AlreadyClassifiedError(Exception):
    """Raised when a domain has already been classified."""


# ---------------------------------------------------------------------------
# 1. setup_env
# ---------------------------------------------------------------------------

def setup_env() -> tuple[FirecrawlApp, genai.Client, str]:
    """
    Load environment variables, validate API keys, parse the raw inbound
    company list into a clean CSV, and return initialised API clients plus
    the ICP research text.
    """
    load_dotenv()

    firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not firecrawl_key:
        log.error("FIRECRAWL_API_KEY is missing from .env")
        sys.exit(1)
    if not gemini_key:
        log.error("GEMINI_API_KEY is missing from .env")
        sys.exit(1)

    log.info("API keys loaded from .env")

    firecrawl = FirecrawlApp(api_key=firecrawl_key)
    gemini_client = genai.Client(api_key=gemini_key)

    log.info("Firecrawl and Gemini clients initialised")

    # --- Parse raw company list into CSV --------------------------------
    _parse_raw_companies(RAW_COMPANIES_FILE, INBOUND_CSV)

    # --- Load ICP research context -------------------------------------
    with open(ICP_RESEARCH_FILE, "r", encoding="utf-8") as f:
        icp_research = f.read()
    log.info("Loaded Sonar ICP research context (%d chars)", len(icp_research))

    # --- Ensure classified CSV exists with header ----------------------
    _ensure_classified_csv()

    return firecrawl, gemini_client, icp_research


def _ensure_classified_csv() -> None:
    """Create the classified CSV with a header row if it doesn't exist or is empty."""
    needs_create = (
        not os.path.exists(CLASSIFIED_CSV)
        or os.path.getsize(CLASSIFIED_CSV) == 0
    )
    if needs_create:
        with open(CLASSIFIED_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(CLASSIFIED_COLUMNS)
        log.info("Created %s with header row", CLASSIFIED_CSV)
    else:
        log.info("%s already exists — will append new classifications", CLASSIFIED_CSV)


def _read_classified_csv() -> pd.DataFrame:
    """Safely read the classified CSV, returning an empty DataFrame if needed."""
    try:
        return pd.read_csv(CLASSIFIED_CSV)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=CLASSIFIED_COLUMNS)


def _parse_raw_companies(raw_path: str, csv_path: str) -> None:
    """
    Read the alternating-line raw company file and write a properly quoted
    CSV with columns: company_name, company_domain.
    """
    with open(raw_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    # First two lines are header labels ("Company Name", "Domain") — skip them
    data_lines = lines[2:]

    if len(data_lines) % 2 != 0:
        log.warning(
            "Odd number of data lines (%d) — last entry may be incomplete",
            len(data_lines),
        )

    companies: list[tuple[str, str]] = []
    for i in range(0, len(data_lines) - 1, 2):
        name = data_lines[i]
        domain = data_lines[i + 1]
        companies.append((name, domain))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["company_name", "company_domain"])
        for name, domain in companies:
            writer.writerow([name, domain])

    log.info("Parsed %d companies from %s → %s", len(companies), raw_path, csv_path)


# ---------------------------------------------------------------------------
# 2. scrape_website
# ---------------------------------------------------------------------------

SCRAPE_MAX_CHARS = 10_000

PARKED_SIGNALS = [
    "domain is for sale",
    "buy this domain",
    "this domain may be for sale",
    "parked free",
    "parked domain",
    "domain parking",
    "is available for purchase",
    "make an offer on this domain",
    "this webpage is parked",
    "domain has expired",
    "renew this domain",
    "hugedomains",
    "dan.com",
    "afternic",
    "sedo.com",
    "godaddy auctions",
]

JUNK_SIGNALS = [
    "cookie",
    "consent",
    "privacy policy",
    "we use cookies",
    "accept all",
    "reject all",
    "customize consent",
]

JUNK_RATIO_THRESHOLD = 0.40


def _is_parked_or_junk(markdown: str) -> str | None:
    """
    Return a reason string if the scraped markdown looks like a parked domain,
    for-sale page, or cookie-wall-only content.  Returns None if the content
    appears legitimate.
    """
    lower = markdown.lower()

    for signal in PARKED_SIGNALS:
        if signal in lower:
            return f"parked/for-sale domain (matched: '{signal}')"

    junk_chars = sum(
        len(line)
        for line in markdown.splitlines()
        if any(s in line.lower() for s in JUNK_SIGNALS)
    )
    total_chars = max(len(markdown), 1)
    if junk_chars / total_chars > JUNK_RATIO_THRESHOLD:
        return f"cookie/consent wall ({junk_chars}/{total_chars} chars = {junk_chars/total_chars:.0%} junk)"

    return None


def google_search(
    company_name: str,
    domain: str,
    gemini_client: genai.Client,
) -> str:
    """
    Use Gemini with Google Search grounding to gather ~200 words of context
    about a company whose website could not be scraped.
    """
    from google.genai import types

    prompt = (
        f"Search for information about the company \"{company_name}\" "
        f"(website: {domain}). Provide a factual summary of approximately "
        f"200 words describing what the company does, the industry it "
        f"operates in, its products or services, and any available details "
        f"about its size or customer base."
    )

    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        text = (resp.text or "").strip()
        if not text:
            log.warning("GOOGLE_SEARCH  %s — Gemini returned empty response", domain)
            return f"[No information found via Google Search for {company_name} ({domain})]"
        log.info("GOOGLE_SEARCH  %s — retrieved %d chars via Gemini grounding", domain, len(text))
        return text
    except Exception as e:
        log.error("GOOGLE_SEARCH  %s — Gemini search failed: %s", domain, e)
        return f"[Google Search failed for {company_name} ({domain}): {e}]"


STOP_WORDS = {
    "the", "inc", "llc", "llp", "ltd", "corp", "pllc", "and", "for",
    "of", "group", "legal", "financial", "healthcare", "wealth", "services",
    "company", "companies", "solutions", "consulting", "partners",
    "technology", "technologies", "global", "digital", "systems", "accounting", "creative",
}


def _meaningful_name_tokens(company_name: str) -> list[str]:
    """
    Tokenize a company name into lowercase alphanumeric words, dropping
    stop words and very short tokens. Used to compare name vs. domain / page text.
    """
    raw = re.sub(r"[^\w\s]", " ", company_name.lower())
    tokens: list[str] = []
    for w in raw.split():
        w = w.strip()
        if len(w) < 2 or w in STOP_WORDS:
            continue
        tokens.append(w)
    return tokens


def _check_name_vs_domain(company_name: str, domain: str) -> bool:
    """
    Return True if the company name has zero overlap with the domain string.
    Catches obvious cases like "La Jolla Digital" / "rcmhealthgroup.com".
    """
    tokens = _meaningful_name_tokens(company_name)
    if not tokens:
        return False
    domain_lower = domain.lower().replace("-", "").replace(".", " ")
    return not any(t in domain_lower for t in tokens)


def _check_domain_mismatch(company_name: str, domain: str, markdown: str) -> bool:
    """
    Return True if the scraped website content appears to belong to a
    completely different entity than the company name provided.

    Checks the first 2000 chars (titles / headings / hero text) for any
    meaningful overlap with the company name tokens.
    """
    tokens = _meaningful_name_tokens(company_name)
    if not tokens:
        return False

    header_section = markdown[:2000].lower()
    matches = sum(1 for t in tokens if t in header_section)
    return matches == 0


def scrape_website(
    company_name: str,
    domain: str,
    firecrawl: FirecrawlApp,
    gemini_client: genai.Client,
    classified_domains: set[str],
) -> tuple[str, bool, bool]:
    """
    Scrape a company's home page using Firecrawl.

    Returns
    -------
    (scraped_text, used_fallback, domain_mismatch)
        scraped_text    : str  — page content or google search summary.
        used_fallback   : bool — True when firecrawl failed and text came
                                 from the google_search helper.
        domain_mismatch : bool — True when the website content does not
                                 appear to match the provided company name.

    Raises
    ------
    AlreadyClassifiedError
        If the domain already exists in the classified CSV.
    """
    if domain in classified_domains:
        log.info("SKIP  %s — already in classified CSV", domain)
        raise AlreadyClassifiedError(domain)

    url = f"https://{domain}"

    # --- Attempt Firecrawl scrape -------------------------------------
    try:
        log.info("SCRAPE %s — requesting firecrawl...", domain)
        result = firecrawl.scrape(url, formats=["markdown"])
        markdown = (result.markdown or "").strip()

        if not markdown:
            log.warning("SCRAPE %s — firecrawl returned empty markdown, falling back to Google Search", domain)
            name_domain_mismatch = _check_name_vs_domain(company_name, domain)
            if name_domain_mismatch:
                log.warning("MISMATCH %s — domain does not match company name '%s'", domain, company_name)
            return google_search(company_name, domain, gemini_client), True, name_domain_mismatch

        junk_reason = _is_parked_or_junk(markdown)
        if junk_reason:
            log.warning("SCRAPE %s — %s, falling back to Google Search", domain, junk_reason)
            name_domain_mismatch = _check_name_vs_domain(company_name, domain)
            if name_domain_mismatch:
                log.warning("MISMATCH %s — domain does not match company name '%s'", domain, company_name)
            return google_search(company_name, domain, gemini_client), True, name_domain_mismatch

        if len(markdown) > SCRAPE_MAX_CHARS:
            markdown = markdown[:SCRAPE_MAX_CHARS]
            log.info("SCRAPE %s — truncated to %d chars", domain, SCRAPE_MAX_CHARS)

        mismatch = _check_domain_mismatch(company_name, domain, markdown)
        if mismatch:
            log.warning(
                "MISMATCH %s — website content does not appear to match company name '%s'",
                domain, company_name,
            )

        log.info("SCRAPE %s — success (%d chars)", domain, len(markdown))
        return markdown, False, mismatch

    except Exception as e:
        log.warning("SCRAPE %s — firecrawl failed (%s), falling back to Google Search", domain, e)
        name_domain_mismatch = _check_name_vs_domain(company_name, domain)
        if name_domain_mismatch:
            log.warning("MISMATCH %s — domain does not match company name '%s'", domain, company_name)
        return google_search(company_name, domain, gemini_client), True, name_domain_mismatch


# ---------------------------------------------------------------------------
# 3. classify_company
# ---------------------------------------------------------------------------

CLASSIFY_MODEL = "gemini-2.5-flash"
CLASSIFY_MAX_RETRIES = 2

SYSTEM_PROMPT = """You are a lead qualification analyst for Sonar, which provides evidence-backed data privacy and compliance for professional service businesses (browser-based "Invisible Guardrail" against GenAI and other data leaks). Classify inbound companies using the Sonar ICP research below.

<sonar_icp_research>
{icp_research}
</sonar_icp_research>

Given the company name, domain, and website content, classify along three dimensions. Use ONLY the definitions below.

## 1. firm_type — exactly one of: "Law", "Financial", "Healthcare", "Accounting", "Creative"

- "Law": Legal practices (litigation, corporate, family law, etc.).
- "Financial": Wealth management, RIAs, broker-dealers, firms managing client assets or advisory services.
- "Healthcare": Medical, dental, therapy, home health, pharmacies, telehealth that delivers care — or adjacent clinic operations.
- "Accounting": CPA firms, tax preparation, bookkeeping handling sensitive financial or taxpayer data.
- "Creative": Marketing, advertising, design agencies that handle client PII or campaign data (map other professional-adjacent firms here only if they fit; if none apply, pick the closest vertical).

If the business is clearly outside professional services (e.g. pure manufacturing, retail with no advisory role), still pick the closest of the five types for categorization; use icp_fit "Not a fit" to reflect poor Sonar fit.

## 2. icp_fit — exactly "Strong", "Weak", or "Not a fit"

Align with Sonar's rubric from the research:

- "Strong": Regulated professional services with real client PII/PHI exposure; roughly 10–100 seats; often MSP-managed Chrome/Edge; high urgency (e.g. RIA/law wealth segments, SEC Reg S-P / HIPAA / ABA 1.6 style obligations). GenAI or browser-heavy workflows are a plus.
- "Weak": Some regulated context or sensitive data but smaller shops, stretched IT, or lower compliance urgency; or creative with meaningful PII but weaker mandate fit.
- "Not a fit": No meaningful sensitive client data, no compliance driver, wrong industry (e.g. consumer goods brand with no professional services), or far outside Sonar's SMB professional-services focus.

## 3. firm_user_estimate — integer (0 if not applicable)

Estimate **people who would use Sonar (browser seats / staff)** — attorneys, advisors, clinicians, accountants, or agency staff who work in the browser — not revenue or API transaction counts.

Heuristics: use headcount clues from the site (team pages, "About", job posts). Solo practice ~1–5; small firm ~5–25; mid-size ~25–100. If unknown, conservative estimate with brief rationale in firm_user_estimate_reason. Use 0 only for "Not a fit" when no sensible estimate exists.

## Domain-name verification

IMPORTANT: The website may describe a **different** entity than the company name (e.g. "La Jolla Digital" vs site for another brand). Classify from **website content**; if mismatch, say so in icp_reason.

## Output format

Return ONLY a JSON object with exactly these fields:
- "firm_type": "Law" | "Financial" | "Healthcare" | "Accounting" | "Creative"
- "firm_reason": string — 15 words or fewer
- "icp_fit": "Strong" | "Weak" | "Not a fit"
- "icp_reason": string — 15 words or fewer (note name/site mismatch here if applicable)
- "firm_user_estimate": integer
- "firm_user_estimate_reason": string — 15 words or fewer (how you estimated seat count)

CRITICAL FORMATTING RULES:
- Do NOT use commas in any reason field. Use semicolons or dashes instead.
- Do NOT include company_name or company_domain in your JSON — those are injected by the system.
- Do NOT include any text outside the JSON object. No markdown fences, no explanation.
- Every reason field MUST be 15 words or fewer. Count carefully."""


def classify_company(
    company_name: str,
    company_domain: str,
    scraped_text: str,
    icp_research: str,
    gemini_client: genai.Client,
    domain_mismatch: bool = False,
) -> CompanyClassification:
    """
    Send scraped website text + ICP research context to Gemini and return
    a validated CompanyClassification.
    """
    from google.genai import types

    system = SYSTEM_PROMPT.format(icp_research=icp_research)

    mismatch_note = ""
    if domain_mismatch:
        mismatch_note = (
            "\n\nWARNING: The system detected that this website may belong to "
            "a DIFFERENT entity than the company name provided. Verify carefully "
            "and note the discrepancy in icp_reason if confirmed."
        )

    user_message = (
        f"Classify this company:\n\n"
        f"Company Name: {company_name}\n"
        f"Domain: {company_domain}\n\n"
        f"--- WEBSITE CONTENT ---\n{scraped_text}\n--- END ---"
        f"{mismatch_note}"
    )

    last_error: Exception | None = None

    for attempt in range(1, CLASSIFY_MAX_RETRIES + 1):
        try:
            log.info("CLASSIFY %s — sending to Gemini (attempt %d)", company_domain, attempt)

            resp = gemini_client.models.generate_content(
                model=CLASSIFY_MODEL,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            raw = (resp.text or "").strip()
            if not raw:
                raise ValueError("Gemini returned an empty response")

            log.info("CLASSIFY %s — received response (%d chars)", company_domain, len(raw))

            data = json.loads(raw)

            classification = CompanyClassification(
                company_name=company_name,
                company_domain=company_domain,
                firm_type=data["firm_type"],
                firm_reason=data["firm_reason"],
                icp_fit=data["icp_fit"],
                icp_reason=data["icp_reason"],
                firm_user_estimate=int(data["firm_user_estimate"]),
                firm_user_estimate_reason=data["firm_user_estimate_reason"],
            )

            log.info(
                "CLASSIFY %s — %s | %s | ~%d users",
                company_domain,
                classification.firm_type,
                classification.icp_fit,
                classification.firm_user_estimate,
            )
            return classification

        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            last_error = e
            log.warning("CLASSIFY %s — attempt %d failed: %s", company_domain, attempt, e)

    raise RuntimeError(
        f"classify_company failed for {company_name} ({company_domain}) "
        f"after {CLASSIFY_MAX_RETRIES} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# 4. append_csv
# ---------------------------------------------------------------------------

def append_csv(classification: CompanyClassification) -> None:
    """
    Write one classified company row to classified-companies.csv immediately
    after classification completes, before moving to the next company.
    """
    row = classification.model_dump()

    with open(CLASSIFIED_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=CLASSIFIED_COLUMNS, quoting=csv.QUOTE_MINIMAL
        )
        writer.writerow(row)

    log.info(
        "CSV_WRITE %s — appended row (%s | %s | ~%d users)",
        classification.company_name,
        classification.firm_type,
        classification.icp_fit,
        classification.firm_user_estimate,
    )


# ---------------------------------------------------------------------------
# 5. main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("  Sonar ICP Classification System")
    print("=" * 60 + "\n")

    firecrawl, gemini_client, icp_research = setup_env()

    df = pd.read_csv(INBOUND_CSV)
    total = len(df)
    log.info("Loaded %d companies from %s", total, INBOUND_CSV)

    classified_df = _read_classified_csv()
    classified_domains: set[str] = set()
    for d in classified_df["company_domain"].tolist():
        classified_domains.add(d.lstrip("*"))
    if classified_domains:
        log.info(
            "%d companies already classified — will skip them",
            len(classified_domains),
        )

    # --- Scrape → classify → append, one company at a time -------------
    success_count = 0
    skip_count = 0
    fail_count = 0
    fallback_companies: list[str] = []
    failed_companies: list[tuple[str, str, str]] = []

    for position, (_, row) in enumerate(df.iterrows(), start=1):
        name = row["company_name"]
        domain = row["company_domain"]

        # --- already classified? ---------------------------------------
        if domain in classified_domains:
            log.info("[%d/%d] SKIP %s (%s) — already in classified CSV", position, total, name, domain)
            skip_count += 1
            continue

        log.info("[%d/%d] START %s (%s)", position, total, name, domain)

        # --- scrape ----------------------------------------------------
        try:
            scraped_text, used_fallback, domain_mismatch = scrape_website(
                company_name=name,
                domain=domain,
                firecrawl=firecrawl,
                gemini_client=gemini_client,
                classified_domains=classified_domains,
            )
        except AlreadyClassifiedError:
            skip_count += 1
            continue
        except Exception as e:
            log.error(
                "[%d/%d] FAIL %s (%s) — scrape error: %s", position, total, name, domain, e
            )
            fail_count += 1
            failed_companies.append((name, domain, f"Scrape error: {e}"))
            continue

        if used_fallback:
            fallback_companies.append(f"{name} ({domain})")

        if not scraped_text or not scraped_text.strip():
            log.error(
                "[%d/%d] FAIL %s (%s) — no text returned from scrape or Google Search",
                position, total, name, domain,
            )
            fail_count += 1
            failed_companies.append((name, domain, "No scraped text returned"))
            continue

        # --- classify --------------------------------------------------
        try:
            classification = classify_company(
                company_name=name,
                company_domain=f"*{domain}" if used_fallback else domain,
                scraped_text=scraped_text,
                icp_research=icp_research,
                gemini_client=gemini_client,
                domain_mismatch=domain_mismatch,
            )
        except Exception as e:
            log.error(
                "[%d/%d] FAIL %s (%s) — Gemini classification error: %s",
                position, total, name, domain, e,
            )
            fail_count += 1
            failed_companies.append((name, domain, f"Classification error: {e}"))
            continue

        # --- append to CSV ---------------------------------------------
        try:
            append_csv(classification)
        except Exception as e:
            log.error(
                "[%d/%d] FAIL %s (%s) — CSV write error: %s",
                position, total, name, domain, e,
            )
            fail_count += 1
            failed_companies.append((name, domain, f"CSV write error: {e}"))
            continue

        classified_domains.add(domain)
        success_count += 1

    # ===================================================================
    #  FINAL SUMMARY
    # ===================================================================
    print("\n" + "=" * 60)
    print("  CLASSIFICATION COMPLETE")
    print("=" * 60)
    print(f"\n  Total companies:      {total}")
    print(f"  Successfully classified: {success_count}")
    print(f"  Skipped (already done):  {skip_count}")
    print(f"  Failed:                  {fail_count}")

    if fallback_companies:
        print(f"\n  Google Search fallback used for {len(fallback_companies)} companies (flagged with * in CSV):")
        for fc_name in fallback_companies:
            print(f"    • {fc_name}")

    if failed_companies:
        print(f"\n  The following {len(failed_companies)} companies could NOT be classified:")
        for fname, fdomain, freason in failed_companies:
            print(f"    ✗ {fname} ({fdomain}) — {freason}")
    else:
        print("\n  All companies were classified successfully.")

    # --- Preview table of first 15 classified companies ----------------
    classified_df = _read_classified_csv()
    if not classified_df.empty:
        preview = classified_df.head(15)

        table_data = []
        for _, r in preview.iterrows():
            table_data.append([
                r["company_name"][:30],
                str(r["company_domain"])[:25],
                r["firm_type"],
                str(r["firm_reason"])[:40],
                r["icp_fit"],
                str(r["icp_reason"])[:40],
                f"{int(r['firm_user_estimate']):,}",
                str(r["firm_user_estimate_reason"])[:40],
            ])

        headers = [
            "Company", "Domain", "Firm", "Firm Reason",
            "ICP Fit", "ICP Reason", "Users", "Users Reason",
        ]

        print("\n" + "-" * 60)
        print("  First 15 Classified Companies")
        print("-" * 60)
        print(tabulate(table_data, headers=headers, tablefmt="grid"))
    else:
        print("\n  No classifications recorded.")

    print()


if __name__ == "__main__":
    main()
