# OGGY ST AUTHH — telegram bot (webhook, render-ready)
import os
import io
import asyncio
import logging
import random
import time

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from checker import stripe_auth2_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("OGGY")

DEFAULT_TOKEN = "8864619886:AAFaQSwFV9MmDppZ7TTc1jMSOG6-OP2fYas"
TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_TOKEN).strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN missing")

PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "oggy_" + os.urandom(6).hex())
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/") or RENDER_URL
WEBHOOK_PATH = "/webhook"
CONCURRENCY = int(os.environ.get("CONCURRENCY", "3"))
MAX_CARDS = int(os.environ.get("MAX_CARDS", "500"))

BOT_NAME = "OGGY ST AUTHH"

HITS_LOCK = asyncio.Lock()


def _parse_cards(raw: bytes):
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"): continue
        if s.count("|") == 3:
            out.append(s)
    return out[:MAX_CARDS]


def _is_hit(resp: str) -> bool:
    rl = resp.lower()
    if "all sites failed" in rl: return False
    return any(k in rl for k in (
        "payment method added", "3ds challenge",
        "ccn live", "insufficient_funds", "incorrect_cvc",
    ))


async def _run_batch(cards, progress_cb):
    total = len(cards)
    sem = asyncio.Semaphore(CONCURRENCY)
    hits, all_results = [], []
    done = {"n": 0}

    async def worker(card):
        async with sem:
            t0 = time.time()
            try:
                res = await stripe_auth2_check(card, None)
            except Exception as e:
                res = {"Response": f"err: {e}", "Gateway": "Stripe-Auth2", "Price": "$0"}
            dt = time.time() - t0
            resp = res.get("Response", "?")
            all_results.append((card, resp, dt))
            if _is_hit(resp):
                hits.append((card, resp))
            done["n"] += 1
            if done["n"] % 5 == 0 or done["n"] == total:
                try:
                    await progress_cb(done["n"], total, len(hits))
                except Exception:
                    pass

    await asyncio.gather(*(worker(c) for c in cards))
    return hits, all_results


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🟢 {BOT_NAME}\n"
        "stripe auth2 checker\n\n"
        "send me a .txt file — one card per line:\n"
        "cc|mm|yy|cvv\n\n"
        f"limit: {MAX_CARDS} cards · concurrency {CONCURRENCY}\n"
        "use /help for more."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{BOT_NAME} — help\n\n"
        "• send a .txt file with cards (cc|mm|yy|cvv per line)\n"
        "• bot checks each and replies with:\n"
        "   - hits.txt (live / 3ds / ccn)\n"
        "   - results.txt (full breakdown)\n"
        "• max file size: 5MB\n"
        f"• max cards per run: {MAX_CARDS}\n\n"
        "commands:\n"
        "/start — intro\n"
        "/help — this"
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc: return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("send a .txt file.")
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("file too large (max 5MB).")
        return

    status = await update.message.reply_text("downloading…")
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

    await status.edit_text(f"checking {len(cards)} cards… 0/{len(cards)}")

    last_edit = {"t": 0.0}
    async def progress(done, total, hit_count):
        now = time.time()
        if now - last_edit["t"] < 2.0 and done != total:
            return
        last_edit["t"] = now
        try:
            await status.edit_text(
                f"checking {total} cards… {done}/{total} · hits {hit_count}"
            )
        except Exception:
            pass

    t0 = time.time()
    hits, all_results = await _run_batch(cards, progress)
    dt = time.time() - t0

    out = io.BytesIO()
    out.write(b"# OGGY ST AUTHH — full results\n")
    out.write(b"# card | response | time_s\n")
    for card, resp, secs in all_results:
        out.write(f"{card} | {resp} | {secs:.1f}s\n".encode())
    out.seek(0)

    summary = (
        f"{BOT_NAME} — done in {dt:.1f}s\n"
        f"checked: {len(cards)}\n"
        f"hits: {len(hits)}"
    )
    await status.edit_text(summary)

    if hits:
        hb = io.BytesIO()
        hb.write(b"# OGGY ST AUTHH — hits\n")
        for card, resp in hits:
            hb.write(f"{card} | {resp}\n".encode())
        hb.seek(0)
        await update.message.reply_document(
            document=hb, filename="hits.txt",
            caption=f"{len(hits)} hit(s)",
        )

    await update.message.reply_document(
        document=out, filename="results.txt",
        caption=f"full results — {len(cards)} checked",
    )


async def webhook_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    app: Application = request.app["tg_app"]
    update = Update.de_json(data, app.bot)
    await app.update_queue.put(update)
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
    tg_app = Application.builder().token(TOKEN).updater(None).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_get("/", health)
    web_app.router.add_post(WEBHOOK_PATH, webhook_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    web.run_app(web_app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
