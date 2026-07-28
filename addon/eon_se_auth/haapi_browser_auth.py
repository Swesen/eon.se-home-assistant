"""
Playwright HAAPI auth — direct approach.

Injects the @curity/identityserver-haapi-web-driver into a Playwright page,
calls createHaapiFetch() to initialize the HAAPI session (which handles DPoP + CAT attestation
via the Curity-hosted iframe), then drives the HAAPI authorization flow:
  - GET /neo/oauth/v2/authorization  → hypermedia JSON with login form
  - POST credentials  → get authorization code
  - Exchange code for Bearer token

Input (stdin): JSON {"username": "...", "password": "..."}
Output (stdout): JSON {"code": "...", "code_verifier": "..."} or {"error": "..."}
"""
import asyncio
import json
import sys
import urllib.parse
import secrets
import hashlib
import base64

input_data = json.loads(sys.stdin.read())
username = input_data["username"]
password = input_data["password"]

HAAPI_CONFIG = {
    "clientId": "eon-web-light-haapi",
    "tokenEndpoint": "https://api.apps.eon.se/neo/oauth/v2/token",
}

OAUTH_SCOPE = (
    "cjcv cjip cjmc cjpf cjim nfda openid cjrn movingjourney cjdsp cjero stgo "
    "workorder outagehistory:read-restricted serviceorder:create-restricted "
    "serviceorder:delete-restricted serviceorder:read-restricted "
    "faultreport:read-restricted faultreport:create-restricted "
    "outage:read-restricted installation:read-restricted "
    "outagecompensation:read-restricted damagenotification:create-restricted "
    "damagenotification:read-restricted invoice:read-restricted "
    "hancustchoice:read-restricted hancustchoice:update-restricted "
    "contractaccount:update-restricted elnastatus:read-restricted "
    "elnastatus:update-restricted meterreading:read-restricted "
    "nsporder:create-restricted salesforce deal:read-restricted "
    "damagenotification:update-restricted damagenotification:delete-restricted "
    "pubregperson:read-restricted bankid-sign store-poa power-consumption:read-all "
    "disruption-alert:update-restricted meterreading:create-restricted "
    "nspinspdoc:read-restricted nspinspdoc:create-all power-consumption:update-restricted "
    "power-consumption:create-restricted nspengagements:read-restricted "
    "logexport:read-restricted logexport:create-restricted nspgridinfo:read-all "
    "nspcontractaccounts:read-restricted nspeligibleoffers:read-all credit-status "
    "document:read-restricted"
)

REDIRECT_URI = "https://www.eon.se/"
ACR = "urn:se:curity:authentication:isu-sap-authenticator:isu-sap-authenticator-recaptcha"


def _pkce():
    cv = secrets.token_urlsafe(64)
    cc = base64.urlsafe_b64encode(hashlib.sha256(cv.encode()).digest()).rstrip(b"=").decode()
    return cv, cc


