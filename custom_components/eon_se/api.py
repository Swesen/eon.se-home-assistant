"""
E.ON Sweden API client.

=== Authentication flow (fully discovered via DevTools, 2026-07-27) ===

E.ON uses Curity HAAPI (Hypermedia Authentication API) with DPoP
(Demonstrating Proof of Possession – RFC 9449) for all auth requests.

Steps:
  1. GET  /bin/eon-se/neon/secure-configuration
         → Returns OAuth config JSON (client_id, scopes, redirect_uri, …)

  2. Generate a per-session RSA-2048 key pair.
     Every auth request to api.apps.eon.se must carry an
     "Authorization: DPoP <jwt>" header where the JWT is:
       header: {"alg":"PS256", "jwk": <public key as JWK>, "typ":"dpop+jwt"}
       payload: {"jti": <uuid>, "htm": <HTTP method>, "htu": <URL>,
                 "iat": <now>, "nonce": <server nonce>, "ath": <token hash>}
     signed with the private key.
     NOTE: HA has no built-in JWK/DPoP support, so we use PyJWT + cryptography.

  3. GET  https://api.apps.eon.se/authn/authenticate
         Params: acr, serviceProviderId=oauth-test, forceAuthN=true,
                 resumePath=/neo/oauth/v2/authorization, state=<random>,
                 client_id=eon-web-light-haapi
         Response: application/vnd.auth+json  (HAAPI response model)
         Sets: session-id header, _oq cookie

  4. GET  https://api.apps.eon.se/authn/authenticate/isu-sap-authenticator-recaptcha
         (URL returned in the previous response's "links")
         Response: HAAPI form model describing the username/password fields.
         NOTE: MTCaptcha token is requested here but we skip it – see below.

  5. POST https://api.apps.eon.se/authn/authenticate/isu-sap-authenticator-recaptcha
         Content-Type: application/x-www-form-urlencoded   (~60 bytes)
         Body: personnummer=<YYYYMMDDXXXX>&password=<...>
               (field names confirmed as "personnummer" and "password")
         Response: HAAPI polling/redirect response

  6. POST https://api.apps.eon.se/neo/oauth/v2/authorization
         Content-Type: application/x-www-form-urlencoded  (~79 bytes)
         (parameters from the previous HAAPI response)

  7. POST https://api.apps.eon.se/neo/oauth/v2/token
         Content-Type: application/x-www-form-urlencoded  (~217 bytes)
         grant_type=authorization_code&code=<...>&redirect_uri=<...>&
         code_verifier=<...>&client_id=eon-web-light-haapi
         Response: JSON with access_token, id_token, refresh_token  (2858 bytes)

  The access_token (format "_0XBPWQQ_<uuid>") is then used as Bearer on
  eonmycoapirun.azurewebsites.net.

=== MTCaptcha note ===
  The browser validates a MTCaptcha before step 5, but the CAPTCHA result is
  only checked by a separate endpoint; the authenticator endpoint itself does
  not require a valid CAPTCHA token in our tests.  If E.ON hardens this in
  future, a CAPTCHA service would be required.

=== DPoP note ===
  DPoP requires the `cryptography` package (already a HA core dependency).
  We generate a fresh RSA key pair per session and include a valid DPoP
  proof in every request to api.apps.eon.se.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import secrets
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp

from .const import (
    AUTH_SERVER,
    BASE_URL,
    CONSUMPTION_API_URL,
    CSV_DATE_FORMAT,
    CSV_DECIMAL_SEPARATOR,
    CSV_DELIMITER,
    EXPORT_LOG_URL,
    FACILITIES_API_URL,
    HAAPI_AUTHN_FORM_URL,
    HAAPI_AUTHN_START_URL,
    HAAPI_AUTHN_SUBMIT_URL,
    HAAPI_AUTHZ_URL,
    INTERVAL_DIRECTION,
    INTERVAL_END,
    INTERVAL_KWH,
    INTERVAL_QUALITY,
    INTERVAL_START,
    OAUTH_ACR,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    RESOLUTION_15MIN,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

# Browser-like headers (match what Chrome sends – important for Cloudflare)
_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_API_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

_HAAPI_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "application/vnd.auth+json",
    "Origin": "https://api.apps.eon.se",
    "Referer": "https://api.apps.eon.se/neo/oauth/v2/token/cat?client_id=eon-web-light-haapi",
}


class EonAuthError(Exception):
    """Raised when authentication fails."""


class EonApiError(Exception):
    """Raised when an API call fails."""


# ---------------------------------------------------------------------------
# DPoP helpers
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _generate_dpop_keypair() -> tuple[Any, dict]:
    """Generate an RSA-2048 key pair and return (private_key, public_jwk_dict)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.asymmetric.rsa import (
            RSAPrivateKey,
            RSAPublicNumbers,
        )
    except ImportError as err:
        raise EonApiError(
            "The 'cryptography' package is required for E.ON auth. "
            "It is bundled with Home Assistant core."
        ) from err

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = private_key.public_key().public_numbers()

    def _int_to_b64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return _b64url(n.to_bytes(length, "big"))

    jwk = {
        "alg": "PS256",
        "e": _int_to_b64url(pub.e),
        "ext": True,
        "key_ops": ["verify"],
        "kty": "RSA",
        "n": _int_to_b64url(pub.n),
    }
    return private_key, jwk


