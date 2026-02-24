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
    if msg.video:
        v = msg.video
        return v.file_id, v.file_name or f"video_{v.file_unique_id}.mp4", v.file_size, "video"
    if msg.document:
        d = msg.document
        return d.file_id, d.file_name or f"doc_{d.file_unique_id}", d.file_size, "document"
    if msg.audio:
        a = msg.audio
        return a.file_id, a.file_name or f"audio_{a.file_unique_id}.mp3", a.file_size, "audio"
    if msg.animation:
        an = msg.animation
        return an.file_id, an.file_name or f"anim_{an.file_unique_id}.gif", an.file_size, "animation"
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

    # ── 1. Fetch the source message ──────────────────────────────────────────
    try:
        msg: Message = await bot.forward_message(
            chat_id=target_chat_id,         # temp forward to extract info
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
            disable_notification=True,
        )
        # We forwarded it; now delete it — we will re-send with custom caption
        await msg.delete()
    except TelegramError as exc:
        logger.warning(
            "Could not forward msg %d from %d: %s — trying copy_message",
            source_msg_id, source_chat_id, exc
        )
        # Fallback: copy_message (works for private channels too)
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

    # ── 2. Get the original message object for file_id ───────────────────────
    # We forward to ourselves (bot private chat) to read the message object
    try:
        src_msg: Message = await bot.forward_message(
            chat_id=source_chat_id,      # won't work for channels we can't read
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
        )
    except TelegramError:
        src_msg = None

    # If forward to self failed, try getting from the target forward we made
    # In practice: use copy_message with a custom caption directly.
    # Below is the production path: fetch via getMessages API approach.
    try:
        actual_msg = await bot.get_message(
            chat_id=source_chat_id,
            message_id=source_msg_id,
        ) if hasattr(bot, "get_message") else None
    except Exception:
        actual_msg = None

    # ── 3. Build metadata + caption, then send ───────────────────────────────
    # Because get_message is not available in all versions, we use a robust
    # approach: send_document / send_video with file_id from copy approach.
    return await _publish_with_caption(
        bot=bot,
        source_chat_id=source_chat_id,
        source_msg_id=source_msg_id,
        target_chat_id=target_chat_id,
        tmdb_api_key=tmdb_api_key,
        omdb_api_key=omdb_api_key,
        api_timeout=api_timeout,
        channel_username=channel_username,
        channel_link=channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
        db=db,
    )


async def _publish_with_caption(
    *,
    bot: Bot,
    source_chat_id: int,
    source_msg_id: int,
    target_chat_id: int,
    tmdb_api_key: str,
    omdb_api_key: str,
    api_timeout: int,
    channel_username: str,
    channel_link: str,
    custom_tag: str,
    extra_tags: list[str] | None,
    db: Database | None,
) -> bool:
    """
    Core publish logic using copy_message + subsequent editMessageCaption.

    This is the most reliable zero-download method:
      1. copy_message to target (no file download, Telegram-side copy)
      2. Build rich caption from metadata
      3. editMessageCaption to replace the plain caption
    """

    # Step 1: copy_message (Telegram handles the file server-side)
    try:
        copied: Message = await bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
        )
    except TelegramError as exc:
        logger.error("copy_message failed (chat=%d msg=%d): %s", source_chat_id, source_msg_id, exc)
        return False

    # Step 2: We can't directly inspect the file from copy_message response.
    # Read the filename from the original channel if possible, otherwise use
    # the caption of the source message. We do a best-effort approach here.
    filename = ""
    file_size: int | None = None

    # Try to get file info via forwarding to a temp place  (admin's private chat)
    # Since we only have source_chat_id + msg_id, we use the caption text as title hint
    # and rely entirely on the guessit parse of the forwarded caption or message text.
    #
    # In a real deployment where the bot is admin in source channel:
    # Use Bot API getUpdates / webhook to catch the original message and store file_id.
    # The handle_source_message() function in main.py does exactly that.

    # Step 3: Build caption with whatever filename we have (may be empty → defaults)
    caption = await _build_caption_for(
        filename=filename,
        file_size=file_size,
        tmdb_api_key=tmdb_api_key,
        omdb_api_key=omdb_api_key,
        api_timeout=api_timeout,
        channel_username=channel_username,
        channel_link=channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
    )

    # Step 4: edit the copied message caption
    if caption:
        try:
            await bot.edit_message_caption(
                chat_id=target_chat_id,
                message_id=copied.message_id,
                caption=caption[:1024],
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            logger.warning("edit_message_caption failed: %s", exc)
            # Not fatal — file was already posted

    if db:
        db.log_post(source_chat_id, source_msg_id, target_chat_id, copied.message_id, filename)

    return True


async def _build_caption_for(
    *,
    filename: str,
    file_size: int | None,
    tmdb_api_key: str,
    omdb_api_key: str,
    api_timeout: int,
    channel_username: str,
    channel_link: str,
    custom_tag: str,
    extra_tags: list[str] | None,
) -> str:
    """Asynchronous caption builder."""
    cleaned = pre_clean_filename(filename) if filename else "Unknown"
    try:
        guess: dict[str, Any] = dict(guessit.guessit(cleaned))
    except Exception:
        guess = {}

    raw_title = str(guess.get("title") or Path(filename).stem if filename else "Unknown")
    raw_year  = int(guess.get("year")) if guess.get("year") else None
    ctype     = detect_content_type(filename or "", guess)

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

    return build_caption(
        content_type=ctype,
        meta=meta,
        guess=guess,
        file_size=file_size,
        channel_username=channel_username,
        channel_link=channel_link,
        custom_tag=custom_tag,
        extra_tags=extra_tags,
    )


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