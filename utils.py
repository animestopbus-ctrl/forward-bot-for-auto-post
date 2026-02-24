"""
utils.py  –  Helpers & multi-API metadata cascade
══════════════════════════════════════════════════

Free APIs integrated (priority order per content type)
───────────────────────────────────────────────────────
Tier  API          Key?  Endpoint base
────  ───────────  ────  ────────────────────────────────────────────────────
1     TVMaze       NO    https://api.tvmaze.com
2     Jikan v4     NO    https://api.jikan.moe/v4
3     Kitsu        NO    https://kitsu.io/api/edge
4     AniList GQL  NO    https://graphql.anilist.co
5     TMDB         YES*  https://api.themoviedb.org/3   (* free registration)
6     OMDb         YES*  https://www.omdbapi.com        (* free email signup)
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import html as html_module
import logging
import re
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# format_size
# ─────────────────────────────────────────────────────────────────────────────

def format_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


# ─────────────────────────────────────────────────────────────────────────────
# pre_clean_filename
# ─────────────────────────────────────────────────────────────────────────────

_NOISE = [
    re.compile(r"www\.\S+", re.I),
    re.compile(r"\[.*?\]"),
    re.compile(r"\{.*?\}"),
    re.compile(r"\b(mkv|mp4|avi|mov|flv|wmv|webm|m4v)\b", re.I),
    re.compile(
        r"\b(HDTV|WEB-?DL|WEB-?RIP|BluRay|BRRip|DVDRip|HDRip|"
        r"AMZN|NF|DSNP|HULU|HBO|PCOK|ATVP|DSNP|STAN|iT)\b", re.I),
    re.compile(r"\bDD\+?\d\.\d\b", re.I),
    re.compile(r"\b(x264|x265|HEVC|H\.?264|H\.?265|AVC|AAC|DDP?5\.1)\b", re.I),
    re.compile(r"\b(10bit|HDR|SDR|DoVi|Atmos|DTS)\b", re.I),
    re.compile(r"[\-_\.]{2,}"),
]


def pre_clean_filename(filename: str) -> str:
    name = Path(filename).stem
    for p in _NOISE:
        name = p.sub(" ", name)
    name = re.sub(r"[\.\-_]", " ", name)
    return " ".join(name.split())


# ─────────────────────────────────────────────────────────────────────────────
# Language detection
# ─────────────────────────────────────────────────────────────────────────────

_LANG_MAP: dict[str, str] = {
    "en": "🇬🇧 English", "english": "🇬🇧 English",
    "hi": "🇮🇳 Hindi",   "hindi":   "🇮🇳 Hindi",
    "ta": "🇮🇳 Tamil",   "tamil":   "🇮🇳 Tamil",
    "te": "🇮🇳 Telugu",  "telugu":  "🇮🇳 Telugu",
    "ml": "🇮🇳 Malayalam","malayalam":"🇮🇳 Malayalam",
    "kn": "🇮🇳 Kannada", "kannada": "🇮🇳 Kannada",
    "ko": "🇰🇷 Korean",  "korean":  "🇰🇷 Korean",
    "ja": "🇯🇵 Japanese","japanese":"🇯🇵 Japanese",
    "zh": "🇨🇳 Chinese", "chinese": "🇨🇳 Chinese",
    "fr": "🇫🇷 French",  "french":  "🇫🇷 French",
    "es": "🇪🇸 Spanish", "spanish": "🇪🇸 Spanish",
    "de": "🇩🇪 German",  "german":  "🇩🇪 German",
    "ar": "🇸🇦 Arabic",  "arabic":  "🇸🇦 Arabic",
    "pt": "🇧🇷 Portuguese","portuguese":"🇧🇷 Portuguese",
    "ru": "🇷🇺 Russian", "russian": "🇷🇺 Russian",
    "it": "🇮🇹 Italian", "italian": "🇮🇹 Italian",
    "tr": "🇹🇷 Turkish", "turkish": "🇹🇷 Turkish",
    "th": "🇹🇭 Thai",    "thai":    "🇹🇭 Thai",
}


def detect_languages(guess: dict[str, Any]) -> str:
    raw: list[Any] = []
    for k in ("language", "audio_language"):
        v = guess.get(k)
        if v is None:
            continue
        raw.extend(v if isinstance(v, list) else [v])

    seen, added = [], set()
    for item in raw:
        token = str(item).lower().strip()
        label = _LANG_MAP.get(token, str(item).capitalize())
        if label not in added:
            seen.append(label)
            added.add(label)
    return ", ".join(seen) if seen else "🇬🇧 English"


# ─────────────────────────────────────────────────────────────────────────────
# Resolution helpers
# ─────────────────────────────────────────────────────────────────────────────

_SIZE_MAP = {
    "4320p": "7680×4320 (8K)",
    "2160p": "3840×2160 (4K UHD)",
    "1440p": "2560×1440 (2K)",
    "1080p": "1920×1080 (Full HD)",
    "720p":  "1280×720 (HD)",
    "576p":  "720×576 (SD)",
    "480p":  "720×480 (SD)",
    "360p":  "480×360 (Low)",
}


def resolution_from_guess(guess: dict[str, Any]) -> str:
    raw = str(guess.get("screen_size", "")).lower()
    for token, label in _SIZE_MAP.items():
        if token in raw:
            return label
    return raw.upper() if raw else "N/A"


# ─────────────────────────────────────────────────────────────────────────────
# Low-level HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

async def _get(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 10,
) -> dict[str, Any] | None:
    try:
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("GET %s failed: %s", url, exc)
    return None


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    timeout: int = 10,
) -> dict[str, Any] | None:
    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("POST %s failed: %s", url, exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ① TVMaze  (NO KEY REQUIRED)
#    https://api.tvmaze.com/singlesearch/shows?q=<title>
# ─────────────────────────────────────────────────────────────────────────────

async def _tvmaze(session: aiohttp.ClientSession, title: str, timeout: int) -> dict | None:
    data = await _get(session, "https://api.tvmaze.com/singlesearch/shows",
                      params={"q": title}, timeout=timeout)
    if not data:
        return None
    genres = data.get("genres", [])
    summary = html_module.unescape(re.sub(r"<[^>]+>", "", data.get("summary") or ""))
    return {
        "title":    data.get("name", "Unknown"),
        "year":     str(data.get("premiered") or "")[:4],
        "rating":   str(data.get("rating", {}).get("average") or "N/A"),
        "genres":   ", ".join(genres) if genres else "N/A",
        "overview": summary or "No synopsis available.",
        "director": "N/A",
        "cast":     "N/A",
        "runtime":  f"{data.get('averageRuntime', 'N/A')} min",
        "language": data.get("language", "N/A"),
        "country":  (data.get("network") or {}).get("country", {}).get("name", "N/A"),
        "source":   "TVMaze",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ② Jikan v4 / MyAnimeList  (NO KEY REQUIRED)
#    https://api.jikan.moe/v4/anime?q=<title>&limit=1
# ─────────────────────────────────────────────────────────────────────────────

async def _jikan(session: aiohttp.ClientSession, title: str, timeout: int) -> dict | None:
    data = await _get(
        session, "https://api.jikan.moe/v4/anime",
        params={"q": title, "limit": "1", "sfw": "true"},
        timeout=timeout,
    )
    if not data or not data.get("data"):
        return None
    d = data["data"][0]
    genres   = [g["name"] for g in d.get("genres", [])]
    studios  = [s["name"] for s in d.get("studios", [])]
    return {
        "title":    d.get("title_english") or d.get("title", "Unknown"),
        "year":     str(d.get("aired", {}).get("from") or "")[:4],
        "rating":   str(d.get("score", "N/A")),
        "genres":   ", ".join(genres) if genres else "N/A",
        "overview": (d.get("synopsis") or "No synopsis available."),
        "director": ", ".join(studios) if studios else "N/A",
        "cast":     "N/A",
        "runtime":  str(d.get("duration", "N/A")),
        "language": "Japanese",
        "country":  "Japan",
        "source":   "Jikan (MAL)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ③ Kitsu  (NO KEY REQUIRED)
#    https://kitsu.io/api/edge/anime?filter[text]=<title>
# ─────────────────────────────────────────────────────────────────────────────

async def _kitsu(session: aiohttp.ClientSession, title: str, timeout: int) -> dict | None:
    data = await _get(
        session,
        "https://kitsu.io/api/edge/anime",
        params={"filter[text]": title, "page[limit]": "1"},
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        },
        timeout=timeout,
    )
    if not data or not data.get("data"):
        return None
    a = data["data"][0]["attributes"]
    cats = [
        c["attributes"]["title"]
        for c in data.get("included", [])
        if c.get("type") == "categories"
    ]
    synopsis = a.get("synopsis") or a.get("description") or "No synopsis available."
    return {
        "title":    a.get("titles", {}).get("en_jp") or a.get("canonicalTitle", "Unknown"),
        "year":     str(a.get("startDate") or "")[:4],
        "rating":   str(a.get("averageRating") or "N/A"),
        "genres":   ", ".join(cats[:4]) if cats else "N/A",
        "overview": synopsis,
        "director": "N/A",
        "cast":     "N/A",
        "runtime":  f"{a.get('episodeLength', 'N/A')} min",
        "language": "Japanese",
        "country":  "Japan",
        "source":   "Kitsu",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ④ AniList  (NO KEY REQUIRED — GraphQL)
#    https://graphql.anilist.co
# ─────────────────────────────────────────────────────────────────────────────

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    title { english romaji }
    startDate { year }
    averageScore
    genres
    description(asHtml: false)
    studios(isMain: true) { nodes { name } }
    duration
    countryOfOrigin
  }
}
"""


