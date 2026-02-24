"""
admin.py  –  Full Telegram Admin Dashboard
══════════════════════════════════════════════════════════════════════

All admin commands
──────────────────
/start            Show the main admin dashboard menu
/help             Full command reference

— Channel config —
/setsource        Set source channel (reply-forward a msg or paste ID/link)
/settarget        Set target channel (ID or username)
/channels         Show current source + target channels

— Queue & scheduling —
/setstart         Mark the start message (forward it or paste t.me link)
/interval <val>   Set posting interval  e.g. /interval 10m  /interval 2h
/pause            Pause the posting queue
/resume           Resume the posting queue
/skipnext         Skip the next message in the queue
/queue            Show queue status (current pointer, interval, next post)

— Caption / tags —
/settag <text>    Set a custom footer tag  e.g. /settag ⚡ Powered by @Chan
/cleartag         Remove the custom tag
/addtag <text>    Add an extra tag line
/removetag <text> Remove a specific extra tag
/tags             List all current tags

— Admin management —
/addadmin <id>    Grant admin rights to a user
/removeadmin <id> Revoke admin rights
/admins           List all current admins

— Stats & info —
/status           Full bot status dashboard
/stats            Posting statistics
/testpost         Force-post the next queued message right now (skip interval)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from database import Database
    from config import Config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Auth guard
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(user_id: int, cfg: "Config", db: "Database") -> bool:
    return user_id in cfg.admin_ids or user_id in db.extra_admins()


def _admin_only(func):
    """Decorator: silently ignore non-admin users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        cfg: Config   = context.bot_data["config"]
        db:  Database = context.bot_data["db"]
        if not _is_admin(uid, cfg, db):
            await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Interval parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_interval(text: str) -> int | None:
    """
    Parse human interval string → seconds.
    Examples: "10m", "2h", "30s", "1h30m", "90"
    Returns None if unparseable.
    """
    text = text.strip().lower()
    total = 0
    pattern = re.findall(r"(\d+)\s*([smhd]?)", text)
    if not pattern:
        return None
    for value, unit in pattern:
        n = int(value)
        if unit in ("", "s"):
            total += n
        elif unit == "m":
            total += n * 60
        elif unit == "h":
            total += n * 3600
        elif unit == "d":
            total += n * 86400
    return total if total > 0 else None


def fmt_interval(seconds: int) -> str:
    """Pretty-print seconds as "Xh Ym Zs"."""
    td = timedelta(seconds=seconds)
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    d      = td.days
    parts  = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Link / ID parsers
# ─────────────────────────────────────────────────────────────────────────────

