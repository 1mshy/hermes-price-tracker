#!/usr/bin/env python3
"""Silent price watchdog template (no_agent cron job).

Copy to ~/.hermes/scripts/check_<item>.py, set ASIN/THRESHOLD/URLS.
Prints ONE alert line when price <= THRESHOLD; prints nothing (and exits 0)
when above threshold or when the page can't be read (bot challenge) --
so the cron job never spams the user.

ALERT BRANCH MUST BE VERIFIED (e.g. by string-replacing priceAmount in a
fetched page and running parse_price on it) before the job is declared live.
"""
import re
import subprocess
import sys

ASIN = "B0FWJBDX6Z"   # TODO: set
THRESHOLD = 50.0      # TODO: set, in the currency the page displays (check it!)

URLS = [
    # TODO: set. Same ASIN usually resolves on both storefronts; keep both as
    # redundancy if one region serves a bot challenge.
    ("amazon.ca", "https://www.amazon.ca/dp/" + ASIN),
    ("amazon.com", "https://www.amazon.com/dp/" + ASIN),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch(url):
    try:
        out = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", "30",
             "-A", UA, "-H", "Accept-Language: en-CA,en;q=0.9", url],
            capture_output=True, text=True, timeout=45,
        )
        return out.stdout
    except Exception:
        return ""


def is_challenge(html_text):
    head = html_text[:30000]
    return (len(html_text) < 50000
            or "Enter the characters you see" in html_text
            or ("Captcha" in head and "Robot Check" in head))


def parse_price(html_text):
    """Return (price_float, currency_symbol) or None."""
    # 1) embedded JSON price block -- best signal
    m = re.search(r'"priceAmount":([\d.]+),\s*"currencySymbol":"([^"]+)"',
                  html_text)
    if m:
        return float(m.group(1)), m.group(2)
    # 2) a-offscreen price inside the buy-box div
    i = html_text.find('id="corePriceDisplay')
    if i > 0:
        seg = html_text[i:i + 6000]
        m2 = re.search(r'class="a-offscreen">\s*(?:C\$|\$)([\d,]+(?:\.\d+)?)',
                       seg)
        if m2:
            return float(m2.group(1).replace(",", "")), "$"
    # 3) last resort: first a-offscreen price on the page (may be a related
    #    product -- treat a hit here with suspicion and verify the title)
    m3 = re.search(r'class="a-offscreen">\s*(?:C\$|\$)([\d,]+(?:\.\d+)?)',
                   html_text)
    if m3:
        return float(m3.group(1).replace(",", "")), "$"
    return None


def main():
    for name, url in URLS:
        html_text = fetch(url)
        if not html_text or is_challenge(html_text):
            continue  # silent on read failure
        parsed = parse_price(html_text)
        if not parsed:
            continue
        price, symbol = parsed
        if price <= THRESHOLD:
            print(f"PRICE ALERT: {ASIN} is now {symbol}{price:.2f} on {name} "
                  f"(target <= {symbol}{THRESHOLD:.0f}).\n{url}")
            sys.exit(0)
    sys.exit(0)  # above threshold -- stay silent


if __name__ == "__main__":
    main()
