"""
config.py  –  Environment variable loader
═══════════════════════════════════════════════════════════════════════════

FREE API REFERENCE TABLE
──────────────────────────────────────────────────────────────────────────
API           Key?  Sign-up?  Limit                 Best for
────────────  ────  ────────  ────────────────────  ──────────────────────
TVMaze        NO ✅  NO ✅    20 req/10 s (soft)    TV shows & episodes
Jikan v4      NO ✅  NO ✅    60 req/min            Anime (MAL data)
Kitsu         NO ✅  NO ✅    Generous (unlisted)   Anime backup
AniList GQL   NO ✅  NO ✅    90 req/min            Anime (rich data)
TMDB          YES   Free reg  ~40 req/10 s          Movies + TV (best)
OMDb          YES   Email     1 000 req/day         Movies fallback
──────────────────────────────────────────────────────────────────────────
The bot works with ZERO keys using the top four.
Add TMDB/OMDb keys for even richer movie metadata.
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    bot_token: str
    # Comma-separated Telegram user IDs that may use admin commands
    admin_ids: List[int]

    # Default source / target channels (can be overridden via admin commands)
    source_channel_id: int   # negative int  e.g. -1001234567890
    target_channel_id: int

    # ── Optional API keys ─────────────────────────────────────────────────────
    # TMDB  → https://www.themoviedb.org/settings/api   (free, no CC)
    tmdb_api_key: str = ""
    # OMDb  → https://www.omdbapi.com/apikey.aspx       (free, email only)
    omdb_api_key: str = ""

    # ── Caption footer defaults ───────────────────────────────────────────────
    channel_username: str = "@YourChannel"
    channel_link: str     = "https://t.me/YourChannel"

    # ── Network ───────────────────────────────────────────────────────────────
    api_timeout: int = 10      # seconds per HTTP request

    # ── SQLite path ───────────────────────────────────────────────────────────
    db_path: str = "bot_state.db"


def load_config() -> Config:
    """Build Config from environment variables; raise on missing required ones."""

    def require(key: str) -> str:
        v = os.environ.get(key, "").strip()
        if not v:
            raise KeyError(
                f"Required env variable '{key}' is not set.\n"
                "Copy .env.example → .env and fill in your values."
            )
        return v

    def opt(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    raw_admins = opt("ADMIN_IDS", "")
    admin_ids: List[int] = (
        [int(x.strip()) for x in raw_admins.split(",") if x.strip().lstrip("-").isdigit()]
        if raw_admins else []
    )

    return Config(
        bot_token=require("BOT_TOKEN"),
        admin_ids=admin_ids,
        source_channel_id=int(opt("SOURCE_CHANNEL_ID", "0") or "0"),
        target_channel_id=int(opt("TARGET_CHANNEL_ID", "0") or "0"),
        tmdb_api_key=opt("TMDB_API_KEY"),
        omdb_api_key=opt("OMDB_API_KEY"),
        channel_username=opt("CHANNEL_USERNAME", "@THEUPDATEDGUYS"),
        channel_link=opt("CHANNEL_LINK", "https://t.me/THEUPDATEDGUYS"),
        api_timeout=int(opt("API_TIMEOUT", "10")),
        db_path=opt("DB_PATH", "bot_state.db"),

    )
