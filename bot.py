import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import db
from scheduler import run_scheduler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:2b")

IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent / "data"

# ConversationHandler states
COMPOSING, AWAITING_TIME = range(2)

# Per-user draft storage: {user_id: [{"text": str, "image_paths": [str]}]}
drafts: dict[int, list[dict]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        return await func(update, ctx)
    return wrapper


async def parse_datetime(user_input: str) -> datetime | None:
    """Parse natural language datetime using Ollama. Returns UTC datetime."""
    now_ist = datetime.now(IST)
    prompt = (
        f"Convert this to an ISO 8601 datetime string (YYYY-MM-DDTHH:MM:SS). "
        f"Current date/time is {now_ist.strftime('%Y-%m-%dT%H:%M:%S')} IST. "
        f"Input: \"{user_input}\". "
        f"Respond with JSON only, no explanation. "
        f"Format: {{\"datetime\": \"YYYY-MM-DDTHH:MM:SS\"}}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False},
            )
            data = resp.json()
            result = json.loads(data["response"])
            dt_str = result["datetime"]
            # Parse as IST, convert to UTC
            dt_ist = datetime.fromisoformat(dt_str).replace(tzinfo=IST)
            return dt_ist.astimezone(timezone.utc)
    except Exception as e:
        logger.warning(f"Ollama parse failed: {e}, trying direct ISO parse")
        # Fallback: try direct ISO parse
        try:
            dt = datetime.fromisoformat(user_input)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST).astimezone(timezone.utc)
            return dt
        except Exception:
            return None


def format_thread_preview(tweets: list[dict]) -> str:
    lines = []
    for i, tweet in enumerate(tweets):
        images = tweet.get("image_paths", [])
        img_note = f" [{len(images)} image(s)]" if images else ""
        text = tweet.get("text") or "(image only)"
        lines.append(f"**{i+1}/{len(tweets)}**{img_note}\n{text}")
    return "\n\n---\n\n".join(lines)


async def save_photo(msg: Message) -> str:
    """Download the best-quality photo from a message to data/images/."""
    img_dir = DATA_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    photo = msg.photo[-1]  # largest size
    file = await photo.get_file()
    path = img_dir / f"{uuid.uuid4()}.jpg"
    await file.download_to_drive(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@auth_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Twitter Thread Scheduler\n\n"
        "/thread — start composing a new thread\n"
        "/threads — list scheduled threads\n"
        "/cancel_thread <id> — cancel a scheduled thread\n\n"
        "While composing:\n"
        "  Send text or photo messages (one per tweet)\n"
        "  /preview — preview full thread\n"
        "  /done — finish and schedule\n"
        "  /discard — discard current draft"
    )


# ---------------------------------------------------------------------------
# Thread composition ConversationHandler
# ---------------------------------------------------------------------------

