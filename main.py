"""
main.py  –  Entry point for the Movie Publisher Bot
════════════════════════════════════════════════════════════════════════════

Two independent flows run simultaneously:

  A) Real-time flow
     ──────────────
     Bot is admin in source channel → receives channel_post updates.
     handle_source_message() processes each media message immediately
     (or queues it depending on interval setting).

  B) Scheduled queue flow
     ─────────────────────
     Admin sets /setstart to a specific message_id.
     The scheduler job runs every <interval> seconds, fetches the
     message at current_msg_id from source, publishes it, increments ptr.
     This handles backfilling older posts.

Both flows produce identical output: zero-download Telegram copy with
a rich HTML caption.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from admin import _publisher_job_callback, register_admin_handlers
from config import Config, load_config
from database import Database
from publisher import publish_media_message

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Silence noisy loggers
for _noisy in ("httpx", "httpcore", "hachoir"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Real-time source channel handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_source_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Triggered for every media post in the source channel.

    If the bot is in 'paused' state, the message is silently ignored.
    Otherwise it is published immediately to the target channel.

    Note: This handler fires for live messages arriving AFTER the bot
    was added.  For historical messages, use /setstart + scheduler.
    """
    cfg: Config   = context.bot_data["config"]
    db:  Database = context.bot_data["db"]

    if db.get_bool("paused", False):
        return

    msg = update.channel_post or update.message
    if msg is None:
        return

    # Resolve source / target (DB overrides config defaults)
    src_raw = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt_raw = db.get("target_channel_id") or str(cfg.target_channel_id)

    try:
        source_chat_id = int(src_raw)
    except (ValueError, TypeError):
        source_chat_id = None

    try:
        target_chat_id = int(tgt_raw)
    except (ValueError, TypeError):
        target_chat_id = None

    # Only process messages from the configured source channel
    if source_chat_id and msg.chat_id != source_chat_id:
        return
    if not target_chat_id:
        logger.warning("Target channel not set — ignoring message %d", msg.message_id)
        return

    # Skip already-posted messages (can happen if bot restarts mid-session)
    if db.was_posted(msg.chat_id, msg.message_id):
        logger.debug("msg %d already posted, skipping.", msg.message_id)
        return

    custom_tag = db.get("custom_tag", "")
    extra_tags = [t for t in (db.get("extra_tags") or "").split("|||") if t]

    await publish_media_message(
        bot=context.bot,
        msg=msg,
        target_chat_id=target_chat_id,
        tmdb_api_key=cfg.tmdb_api_key,
        omdb_api_key=cfg.omdb_api_key,
        api_timeout=cfg.api_timeout,
        channel_username=db.get("channel_username") or cfg.channel_username,
        channel_link=db.get("channel_link") or cfg.channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
        db=db,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Post-init: start scheduler
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Called once after the Application is initialised."""
    db:  Database = application.bot_data["db"]
    cfg: Config   = application.bot_data["config"]

    interval = db.get_int("interval_seconds", 600)
    jq = application.job_queue
    if jq:
        jq.run_repeating(
            _publisher_job_callback,
            interval=interval,
            first=10,        # first run 10 s after startup
            name="publisher_job",
        )
        logger.info("Scheduler started: posting every %d s", interval)
    else:
        logger.warning(
            "JobQueue not available. Install python-telegram-bot[job-queue] "
            "for scheduled posting."
        )

    logger.info(
        "Bot ready. Source: %s | Target: %s",
        db.get("source_channel_id") or cfg.source_channel_id or "not set",
        db.get("target_channel_id") or cfg.target_channel_id or "not set",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Application builder
# ─────────────────────────────────────────────────────────────────────────────

def build_application(cfg: Config) -> Application:
    db = Database(cfg.db_path)

    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .build()
    )

    # Shared state available in every handler
    app.bot_data["config"] = cfg
    app.bot_data["db"]     = db

    # ── Admin commands ───────────────────────────────────────────────────────
    register_admin_handlers(app)

    # ── Real-time source channel listener ────────────────────────────────────
    # Listen to both channel_post and forwarded messages (private/group sources)
    media_filter = (
        filters.VIDEO
        | filters.Document.ALL
        | filters.AUDIO
        | filters.ANIMATION
    )
    # Channel posts
    app.add_handler(
        MessageHandler(filters.ChatType.CHANNEL & media_filter, handle_source_message)
    )
    # Group / private messages (if source is a group or supergroup)
    app.add_handler(
        MessageHandler(
            (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & media_filter,
            handle_source_message,
        )
    )

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()

    # Pre-initialize asyncio event loop to prevent RuntimeError in ptb on Python 3.12/3.14+
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if sys.platform.lower().startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    cfg = load_config()
    app = build_application(cfg)

    logger.info("Starting Movie Publisher Bot (polling)…")
    app.run_polling(
        allowed_updates=["message", "channel_post", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