async def main():
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    import urllib.request
    import tarfile
    import io

    # Download the HAAPI web driver JS from npm
    print("Downloading HAAPI web driver...", file=sys.stderr)
    npm_url = (
        "https://registry.npmjs.org/@curity/identityserver-haapi-web-driver"
        "/-/identityserver-haapi-web-driver-1.5.0.tgz"
    )
    with urllib.request.urlopen(npm_url) as r:
        tgz_data = r.read()
    tf = tarfile.open(fileobj=io.BytesIO(tgz_data), mode="r:gz")
    js_content = tf.extractfile(
        next(m for m in tf.getmembers() if m.name.endswith("index.js"))
    ).read().decode("utf-8")
    print(f"Driver loaded ({len(js_content)} bytes)", file=sys.stderr)

    code_verifier, code_challenge = _pkce()
    state = secrets.token_urlsafe(24)

    authz_params = urllib.parse.urlencode({
        "client_id": HAAPI_CONFIG["clientId"],
        "scope": OAUTH_SCOPE,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "redirect_uri": REDIRECT_URI,
        "acr": ACR,
        "prompt": "login",
    })
    authz_url = f"https://api.apps.eon.se/neo/oauth/v2/authorization?{authz_params}"

    # JS that will be evaluated in Playwright to drive the HAAPI flow
    haapi_js = r"""
async ({driverSrc, authzUrl, username, password}) => {
    const logs = [];
    const log = (...args) => { const s = args.map(a => String(a)).join(' '); logs.push(s); console.log(s); };
    const AUTH_BASE = 'https://api.apps.eon.se';
    const REDIRECT_PREFIX = 'https://www.eon.se';

    try {
        // Inject the driver into the page
        const script = document.createElement('script');
        script.textContent = driverSrc;
        document.head.appendChild(script);
        
        const { createHaapiFetch } = HaapiDriver;
        if (!createHaapiFetch) {
            return {error: 'createHaapiFetch not found', logs};
        }
        log('createHaapiFetch loaded');
        
        window['se:curity:web-cat:debug'] = true;

        const haapiFetch = createHaapiFetch({
            clientId: 'eon-web-light-haapi',
            tokenEndpoint: AUTH_BASE + '/neo/oauth/v2/token',
            timeout: 30,
        });
        
        log('Initializing haapiFetch (CAT attestation)...');
        try {
            await haapiFetch.init();
            log('HAAPI initialized OK');
        } catch(e) {
            return {error: 'init failed: ' + e.message, logs};
        }
        
        log('Making authz request...');
        let resp = await haapiFetch(authzUrl);
        let data = await resp.json();
        log('authz response type: ' + data.type + ' status: ' + resp.status);
        
        // Navigate the hypermedia flow to find and submit the login form
        for (let step = 0; step < 10; step++) {
            log('Step ' + step + ': type=' + (data.type || 'none'));
            
            // Error case
            if (data.type && data.type.includes('problem')) {
                return {error: 'Curity error: ' + (data.code || data.type) + ' msg: ' + JSON.stringify(data.messages||[]), logs};
            }
            
            // Check for auth code in redirect URL (successful completion)
            if (data.type === 'redirection-step') {
                const redirectAction = (data.actions || []).find(a => a.kind === 'redirect');
                if (redirectAction) {
                    const redirectUrl = (redirectAction.model || {}).href || redirectAction.href;
                    const fullUrl = redirectUrl.startsWith('/') ? AUTH_BASE + redirectUrl : redirectUrl;
                    log('Following redirection-step to: ' + fullUrl.substring(0, 100));
                    
                    // Check if this is the final redirect to redirect_uri with code
                    if (fullUrl.startsWith(REDIRECT_PREFIX) || fullUrl.includes('code=')) {
                        try {
                            const url = new URL(fullUrl);
                            const code = url.searchParams.get('code');
                            if (code) {
                                log('Got auth code from redirect URL!');
                                return {code, logs};
                            }
                        } catch(e) {}
                    }
                    
                    // If redirecting back to the authz endpoint, POST the form fields (Curity completes the flow)
                    if (fullUrl.includes('/neo/oauth/v2/authorization')) {
                        log('Authz completion redirect action: ' + JSON.stringify(redirectAction).substring(0, 500));
                        log('Following authz completion redirect (POST): ' + fullUrl.substring(0, 100));
                        
                        // Build POST body from the hidden fields
                        const model = redirectAction.model || {};
                        const method = model.method || 'GET';
                        let body = undefined;
                        if (model.fields && model.fields.length > 0) {
                            body = new URLSearchParams();
                            for (const f of model.fields) {
                                if (f.value !== undefined) body.set(f.name, String(f.value));
                            }
                        }
                        
                        resp = await haapiFetch(fullUrl, {method, body});
                        log('Authz POST status=' + resp.status);
                        
                        try {
                            data = await resp.json();
                            log('Authz POST response type=' + (data.type || 'none') + ' keys=' + Object.keys(data).join(','));
                            
                            // oauth-authorization-response: code is in properties.code
                            if (data.type === 'oauth-authorization-response') {
                                const code = (data.properties || {}).code;
                                if (code) { log('Got code from oauth-authorization-response!'); return {code, logs}; }
                                // Also check links
                                for (const link of (data.links || [])) {
                                    if (link.rel === 'authorization-response' && link.href) {
                                        try {
                                            const url = new URL(link.href);
                                            const c = url.searchParams.get('code');
                                            if (c) { log('Got code from authorization-response link!'); return {code: c, logs}; }
                                        } catch(e) {}
                                    }
                                }
                            }
                            
                            // Look for auth code
                            if (data.code && !data.type) return {code: data.code, logs};
                            if (data.redirectTo) {
                                try {
                                    const url = new URL(data.redirectTo);
                                    const code = url.searchParams.get('code');
                                    if (code) return {code, logs};
                                } catch(e) {}
                            }
                            // Check for final redirect in actions
                            for (const a of (data.actions || [])) {
                                const href = (a.model || {}).href || a.href || '';
                                const fullHref = href.startsWith('/') ? 'https://api.apps.eon.se' + href : href;
                                if (fullHref.includes('code=') || fullHref.startsWith('https://www.eon.se')) {
                                    try {
                                        const url = new URL(fullHref);
                                        const code = url.searchParams.get('code');
                                        if (code) return {code, logs};
                                    } catch(e) {}
                                }
                            }
                        } catch(e) {
                            log('Authz POST response not JSON: ' + e.message + ' status=' + resp.status);
                        }
                        continue;
                    }
                    
                    resp = await haapiFetch(fullUrl);
                    data = await resp.json();
                    continue;
                }
            }
            
            // Look for credential form (match by field type, not name)
            if (data.actions) {
                let submittedCreds = false;
                for (const action of data.actions) {
                    const model = action.model || {};
                    const fields = model.fields || [];
                    const userField = fields.find(f => 
                        f.type === 'username' || f.name === 'userName' || 
                        f.name === 'username' || f.name === 'accountId'
                    );
                    const passField = fields.find(f => 
                        f.type === 'password' || f.name === 'password'
                    );
                    
                    if (userField && passField) {
                        const credUrl = model.href || action.href;
                        const fullUrl = credUrl.startsWith('/') ? AUTH_BASE + credUrl : credUrl;
                        log('Submitting credentials to: ' + fullUrl.substring(0, 80));
                        
                        const body = new URLSearchParams();
                        for (const f of fields) {
                            if (f === userField) {
                                body.set(f.name, username);
                            } else if (f === passField) {
                                body.set(f.name, password);
                            } else if (f.value !== undefined) {
                                body.set(f.name, String(f.value));
                            }
                        }
                        
                        resp = await haapiFetch(fullUrl, {
                            method: model.method || 'POST',
                            body,
                        });
                        data = await resp.json();
                        log('Cred response status=' + resp.status + ' type=' + (data.type || 'none'));
                        submittedCreds = true;
                        break;
                    }
                }
                
                if (!submittedCreds) {
                    // No credential form found — log and break
                    log('No credential form in actions: ' + JSON.stringify(data.actions).substring(0, 300));
                    break;
                }
                continue;
            }
            
            // Follow next link
            const nextLink = (data.links || []).find(l => l.rel === 'next');
            if (nextLink) {
                const fullUrl = nextLink.href.startsWith('/') ? AUTH_BASE + nextLink.href : nextLink.href;
                log('Following next: ' + fullUrl.substring(0, 80));
                resp = await haapiFetch(fullUrl);
                data = await resp.json();
                continue;
            }
            
            break;
        }
        
        return {
            error: 'Flow ended without auth code',
            lastData: JSON.stringify(data).substring(0, 1500),
            logs,
        };
        
    } catch(e) {
        return {error: e.message, stack: e.stack, logs};
    }
}
"""

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        # Navigate to www.eon.se as the base page (same as real-world usage of HAAPI driver)
        # The HAAPI driver will create an iframe on api.apps.eon.se (cross-origin),
        # and postMessage works cross-origin with "*" as target.
        print("Loading base page (www.eon.se)...", file=sys.stderr)
        try:
            await page.goto(
                "https://www.eon.se/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            print(f"Base page error (non-fatal): {e}", file=sys.stderr)

        print("Running HAAPI JS flow...", file=sys.stderr)
        try:
            result = await page.evaluate(haapi_js, {
                "driverSrc": js_content,
                "authzUrl": authz_url,
                "username": username,
                "password": password,
            })
        except Exception as e:
            print(f"page.evaluate error: {e}", file=sys.stderr)
            result = {"error": str(e)}

        await browser.close()

    logs = result.get("logs", [])
    for log_line in logs:
        print(f"  JS: {log_line}", file=sys.stderr)
    print(f"Result: {json.dumps({k: v for k, v in result.items() if k != 'logs'})[:500]}", file=sys.stderr)

    if result.get("code"):
        print(json.dumps({
            "code": result["code"],
            "code_verifier": code_verifier,
        }))
    elif result.get("error"):
        print(json.dumps({"error": result["error"]}))
    else:
        print(json.dumps({"error": f"Unexpected: {json.dumps(result)[:300]}"}))


asyncio.run(main())