@auth_only
async def cmd_thread(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    drafts[uid] = []
    await update.message.reply_text(
        "Composing a new thread. Send your tweets one by one.\n"
        "Text, photos, or photos with captions. Each message = one tweet.\n\n"
        "/preview  /done  /discard"
    )
    return COMPOSING


async def handle_tweet_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if len(text) > 280:
        await update.message.reply_text(
            f"That's {len(text)} chars — over the 280-char limit. Please shorten and resend."
        )
        return COMPOSING

    drafts[uid].append({"text": text, "image_paths": []})
    n = len(drafts[uid])
    await update.message.reply_text(f"Tweet {n} added.")
    return COMPOSING


async def handle_tweet_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    caption = (update.message.caption or "").strip()

    if caption and len(caption) > 280:
        await update.message.reply_text(
            f"Caption is {len(caption)} chars — over the 280-char limit. Please resend with a shorter caption."
        )
        return COMPOSING

    path = await save_photo(update.message)

    # Check if this photo is part of a media group already being assembled
    media_group_id = update.message.media_group_id
    if media_group_id:
        # Find existing tweet for this media group
        key = f"mg_{media_group_id}"
        if not hasattr(ctx, "_mg"):
            ctx._mg = {}
        if key in ctx._mg:
            idx = ctx._mg[key]
            entry = drafts[uid][idx]
            if len(entry["image_paths"]) >= 4:
                await update.message.reply_text("Max 4 images per tweet — extra images ignored.")
            else:
                entry["image_paths"].append(path)
                if caption and not entry["text"]:
                    entry["text"] = caption
            return COMPOSING
        else:
            # New media group
            drafts[uid].append({"text": caption, "image_paths": [path]})
            ctx._mg[key] = len(drafts[uid]) - 1
            n = len(drafts[uid])
            await update.message.reply_text(f"Tweet {n} started with image.")
            return COMPOSING
    else:
        # Single photo
        drafts[uid].append({"text": caption, "image_paths": [path]})
        n = len(drafts[uid])
        await update.message.reply_text(f"Tweet {n} added with image.")
        return COMPOSING


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not drafts.get(uid):
        await update.message.reply_text("No tweets in draft yet.")
        return COMPOSING
    preview = format_thread_preview(drafts[uid])
    await update.message.reply_text(f"Thread preview ({len(drafts[uid])} tweets):\n\n{preview}", parse_mode="Markdown")
    return COMPOSING


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not drafts.get(uid):
        await update.message.reply_text("No tweets in draft. Add some first.")
        return COMPOSING
    await update.message.reply_text(
        f"Thread has {len(drafts[uid])} tweet(s). When should I post it?\n\n"
        "Send a time like:\n"
        "  • now\n"
        "  • tomorrow 9am\n"
        "  • 2026-03-10 14:00\n"
        "  • in 2 hours"
    )
    return AWAITING_TIME


async def cmd_discard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    drafts.pop(uid, None)
    await update.message.reply_text("Draft discarded.")
    return ConversationHandler.END


async def handle_schedule_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_input = update.message.text.strip()

    if user_input.lower() == "now":
        scheduled_utc = datetime.now(timezone.utc)
    else:
        scheduled_utc = await parse_datetime(user_input)
        if not scheduled_utc:
            await update.message.reply_text(
                "Couldn't parse that time. Try again (e.g. 'tomorrow 9am', '2026-03-10 14:00', 'in 3 hours')."
            )
            return AWAITING_TIME

    # Store as UTC ISO string (no tz suffix for SQLite simplicity, but it's UTC)
    scheduled_str = scheduled_utc.strftime("%Y-%m-%d %H:%M:%S")

    tweets = drafts.pop(uid)
    thread_id = db.create_thread(scheduled_str)
    for i, tweet in enumerate(tweets):
        db.add_tweet(thread_id, i, tweet["text"], tweet["image_paths"])

    # Display time back in IST
    scheduled_ist = scheduled_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    await update.message.reply_text(
        f"Thread #{thread_id} scheduled for {scheduled_ist}.\n"
        f"/cancel_thread {thread_id} to remove it."
    )
    return ConversationHandler.END


async def conv_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /preview, /done, or /discard. Send text or photos to add tweets.")
    return COMPOSING


# ---------------------------------------------------------------------------
# /threads and /cancel_thread
# ---------------------------------------------------------------------------

@auth_only
async def cmd_threads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    threads = db.get_scheduled_threads()
    if not threads:
        await update.message.reply_text("No scheduled threads.")
        return

    lines = []
    for t in threads:
        tweets = db.get_tweets(t["id"])
        # Convert UTC to IST for display
        try:
            dt_utc = datetime.fromisoformat(t["scheduled_at"]).replace(tzinfo=timezone.utc)
            dt_ist = dt_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        except Exception:
            dt_ist = t["scheduled_at"]
        lines.append(f"#{t['id']} — {len(tweets)} tweet(s) — {dt_ist}")

    await update.message.reply_text("Scheduled threads:\n\n" + "\n".join(lines))


@auth_only
async def cmd_cancel_thread(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /cancel_thread <id>")
        return
    thread_id = int(args[0])
    if db.cancel_thread(thread_id):
        db.delete_thread_images(thread_id)
        await update.message.reply_text(f"Thread #{thread_id} cancelled.")
    else:
        await update.message.reply_text(f"Thread #{thread_id} not found or already posted.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    asyncio.create_task(run_scheduler(application.bot, ALLOWED_USER_ID))


def main():
    db.init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("thread", cmd_thread)],
        states={
            COMPOSING: [
                CommandHandler("preview", cmd_preview),
                CommandHandler("done", cmd_done),
                CommandHandler("discard", cmd_discard),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tweet_text),
                MessageHandler(filters.PHOTO, handle_tweet_photo),
                MessageHandler(filters.ALL, conv_fallback),
            ],
            AWAITING_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_time),
            ],
        },
        fallbacks=[CommandHandler("discard", cmd_discard)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("threads", cmd_threads))
    app.add_handler(CommandHandler("cancel_thread", cmd_cancel_thread))
    app.add_handler(conv)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
