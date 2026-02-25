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

def _main_keyboard(db: Database) -> InlineKeyboardMarkup:
    paused = db.get_bool("paused", False)
    autolive = db.get_bool("auto_forward", False)

    pause_btn = InlineKeyboardButton("▶️ Resume Queue", callback_data="cb_resume") if paused else InlineKeyboardButton("⏸ Pause Queue", callback_data="cb_pause")
    autolive_btn = InlineKeyboardButton("🔴 AutoLive: OFF", callback_data="cb_autolive_toggle") if not autolive else InlineKeyboardButton("🟢 AutoLive: ON", callback_data="cb_autolive_toggle")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status",    callback_data="cb_status"),
            InlineKeyboardButton("📈 Stats",     callback_data="cb_stats"),
        ],
        [
            pause_btn,
            autolive_btn,
        ],
        [
            InlineKeyboardButton("⏭ Skip Next",  callback_data="cb_skip"),
            InlineKeyboardButton("🧪 Test Post",  callback_data="cb_testpost"),
        ],
        [
            InlineKeyboardButton("🔗 Channels",  callback_data="cb_channels"),
            InlineKeyboardButton("🏷 Tags",       callback_data="cb_tags"),
            InlineKeyboardButton("🛡 Filters",    callback_data="cb_filters"),
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
    autolive = db.get_bool("auto_forward", False)
    interval = db.get_int("interval_seconds", 600)
    src      = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt      = db.get("target_channel_id") or str(cfg.target_channel_id)
    ptr      = db.get_int("current_msg_id", 0)
    posted   = db.total_posted()

    status_icon = "⏸ PAUSED" if paused else "▶️ RUNNING"
    live_icon   = "🟢 ON" if autolive else "🔴 OFF"

    text = (
        "🎬  <b>Movie Publisher Bot — Admin Panel</b>\n\n"
        "<blockquote>"
        f"<b>Queue Status :</b>  {status_icon}\n"
        f"<b>Auto-Live  :</b>  {live_icon}\n"
        f"<b>Source     :</b>  <code>{src}</code>\n"
        f"<b>Target     :</b>  <code>{tgt}</code>\n"
        f"<b>Interval   :</b>  <code>{fmt_interval(interval)}</code>\n"
        f"<b>Pointer    :</b>  msg_id <code>{ptr}</code>\n"
        f"<b>Posted     :</b>  <code>{posted}</code> files"
        "</blockquote>\n"
        "<i>Use the buttons below to control the bot.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_main_keyboard(db))


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
    "/pause      — Pause queue posting\n"
    "/resume     — Resume queue posting\n"
    "/skipnext   — Skip the next queued message\n"
    "/queue      — Show queue status\n"
    "/testpost   — Force-post now (ignore interval)\n\n"
    "<b>Live Auto-Forwarding</b>\n"
    "/autolive on   — Enable instant posting of new channel messages\n"
    "/autolive off  — Disable instant posting (queue only)\n\n"
    "<b>Caption & Tags</b>\n"
    "/settag ⚡ Powered by @Chan  — Set footer tag\n"
    "/cleartag   — Remove footer tag\n"
    "/addtag <text>   — Add extra tag line\n"
    "/removetag <text>— Remove a tag line\n"
    "/tags       — List all tags\n\n"
    "<b>Filters</b>\n"
    "/addfilter <word>  — Block messages containing this word\n"
    "/removefilter <word> — Unblock a word\n"
    "/filters    — List all active filters\n\n"
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
    autolive = db.get_bool("auto_forward", False)
    interval = db.get_int("interval_seconds", 600)
    src      = db.get("source_channel_id") or str(cfg.source_channel_id)
    tgt      = db.get("target_channel_id") or str(cfg.target_channel_id)
    ptr      = db.get_int("current_msg_id", 0)
    start_id = db.get_int("start_msg_id", 0)
    posted   = db.total_posted()
    last     = db.last_post_time() or "Never"
    tag      = db.get("custom_tag", "")
    extra    = [t for t in (db.get("extra_tags") or "").split("|||") if t]
    filters_lst = db.get_filters()

    tag_display  = tag if tag else "<i>none</i>"
    extra_display = "\n".join(f"  • {t}" for t in extra) if extra else "  <i>none</i>"
    filter_display = "\n".join(f"  • {f}" for f in filters_lst) if filters_lst else "  <i>none</i>"

    text = (
        "📊  <b>Full Bot Status</b>\n\n"
        "<blockquote>"
        f"<b>Queue State:</b> {'⏸ PAUSED' if paused else '▶️ RUNNING'}\n"
        f"<b>Auto-Live  :</b> {'🟢 ON' if autolive else '🔴 OFF'}\n"
        f"<b>Source     :</b> <code>{src}</code>\n"
        f"<b>Target     :</b> <code>{tgt}</code>\n"
        f"<b>Interval   :</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Start msg  :</b> <code>{start_id}</code>\n"
        f"<b>Current ptr:</b> <code>{ptr}</code>\n"
        f"<b>Total posts:</b> <code>{posted}</code>\n"
        f"<b>Last post  :</b> <code>{last}</code>\n"
        f"<b>Active Fltrs:</b> {len(filters_lst)}\n"
        f"<b>Footer tag :</b> {tag_display}\n"
        f"<b>Extra tags :</b>\n{extra_display}"
        "</blockquote>"
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
        "📈  <b>Posting Statistics</b>\n\n"
        "<blockquote>"
        f"<b>Total posted   :</b> <code>{posted}</code>\n"
        f"<b>Last post      :</b> <code>{last}</code>\n"
        f"<b>Current interval:</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Estimated/day  :</b> <code>~{daily}</code>\n"
        f"<b>Estimated/week :</b> <code>~{weekly}</code>"
        "</blockquote>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /setsource  –  accepts forwarded message OR text with ID/link
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_setsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    msg = update.message

    # Case 1: message is forwarded from a channel (PTB v21+ uses forward_origin)
    chat_id = None
    if hasattr(msg, "forward_origin") and msg.forward_origin:
        if getattr(msg.forward_origin, "chat", None):
            chat_id = msg.forward_origin.chat.id
    elif getattr(msg, "forward_from_chat", None):
        chat_id = msg.forward_from_chat.id

    if chat_id:
        db.set("source_channel_id", chat_id)
        await msg.reply_text(
            f"✅ Source channel set to <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None):
        await msg.reply_text("❌ Could not read the source channel. Channel ID is hidden due to privacy settings. Try pasting the ID/link.")
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

    chat_id = None
    if hasattr(msg, "forward_origin") and msg.forward_origin:
        if getattr(msg.forward_origin, "chat", None):
            chat_id = msg.forward_origin.chat.id
    elif getattr(msg, "forward_from_chat", None):
        chat_id = msg.forward_from_chat.id

    if chat_id:
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

    # Case 1: forwarded message — capture its original message_id and chat (PTB v21 uses forward_origin)
    chat_id = None
    msg_id = None

    if hasattr(msg, "forward_origin") and msg.forward_origin:
        origin = msg.forward_origin
        if getattr(origin, "chat", None):
            chat_id = origin.chat.id
            msg_id = getattr(origin, "message_id", None)
    elif getattr(msg, "forward_from_chat", None):
        chat_id = msg.forward_from_chat.id
        msg_id  = getattr(msg, "forward_from_message_id", None)

    if chat_id and msg_id:
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

    if getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None):
        await msg.reply_text("❌ Could not read the source channel from this forward (likely cloaked by privacy settings). Please paste the t.me link instead.")
        return

    # Case 2: t.me link or raw msg ID
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

    if raw.isdigit():
        msg_id = int(raw)
        chat_id = db.get("source_channel_id")
        if chat_id:
            db.set("start_msg_id", msg_id)
            db.set("current_msg_id", msg_id)
            await msg.reply_text(
                f"✅  Start message ID set to <code>{msg_id}</code>!\n\n"
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
        "  /setstart https://t.me/ChannelName/99\n\n"
        "Option 3 — Paste the message ID directly (if source channel is set):\n"
        "  /setstart 99",
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
            first=5, # start in 5 seconds to give immediate feedback
            name="publisher_job",
        )

    await update.message.reply_text(
        f"✅  Interval set to <b>{fmt_interval(seconds)}</b>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /pause  /resume  /autolive
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    db.set("paused", "true")
    text = "⏸  Bot paused. Posts will not be sent until /resume."
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=_main_keyboard(db))
    else:
        await update.message.reply_text(text)


@_admin_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    db.set("paused", "false")
    # Ensure the scheduler job is running
    _ensure_job(context)
    text = "▶️  Bot resumed! Next post in the scheduled interval."
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=_main_keyboard(db))
    else:
        await update.message.reply_text(text)


@_admin_only
async def cmd_autolive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip().lower()

    if raw == "on":
        db.set("auto_forward", "true")
        await update.message.reply_text("🟢  <b>Auto-Live:</b> ON\n\nNew messages posted to the source channel will now be forwarded immediately.", parse_mode=ParseMode.HTML)
    elif raw == "off":
        db.set("auto_forward", "false")
        await update.message.reply_text("🔴  <b>Auto-Live:</b> OFF\n\nThe bot will now <i>only</i> post from the queue scheduler (/setstart).", parse_mode=ParseMode.HTML)
    else:
        state = "🟢 ON" if db.get_bool("auto_forward", False) else "🔴 OFF"
        await update.message.reply_text(
            f"<b>Auto-Live Status:</b> {state}\n\n"
            "Use <code>/autolive on</code> to instantly forward new posts.\n"
            "Use <code>/autolive off</code> to disable this and only use the scheduled queue.",
            parse_mode=ParseMode.HTML
        )

@_admin_only
async def cmd_autolive_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    current = db.get_bool("auto_forward", False)
    new_state = not current
    db.set("auto_forward", "true" if new_state else "false")
    
    state_str = "🟢 ON" if new_state else "🔴 OFF"
    text = f"<b>Auto-Live toggled to:</b> {state_str}"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=_main_keyboard(db))
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
        "🗂  <b>Queue Status</b>\n\n"
        "<blockquote>"
        f"<b>State       :</b> {'⏸ Paused' if paused else '▶️ Running'}\n"
        f"<b>Start msg   :</b> <code>{start_id}</code>\n"
        f"<b>Current ptr :</b> <code>{ptr}</code> (next to post)\n"
        f"<b>Interval    :</b> <code>{fmt_interval(interval)}</code>\n"
        f"<b>Next post ~  :</b> <code>{next_str}</code>"
        "</blockquote>"
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
# Filter commands
# ─────────────────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_addfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip().lower()
    if not raw:
        await update.message.reply_text("Usage: /addfilter <keyword or phrase>", parse_mode=ParseMode.HTML)
        return
    db.add_filter(raw)
    await update.message.reply_text(f"✅  Filter added. Messages containing <code>{raw}</code> will be ignored.", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_removefilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    raw = " ".join(context.args or []).strip().lower()
    if not raw:
        await update.message.reply_text("Usage: /removefilter <keyword>", parse_mode=ParseMode.HTML)
        return
    db.remove_filter(raw)
    await update.message.reply_text(f"🗑  Filter removed: <code>{raw}</code>", parse_mode=ParseMode.HTML)


@_admin_only
async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    banned = db.get_filters()
    banned_str = "\n".join(f"  • <code>{f}</code>" for f in banned) if banned else "  <i>none</i>"
    await update.message.reply_text(
        f"🛡  <b>Active Banned Keywords</b>\n\n{banned_str}\n\nAny message containing these words in their caption or filename will NOT be forwarded.",
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
        "cb_autolive_toggle": cmd_autolive_toggle,
        "cb_skip":     cmd_skipnext,
        "cb_testpost": cmd_testpost,
        "cb_channels": cmd_channels,
        "cb_tags":     cmd_tags,
        "cb_filters":  cmd_filters,
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

    # Check filters before doing anything heavy
    msg = None
    try:
        msg = await context.bot.forward_message(
            chat_id=context.bot.id, # forward to self to inspect
            from_chat_id=source_chat_id,
            message_id=ptr,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(f"Failed to pre-fetch message {ptr} for filter check: {e}")

    if msg:
        from publisher import _extract_media
        media_info = _extract_media(msg)
        caption_text = (msg.caption or msg.text or "").lower()
        file_name = (media_info[1] if media_info else "").lower()
        search_str = f"{caption_text} {file_name}"
        
        banned_words = db.get_filters()
        for word in banned_words:
            if word in search_str:
                logger.info("msg_id %d blocked by filter: '%s', advancing pointer.", ptr, word)
                db.set("current_msg_id", ptr + 1)
                try:
                    await msg.delete()
                except:
                    pass
                return

        try:
            await msg.delete()
        except:
            pass

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
        ("setchannel",  cmd_setsource), # Alias
        ("settarget",   cmd_settarget),
        ("channels",    cmd_channels),
        ("setstart",    cmd_setstart),
        ("interval",    cmd_interval),
        ("pause",       cmd_pause),
        ("resume",      cmd_resume),
        ("autolive",    cmd_autolive),
        ("skipnext",    cmd_skipnext),
        ("testpost",    cmd_testpost),
        ("queue",       cmd_queue),
        ("settag",      cmd_settag),
        ("cleartag",    cmd_cleartag),
        ("addtag",      cmd_addtag),
        ("removetag",   cmd_removetag),
        ("tags",        cmd_tags),
        ("addfilter",   cmd_addfilter),
        ("removefilter",cmd_removefilter),
        ("filters",     cmd_filters),
        ("addadmin",    cmd_addadmin),
        ("removeadmin", cmd_removeadmin),
        ("admins",      cmd_admins),
    ]
    for cmd_name, fn in cmds:
        app.add_handler(CommandHandler(cmd_name, fn))

    app.add_handler(CallbackQueryHandler(handle_callback))