def parse_tme_link(text: str) -> tuple[int, int] | None:
    """
    Parse a t.me message link into (chat_id, message_id).

    Supported formats:
      https://t.me/c/1234567890/99          → private channel (-1001234567890, 99)
      https://t.me/ChannelUsername/99        → public channel (username stored as str)
      https://t.me/channelname/thread/99     → topic (skipped for now)
    """
    m = re.search(r"t\.me/c/(\d+)/(\d+)", text)
    if m:
        chat_id  = -1000000000000 - int(m.group(1))   # reconstruct -100… ID
        msg_id   = int(m.group(2))
        return chat_id, msg_id

    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", text)
    if m:
        username = "@" + m.group(1)
        msg_id   = int(m.group(2))
        # Return username as a sentinel — caller resolves it
        return username, msg_id  # type: ignore[return-value]

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard keyboard
# ─────────────────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status",    callback_data="cb_status"),
            InlineKeyboardButton("📈 Stats",     callback_data="cb_stats"),
        ],
        [
            InlineKeyboardButton("▶️ Resume",    callback_data="cb_resume"),
            InlineKeyboardButton("⏸ Pause",      callback_data="cb_pause"),
        ],
        [
            InlineKeyboardButton("⏭ Skip Next",  callback_data="cb_skip"),
            InlineKeyboardButton("🧪 Test Post",  callback_data="cb_testpost"),
        ],
        [
            InlineKeyboardButton("🔗 Channels",  callback_data="cb_channels"),
            InlineKeyboardButton("🏷 Tags",       callback_data="cb_tags"),
        ],
        [
            InlineKeyboardButton("❓ Help",       callback_data="cb_help"),
        ],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# /start — main dashboard
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    cfg: Config  = context.bot_data["config"]

    paused   = db.get_bool("paused", False)
    interval = db.get_int("interval_seconds", 600)
    src      = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt      = db.get("target_channel_id") or str(cfg.target_channel_id)
    ptr      = db.get_int("current_msg_id", 0)
    posted   = db.total_posted()

    status_icon = "⏸ PAUSED" if paused else "▶️ RUNNING"

    text = (
        "🎬  <b>Movie Publisher Bot — Admin Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Status   :</b>  {status_icon}\n"
        f"<b>Source   :</b>  <code>{src}</code>\n"
        f"<b>Target   :</b>  <code>{tgt}</code>\n"
        f"<b>Interval :</b>  <code>{fmt_interval(interval)}</code>\n"
        f"<b>Pointer  :</b>  msg_id <code>{ptr}</code>\n"
        f"<b>Posted   :</b>  <code>{posted}</code> files\n\n"
        "Use the buttons below or type /help for all commands."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "📖  <b>Admin Command Reference</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Channel Config</b>\n"
    "/setsource  — Set source channel\n"
    "   → Forward any message from it, or paste channel ID / t.me link\n"
    "/settarget  — Set target channel\n"
    "   → Paste channel ID or @username\n"
    "/channels   — Show current channels\n\n"
    "<b>Queue & Scheduling</b>\n"
    "/setstart   — Set the start message\n"
    "   → Forward the message you want to start from\n"
    "   → Or paste its t.me link\n"
    "/interval 10m  — Post every 10 minutes\n"
    "/interval 2h   — Post every 2 hours\n"
    "/interval 30s  — Post every 30 seconds\n"
    "/pause      — Pause posting\n"
    "/resume     — Resume posting\n"
    "/skipnext   — Skip the next queued message\n"
    "/queue      — Show queue status\n"
    "/testpost   — Force-post now (ignore interval)\n\n"
    "<b>Caption & Tags</b>\n"
    "/settag ⚡ Powered by @Chan  — Set footer tag\n"
    "/cleartag   — Remove footer tag\n"
    "/addtag &lt;text&gt;   — Add extra tag line\n"
    "/removetag &lt;text&gt;— Remove a tag line\n"
    "/tags       — List all tags\n\n"
    "<b>Admin Management</b>\n"
    "/addadmin 123456   — Grant admin\n"
    "/removeadmin 123456— Revoke admin\n"
    "/admins     — List all admins\n\n"
    "<b>Info</b>\n"
    "/status     — Full status\n"
    "/stats      — Posting statistics\n"
)


