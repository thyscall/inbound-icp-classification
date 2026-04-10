"""One-time script to verify both API keys are functional."""

import os
from dotenv import load_dotenv
from firecrawl import FirecrawlApp
from google import genai

load_dotenv()

print("=" * 50)
print("API VERIFICATION")
print("=" * 50)

# --- Firecrawl -----------------------------------------------------------
print("\n[1/2] Testing Firecrawl API...")
try:
    fc = FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY"))
    result = fc.scrape("https://example.com", formats=["markdown"])
    text = (result.markdown or "")[:200]
    print(f"  OK — scraped example.com, preview: {text[:80]}...")
except Exception as e:
    print(f"  FAIL — {e}")

# --- Gemini --------------------------------------------------------------
print("\n[2/2] Testing Gemini API...")
try:
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="Reply with exactly: API_OK",
    )
    print(f"  OK — Gemini responded: {resp.text.strip()}")
except Exception as e:
    print(f"  FAIL — {e}")

print("\n" + "=" * 50)
print("Verification complete.")
