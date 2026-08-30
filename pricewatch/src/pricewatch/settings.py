"""Runtime configuration, read once from the environment."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # storage
    pw_db_url: str = "sqlite:////data/pricewatch.db"
    #: Last-resort currency for a page that prints a bare number and says
    #: nothing about which money it is. Not the user's preference — that is
    #: pw_preferred_currency below.
    pw_currency: str = "USD"

    # locale — what the user wants quoted, and which storefronts they buy from.
    # Prices are never silently converted: a preference makes the engine reach
    # for the storefront that natively bills in this currency, and label
    # everything else as foreign. Blank = no preference (US/USD-leaning).
    pw_preferred_currency: str = ""
    #: ISO-3166 country picking regional storefronts (amazon.ca,
    #: ca.store.bambulab.com, AliExpress's locale cookie). Blank → derived from
    #: pw_preferred_currency.
    pw_region: str = ""

    # fetching
    pw_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    pw_request_timeout: float = 25.0
    pw_per_host_rps: float = 0.4
    pw_browser_enabled: bool = True
    pw_http_proxy: str = ""
    # TLS/HTTP2 fingerprint to present on the plain-HTTP path. Modern retail bot
    # walls (Cloudflare, Akamai, Amazon) fingerprint the ClientHello, not just
    # headers, so a real-browser fingerprint via curl_cffi clears most of them
    # without paying for a headless browser. Any curl_cffi target works here
    # (e.g. chrome, chrome131, chrome124, safari17_0); empty disables it and
    # falls back to plain httpx.
    pw_impersonate: str = "chrome"
    # Where to remember which fetch strategy each host actually needs, so repeat
    # lookups skip straight to what works instead of re-climbing the ladder.
    # Empty → derive a path next to the SQLite DB; set to "off" to disable.
    pw_playbook_path: str = ""

    # MCP transport — the server enforces DNS-rebinding protection, so every
    # hostname the agent may reach it by has to be listed.
    pw_mcp_allowed_hosts: str = "pricewatch:8000,pricewatch,localhost:8000,127.0.0.1:8000"

    # scheduling
    pw_check_cron: str = "*/30 * * * *"

    # community intel: comma-separated default subreddits for community_pulse
    # (blank = the built-in 3D-printing/deal set)
    pw_community_subreddits: str = ""

    # notifications
    discord_webhook_url: str = ""
    # ntfy.sh push — zero-account: set an unguessable topic, subscribe to it
    # in the ntfy app. NTFY_SERVER supports self-hosted instances.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    signal_api_url: str = ""
    signal_from: str = ""
    signal_to: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    twilio_whatsapp_to: str = ""
    callmebot_phone: str = ""
    callmebot_apikey: str = ""

    # optional retail APIs
    bestbuy_api_key: str = ""
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    keepa_api_key: str = ""
    serpapi_key: str = ""


settings = Settings()