@_admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database  = context.bot_data["db"]
    cfg: Config   = context.bot_data["config"]

    paused   = db.get_bool("paused", False)
    interval = db.get_int("interval_seconds", 600)
    src      = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt      = db.get("target_channel_id") or str(cfg.target_channel_id)
    ptr      = db.get_int("current_msg_id", 0)
    start_id = db.get_int("start_msg_id", 0)
    posted   = db.total_posted()
    last     = db.last_post_time() or "Never"
    tag      = db.get("custom_tag", "")
    extra    = [t for t in (db.get("extra_tags") or "").split("|||") if t]

    tag_display  = tag if tag else "<i>none</i>"
    extra_display = "\n".join(f"  • {t}" for t in extra) if extra else "  <i>none</i>"

    text = (
        "📊  <b>Full Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>State      :</b> {'⏸ PAUSED' if paused else '▶️ RUNNING'}\n"
        f"<b>Source     :</b> <code>{src}</code>\n"
        f"<b>Target     :</b> <code>{tgt}</code>\n"
        f"<b>Interval   :</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Start msg  :</b> <code>{start_id}</code>\n"
        f"<b>Current ptr:</b> <code>{ptr}</code>\n"
        f"<b>Total posts:</b> <code>{posted}</code>\n"
        f"<b>Last post  :</b> <code>{last}</code>\n"
        f"<b>Footer tag :</b> {tag_display}\n"
        f"<b>Extra tags :</b>\n{extra_display}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    posted   = db.total_posted()
    last     = db.last_post_time() or "Never"
    interval = db.get_int("interval_seconds", 600)

    if posted > 0 and interval > 0:
        daily = int(86400 / interval)
        weekly = daily * 7
    else:
        daily = weekly = 0

    text = (
        "📈  <b>Posting Statistics</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Total posted   :</b> <code>{posted}</code>\n"
        f"<b>Last post      :</b> <code>{last}</code>\n"
        f"<b>Current interval:</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Estimated/day  :</b> <code>~{daily}</code>\n"
        f"<b>Estimated/week :</b> <code>~{weekly}</code>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /setsource  –  accepts forwarded message OR text with ID/link
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_setsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    msg = update.message

    # Case 1: message is forwarded from a channel
    if msg.forward_from_chat:
        chat_id = msg.forward_from_chat.id
        db.set("source_channel_id", chat_id)
        await msg.reply_text(
            f"✅ Source channel set to <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Case 2: user typed an ID or t.me link
    raw = " ".join(context.args or []).strip() if context.args else (msg.text or "").strip()
    raw = raw.replace("/setsource", "").strip()

    if "t.me" in raw:
        parsed = parse_tme_link(raw)
        if parsed:
            chat_id = parsed[0]
            db.set("source_channel_id", chat_id)
            await msg.reply_text(
                f"✅ Source channel set to <code>{chat_id}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    if raw.lstrip("-").isdigit():
        db.set("source_channel_id", raw)
        await msg.reply_text(
            f"✅ Source channel set to <code>{raw}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Username
    if raw.startswith("@"):
        db.set("source_channel_id", raw)
        await msg.reply_text(
            f"✅ Source channel set to <code>{raw}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await msg.reply_text(
        "❓  Send me the source channel in one of these ways:\n"
        "• Forward any message from it\n"
        "• Paste the channel ID (e.g. <code>-1001234567890</code>)\n"
        "• Paste the @username\n"
        "• Paste a t.me message link",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /settarget
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_settarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    msg = update.message

    if msg.forward_from_chat:
        chat_id = msg.forward_from_chat.id
        db.set("target_channel_id", chat_id)
        await msg.reply_text(
            f"✅ Target channel set to <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = " ".join(context.args or []).strip() if context.args else ""
    if raw:
        db.set("target_channel_id", raw)
        await msg.reply_text(
            f"✅ Target channel set to <code>{raw}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await msg.reply_text(
        "Usage: /settarget <code>-1001234567890</code> or <code>@username</code>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /channels
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    cfg: Config  = context.bot_data["config"]
    src = db.get("source_channel_id") or str(cfg.source_channel_id) or "<i>not set</i>"
    tgt = db.get("target_channel_id") or str(cfg.target_channel_id) or "<i>not set</i>"
    await update.message.reply_text(
        f"🔗 <b>Channel Config</b>\n\n"
        f"<b>Source :</b> <code>{src}</code>\n"
        f"<b>Target :</b> <code>{tgt}</code>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /setstart  –  set the start-from message
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    msg = update.message

    # Case 1: forwarded message — capture its original message_id and chat
    if msg.forward_from_chat and msg.forward_from_message_id:
        chat_id = msg.forward_from_chat.id
        msg_id  = msg.forward_from_message_id
        db.set("source_channel_id", chat_id)
        db.set("start_msg_id",      msg_id)
        db.set("current_msg_id",    msg_id)
        await msg.reply_text(
            f"✅  Start message set!\n\n"
            f"<b>Channel :</b> <code>{chat_id}</code>\n"
            f"<b>Msg ID  :</b> <code>{msg_id}</code>\n\n"
            "The bot will begin posting from this message.\n"
            "Use /resume to start the queue.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Case 2: t.me link
    raw = " ".join(context.args or []).strip()
    if not raw:
        raw = (msg.text or "").replace("/setstart", "").strip()

    if "t.me" in raw:
        parsed = parse_tme_link(raw)
        if parsed:
            chat_id, msg_id = parsed
            db.set("source_channel_id", chat_id)
            db.set("start_msg_id",      msg_id)
            db.set("current_msg_id",    msg_id)
            await msg.reply_text(
                f"✅  Start message set from link!\n\n"
                f"<b>Channel :</b> <code>{chat_id}</code>\n"
                f"<b>Msg ID  :</b> <code>{msg_id}</code>\n\n"
                "Use /resume to start.",
                parse_mode=ParseMode.HTML,
            )
            return

    await msg.reply_text(
        "📌  <b>How to set a start message:</b>\n\n"
        "Option 1 — Forward the message:\n"
        "  Go to the source channel, find the message you want\n"
        "  to start from, and <b>forward it to this bot</b>.\n\n"
        "Option 2 — Paste the t.me link:\n"
        "  /setstart https://t.me/c/1234567890/99\n"
        "  /setstart https://t.me/ChannelName/99",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /interval
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()

    if not raw:
        current = db.get_int("interval_seconds", 600)
        await update.message.reply_text(
            f"⏱  Current interval: <b>{fmt_interval(current)}</b>\n\n"
            "Usage: /interval &lt;value&gt;\n"
            "Examples:\n"
            "  /interval 30s\n"
            "  /interval 10m\n"
            "  /interval 2h\n"
            "  /interval 1h30m\n"
            "  /interval 1d",
            parse_mode=ParseMode.HTML,
        )
        return

    seconds = parse_interval(raw)
    if seconds is None:
        await update.message.reply_text(
            "❌  Could not parse that interval.\n"
            "Examples: <code>30s</code>  <code>10m</code>  <code>2h</code>  <code>1d</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    db.set("interval_seconds", seconds)

    # Reschedule the job
    jq = context.job_queue
    if jq:
        for job in jq.get_jobs_by_name("publisher_job"):
            job.schedule_removal()
        jq.run_repeating(
            _publisher_job_callback,
            interval=seconds,
            first=seconds,
            name="publisher_job",
        )

    await update.message.reply_text(
        f"✅  Interval set to <b>{fmt_interval(seconds)}</b>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /pause  /resume
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    db.set("paused", "true")
    await update.message.reply_text("⏸  Bot paused. Posts will not be sent until /resume.")


@_admin_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    db.set("paused", "false")
    # Ensure the scheduler job is running
    _ensure_job(context)
    await update.message.reply_text("▶️  Bot resumed! Next post in the scheduled interval.")


# ─────────────────────────────────────────────────────────────────────────────
# /queue
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    paused   = db.get_bool("paused", False)
    interval = db.get_int("interval_seconds", 600)
    ptr      = db.get_int("current_msg_id", 0)
    start_id = db.get_int("start_msg_id", 0)

    if interval > 0:
        next_post = datetime.utcnow() + timedelta(seconds=interval)
        next_str  = next_post.strftime("%H:%M:%S UTC")
    else:
        next_str = "N/A"

    text = (
        "🗂  <b>Queue Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>State       :</b> {'⏸ Paused' if paused else '▶️ Running'}\n"
        f"<b>Start msg   :</b> <code>{start_id}</code>\n"
        f"<b>Current ptr :</b> <code>{ptr}</code> (next to post)\n"
        f"<b>Interval    :</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Next post ~  :</b> <code>{next_str}</code>\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /skipnext
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_skipnext(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    ptr = db.get_int("current_msg_id", 0)
    if ptr > 0:
        db.set("current_msg_id", ptr + 1)
        await update.message.reply_text(
            f"⏭  Skipped msg_id <code>{ptr}</code>. Next post will be <code>{ptr + 1}</code>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("❓  No start message set. Use /setstart first.")


# ─────────────────────────────────────────────────────────────────────────────
# /testpost  –  force-post the next item immediately
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_testpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🧪  Force-posting next message now…")
    await _publisher_job_callback(context)


# ─────────────────────────────────────────────────────────────────────────────
# Tag commands
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_settag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text(
            "Usage: /settag ⚡ Powered by @MyChannel",
            parse_mode=ParseMode.HTML,
        )
        return
    db.set("custom_tag", raw)
    await update.message.reply_text(f"✅  Footer tag set:\n<b>{raw}</b>", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_cleartag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    db.delete("custom_tag")
    await update.message.reply_text("🗑  Footer tag cleared.")


@_admin_only
async def cmd_addtag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text("Usage: /addtag &lt;text&gt;", parse_mode=ParseMode.HTML)
        return
    existing = db.get("extra_tags") or ""
    tags = [t for t in existing.split("|||") if t]
    if raw not in tags:
        tags.append(raw)
    db.set("extra_tags", "|||".join(tags))
    await update.message.reply_text(f"✅  Tag added: <b>{raw}</b>", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_removetag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()
    existing = db.get("extra_tags") or ""
    tags = [t for t in existing.split("|||") if t and t != raw]
    db.set("extra_tags", "|||".join(tags))
    await update.message.reply_text(f"🗑  Tag removed: <code>{raw}</code>", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    custom = db.get("custom_tag") or "<i>none</i>"
    extra  = [t for t in (db.get("extra_tags") or "").split("|||") if t]
    extra_str = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(extra)) if extra else "  <i>none</i>"
    await update.message.reply_text(
        f"🏷  <b>Current Tags</b>\n\n"
        f"<b>Footer tag :</b> {custom}\n\n"
        f"<b>Extra tags :</b>\n{extra_str}",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Admin management
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Usage: /addadmin &lt;user_id&gt;", parse_mode=ParseMode.HTML)
        return
    uid = int(raw)
    db.add_admin(uid)
    await update.message.reply_text(f"✅  <code>{uid}</code> is now an admin.", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Usage: /removeadmin &lt;user_id&gt;", parse_mode=ParseMode.HTML)
        return
    uid = int(raw)
    db.remove_admin(uid)
    await update.message.reply_text(f"🗑  <code>{uid}</code> removed from admins.", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database  = context.bot_data["db"]
    cfg: Config   = context.bot_data["config"]
    all_admins    = list(set(cfg.admin_ids + db.extra_admins()))
    lines         = "\n".join(f"  • <code>{uid}</code>" for uid in all_admins)
    await update.message.reply_text(
        f"👑  <b>Admins</b>\n{lines}",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inline button callbacks
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    handlers = {
        "cb_status":   cmd_status,
        "cb_stats":    cmd_stats,
        "cb_resume":   cmd_resume,
        "cb_pause":    cmd_pause,
        "cb_skip":     cmd_skipnext,
        "cb_testpost": cmd_testpost,
        "cb_channels": cmd_channels,
        "cb_tags":     cmd_tags,
        "cb_help":     cmd_help,
    }
    fn = handlers.get(data)
    if fn:
        # patch update so handler thinks it's a message reply
        update._effective_message = query.message  # type: ignore[attr-defined]
        update.message = query.message              # type: ignore[assignment]
        await fn(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# Publisher job (called by scheduler)
# ─────────────────────────────────────────────────────────────────────────────

async def _publisher_job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Scheduled job: fetch the next message_id from source channel and publish it.
    Increments current_msg_id on success (and skips non-media).
    """
    from publisher import publish_message

    db: Database  = context.bot_data["db"]
    cfg: "Config" = context.bot_data["config"]

    if db.get_bool("paused", False):
        logger.debug("Publisher job: paused, skipping.")
        return

    src_raw = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt_raw = db.get("target_channel_id") or str(cfg.target_channel_id)

    if not src_raw or src_raw in ("0", "None") or not tgt_raw or tgt_raw in ("0", "None"):
        logger.warning("Publisher job: source or target channel not set.")
        return

    try:
        source_chat_id = int(src_raw)
        target_chat_id = int(tgt_raw)
    except ValueError:
        source_chat_id = src_raw  # username
        target_chat_id = tgt_raw

    ptr = db.get_int("current_msg_id", 0)
    if ptr == 0:
        logger.info("Publisher job: no start message set.")
        return

    if db.was_posted(source_chat_id, ptr):
        logger.info("msg_id %d already posted, advancing pointer.", ptr)
        db.set("current_msg_id", ptr + 1)
        return

    custom_tag  = db.get("custom_tag", "")
    extra_raw   = db.get("extra_tags", "")
    extra_tags  = [t for t in extra_raw.split("|||") if t] if extra_raw else []

    logger.info("Publishing msg_id=%d from %s → %s", ptr, source_chat_id, target_chat_id)

    success = await publish_message(
        bot=context.bot,
        source_chat_id=source_chat_id,
        source_msg_id=ptr,
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

    if success:
        db.set("current_msg_id", ptr + 1)
    else:
        # Advance anyway to avoid getting stuck on a deleted/non-media message
        logger.warning("msg_id %d failed, advancing pointer anyway.", ptr)
        db.set("current_msg_id", ptr + 1)


def _ensure_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the publisher job if not already running."""
    db: Database = context.bot_data["db"]
    interval = db.get_int("interval_seconds", 600)
    jq = context.job_queue
    if not jq:
        return
    existing = jq.get_jobs_by_name("publisher_job")
    if not existing:
        jq.run_repeating(
            _publisher_job_callback,
            interval=interval,
            first=interval,
            name="publisher_job",
        )
        logger.info("Publisher job scheduled every %s", fmt_interval(interval))


# ─────────────────────────────────────────────────────────────────────────────
# Register all handlers
# ─────────────────────────────────────────────────────────────────────────────

def register_admin_handlers(app) -> None:
    """Attach all admin command handlers to the PTB Application."""
    cmds = [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("status",      cmd_status),
        ("stats",       cmd_stats),
        ("setsource",   cmd_setsource),
        ("settarget",   cmd_settarget),
        ("channels",    cmd_channels),
        ("setstart",    cmd_setstart),
        ("interval",    cmd_interval),
        ("pause",       cmd_pause),
        ("resume",      cmd_resume),
        ("skipnext",    cmd_skipnext),
        ("testpost",    cmd_testpost),
        ("queue",       cmd_queue),
        ("settag",      cmd_settag),
        ("cleartag",    cmd_cleartag),
        ("addtag",      cmd_addtag),
        ("removetag",   cmd_removetag),
        ("tags",        cmd_tags),
        ("addadmin",    cmd_addadmin),
        ("removeadmin", cmd_removeadmin),
        ("admins",      cmd_admins),
    ]
    for cmd_name, fn in cmds:
        app.add_handler(CommandHandler(cmd_name, fn))

    app.add_handler(CallbackQueryHandler(handle_callback))