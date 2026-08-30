"""The store catalog: which storefront runs on what, and how to search it.

`kind` selects the extraction strategy:
  shopify     — /products/<handle>.js + /search/suggest.json   (exact, cheap)
  woo         — /wp-json/wc/store/v1                            (exact, cheap)
  structured  — schema.org JSON-LD / OpenGraph over plain HTTP
  browser     — same, but requires Chromium (bot-walled or JS-only)
  api         — official retailer API adapter
"""
from __future__ import annotations

TECH = "tech"
PRINT3D = "3dprinting"
PARTS = "components"

CATALOG: list[dict] = [
    # ── big-box / general tech ───────────────────────────────────────────
    {"key": "amazon",       "label": "Amazon",            "kind": "api",        "tags": [TECH, PRINT3D], "domains": ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de"),
     "probe": "https://www.amazon.com/dp/B0FWJBDX6Z",
     "note": "Keyword search and most PDPs read over HTTP with a browser TLS fingerprint, no key needed; set KEEPA_API_KEY for hardened pages and bulk reliability."},
    {"key": "bestbuy",      "label": "Best Buy",          "kind": "api",        "tags": [TECH],          "domains": ("bestbuy.com",),
     "probe": "https://www.bestbuy.com/site/6535723.p",
     "note": "Prefer BESTBUY_API_KEY (free at developer.bestbuy.com); without it the HTML is JS-rendered."},
    {"key": "walmart",      "label": "Walmart",           "kind": "api",        "tags": [TECH, PRINT3D], "domains": ("walmart.com",),
     "probe": "https://www.walmart.com/ip/17835006350",
     "note": "PDPs read over HTTP via the browser fingerprint; a residential proxy (PW_HTTP_PROXY) helps under heavy load."},
    {"key": "ebay",         "label": "eBay",              "kind": "api",        "tags": [TECH, PRINT3D], "domains": ("ebay.com",),
     "note": "Reads keylessly via fingerprinted HTTP + JSON-LD (items) and HTML search; EBAY_APP_ID + EBAY_CERT_ID (free at developer.ebay.com) remain the most reliable path."},
    {"key": "aliexpress",   "label": "AliExpress",        "kind": "api",        "tags": [TECH, PRINT3D], "domains": ("aliexpress.com", "aliexpress.us"),
     "note": "Search-only marketplace: prices come from the search page, in the currency PW_PREFERRED_CURRENCY pins the locale cookie to (USD by default); product pages block automated reads, so listings cannot be tracked."},
    {"key": "newegg",       "label": "Newegg",            "kind": "selector",   "tags": [TECH],          "domains": ("newegg.com",),
     "probe": "https://www.newegg.com/Creality-K2-Pro-Combo/p/N82E16828285069"},
    {"key": "microcenter",  "label": "Micro Center",      "kind": "browser",    "tags": [TECH, PRINT3D], "domains": ("microcenter.com",),
     "probe": "https://www.microcenter.com/product/676670/",
     "note": "Cloudflare wall keys on IP reputation; needs a residential proxy via PW_HTTP_PROXY."},
    {"key": "bhphoto",      "label": "B&H Photo",         "kind": "structured", "tags": [TECH],          "domains": ("bhphotovideo.com",),
     "note": "Cloudflare-walled; cleared by the impersonated HTTP fetch, then schema.org JSON-LD."},
    {"key": "adorama",      "label": "Adorama",           "kind": "browser",    "tags": [TECH],          "domains": ("adorama.com",),
     "probe": "https://www.adorama.com/apix1c.html",
     "note": "DataDome wall keys on IP reputation; needs a residential proxy via PW_HTTP_PROXY."},
    {"key": "target",       "label": "Target",            "kind": "browser",    "tags": [TECH],          "domains": ("target.com",),
     "probe": "https://www.target.com/p/-/A-88378272",
     "note": "Prices are client-rendered (Redsky API); needs a browser or a residential proxy via PW_HTTP_PROXY."},

    # ── 3D printing: OEM stores ──────────────────────────────────────────
    {"key": "bambulab",     "label": "Bambu Lab",         "kind": "structured",    "tags": [PRINT3D], "domains": ("us.store.bambulab.com", "store.bambulab.com", "ca.store.bambulab.com", "eu.store.bambulab.com", "bambulab.com")},
    {"key": "prusa",        "label": "Prusa Research",    "kind": "structured", "tags": [PRINT3D], "domains": ("prusa3d.com",)},
    {"key": "elegoo",       "label": "Elegoo",            "kind": "shopify",    "tags": [PRINT3D], "domains": ("us.elegoo.com", "elegoo.com")},
    {"key": "anycubic",     "label": "Anycubic",          "kind": "shopify",    "tags": [PRINT3D], "domains": ("store.anycubic.com", "anycubic.com")},
    {"key": "creality",     "label": "Creality",          "kind": "structured",    "tags": [PRINT3D], "domains": ("store.creality.com", "creality.com")},
    {"key": "qidi",         "label": "QIDI Tech",         "kind": "shopify",    "tags": [PRINT3D], "domains": ("qidi3d.com",)},
    {"key": "sovol",        "label": "Sovol 3D",          "kind": "shopify",    "tags": [PRINT3D], "domains": ("sovol3d.com",)},

    # ── 3D printing: retailers & parts ───────────────────────────────────
    {"key": "matterhackers","label": "MatterHackers",     "kind": "browser",    "tags": [PRINT3D], "domains": ("matterhackers.com",)},
    {"key": "printedsolid", "label": "Printed Solid",     "kind": "shopify",    "tags": [PRINT3D], "domains": ("printedsolid.com",)},
    {"key": "e3d",          "label": "E3D Online",        "kind": "shopify",    "tags": [PRINT3D], "domains": ("e3d-online.com",)},
    {"key": "slice",        "label": "Slice Engineering", "kind": "shopify",    "tags": [PRINT3D], "domains": ("sliceengineering.com",)},
    {"key": "microswiss",   "label": "Micro Swiss",       "kind": "shopify",    "tags": [PRINT3D], "domains": ("store.micro-swiss.com", "micro-swiss.com")},
    {"key": "west3d",       "label": "West3D",            "kind": "shopify",    "tags": [PRINT3D], "domains": ("west3d.com",)},
    {"key": "fabreeko",     "label": "Fabreeko",          "kind": "shopify",    "tags": [PRINT3D], "domains": ("fabreeko.com",)},
    {"key": "kb3d",         "label": "KB-3D",             "kind": "structured",    "tags": [PRINT3D], "domains": ("kb-3d.com",),
     "note": "Custom storefront with no structured price data."},
    {"key": "filastruder",  "label": "Filastruder",       "kind": "shopify",    "tags": [PRINT3D], "domains": ("filastruder.com",)},
    {"key": "gulfcoast",    "label": "Gulf Coast Robotics","kind": "shopify",   "tags": [PRINT3D], "domains": ("gulfcoast-robotics.com",),
     "note": "Site frequently unreachable from outside the US."},
    {"key": "th3d",         "label": "TH3D Studio",       "kind": "browser",    "tags": [PRINT3D], "domains": ("th3dstudio.com",)},
    {"key": "ldo",          "label": "LDO Motors",        "kind": "structured", "tags": [PRINT3D], "domains": ("ldomotors.com",),
     "note": "Custom storefront with no structured price data."},
    {"key": "siboor",       "label": "Siboor",            "kind": "browser",    "tags": [PRINT3D], "domains": ("siboor.com",),
     "note": "Cloudflare-protected; needs a residential proxy via PW_HTTP_PROXY."},
    {"key": "3djake",       "label": "3DJake",            "kind": "selector",   "tags": [PRINT3D], "domains": ("3djake.com", "3djake.us"),
     "probe": "https://www.3djake.com/3djake/ecopla"},

    # ── filament ─────────────────────────────────────────────────────────
    {"key": "polymaker",    "label": "Polymaker",         "kind": "shopify",    "tags": [PRINT3D], "domains": ("shop.polymaker.com", "us.polymaker.com", "polymaker.com")},
    {"key": "protopasta",   "label": "Proto-pasta",       "kind": "shopify",    "tags": [PRINT3D], "domains": ("proto-pasta.com",)},
    {"key": "atomic",       "label": "Atomic Filament",   "kind": "shopify",    "tags": [PRINT3D], "domains": ("atomicfilament.com",)},
    {"key": "overture",     "label": "Overture",          "kind": "shopify",    "tags": [PRINT3D], "domains": ("overture3d.com",)},
    {"key": "fillamentum",  "label": "Fillamentum",       "kind": "shopify",    "tags": [PRINT3D], "domains": ("shop.fillamentum.com", "fillamentum.com")},
    {"key": "sunlu",        "label": "SUNLU",             "kind": "shopify",    "tags": [PRINT3D], "domains": ("www.sunlu.com", "sunlu.com"),
     "note": "Storefront exposes no machine-readable price; URL tracking unreliable."},

    # ── electronics / components ─────────────────────────────────────────
    {"key": "digikey",      "label": "DigiKey",           "kind": "structured", "tags": [PARTS], "domains": ("digikey.com",),
     "probe": "https://www.digikey.com/en/products/detail/analog-devices-inc/TMC2209-LA-T/9764111",
     "note": "Cloudflare-walled; cleared by the impersonated HTTP fetch, then schema.org JSON-LD."},
    {"key": "mouser",       "label": "Mouser",            "kind": "browser",    "tags": [PARTS], "domains": ("mouser.com",),
     "probe": "https://www.mouser.com/ProductDetail/Analog-Devices/TMC2209-LA-T",
     "note": "Akamai wall keys on IP reputation; needs a residential proxy via PW_HTTP_PROXY."},
]

BY_KEY = {entry["key"]: entry for entry in CATALOG}


def keys_for_tag(tag: str) -> list[str]:
    return [e["key"] for e in CATALOG if tag in e["tags"]]
