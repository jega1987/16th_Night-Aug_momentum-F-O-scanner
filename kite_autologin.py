"""
Automated Kite login via TOTP.

Kite access tokens expire every morning and are normally issued through a
browser redirect. This module reproduces that browser flow with direct HTTP
calls so the token can be refreshed on a schedule, with no human in the loop.

    GET  /connect/login?v=3&api_key=...   -> session cookies + redirect URL
    POST /api/login   (user_id, password) -> request_id
    POST /api/twofa   (user_id, request_id, twofa_value=TOTP) -> authenticated
    GET  <login_url>&skip_session=true    -> request_token in the redirect
    generate_session(request_token, secret) -> access_token   (kiteconnect lib)

IMPORTANT - TERMS OF SERVICE
    Zerodha treats scripted login as a violation of the Kite Connect API terms.
    It is widely used and unofficially tolerated, but the risk is the account
    holder's: API access can be flagged or suspended, and the undocumented
    endpoints below can change without notice. This module is enabled only when
    KITE_AUTO_LOGIN=true is explicitly set. The manual /kite/login route remains
    as a break-glass fallback for the mornings this flow breaks.

The four secrets this needs (Kite user id, password, TOTP seed, API secret)
live only in Railway environment variables, never in the repo.
"""
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from config import cfg

logger = logging.getLogger(__name__)

LOGIN_REDIRECT = "https://kite.trade/connect/login?v=3&api_key={api_key}"
API_LOGIN = "https://kite.zerodha.com/api/login"
API_TWOFA = "https://kite.zerodha.com/api/twofa"


class AutoLoginError(RuntimeError):
    """Auto-login failed. The step attribute says where, for a clear log."""
    def __init__(self, step: str, message: str):
        self.step = step
        super().__init__(f"[{step}] {message}")


def _totp_now(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(secret.replace(" ", "")).now()


def fetch_request_token() -> str:
    """Run the browser flow headlessly and return a fresh request_token."""
    import httpx

    missing = [n for n, v in (
        ("KITE_USER_ID", cfg.KITE_USER_ID),
        ("KITE_PASSWORD", cfg.KITE_PASSWORD),
        ("KITE_TOTP_SECRET", cfg.KITE_TOTP_SECRET),
        ("KITE_API_KEY", cfg.KITE_API_KEY),
    ) if not v]
    if missing:
        raise AutoLoginError("config", "missing: " + ", ".join(missing))

    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        # 1. Seed the session and capture where the redirect lands.
        try:
            first = client.get(LOGIN_REDIRECT.format(api_key=cfg.KITE_API_KEY))
            login_url = str(first.url)
        except Exception as exc:
            raise AutoLoginError("redirect", str(exc))

        # 2. Username + password -> request_id
        try:
            resp = client.post(API_LOGIN, data={
                "user_id": cfg.KITE_USER_ID,
                "password": cfg.KITE_PASSWORD,
            })
            body = resp.json()
        except Exception as exc:
            raise AutoLoginError("login", str(exc))
        if body.get("status") == "error":
            raise AutoLoginError("login", body.get("message", "login rejected - check user id / password"))
        request_id = (body.get("data") or {}).get("request_id")
        if not request_id:
            raise AutoLoginError("login", f"no request_id in response: {str(body)[:120]}")

        # 3. TOTP second factor
        try:
            twofa = client.post(API_TWOFA, data={
                "user_id": cfg.KITE_USER_ID,
                "request_id": request_id,
                "twofa_value": _totp_now(cfg.KITE_TOTP_SECRET),
                "twofa_type": "totp",
                "skip_session": "true",
            })
            tbody = twofa.json()
        except Exception as exc:
            raise AutoLoginError("twofa", str(exc))
        if tbody.get("status") == "error":
            raise AutoLoginError("twofa", tbody.get("message",
                                 "TOTP rejected - check the seed and the server clock"))

        # 4. Re-hit the login URL; the request_token comes back in the redirect.
        token = _extract_token(client, login_url)
        if not token:
            raise AutoLoginError("request_token",
                                 "flow completed but no request_token in the redirect - "
                                 "Kite may have changed the login flow")
        return token


def _extract_token(client, login_url: str) -> Optional[str]:
    """The token surfaces either in the final URL or in a redirect exception."""
    import httpx

    url = login_url + ("&" if "?" in login_url else "?") + "skip_session=true"
    try:
        # Do NOT follow this redirect. The request_token is in the location
        # header of the 302 to the app's registered redirect URL; following it
        # would navigate away to that URL (which may 404 or not echo the token)
        # and lose it.
        resp = client.get(url, follow_redirects=False)
        found = _token_from_url(resp.headers.get("location", ""))
        if found:
            return found
        found = _token_from_url(str(resp.url))
        if found:
            return found
        for record in resp.history:
            found = _token_from_url(record.headers.get("location", ""))
            if found:
                return found
    except httpx.HTTPError as exc:
        # Some flows raise on the final redirect to a custom scheme; the token
        # is in the exception's URL.
        found = _token_from_url(str(getattr(exc, "request", "")) + " " + str(exc))
        if found:
            return found
    return None


def _token_from_url(url: str) -> Optional[str]:
    if not url or "request_token" not in url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        if qs.get("request_token"):
            return qs["request_token"][0]
    except Exception:
        pass
    match = re.search(r"request_token=([A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def auto_login() -> str:
    """
    Full refresh: browser flow -> request_token -> access_token, stored in the
    database by KiteFeed. Returns the access token. Raises AutoLoginError with a
    specific step on failure.
    """
    from feed_kite import KiteFeed

    logger.info("[AutoLogin] Starting TOTP login for %s", cfg.KITE_USER_ID)
    request_token = fetch_request_token()
    logger.info("[AutoLogin] request_token obtained, exchanging for access_token")
    data = KiteFeed.exchange_request_token(request_token)
    who = data.get("user_name") or data.get("user_id") or cfg.KITE_USER_ID
    logger.info("[AutoLogin] Success - signed in as %s", who)
    return data.get("access_token", "")
