"""
publisher.py  –  Zero-download forwarder + metadata pipeline
═════════════════════════════════════════════════════════════

Core flow
─────────
1.  Receive (source_chat_id, message_id)
2.  Fetch that Message via bot.forward / copy_message approach — no file download
3.  Extract file_id, filename, file_size
4.  Run guessit on the cleaned filename
5.  Detect content type
6.  Fetch metadata through the 6-API cascade
7.  Build HTML caption
8.  Send to target channel via send_video / send_document (file_id only)
9.  Log to database
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import guessit
from telegram import Bot, Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

from caption import HEADER_MAP, build_caption, detect_content_type
from database import Database
from utils import fetch_smart_metadata, pre_clean_filename

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_media(msg: Message) -> tuple[str, str, int | None, str] | None:
    """
    Pull (file_id, filename, file_size, media_kind) from any media message.
    Returns None if the message has no supported media.
    """
    caption_hint = ""
    if getattr(msg, "caption", None):
        caption_hint = msg.caption.split('\n')[0].strip()
        # Remove any unwanted characters from caption_hint to make it a generic filename
        import re
        caption_hint = re.sub(r'[\\/*?:"<>|]', "", caption_hint)[:60]

    if msg.video:
        v = msg.video
        fn = getattr(v, "file_name", "") or ""
        if not fn or fn.startswith("video_") or "unknown" in fn.lower():
            fn = (caption_hint + ".mp4") if caption_hint else f"video_{v.file_unique_id}.mp4"
        return v.file_id, fn, v.file_size, "video"
    if msg.document:
        d = msg.document
        fn = getattr(d, "file_name", "") or ""
        if not fn or fn.startswith("doc_") or "unknown" in fn.lower():
            fn = (caption_hint + ".mkv") if caption_hint else f"doc_{d.file_unique_id}"
        return d.file_id, fn, d.file_size, "document"
    if msg.audio:
        a = msg.audio
        fn = getattr(a, "file_name", "") or ""
        if not fn or fn.startswith("audio_"):
            fn = (caption_hint + ".mp3") if caption_hint else f"audio_{a.file_unique_id}.mp3"
        return a.file_id, fn, a.file_size, "audio"
    if msg.animation:
        an = msg.animation
        fn = getattr(an, "file_name", "") or ""
        if not fn or fn.startswith("anim_"):
            fn = (caption_hint + ".gif") if caption_hint else f"anim_{an.file_unique_id}.gif"
        return an.file_id, fn, an.file_size, "animation"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main publisher coroutine
# ─────────────────────────────────────────────────────────────────────────────

async def publish_message(
    *,
    bot: Bot,
    source_chat_id: int,
    source_msg_id: int,
    target_chat_id: int,
    tmdb_api_key: str = "",
    omdb_api_key: str = "",
    api_timeout: int = 10,
    channel_username: str = "@YourChannel",
    channel_link: str = "https://t.me/YourChannel",
    custom_tag: str = "",
    extra_tags: list[str] | None = None,
    db: Database | None = None,
) -> bool:
    """
    Forward one media message from source → target with a rich caption.
    Returns True on success, False on failure.
    """
    # ── 1. Fetch the source message by forwarding temporarily ──
    try:
        temp_msg: Message = await bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
            disable_notification=True,
        )
    except TelegramError as exc:
        logger.warning(
            "Could not forward msg %d from %s: %s — skipping or trying fallback",
            source_msg_id, source_chat_id, exc
        )
        # Attempt simple copy_message if forward fails (no rich metadata)
        try:
            sent = await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_msg_id,
                parse_mode=ParseMode.HTML,
            )
            if db:
                db.log_post(source_chat_id, source_msg_id, target_chat_id, sent.message_id)
            return True
        except TelegramError as exc2:
            logger.error("copy_message also failed for msg %d: %s", source_msg_id, exc2)
            return False

    # ── 2. Extract media info ──
    media_info = _extract_media(temp_msg)
    
    if not media_info:
        # Not a media msg. Just keep the forward or send basic message.
        try:
            await temp_msg.delete()
        except TelegramError:
            pass
        return True

    file_id, filename, file_size, media_kind = media_info

    # ── 3. Build Metadata & Caption ──
    cleaned   = pre_clean_filename(filename) if filename else "Unknown"
    try:
        guess: dict[str, Any] = dict(guessit.guessit(cleaned))
    except Exception:
        guess = {}

    raw_title  = str(guess.get("title") or Path(filename).stem if filename else "Unknown")
    raw_year   = int(guess.get("year")) if guess.get("year") else None
    ctype      = detect_content_type(filename or "", guess)

    try:
        meta = await fetch_smart_metadata(
            title=raw_title,
            year=raw_year,
            content_type=ctype,
            tmdb_api_key=tmdb_api_key,
            omdb_api_key=omdb_api_key,
            timeout=api_timeout,
        )
    except Exception as exc:
        logger.error("Caption metadata fetch failed: %s", exc)
        meta = {}

    caption = build_caption(
        content_type=ctype,
        meta=meta,
        guess=guess,
        file_size=file_size,
        channel_username=channel_username,
        channel_link=channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
    )[:1024]

    # ── 4. Delete the temp forwarded message ──
    try:
        await temp_msg.delete()
    except TelegramError:
        pass

    # ── 5. Send properly with file_id ──
    send_kwargs = {
        "chat_id": target_chat_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
    }

    sent_msg: Message | None = None
    try:
        if media_kind == "video":
            sent_msg = await bot.send_video(video=file_id, **send_kwargs)
        elif media_kind == "audio":
            sent_msg = await bot.send_audio(audio=file_id, **send_kwargs)
        elif media_kind == "animation":
            sent_msg = await bot.send_animation(animation=file_id, **send_kwargs)
        else:
            sent_msg = await bot.send_document(document=file_id, **send_kwargs)
    except TelegramError as exc:
        logger.error("Primary send failed for '%s': %s — falling back to copy_message", filename, exc)
        try:
            sent_msg = await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_msg_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc2:
            logger.critical("Both send methods failed: %s", exc2)
            return False

    if db and sent_msg:
        db.log_post(source_chat_id, source_msg_id, target_chat_id, sent_msg.message_id, filename)

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Primary async publisher (used by handle_source_message in main.py)
# ─────────────────────────────────────────────────────────────────────────────

async def publish_media_message(
    *,
    bot: Bot,
    msg: Message,                   # original Message object from source channel
    target_chat_id: int,
    tmdb_api_key: str = "",
    omdb_api_key: str = "",
    api_timeout: int = 10,
    channel_username: str = "@YourChannel",
    channel_link: str = "https://t.me/YourChannel",
    custom_tag: str = "",
    extra_tags: list[str] | None = None,
    db: Database | None = None,
) -> bool:
    """
    Publish using the actual Message object (available in real-time webhook/polling).
    This is the PREFERRED path — gives us full file_id, filename, size.
    """
    media_info = _extract_media(msg)
    if not media_info:
        return False

    file_id, filename, file_size, media_kind = media_info
    logger.info("Publishing '%s' (kind=%s) → %d", filename, media_kind, target_chat_id)

    # ── Metadata ─────────────────────────────────────────────────────────────
    cleaned   = pre_clean_filename(filename)
    try:
        guess: dict[str, Any] = dict(guessit.guessit(cleaned))
    except Exception:
        guess = {}

    raw_title  = str(guess.get("title") or Path(filename).stem)
    raw_year   = int(guess.get("year")) if guess.get("year") else None
    ctype      = detect_content_type(filename, guess)

    meta = await fetch_smart_metadata(
        title=raw_title,
        year=raw_year,
        content_type=ctype,
        tmdb_api_key=tmdb_api_key,
        omdb_api_key=omdb_api_key,
        timeout=api_timeout,
    )

    caption = build_caption(
        content_type=ctype,
        meta=meta,
        guess=guess,
        file_size=file_size,
        channel_username=channel_username,
        channel_link=channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
    )[:1024]

    # ── Send (zero download — file_id only) ───────────────────────────────────
    send_kwargs: dict[str, Any] = {
        "chat_id":      target_chat_id,
        "caption":      caption,
        "parse_mode":   ParseMode.HTML,
    }

    sent_msg: Message | None = None
    try:
        if media_kind == "video":
            sent_msg = await bot.send_video(video=file_id, **send_kwargs)
        elif media_kind == "audio":
            sent_msg = await bot.send_audio(audio=file_id, **send_kwargs)
        elif media_kind == "animation":
            sent_msg = await bot.send_animation(animation=file_id, **send_kwargs)
        else:
            sent_msg = await bot.send_document(document=file_id, **send_kwargs)

        logger.info("✅ Posted '%s' → msg_id %s", filename, sent_msg.message_id if sent_msg else "?")

    except TelegramError as exc:
        logger.error("Primary send failed for '%s': %s — falling back to copy_message", filename, exc)
        try:
            sent_msg = await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc2:
            logger.critical("Both send methods failed for '%s': %s", filename, exc2)
            return False

    if db and sent_msg:
        db.log_post(msg.chat_id, msg.message_id, target_chat_id, sent_msg.message_id, filename)

    return True
