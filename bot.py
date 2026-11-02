# OGGY ST AUTHH — telegram bot (webhook, high-performance, multi-user, /stop)
import os
import io
import asyncio
import logging
import time
from datetime import datetime

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from checker import stripe_auth2_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("OGGY")

DEFAULT_TOKEN = "8864619886:AAFk9JheVoMoct6-ASop9f0oiz400ITEDVk"
TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_TOKEN).strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN missing")

PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "oggy_" + os.urandom(6).hex())
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/") or RENDER_URL
WEBHOOK_PATH = "/webhook"

CONCURRENCY = int(os.environ.get("CONCURRENCY", "50"))
MAX_CARDS = int(os.environ.get("MAX_CARDS", "100000"))

BOT_NAME = "OGGY ST AUTHH"

# Per-user locks and task tracking
USER_SEMS = {}
USER_TASKS = {}

def get_user_sem(user_id: int) -> asyncio.Semaphore:
    if user_id not in USER_SEMS:
        USER_SEMS[user_id] = asyncio.Semaphore(30)
    return USER_SEMS[user_id]


# -- helpers ----------------------------------------------------------------

def _parse_cards(raw: bytes):
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.count("|") == 3:
            out.append(s)
    return out[:MAX_CARDS]


def _is_hit(resp: str) -> bool:
    rl = resp.lower()
    if "all sites failed" in rl:
        return False
    return any(k in rl for k in (
        "payment method added", "3ds challenge",
        "ccn live", "insufficient_funds", "incorrect_cvc",
    ))


def _emoji_for(resp: str) -> str:
    rl = resp.lower()
    if "payment method added" in rl: return "💳"
    if "3ds" in rl: return "🔐"
    if "ccn live" in rl or "insufficient" in rl or "incorrect_cvc" in rl: return "💰"
    if any(k in rl for k in ("declined", "expired", "not_supported", "do_not_honor",
                             "fraudulent", "stolen", "lost", "pickup", "restricted")):
        return "❌"
    if "all sites failed" in rl or "empty ajax" in rl or "session mismatch" in rl:
        return "⚠️"
    return "ℹ️"


def _format_hit_log(card: str, resp: str, dt: float, idx: int) -> str:
    emoji = _emoji_for(resp)
    safe_card = card.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_resp = resp.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ts = datetime.now().strftime("%H:%M:%S")
    
    return (
        f"{emoji} <b>HIT DETECTED</b> {emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Card:</b>\n"
        f"<code>{safe_card}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Res :</b> <code>{safe_resp}</code>\n"
        f"⚡ <b>GW  :</b> <code>Stripe-Auth2</code>\n"
        f"⏱ <b>Time:</b> <code>{dt:.1f}s</code>\n"
        f"🔢 <b>Idx :</b> <code>#{idx}</code>\n"
        f"🕒 <b>At  :</b> <code>{ts}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{BOT_NAME}</i>"
    )


# -- queue-based batch runner -----------------------------------------------

async def _run_batch(user_id, cards, progress_cb, hit_cb):
    total = len(cards)
    queue = asyncio.Queue()
    for c in cards:
        queue.put_nowait(c)
        
    user_sem = get_user_sem(user_id)
    hits, all_results = [], []
    done = {"n": 0}
    
    async def worker():
        while True:
            try:
                card = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
                
            t0 = time.time()
            async with user_sem:
                try:
                    res = await stripe_auth2_check(card, None)
                except Exception as e:
                    res = {"Response": f"err: {e}", "Gateway": "Stripe-Auth2", "Price": "$0"}
                
            dt = time.time() - t0
            resp = res.get("Response", "?")
            
            done["n"] += 1
            idx = done["n"]
            all_results.append((card, resp, dt))
            
            if _is_hit(resp):
                hits.append((card, resp))
                try:
                    await hit_cb(card, resp, dt, idx)
                except Exception:
                    pass
                    
            try:
                await progress_cb(idx, total, len(hits))
            except Exception:
                pass
                
            queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(min(CONCURRENCY, total))]
    await asyncio.gather(*workers)
    return hits, all_results


# -- commands ---------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🟢 <b>{BOT_NAME}</b>\n"
        f"<i>stripe auth2 checker</i>\n\n"
        f"send a single card:\n"
        f"<code>cc|mm|yy|cvv</code>\n\n"
        f"or send a .txt file (one card per line)\n\n"
        f"limit: {MAX_CARDS:,} cards\n"
        f"use /help for more.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>{BOT_NAME} — help</b>\n\n"
        f"• send a single card in chat: <code>cc|mm|yy|cvv</code>\n"
        f"• or send a .txt file with one card per line\n"
        f"• bot replies with:\n"
        f"   - live result (single) / hits.txt + results.txt (file)\n"
        f"   - instant hit logs as they happen\n"
        f"• max file size: 5MB\n"
        f"• max cards per run: {MAX_CARDS:,}\n\n"
        f"commands:\n"
        f"/start — intro\n"
        f"/stop — cancel current run\n"
        f"/help — this",
        parse_mode="HTML",
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = USER_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 stopping your current session…")
    else:
        await update.message.reply_text("no active session found.")