def _dpop_proof(
    private_key: Any,
    public_jwk: dict,
    method: str,
    url: str,
    nonce: str | None = None,
    access_token: str | None = None,
) -> str:
    """Build a DPoP proof JWT (PS256) for a given HTTP method + URL."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as err:
        raise EonApiError("cryptography package missing") from err

    header = {"kid": "dzs-tG6Yq61KaWtNfeEG", "alg": "HS256"}  # alg used by eon.se client
    # Use the same header format as the browser
    header = {
        "jwk": public_jwk,
        "alg": "PS256",
        "typ": "dpop+jwt",
    }
    payload: dict[str, Any] = {
        "jti": secrets.token_urlsafe(32),
        "htm": method.upper(),
        "htu": url,
        "iat": int(time.time()),
    }
    if nonce:
        payload["nonce"] = nonce
    if access_token:
        # ath = BASE64URL(SHA256(ASCII(access_token)))
        ath = _b64url(hashlib.sha256(access_token.encode()).digest())
        payload["ath"] = ath

    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    signature = private_key.sign(
        signing_input,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class EonApiClient:
    """Async HTTP client for the E.ON Sweden backend API."""

    def __init__(self, personnummer: str, password: str, session: aiohttp.ClientSession, browser_username_cookie: str | None = None) -> None:
        self._personnummer = personnummer
        self._password = password
        self._session = session
        self._bearer_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._dpop_private_key: Any = None
        self._dpop_public_jwk: dict = {}
        self._haapi_nonce: str | None = None
        self._browser_username_cookie = browser_username_cookie
        self._on_token_refresh: Any = None  # optional async callback set by __init__.py

    # ------------------------------------------------------------------
    # Authentication  (Curity HAAPI + DPoP + PKCE)
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Full authentication flow to obtain a Bearer token.

        Uses a headless Chromium browser (via Playwright) to complete the HAAPI
        authorization flow — the browser runs the Curity WASM attestation natively,
        fills in credentials via the HAAPI hypermedia API, and returns the auth code.
        """
        self._dpop_private_key, self._dpop_public_jwk = _generate_dpop_keypair()
        auth_result = await self._browser_haapi_auth()
        token_response = await self._exchange_code(
            auth_result["code"], auth_result["code_verifier"]
        )
        self._store_token_response(token_response)
        _LOGGER.debug("Authenticated via browser, token prefix: %s\u2026", (self._bearer_token or "")[:12])

    def _store_token_response(self, resp: dict) -> None:
        """Persist access_token, refresh_token, and expiry from a token endpoint response."""
        import time as _time
        access_token = resp.get("access_token") or resp.get("token")
        if not access_token:
            raise EonAuthError("No access_token in token response")
        self._bearer_token = access_token
        self._refresh_token = resp.get("refresh_token")
        expires_in = int(resp.get("expires_in") or 900)
        self._token_expires_at = _time.monotonic() + expires_in - 60
        _LOGGER.debug(
            "Token stored: expires_in=%ss, has_refresh=%s",
            expires_in, self._refresh_token is not None,
        )
        # Notify the HA integration so it can persist the new tokens
        if self._on_token_refresh is not None:
            import asyncio
            try:
                asyncio.get_event_loop().create_task(self._on_token_refresh())
            except Exception:  # noqa: BLE001
                pass

    async def ensure_token(self) -> None:
        """Ensure the access token is valid, using refresh_token if near expiry.

        Call this in the coordinator's update loop instead of calling authenticate()
        each time. If refresh fails, falls back to a full browser re-auth.
        """
        import time as _time
        if self._bearer_token and _time.monotonic() < self._token_expires_at:
            return  # Still valid
        if self._refresh_token:
            _LOGGER.debug("Access token near-expiry, refreshing via refresh_token...")
            try:
                self._store_token_response(await self._do_refresh())
                _LOGGER.debug("Token refreshed, prefix: %s\u2026", (self._bearer_token or "")[:12])
                return
            except EonApiError as err:
                _LOGGER.warning("Refresh token failed (%s); will re-authenticate via browser", err)
                self._refresh_token = None
                self._bearer_token = None
        _LOGGER.debug("No valid token; doing full browser re-auth...")
        await self.authenticate()

    async def _do_refresh(self) -> dict:
        """POST refresh_token grant to obtain a new access token without a browser."""
        body = urlencode({
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        })
        try:
            async with self._session.post(
                TOKEN_URL,
                data=body,
                headers={
                    **_COMMON_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "*/*",
                    "Origin": BASE_URL,
                },
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EonApiError(f"Token refresh failed: {err}") from err

    async def _browser_haapi_auth(self) -> dict:
        """Launch a headless browser, drive the HAAPI flow, and return {code, code_verifier}."""
        import json as _json
        import asyncio.subprocess as asp
        import sys
        import os

        input_data = _json.dumps({
            "username": self._personnummer,
            "password": self._password,
        })

        script_path = os.path.join(os.path.dirname(__file__), "haapi_browser_auth.py")
        python_exe = sys.executable

        _LOGGER.debug("Launching headless browser for HAAPI auth...")
        proc = await asp.create_subprocess_exec(
            python_exe, script_path,
            stdin=asp.PIPE,
            stdout=asp.PIPE,
            stderr=asp.PIPE,
        )
        stdout, stderr = await proc.communicate(input=input_data.encode())

        if stderr:
            _LOGGER.debug("Browser auth log:\n%s", stderr.decode(errors="replace")[-3000:])

        if proc.returncode != 0:
            raise EonApiError(f"Browser auth subprocess failed (exit {proc.returncode})")

        output = stdout.decode().strip()
        if not output:
            raise EonApiError("Browser auth subprocess produced no output")

        try:
            result = _json.loads(output)
        except Exception as e:
            raise EonApiError(f"Browser auth returned invalid JSON: {output[:300]}") from e

        if "error" in result:
            raise EonAuthError(f"Browser auth failed: {result['error']}")

        if "code" not in result or "code_verifier" not in result:
            raise EonAuthError(f"Browser auth missing code/verifier: {result}")

        _LOGGER.debug("Browser HAAPI auth complete, got auth code")
        return result

    async def _haapi_start(self, state: str, code_challenge: str) -> str:
        """Start the OAuth/HAAPI flow and return the session_id.

        Confirmed flow (DevTools 2026-07-27):
          Step A: GET /neo/oauth/v2/authorization?client_id=...&scope=...&
                      response_type=code&code_challenge_method=S256&
                      code_challenge=<pkce>&redirect_uri=...&acr=...&prompt=login
                  → 200 application/vnd.auth+json
                  → Set-Session-Id header
                  → location header pointing to /authn/authenticate?...

          Step B: GET <location>  (i.e. /authn/authenticate?acr=...&state=...)
                  → 200 application/vnd.auth+json
                  → updated Set-Session-Id
        """
        cat_token = await self._get_cat_token()

        # --- Step A: GET /neo/oauth/v2/authorization ---
        authz_params = {
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPE,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "acr": OAUTH_ACR,
            "prompt": "login",
        }
        dpop_a = _dpop_proof(
            self._dpop_private_key, self._dpop_public_jwk,
            "GET", HAAPI_AUTHZ_URL,
            nonce=self._haapi_nonce,
            access_token=cat_token if cat_token else None,
        )
        authz_headers: dict = {
            **_HAAPI_HEADERS,
            "DPoP": dpop_a,
            "Origin": "https://api.apps.eon.se",
        }
        if cat_token:
            # Authorization: DPoP <cat_token> is required; ath in DPoP proof binds them
            authz_headers["Authorization"] = f"DPoP {cat_token}"
            authz_headers["Referer"] = (
                f"https://api.apps.eon.se/neo/oauth/v2/token/cat?client_id={OAUTH_CLIENT_ID}"
            )
        try:
            async with self._session.get(
                HAAPI_AUTHZ_URL,
                params=authz_params,
                headers=authz_headers,
                allow_redirects=False,
            ) as resp:
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                session_id = resp.headers.get("Set-Session-Id", "")
                authn_location = resp.headers.get("location", "")
                _LOGGER.debug(
                    "Authz start: status=%s session_id=%s… location=%s",
                    resp.status, session_id[:20], authn_location[:60] if authn_location else "",
                )
                if resp.status == 401:
                    body = await resp.text()
                    _LOGGER.debug("Authz 401 body: %s", body[:500])
                resp.raise_for_status()
        except aiohttp.ClientError as err:
            raise EonApiError(f"HAAPI authz start failed: {err}") from err

        # --- Step B: GET /authn/authenticate?... (follow the location) ---
        if authn_location:
            dpop_b = _dpop_proof(
                self._dpop_private_key, self._dpop_public_jwk,
                "GET", authn_location.split("?")[0],
                nonce=self._haapi_nonce,
                access_token=cat_token if cat_token else None,
            )
            authn_headers: dict = {
                **_HAAPI_HEADERS,
                "DPoP": dpop_b,
                "session-id": session_id,
                "Origin": "https://api.apps.eon.se",
            }
            if cat_token:
                authn_headers["Authorization"] = f"DPoP {cat_token}"
                authn_headers["Referer"] = (
                    f"https://api.apps.eon.se/neo/oauth/v2/token/cat?client_id={OAUTH_CLIENT_ID}"
                )
            try:
                async with self._session.get(
                    authn_location,
                    headers=authn_headers,
                    allow_redirects=False,
                ) as resp:
                    self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                    session_id = resp.headers.get("Set-Session-Id", session_id)
                    resp.raise_for_status()
            except aiohttp.ClientError as err:
                raise EonApiError(f"HAAPI authn redirect failed: {err}") from err

        return session_id

    async def _seed_cloudflare_cookies(self) -> None:
        """Visit eon.se and the HAAPI auth endpoint to obtain Cloudflare session cookies.

        Cloudflare sets _cfuvid / __cf_bm on first contact per-domain.
        Without them, subsequent requests to api.apps.eon.se get an HTML 401.
        """
        # Seed www.eon.se cookies
        for url in [
            BASE_URL,
            # Hit the HAAPI authn endpoint directly so Cloudflare issues __cf_bm for api.apps.eon.se
            f"{AUTH_SERVER}/authn/authenticate",
        ]:
            try:
                async with self._session.get(
                    url,
                    headers={**_COMMON_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
                    allow_redirects=True,
                ) as resp:
                    cookies = {c.key: c.value for c in resp.cookies.values()}
                    _LOGGER.debug("Seeded cookies from %s status=%s cookies=%s", url, resp.status, list(cookies.keys()))
            except aiohttp.ClientError as exc:
                _LOGGER.debug("Cookie seed request to %s failed (non-fatal): %s", url, exc)

    async def _get_cat_token(self) -> str:
        """Fetch the CAT token. Returns empty string if browser cookies are available (they bypass CAT).

        Step 1: GET /token/cat → challenge blob
        Step 2: POST /token/cat with challenge_response
        Returns the HS256-signed CAT JWT for use as 'Authorization: DPoP <cat>'.
        """
        if self._browser_username_cookie:
            _LOGGER.debug("Browser cookie present — skipping CAT attestation flow")
            return ""

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        cat_url = f"https://api.apps.eon.se/neo/oauth/v2/token/cat?client_id={OAUTH_CLIENT_ID}"
        cat_post_url = "https://api.apps.eon.se/neo/oauth/v2/token/cat"

        # --- Step 1: GET the challenge ---
        dpop_get = _dpop_proof(self._dpop_private_key, self._dpop_public_jwk, "GET", cat_url)
        try:
            async with self._session.get(
                cat_url,
                headers={
                    **_COMMON_HEADERS,
                    "Accept": "application/json, text/plain, */*",
                    "DPoP": dpop_get,
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
            ) as resp:
                resp.raise_for_status()
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                body = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EonApiError(f"Failed to get CAT challenge: {err}") from err

        challenge = body.get("challenge", "")
        _LOGGER.debug("CAT challenge obtained, len=%d prefix=%s…", len(challenge), challenge[:20])

        # --- Step 2: Decrypt 'data' with our private key and POST back ---
        # data is uppercase hex of RSA-OAEP-encrypted bytes; we decrypt and return as challenge_response
        padded = challenge + "=" * (-len(challenge) % 4)
        try:
            challenge_obj = json.loads(base64.urlsafe_b64decode(padded))
            data_field = challenge_obj.get("data", "")
            _LOGGER.debug("CAT challenge data_len=%d", len(data_field))
            data_bytes = bytes.fromhex(data_field)
        except Exception as exc:
            raise EonApiError(f"Failed to parse CAT challenge: {exc}") from exc

        try:
            decrypted = self._dpop_private_key.decrypt(
                data_bytes,
                asym_padding.OAEP(
                    mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            challenge_response = _b64url(decrypted)
            _LOGGER.debug("CAT decryption succeeded, plaintext len=%d", len(decrypted))
        except Exception as exc:
            # Try SHA1 OAEP (older RSA-OAEP default)
            try:
                decrypted = self._dpop_private_key.decrypt(
                    data_bytes,
                    asym_padding.OAEP(
                        mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
                        algorithm=hashes.SHA1(),
                        label=None,
                    ),
                )
                challenge_response = _b64url(decrypted)
                _LOGGER.debug("CAT decryption succeeded (SHA1 OAEP), plaintext len=%d", len(decrypted))
            except Exception as exc2:
                raise EonApiError(f"Failed to decrypt CAT challenge data: {exc2}") from exc2

        dpop_post = _dpop_proof(
            self._dpop_private_key, self._dpop_public_jwk, "POST", cat_post_url,
            nonce=self._haapi_nonce,
        )
        try:
            async with self._session.post(
                cat_post_url,
                data=urlencode({
                    "client_id": OAUTH_CLIENT_ID,
                    "challenge": challenge,
                    "challenge_response": challenge_response,
                }),
                headers={
                    **_COMMON_HEADERS,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "DPoP": dpop_post,
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
            ) as resp:
                body_text = await resp.text()
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                _LOGGER.debug("CAT POST status=%s body=%s", resp.status, body_text[:300])
                if resp.status in (200, 201):
                    result = json.loads(body_text)
                    token = (result.get("access_token") or result.get("token")
                             or result.get("cat") or result.get("challenge") or "")
                    if token:
                        _LOGGER.debug("CAT token obtained, prefix=%s…", token[:20])
                        # Synthesize nonce: {cat_iat}#{random}
                        try:
                            cat_iat = json.loads(
                                base64.urlsafe_b64decode(token.split(".")[1] + "==")
                            ).get("iat", int(time.time()))
                        except Exception:
                            cat_iat = int(time.time())
                        self._haapi_nonce = f"{cat_iat}#{secrets.token_urlsafe(32)}"
                        return token
                _LOGGER.warning("CAT POST failed (status=%s), will try challenge directly", resp.status)
                # Last resort: use challenge as the token directly
                try:
                    cat_iat = json.loads(base64.urlsafe_b64decode(challenge.split(".")[1] + "==")).get("iat", int(time.time()))
                except Exception:
                    cat_iat = int(time.time())
                self._haapi_nonce = f"{cat_iat}#{secrets.token_urlsafe(32)}"
                return challenge
        except aiohttp.ClientError as err:
            raise EonApiError(f"Failed to complete CAT exchange: {err}") from err


    async def _haapi_get_form(self) -> None:
        """GET the authenticator form page to seed the DPoP nonce."""
        dpop = _dpop_proof(
            self._dpop_private_key,
            self._dpop_public_jwk,
            "GET",
            HAAPI_AUTHN_FORM_URL,
            nonce=self._haapi_nonce,
        )
        try:
            async with self._session.get(
                HAAPI_AUTHN_FORM_URL,
                headers={**_HAAPI_HEADERS, "DPoP": dpop},
            ) as resp:
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
        except aiohttp.ClientError as err:
            _LOGGER.debug("HAAPI form GET failed (non-fatal): %s", err)

    async def _haapi_submit_credentials(
        self, session_id: str, state: str, code_verifier: str
    ) -> str:
        """POST credentials and walk the HAAPI response chain to get an auth code."""
        dpop = _dpop_proof(
            self._dpop_private_key,
            self._dpop_public_jwk,
            "POST",
            HAAPI_AUTHN_SUBMIT_URL,
            nonce=self._haapi_nonce,
        )
        # Body is ~60 bytes: personnummer + password  (form-urlencoded)
        # TODO: confirm exact field names if these fail
        body = urlencode({
            "personnummer": self._personnummer,
            "password": self._password,
        })
        try:
            async with self._session.post(
                HAAPI_AUTHN_SUBMIT_URL,
                data=body,
                headers={
                    **_HAAPI_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "DPoP": dpop,
                    "session-id": session_id,
                },
            ) as resp:
                if resp.status in (401, 403):
                    raise EonAuthError("Invalid personnummer or password")
                resp.raise_for_status()
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                session_id = resp.headers.get("Set-Session-Id", session_id)
                haapi_resp = await resp.json(content_type=None)
                _LOGGER.debug("HAAPI submit response: %s", haapi_resp)
        except EonAuthError:
            raise
        except aiohttp.ClientError as err:
            raise EonApiError(f"Credential submission failed: {err}") from err

        # The HAAPI response contains a continuation `token` – POST it with the
        # original `state` to the authorization endpoint.
        # Confirmed payload format (79 bytes): token=<...>&state=<...>
        continuation_token = (
            haapi_resp.get("token")
            or haapi_resp.get("continuationToken")
            or (haapi_resp.get("properties") or {}).get("token")
        )
        if not continuation_token:
            raise EonApiError(
                f"No continuation token in HAAPI credential response: {haapi_resp}"
            )

        return await self._haapi_authz(continuation_token, state, session_id, code_verifier)

    async def _haapi_authz(
        self,
        continuation_token: str,
        state: str,
        session_id: str,
        code_verifier: str,
    ) -> str:
        """POST token+state to the OAuth authorization endpoint.

        Confirmed payload (79 bytes):
            token=<continuation_token>&state=<state>

        The response is application/vnd.auth+json (449 bytes) which contains
        either the authorization code directly or a further continuation.
        """
        dpop = _dpop_proof(
            self._dpop_private_key,
            self._dpop_public_jwk,
            "POST",
            HAAPI_AUTHZ_URL,
            nonce=self._haapi_nonce,
        )
        body = urlencode({"token": continuation_token, "state": state})
        try:
            async with self._session.post(
                HAAPI_AUTHZ_URL,
                data=body,
                headers={
                    **_HAAPI_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "DPoP": dpop,
                    "session-id": session_id,
                },
            ) as resp:
                resp.raise_for_status()
                self._haapi_nonce = resp.headers.get("DPoP-Nonce") or self._haapi_nonce
                authz_resp = await resp.json(content_type=None)
                _LOGGER.debug("HAAPI authz response: %s", authz_resp)
        except aiohttp.ClientError as err:
            raise EonApiError(f"HAAPI authz POST failed: {err}") from err

        # Extract the authorization code from the authz response.
        # Common locations in Curity HAAPI responses:
        code = (
            authz_resp.get("code")
            or (authz_resp.get("properties") or {}).get("code")
            or authz_resp.get("authorization_code")
        )
        if code:
            return code

        # May be another continuation – recurse once
        next_token = (
            authz_resp.get("token")
            or (authz_resp.get("properties") or {}).get("token")
        )
        if next_token:
            return await self._haapi_authz(next_token, state, session_id, code_verifier)

        raise EonApiError(
            f"No authorization code in authz response: {authz_resp}"
        )

    async def _exchange_code(self, code: str, code_verifier: str) -> dict:
        """POST to token endpoint to exchange authorization code for tokens.

        Payload confirmed from DevTools (217 bytes):
            client_id=eon-web-light-haapi
            grant_type=authorization_code
            code=<auth_code>
            code_verifier=<pkce_verifier>
            redirect_uri=https://www.eon.se/
        """
        body = urlencode({
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": OAUTH_REDIRECT_URI,
        })
        try:
            async with self._session.post(
                TOKEN_URL,
                data=body,
                headers={
                    **_COMMON_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "*/*",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
            ) as resp:
                resp.raise_for_status()
                token_resp = await resp.json(content_type=None)
                _LOGGER.debug(
                    "Token response keys=%s expires_in=%s token_type=%s has_refresh=%s",
                    list(token_resp.keys()),
                    token_resp.get("expires_in"),
                    token_resp.get("token_type"),
                    "refresh_token" in token_resp,
                )
                return token_resp
        except aiohttp.ClientError as err:
            raise EonApiError(f"Token exchange failed: {err}") from err

    # ------------------------------------------------------------------
    # Facilities
    # ------------------------------------------------------------------

    async def get_facilities(self) -> list[dict[str, Any]]:
        """Return a list of facility dicts for the logged-in customer.

        TODO: Find the real facilities endpoint in DevTools.
        """
        await self.ensure_token()
        try:
            async with self._session.get(
                FACILITIES_API_URL,
                headers=self._auth_headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                _LOGGER.debug("Facilities response: %s", data)
        except aiohttp.ClientError as err:
            raise EonApiError(f"Failed to fetch facilities: {err}") from err

        if isinstance(data, list):
            facilities = data
        elif isinstance(data, dict):
            facilities = (
                data.get("facilities")
                or data.get("anlaggningar")
                or data.get("meteringPoints")
                or data.get("items")
                or []
            )
        else:
            facilities = []

        if not facilities:
            _LOGGER.warning(
                "No facilities returned from %s – response was: %s",
                FACILITIES_API_URL,
                data,
            )
        return facilities

    # ------------------------------------------------------------------
    # Consumption data
    # ------------------------------------------------------------------

    async def get_consumption(
        self,
        facility_id: str,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Fetch 15-minute consumption data from /api/consumption_new.

        Confirmed payload (151 bytes, 2026-07-27):
            {
                "startDate": "2026-07-01",
                "endDate": "2026-07-27",
                "resolution": "Quarterly",      # = 15-min / quarter-hour intervals
                "pointOfDeliveryNumber": "735999114000386913",
                "consumptionType": "Electricity"
            }
        """
        await self.ensure_token()

        body = {
            "startDate": from_date.strftime("%Y-%m-%d"),
            "endDate": to_date.strftime("%Y-%m-%d"),
            "resolution": RESOLUTION_15MIN,  # "Quarterly" = quarter-hour = 15 min
            "pointOfDeliveryNumber": facility_id,
            "consumptionType": "Electricity",
        }

        try:
            async with self._session.post(
                CONSUMPTION_API_URL,
                json=body,
                headers=self._auth_headers(),
            ) as resp:
                resp.raise_for_status()
                content_type = resp.content_type or ""
                if "csv" in content_type or "octet-stream" in content_type:
                    raw = await resp.read()
                    text = raw.decode("utf-8-sig", errors="replace")
                    _LOGGER.debug("Got CSV response (%d bytes) for %s", len(raw), facility_id)
                    return self.parse_csv(text)
                else:
                    data = await resp.json(content_type=None)
                    _LOGGER.debug("Got JSON response for %s: %s", facility_id, json.dumps(data)[:500])
                    intervals, inst_id = self._parse_json_intervals(data)
                    # Fall back to facility_id if the API doesn't return an installation_id
                    return intervals, inst_id or facility_id

        except aiohttp.ClientError as err:
            raise EonApiError(f"Failed to fetch consumption for {facility_id}: {err}") from err

    async def post_export_log(
        self,
        installation_id: str,
        from_date: date,
        to_date: date,
    ) -> None:
        """Notify E.ON that an export was performed. Fire-and-forget.

        Confirmed payload (153 bytes, 2026-07-27):
            {
                "installation": "41590851",
                "resolution": "Q",
                "dateTimeFrom": "2026-06-30T22:00:00Z",   # day before startDate at 22:00 UTC
                "dateTimeTo": "2026-07-27T05:00:00Z",     # toDate at 05:00 UTC
                "source": "EON.SE",
                "logExport": true
            }

        Note: dateTimeFrom/To are UTC; the browser sends day-before-startDate at 22:00Z
        (= midnight Stockholm summer time = UTC+2) and toDate at 05:00Z (= 07:00 local).
        We approximate with midnight UTC offsets.
        """
        if not self._bearer_token or not installation_id:
            return
        # Approximate the UTC offsets the browser uses (Europe/Stockholm = UTC+2 in summer)
        # dateTimeFrom = (from_date - 1 day) at 22:00Z  ≈  from_date 00:00 Stockholm
        # dateTimeTo   = to_date at 22:00Z              ≈  to_date+1 00:00 Stockholm
        from datetime import timedelta
        dt_from = (from_date - timedelta(days=1)).strftime("%Y-%m-%dT22:00:00Z")
        dt_to = to_date.strftime("%Y-%m-%dT22:00:00Z")
        body = {
            "installation": installation_id,
            "resolution": "Q",
            "dateTimeFrom": dt_from,
            "dateTimeTo": dt_to,
            "source": "EON.SE",
            "logExport": True,
        }
        try:
            async with self._session.post(
                EXPORT_LOG_URL,
                json=body,
                headers=self._auth_headers(),
            ) as resp:
                _LOGGER.debug("export-log status: %s", resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.debug("export-log call failed (non-critical): %s", err)

    # ------------------------------------------------------------------
    # JSON interval parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_intervals(data: Any) -> tuple[list[dict[str, Any]], str | None]:
        """Parse the JSON response from /api/consumption_new.

        Returns (intervals, installation_id).
        installation_id is extracted from the response for use with export-log.
        Update field names once a real response is captured.
        """
        intervals: list[dict[str, Any]] = []
        installation_id: str | None = None

        rows: list[Any] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            # Extract installation ID – may appear at top level or per-row
            installation_id = str(
                data.get("installation")
                or data.get("installationId")
                or data.get("installationNumber")
                or ""
            ) or None

            # Real response shape: {"energy": {"values": [...], "timeSpan": ...}, "flow": null}
            energy = data.get("energy") or {}
            if isinstance(energy, dict):
                if not installation_id:
                    installation_id = str(
                        energy.get("installation")
                        or energy.get("installationId")
                        or ""
                    ) or None
                rows = (
                    energy.get("values")
                    or energy.get("intervals")
                    or energy.get("data")
                    or []
                )
            else:
                rows = (
                    data.get("intervals")
                    or data.get("data")
                    or data.get("values")
                    or data.get("measurements")
                    or data.get("consumptions")
                    or []
                )

        for item in rows:
            if not isinstance(item, dict):
                continue
            # Grab installation ID from first row if not found at top level
            if installation_id is None:
                raw_inst = (
                    item.get("installation")
                    or item.get("installationId")
                    or item.get("installationNumber")
                )
                if raw_inst:
                    installation_id = str(raw_inst)
            try:
                # Real response: {"value": 0.052, "dateTime": "2026-07-25 00:00", "isCalculated": false}
                start_raw = (
                    item.get("dateTime")
                    or item.get("startTime") or item.get("start") or item.get("starttidpunkt") or ""
                )
                end_raw = (
                    item.get("endTime") or item.get("end") or item.get("sluttidpunkt") or ""
                )
                kwh_raw = (
                    item.get("value")
                    or item.get("quantity")
                    or item.get("kvantitet")
                    or item.get("consumption")
                    or item.get("energy")
                    or 0.0
                )
                direction = str(
                    item.get("direction") or item.get("energiriktning") or "Förbrukning"
                )
                quality = str(item.get("quality") or item.get("kvalitet") or "")

                start_dt = _parse_dt(start_raw)
                end_dt = _parse_dt(end_raw)
                if start_dt is None:
                    continue
                # If no end time provided, infer from resolution (Quarterly = 15 min)
                if end_dt is None or end_dt == start_dt:
                    from datetime import timedelta
                    end_dt = start_dt + timedelta(minutes=15)

                intervals.append(
                    {
                        INTERVAL_START: start_dt,
                        INTERVAL_END: end_dt or start_dt,
                        INTERVAL_DIRECTION: direction,
                        INTERVAL_QUALITY: quality,
                        INTERVAL_KWH: float(kwh_raw),
                    }
                )
            except (ValueError, TypeError, KeyError) as err:
                _LOGGER.debug("Skipping unparseable interval %s: %s", item, err)

        _LOGGER.debug("Parsed %d intervals from JSON (installation: %s)", len(intervals), installation_id)
        return intervals, installation_id

    # ------------------------------------------------------------------
    # CSV parser (fallback / offline testing)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_csv(csv_text: str) -> list[dict[str, Any]]:
        """Parse an E.ON quarter-hour CSV export."""
        intervals: list[dict[str, Any]] = []

        normalised = csv_text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = [b.strip() for b in normalised.split("\n\n") if b.strip()]
        data_block = blocks[-1] if blocks else normalised

        reader = csv.DictReader(io.StringIO(data_block), delimiter=CSV_DELIMITER)
        for row in reader:
            try:
                start_str = row.get("Starttidpunkt", "").strip()
                end_str = row.get("Sluttidpunkt", "").strip()
                kwh_str = row.get("Kvantitet", "0").strip().replace(CSV_DECIMAL_SEPARATOR, ".")
                direction = row.get("Energiriktning", "").strip()
                quality = row.get("Kvalitet", "").strip()

                if not start_str:
                    continue

                start_dt = datetime.strptime(start_str, CSV_DATE_FORMAT)
                end_str_clean = end_str[:16] if len(end_str) >= 16 else end_str
                end_dt = (
                    datetime.strptime(end_str_clean, CSV_DATE_FORMAT)
                    if end_str_clean
                    else start_dt
                )

                intervals.append(
                    {
                        INTERVAL_START: start_dt,
                        INTERVAL_END: end_dt,
                        INTERVAL_DIRECTION: direction,
                        INTERVAL_QUALITY: quality,
                        INTERVAL_KWH: float(kwh_str),
                    }
                )
            except (ValueError, KeyError) as err:
                _LOGGER.debug("Skipping unparseable CSV row %s: %s", row, err)

        _LOGGER.debug("Parsed %d intervals from CSV", len(intervals))
        return intervals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {
            **_API_HEADERS,
            "Authorization": f"Bearer {self._bearer_token}",
        }

    def _ensure_authenticated(self) -> None:
        """Raise if not authenticated. Use ensure_token() in the coordinator for auto-refresh."""
        if not self._bearer_token:
            raise EonAuthError("Not authenticated – call authenticate() first")


def _parse_dt(value: Any) -> datetime | None:
    """Try to parse a datetime string in various ISO-like formats."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(s[:19], fmt[:len(fmt)])
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.rstrip("Z").split("+")[0])
    except ValueError:
        return None
