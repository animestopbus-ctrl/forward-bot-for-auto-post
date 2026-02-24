"""
caption.py  –  Content-type detection + HTML caption builder
═════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from utils import detect_languages, format_size, resolution_from_guess

# ─────────────────────────────────────────────────────────────────────────────
# Header map  (exact spec)
# ─────────────────────────────────────────────────────────────────────────────

HEADER_MAP: dict[str, tuple[str, str, str]] = {
    "kdrama": ("🎭 <b>𝗞-𝗗𝗥𝗔𝗠𝗔 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> 🎭", "🍿", "🇰🇷"),
    "cdrama": ("🏮 <b>𝗖-𝗗𝗥𝗔𝗠𝗔 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> 🏮", "🍿", "🇨🇳"),
    "jdrama": ("🎌 <b>𝗝-𝗗𝗥𝗔𝗠𝗔 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> 🎌", "🍿", "🇯🇵"),
    "indian": ("🪷 <b>𝗜𝗡𝗗𝗜𝗔𝗡 𝗖𝗜𝗡𝗘𝗠𝗔</b> 🪷",  "🎥", "🇮🇳"),
    "kmovie": ("🎬 <b>𝗞𝗢𝗥𝗘𝗔𝗡 𝗠𝗢𝗩𝗜𝗘</b> 🎬",  "🎥", "🇰🇷"),
    "jmovie": ("👹 <b>𝗝𝗔𝗣𝗔𝗡𝗘𝗦𝗘 𝗠𝗢𝗩𝗜𝗘</b> 👹", "🎥", "🇯🇵"),
    "anime":  ("✨ <b>𝗔𝗡𝗜𝗠𝗘 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> ✨",  "⛩️", "🎌"),
    "series": ("📺 <b>𝗦𝗘𝗥𝗜𝗘𝗦 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> 📺", "🍿", "⭐"),
    "movie":  ("🎬 <b>𝗠𝗢𝗩𝗜𝗘 𝗘𝗗𝗜𝗧𝗜𝗢𝗡</b> 🎬",  "🎥", "⭐"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Content-type detection
# ─────────────────────────────────────────────────────────────────────────────

_KW: dict[str, re.Pattern] = {
    "anime":  re.compile(r"\b(anime|アニメ|ova|ona|oav)\b", re.I),
    "kdrama": re.compile(r"\b(kdrama|k-drama|korean[\s_-]*drama)\b", re.I),
    "cdrama": re.compile(r"\b(cdrama|c-drama|chinese[\s_-]*drama|华剧|陆剧)\b", re.I),
    "jdrama": re.compile(r"\b(jdrama|j-drama|japanese[\s_-]*drama|ドラマ)\b", re.I),
    "kmovie": re.compile(r"\b(korean[\s_-]*movie|k-?movie)\b", re.I),
    "jmovie": re.compile(r"\b(japanese[\s_-]*movie|j-?movie)\b", re.I),
    "indian": re.compile(
        r"\b(bollywood|tollywood|kollywood|mollywood|"
        r"hindi|tamil|telugu|malayalam|kannada|bengali|marathi|punjabi)\b", re.I
    ),
}


def detect_content_type(filename: str, guess: dict[str, Any]) -> str:
    name = filename.lower()
    for ctype, pattern in _KW.items():
        if pattern.search(name):
            return ctype
    g_type = str(guess.get("type", "movie")).lower()
    return "series" if g_type == "episode" else "movie"


# ─────────────────────────────────────────────────────────────────────────────
# Caption builder
# ─────────────────────────────────────────────────────────────────────────────

def build_caption(
    *,
    content_type: str,
    meta: dict[str, Any],
    guess: dict[str, Any],
    file_size: int | None,
    channel_username: str,
    channel_link: str,
    custom_tag: str = "",          # e.g. "⚡ Powered by @MyChannel"
    extra_tags: list[str] | None = None,
) -> str:
    """
    Assemble the full HTML caption.

    Structure
    ─────────
    <EDITION HEADER>

    <blockquote>
    🎬 Title  🏴
    ├ 📅 Year      : …
    ├ ⭐ Rating    : …
    ├ 🎭 Genre     : …
    ├ 🗣 Language  : …
    ├ 📽 Quality   : …
    ├ 💾 Size      : …
    ├ ⏱ Runtime   : …
    ├ 🎬 Director  : …
    ├ 🌟 Cast      : …
    ╰ 🗂 Source    : …

    📖 Synopsis:
    <i>…</i>
    </blockquote>

    ━━━
    Powered by / footer
    """
    h_data = HEADER_MAP.get(content_type, HEADER_MAP["movie"])
    header, media_emoji, flag_emoji = h_data

    title    = meta.get("title") or str(guess.get("title") or "Unknown")
    year     = meta.get("year") or str(guess.get("year") or "N/A")
    rating   = meta.get("rating", "N/A")
    genres   = meta.get("genres", "N/A")
    raw_ov   = meta.get("overview") or "No synopsis available."
    overview = raw_ov[:320] + ("…" if len(raw_ov) > 320 else "")
    director = meta.get("director", "N/A")
    cast     = meta.get("cast", "N/A")
    runtime  = meta.get("runtime", "N/A")
    country  = meta.get("country", "N/A")
    quality  = resolution_from_guess(guess)
    langs    = detect_languages(guess)
    size_str = format_size(file_size)
    src      = meta.get("source", "N/A")

    # Episode info
    season  = guess.get("season")
    episode = guess.get("episode")
    ep_str  = ""
    if season and episode:
        ep_str = f"S{int(season):02d}E{int(episode):02d}"
    elif episode:
        ep_str = f"EP {int(episode):02d}"

    lines: list[str] = [
        f"{header}",
        "",
        "<blockquote>",
        f"<b>{media_emoji}  {title}</b>  {flag_emoji}",
        "",
    ]

    if ep_str:
        lines.append(f"├ 🎞  <b>Episode  :</b>  <code>{ep_str}</code>")

    lines += [
        f"├ 📅  <b>Year     :</b>  <code>{year}</code>",
        f"├ ⭐  <b>Rating   :</b>  <code>{rating} / 10</code>",
        f"├ 🎭  <b>Genre    :</b>  <code>{genres}</code>",
        f"├ 🌍  <b>Country  :</b>  <code>{country}</code>",
        f"├ 🗣  <b>Language :</b>  <code>{langs}</code>",
        f"├ 📽  <b>Quality  :</b>  <code>{quality}</code>",
        f"├ 💾  <b>Size     :</b>  <code>{size_str}</code>",
        f"├ ⏱  <b>Runtime  :</b>  <code>{runtime}</code>",
    ]

    if director and director != "N/A":
        lines.append(f"├ 🎬  <b>Director :</b>  <code>{director}</code>")
    if cast and cast != "N/A":
        lines.append(f"├ 🌟  <b>Cast     :</b>  <code>{cast}</code>")

    lines += [
        f"╰ 🗂  <b>Source   :</b>  <code>{src}</code>",
        "",
        "📖  <b>Synopsis:</b>",
        f"<i>{overview}</i>",
        "</blockquote>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Footer tags
    if custom_tag:
        lines.append(f"<b>{custom_tag}</b>")
    if extra_tags:
        for tag in extra_tags:
            lines.append(f"<b>{tag}</b>")

    lines += [
        f"<b>{channel_username}</b>",
        f'🔔  <a href="{channel_link}">Join for more!</a>',
    ]

    return "\n".join(lines)