# -- single card ------------------------------------------------------------

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.count("|") != 3:
        return

    user_id = update.effective_user.id
    status = await update.message.reply_text("checking…")
    
    USER_TASKS[user_id] = asyncio.current_task()
    
    try:
        t0 = time.time()
        user_sem = get_user_sem(user_id)
        async with user_sem:
            res = await stripe_auth2_check(text, None)
        dt = time.time() - t0
        resp = res.get("Response", "?")
        
        await status.edit_text(
            _format_hit_log(text, resp, dt, 1),
            parse_mode="HTML"
        )
    except asyncio.CancelledError:
        await status.edit_text("🛑 session stopped.")
    except Exception as e:
        await status.edit_text(f"err: {e}")
    finally:
        USER_TASKS.pop(user_id, None)


# -- file upload ------------------------------------------------------------

SPIN = ["⏳", "⌛", "🔄", "⏳", "⌛", "🔄"]

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("send a .txt file.")
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("file too large (max 5MB).")
        return

    status = await update.message.reply_text("📥 downloading…")
    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await status.edit_text(f"download failed: {e}")
        return

    cards = _parse_cards(raw)
    if not cards:
        await status.edit_text("no valid cards found (need cc|mm|yy|cvv).")
        return

    await status.edit_text(f"🔎 checking {len(cards):,} cards…")

    state = {"spin": 0, "last": 0.0}

    async def progress(done, total, hit_count):
        now = time.time()
        if now - state["last"] < 1.5 and done != total:
            return
        state["last"] = now
        state["spin"] += 1
        icon = SPIN[state["spin"] % len(SPIN)]
        pct = done / total * 100 if total else 0

        try:
            await status.edit_text(
                f"{icon} <b>Checking…</b>\n"
                f"📊 {done:,} / {total:,}  ({pct:.1f}%)\n"
                f"💚 hits: {hit_count}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def broadcast_hit(card, resp, dt, idx):
        try:
            await update.message.reply_text(
                _format_hit_log(card, resp, dt, idx),
                parse_mode="HTML"
            )
        except Exception:
            pass

    USER_TASKS[user_id] = asyncio.current_task()
    try:
        t0 = time.time()
        hits, all_results = await _run_batch(user_id, cards, progress, broadcast_hit)
        dt = time.time() - t0

        out = io.BytesIO()
        out.write(f"# {BOT_NAME} — full results\n".encode("utf-8"))
        out.write("# card | response | time_s\n".encode("utf-8"))
        for card, resp, secs in all_results:
            out.write(f"{card} | {resp} | {secs:.1f}s\n".encode("utf-8"))
        out.seek(0)

        summary = (
            f"✅ <b>{BOT_NAME} — done</b>\n"
            f"⏱ time: {dt:.1f}s\n"
            f"📊 checked: {len(cards):,}\n"
            f"💚 hits: {len(hits):,}"
        )
        await status.edit_text(summary, parse_mode="HTML")

        if hits:
            hb = io.BytesIO()
            hb.write(f"# {BOT_NAME} — hits\n".encode("utf-8"))
            for card, resp in hits:
                hb.write(f"{card} | {resp}\n".encode("utf-8"))
            hb.seek(0)
            await update.message.reply_document(
                document=hb, filename="hits.txt",
                caption=f"💚 {len(hits)} hit(s)",
            )

        await update.message.reply_document(
            document=out, filename="results.txt",
            caption=f"📄 full results — {len(cards):,} checked",
        )
        
    except asyncio.CancelledError:
        await status.edit_text("🛑 session stopped.")
    except Exception as e:
        log.error(f"Batch error: {e}", exc_info=True)
        await status.edit_text(f"❌ error: {e}")
    finally:
        USER_TASKS.pop(user_id, None)


# -- web plumbing -----------------------------------------------------------

async def webhook_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    app: Application = request.app["tg_app"]
    update = Update.de_json(data, app.bot)
    
    asyncio.create_task(app.process_update(update))
    return web.Response(text="ok")


async def health(_request):
    return web.Response(text=f"{BOT_NAME} ok")


async def on_startup(app: web.Application):
    tg: Application = app["tg_app"]
    await tg.initialize()
    await tg.start()
    if WEBHOOK_URL:
        wh_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        await tg.bot.set_webhook(
            url=wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True
        )
        log.info("%s webhook set: %s", BOT_NAME, wh_url)
    else:
        log.warning("WEBHOOK_URL / RENDER_EXTERNAL_URL unset — webhook not registered")
    log.info("%s ready on :%s", BOT_NAME, PORT)


async def on_shutdown(app: web.Application):
    tg: Application = app["tg_app"]
    try:
        await tg.bot.delete_webhook()
    except Exception:
        pass
    await tg.stop()
    await tg.shutdown()


def main():
    log.info("starting %s", BOT_NAME)
    
    tg_app = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .concurrent_updates(True) 
        .build()
    )
    
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("stop", cmd_stop))
    tg_app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_get("/", health)
    web_app.router.add_post(WEBHOOK_PATH, webhook_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    web.run_app(web_app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