async def _anilist(session: aiohttp.ClientSession, title: str, timeout: int) -> dict | None:
    data = await _post_json(
        session,
        "https://graphql.anilist.co",
        {"query": _ANILIST_QUERY, "variables": {"search": title}},
        timeout=timeout,
    )
    if not data:
        return None
    media = (data.get("data") or {}).get("Media")
    if not media:
        return None
    t        = media.get("title", {})
    studios  = [n["name"] for n in (media.get("studios") or {}).get("nodes", [])]
    genres   = media.get("genres", [])
    desc     = (media.get("description") or "No synopsis available.").replace("\n", " ")
    return {
        "title":    t.get("english") or t.get("romaji", "Unknown"),
        "year":     str((media.get("startDate") or {}).get("year") or "N/A"),
        "rating":   str((media.get("averageScore") or "N/A")),
        "genres":   ", ".join(genres[:4]) if genres else "N/A",
        "overview": desc,
        "director": ", ".join(studios) if studios else "N/A",
        "cast":     "N/A",
        "runtime":  f"{media.get('duration', 'N/A')} min",
        "language": "Japanese",
        "country":  media.get("countryOfOrigin", "JP"),
        "source":   "AniList",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ TMDB  (FREE KEY — https://www.themoviedb.org/settings/api)
#    https://api.themoviedb.org/3/search/multi?api_key=…&query=…
# ─────────────────────────────────────────────────────────────────────────────

async def _tmdb(
    session: aiohttp.ClientSession,
    title: str,
    year: int | None,
    api_key: str,
    media_type: str,   # "movie" or "tv"
    timeout: int,
) -> dict | None:
    if not api_key:
        return None
    params: dict[str, str] = {
        "api_key":  api_key,
        "query":    title,
        "language": "en-US",
    }
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = str(year)

    search = await _get(
        session,
        f"https://api.themoviedb.org/3/search/{media_type}",
        params=params, timeout=timeout,
    )
    if not search or not search.get("results"):
        return None

    top_id = search["results"][0]["id"]
    detail = await _get(
        session,
        f"https://api.themoviedb.org/3/{media_type}/{top_id}",
        params={"api_key": api_key, "language": "en-US", "append_to_response": "credits"},
        timeout=timeout,
    )
    if not detail:
        detail = search["results"][0]

    if media_type == "movie":
        crew      = detail.get("credits", {}).get("crew", [])
        directors = [p["name"] for p in crew if p.get("job") == "Director"]
        cast      = [p["name"] for p in detail.get("credits", {}).get("cast", [])[:5]]
        genres    = [g["name"] for g in detail.get("genres", [])]
        return {
            "title":    detail.get("title") or detail.get("original_title", "Unknown"),
            "year":     str(detail.get("release_date") or "")[:4],
            "rating":   str(detail.get("vote_average", "N/A")),
            "genres":   ", ".join(genres) if genres else "N/A",
            "overview": detail.get("overview") or "No synopsis available.",
            "director": ", ".join(directors) if directors else "N/A",
            "cast":     ", ".join(cast) if cast else "N/A",
            "runtime":  f"{detail.get('runtime', 'N/A')} min",
            "language": (detail.get("original_language") or "en").upper(),
            "country":  (detail.get("production_countries") or [{}])[0].get("name", "N/A"),
            "source":   "TMDB",
        }
    else:
        cast   = [p["name"] for p in detail.get("credits", {}).get("cast", [])[:5]]
        genres = [g["name"] for g in detail.get("genres", [])]
        runtime = (detail.get("episode_run_time") or [None])[0]
        return {
            "title":    detail.get("name") or detail.get("original_name", "Unknown"),
            "year":     str(detail.get("first_air_date") or "")[:4],
            "rating":   str(detail.get("vote_average", "N/A")),
            "genres":   ", ".join(genres) if genres else "N/A",
            "overview": detail.get("overview") or "No synopsis available.",
            "director": "N/A",
            "cast":     ", ".join(cast) if cast else "N/A",
            "runtime":  f"{runtime or 'N/A'} min",
            "language": (detail.get("original_language") or "en").upper(),
            "country":  (detail.get("origin_country") or ["N/A"])[0],
            "source":   "TMDB",
        }


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ OMDb  (FREE KEY — https://www.omdbapi.com/apikey.aspx)
#    http://www.omdbapi.com/?apikey=…&t=…
# ─────────────────────────────────────────────────────────────────────────────

async def _omdb(
    session: aiohttp.ClientSession,
    title: str,
    year: int | None,
    api_key: str,
    timeout: int,
) -> dict | None:
    if not api_key:
        return None
    params: dict[str, str] = {"apikey": api_key, "t": title, "r": "json"}
    if year:
        params["y"] = str(year)
    data = await _get(session, "https://www.omdbapi.com", params=params, timeout=timeout)
    if not data or data.get("Response") != "True":
        return None
    return {
        "title":    data.get("Title", "Unknown"),
        "year":     str(data.get("Year", "N/A"))[:4],
        "rating":   data.get("imdbRating", "N/A"),
        "genres":   data.get("Genre", "N/A"),
        "overview": data.get("Plot", "No synopsis available."),
        "director": data.get("Director", "N/A"),
        "cast":     data.get("Actors", "N/A"),
        "runtime":  data.get("Runtime", "N/A"),
        "language": data.get("Language", "N/A"),
        "country":  data.get("Country", "N/A"),
        "source":   "OMDb",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Master cascade
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT: dict[str, Any] = {
    "title":    "Unknown",
    "year":     "N/A",
    "rating":   "N/A",
    "genres":   "N/A",
    "overview": "No synopsis available.",
    "director": "N/A",
    "cast":     "N/A",
    "runtime":  "N/A",
    "language": "N/A",
    "country":  "N/A",
    "source":   "None",
}


async def fetch_smart_metadata(
    title: str,
    year: int | None,
    content_type: str,
    tmdb_api_key: str = "",
    omdb_api_key: str = "",
    timeout: int = 10,
) -> dict[str, Any]:
    """
    Cascade through all 6 APIs; return the first successful result.

    Content-type routing
    ────────────────────
    anime          → Jikan → AniList → Kitsu → TMDB-TV → TVMaze
    kdrama/cdrama
    jdrama/series  → TVMaze → TMDB-TV → OMDb
    movie / rest   → TVMaze → TMDB-Movie → OMDb
    """
    is_anime = content_type == "anime"
    is_tv    = content_type in ("kdrama", "cdrama", "jdrama", "series", "episode")

    async with aiohttp.ClientSession() as session:
        try:
            if is_anime:
                for fn in (
                    lambda: _jikan(session, title, timeout),
                    lambda: _anilist(session, title, timeout),
                    lambda: _kitsu(session, title, timeout),
                    lambda: _tmdb(session, title, year, tmdb_api_key, "tv", timeout),
                    lambda: _tvmaze(session, title, timeout),
                ):
                    result = await fn()
                    if result:
                        logger.info("Metadata for '%s' from %s", title, result["source"])
                        return result

            elif is_tv:
                for fn in (
                    lambda: _tvmaze(session, title, timeout),
                    lambda: _tmdb(session, title, year, tmdb_api_key, "tv", timeout),
                    lambda: _omdb(session, title, year, omdb_api_key, timeout),
                ):
                    result = await fn()
                    if result:
                        logger.info("Metadata for '%s' from %s", title, result["source"])
                        return result

            else:
                for fn in (
                    lambda: _tvmaze(session, title, timeout),
                    lambda: _tmdb(session, title, year, tmdb_api_key, "movie", timeout),
                    lambda: _omdb(session, title, year, omdb_api_key, timeout),
                ):
                    result = await fn()
                    if result:
                        logger.info("Metadata for '%s' from %s", title, result["source"])
                        return result

        except Exception as exc:
            logger.warning("fetch_smart_metadata crash for '%s': %s", title, exc)

    logger.info("No metadata found for '%s'; using defaults.", title)
    return {**_DEFAULT, "title": title or "Unknown"}