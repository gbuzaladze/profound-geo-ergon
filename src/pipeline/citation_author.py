"""Derive a citation `author` for Reddit, YouTube, and Instagram URLs.

Reddit subreddits and many YouTube/Instagram identities are in the path.
Instagram `/p/` and `/reel/` shortcodes, and YouTube watch/shorts URLs, need a
lookup. Instagram classification and post-owner resolution follow the portable
Instagram citation enricher (Instaloader, anonymous then optional session).
YouTube video channels use the public oEmbed endpoint.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

# Instagram path segments that mean a post, not a profile (from the enricher).
POST_MARKERS = {"p", "reel", "tv"}
RESERVED_PATHS = POST_MARKERS | {
    "accounts",
    "direct",
    "explore",
    "reels",
    "stories",
}

YOUTUBE_OEMBED = "https://www.youtube.com/oembed?url={url}&format=json"
YOUTUBE_VIDEO_PATHS = {"watch", "shorts", "embed", "live", "v"}
_FALSEY = {"0", "false", "no", "off"}
_CONSECUTIVE_LOOKUP_FAILURES = 3
_DEFAULT_DELAY_SECONDS = 0.5
# After N consecutive Instaloader failures, pause then keep going (no hard stop).
_DEFAULT_INSTAGRAM_COOLDOWN_SECONDS = 300.0
_OEMBED_TIMEOUT_SECONDS = 15.0
_INSTAGRAM_PROGRESS_EVERY = 25


# URL helpers


def _text(value: object) -> str:
    """Return a stripped string, or empty when the value is missing."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _hostname(url: str, hostname: object = None) -> str:
    """Prefer the row hostname, then the URL host, without a leading www."""
    host = _text(hostname).lower() or (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _path_parts(url: str) -> list[str]:
    """Split the URL path into non-empty segments."""
    return [part for part in urlparse(url).path.split("/") if part]


def _host_matches(host: str, root: str) -> bool:
    """True for the root host and any subdomain (old.reddit.com, m.youtube.com)."""
    return host == root or host.endswith("." + root)


def _env_enabled(name: str) -> bool:
    """True unless `name` is 0, false, no, or off. Unset means enabled."""
    raw = os.getenv(name, "1").strip().casefold()
    return raw not in _FALSEY


def _env_nonnegative_seconds(name: str, default: float) -> float:
    """Non-negative seconds from `name`, or `default` when unset or invalid."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def live_lookup_enabled() -> bool:
    """Live Instagram/YouTube lookups run unless CITATION_AUTHOR_LIVE_LOOKUP is off."""
    return _env_enabled("CITATION_AUTHOR_LIVE_LOOKUP")


def instagram_live_lookup_enabled() -> bool:
    """Instaloader shortcode lookups run unless CITATION_AUTHOR_INSTAGRAM_LIVE_LOOKUP is off.

    URL-parsed Instagram handles still fill. YouTube oEmbed is unchanged.
    """
    return _env_enabled("CITATION_AUTHOR_INSTAGRAM_LIVE_LOOKUP")


def lookup_delay_seconds() -> float:
    """Seconds to wait after each live Instagram lookup (default 0.5)."""
    return _env_nonnegative_seconds(
        "CITATION_AUTHOR_LOOKUP_DELAY_SECONDS", _DEFAULT_DELAY_SECONDS
    )


def instagram_cooldown_seconds() -> float:
    """Seconds to pause after consecutive Instagram failures (default 300)."""
    return _env_nonnegative_seconds(
        "CITATION_AUTHOR_INSTAGRAM_COOLDOWN_SECONDS",
        _DEFAULT_INSTAGRAM_COOLDOWN_SECONDS,
    )


# Instagram classification (ported from Instagram/instagram_citation_enricher.py)


def normalize_instagram_url(raw_url: str) -> str:
    """Force https and www.instagram.com so the same post is keyed consistently."""
    value = raw_url.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host in {"instagram.com", "www.instagram.com"}:
        host = "www.instagram.com"
    return parsed._replace(scheme="https", netloc=host).geturl()


def classify_instagram_url(raw_url: str, hostname: str) -> dict[str, Any]:
    """Return url_type, shortcode, and embedded_username for an Instagram URL."""
    normalized = normalize_instagram_url(raw_url)
    parsed = urlparse(normalized) if normalized else None
    host = (hostname or (parsed.netloc if parsed else "")).lower()
    if "instagram.com" not in host:
        return {
            "url_type": "Non-Instagram",
            "normalized_url": normalized,
            "shortcode": None,
            "embedded_username": None,
        }

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in POST_MARKERS:
        return {
            "url_type": "Post",
            "normalized_url": normalized,
            "shortcode": parts[1],
            "embedded_username": None,
        }
    if len(parts) >= 3 and parts[1].lower() in POST_MARKERS:
        return {
            "url_type": "Post",
            "normalized_url": normalized,
            "shortcode": parts[2],
            "embedded_username": parts[0],
        }
    if parts and parts[0].lower() not in RESERVED_PATHS:
        return {
            "url_type": "Profile",
            "normalized_url": normalized,
            "shortcode": None,
            "embedded_username": parts[0],
        }
    return {
        "url_type": "Other Instagram",
        "normalized_url": normalized,
        "shortcode": None,
        "embedded_username": None,
    }


def identity_from_username(username: str) -> str:
    """Format an Instagram handle the same way as the enricher (`@user`)."""
    normalized = username.strip().lstrip("@")
    return f"@{normalized}"


def instagram_author_from_url(url: str, hostname: str = "") -> str | None:
    """Return `@handle` when the username is already in the Instagram path."""
    classification = classify_instagram_url(url, hostname)
    embedded = classification.get("embedded_username")
    if embedded:
        return identity_from_username(str(embedded))
    return None


# Reddit and YouTube path parsing


def reddit_author_from_url(url: str) -> str | None:
    """Return `r/subreddit` from `/r/{subreddit}/...` paths."""
    parts = _path_parts(url)
    if len(parts) >= 2 and parts[0].casefold() == "r":
        subreddit = parts[1].strip()
        if subreddit:
            return f"r/{subreddit}"
    return None


def youtube_author_from_url(url: str) -> str | None:
    """Return a channel handle from `/@`, `/c/`, `/user/`, or `/channel/` paths."""
    host = _hostname(url)
    parts = _path_parts(url)
    if host == "youtu.be":
        return None
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@"):
        handle = first[1:].strip()
        return f"@{handle}" if handle else None
    if first in {"c", "user"} and len(parts) >= 2:
        name = parts[1].strip()
        return f"@{name}" if name else None
    if first == "channel" and len(parts) >= 2:
        channel_id = parts[1].strip()
        return channel_id or None
    return None


def youtube_video_id(url: str) -> str | None:
    """Return the video id from watch, shorts, embed, live, or youtu.be URLs."""
    parsed = urlparse(url)
    host = _hostname(url)
    parts = _path_parts(url)
    if host == "youtu.be" and parts:
        return parts[0].split("?")[0] or None
    query_id = (parse_qs(parsed.query).get("v") or [None])[0]
    if query_id:
        return query_id
    if parts and parts[0].casefold() in YOUTUBE_VIDEO_PATHS and len(parts) >= 2:
        return parts[1] or None
    return None


def author_from_url(url: object, hostname: object = None) -> str | None:
    """Parse author from the URL when Reddit/YouTube/Instagram put it in the path."""
    raw = _text(url)
    if not raw:
        return None
    host = _hostname(raw, hostname)
    if _host_matches(host, "reddit.com"):
        return reddit_author_from_url(raw)
    if host == "youtu.be" or _host_matches(host, "youtube.com"):
        return youtube_author_from_url(raw)
    if _host_matches(host, "instagram.com"):
        return instagram_author_from_url(raw, host)
    return None


# Live lookups


def _youtube_author_from_oembed_payload(payload: dict[str, Any]) -> str | None:
    """Prefer `@handle` from author_url; otherwise use the channel display name."""
    author_url = _text(payload.get("author_url"))
    if author_url:
        parts = _path_parts(author_url)
        if parts and parts[0].startswith("@"):
            handle = parts[0][1:].strip()
            if handle:
                return f"@{handle}"
    name = _text(payload.get("author_name"))
    return name or None


def resolve_youtube_video(url: str) -> str | None:
    """Fetch the channel for a YouTube video URL via oEmbed. None on failure."""
    oembed_url = YOUTUBE_OEMBED.format(url=quote(url, safe=""))
    request = Request(
        oembed_url,
        headers={"User-Agent": "Novartis-GEO-citation-author"},
    )
    try:
        with urlopen(request, timeout=_OEMBED_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return _youtube_author_from_oembed_payload(payload)


def _build_instaloader(
    session_file: str | None = None,
    login_username: str | None = None,
):
    """Build the same quiet, metadata-only Instaloader used by the enricher."""
    import instaloader

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        max_connection_attempts=1,
        request_timeout=30.0,
    )
    if session_file:
        if not login_username:
            raise ValueError("INSTAGRAM_LOGIN_USERNAME is required with INSTAGRAM_SESSION_FILE")
        loader.load_session_from_file(login_username, filename=session_file)
    return loader


_INSTALOADER_LOADERS: list[Any] | None = None


def _instaloader_loaders() -> list[Any]:
    """Build anonymous and optional session Instaloader clients once per process."""
    global _INSTALOADER_LOADERS
    if _INSTALOADER_LOADERS is not None:
        return _INSTALOADER_LOADERS
    loaders: list[Any] = []
    try:
        loaders.append(_build_instaloader())
    except Exception:
        _INSTALOADER_LOADERS = []
        return _INSTALOADER_LOADERS
    session_file = os.getenv("INSTAGRAM_SESSION_FILE", "").strip()
    login_username = os.getenv("INSTAGRAM_LOGIN_USERNAME", "").strip()
    if session_file:
        try:
            loaders.append(_build_instaloader(session_file, login_username or None))
        except Exception:
            pass
    _INSTALOADER_LOADERS = loaders
    return loaders


def resolve_instagram_shortcode(
    shortcode: str,
    *,
    loader=None,
    fallback_loader=None,
) -> str | None:
    """Return `@handle` for a post/reel shortcode. Tries anonymous, then session."""
    import instaloader

    active_loaders = [item for item in (loader, fallback_loader) if item is not None]
    if not active_loaders:
        active_loaders = _instaloader_loaders()
    if not active_loaders:
        return None

    for active in active_loaders:
        try:
            post = instaloader.Post.from_shortcode(active.context, shortcode)
            username = getattr(post, "owner_username", None)
            if not username:
                username = post.owner_profile.username
            if username:
                return identity_from_username(str(username))
        except Exception:
            continue
    return None


def _has_author(value: object) -> bool:
    """True when a previous run already stored a non-empty author."""
    return bool(_text(value))


def _in_live_window(row: dict[str, object], live_from: str | None) -> bool:
    """True when this row may call Instagram or YouTube.

    `live_from` is the incremental refresh start. Older rows are left as they
    are. A row with no date is included so a new citation is not skipped.
    """
    if not live_from:
        return True
    day = str(row.get("date") or "")
    if not day:
        return True
    return day >= live_from


def apply_authors(
    rows: list[dict[str, object]],
    *,
    live_lookup: bool | None = None,
    resolve_instagram: Callable[[str], str | None] | None = None,
    resolve_youtube: Callable[[str], str | None] | None = None,
    delay_seconds: float | None = None,
    cooldown_seconds: float | None = None,
    sleep: Callable[[float], None] | None = None,
    live_from: str | None = None,
) -> list[dict[str, object]]:
    """Fill `author` from the URL, then live-lookup remaining Instagram/YouTube rows.

    Existing non-empty `author` values are kept when the URL has no identity, so
    incremental SQL rows do not lose a previous Instaloader/oEmbed result.
    `live_from` limits network lookups to that date forward. URL parsing still
    runs on every row. Injected resolvers are for tests; production uses
    Instaloader and oEmbed.
    """
    for row in rows:
        parsed = author_from_url(row.get("url"), row.get("hostname"))
        if parsed:
            row["author"] = parsed
        elif not _has_author(row.get("author")):
            row["author"] = None

    if live_lookup is None:
        live_lookup = live_lookup_enabled()
    # An injected resolver is a test double and still runs. Production skips
    # Instaloader when the Instagram-only switch is off.
    skip_instagram = (
        resolve_instagram is None and not instagram_live_lookup_enabled()
    )
    if live_lookup:
        if live_from:
            print(
                f"Author live lookup from {live_from} "
                "(older rows are not looked up again).",
                flush=True,
            )
        _enrich_authors_live(
            rows,
            resolve_instagram=resolve_instagram,
            resolve_youtube=resolve_youtube,
            skip_instagram=skip_instagram,
            delay_seconds=(
                lookup_delay_seconds() if delay_seconds is None else delay_seconds
            ),
            cooldown_seconds=(
                instagram_cooldown_seconds()
                if cooldown_seconds is None
                else cooldown_seconds
            ),
            sleep=sleep,
            live_from=live_from,
        )
    return rows


def _cached_live_author(
    row: dict[str, object],
    instagram_cache: dict[str, str | None],
    youtube_cache: dict[str, str | None],
) -> str | None:
    """Return a handle already resolved for this row's shortcode or video."""
    url = _text(row.get("url"))
    if not url:
        return None
    host = _hostname(url, row.get("hostname"))
    if _host_matches(host, "instagram.com"):
        classification = classify_instagram_url(url, host)
        shortcode = classification.get("shortcode")
        if classification.get("url_type") == "Post" and shortcode:
            return instagram_cache.get(str(shortcode)) or None
        return None
    if host == "youtu.be" or _host_matches(host, "youtube.com"):
        video_id = youtube_video_id(url)
        if not video_id:
            return None
        return youtube_cache.get(video_id) or None
    return None


def _enrich_authors_live(
    rows: list[dict[str, object]],
    *,
    resolve_instagram: Callable[[str], str | None] | None,
    resolve_youtube: Callable[[str], str | None] | None,
    delay_seconds: float,
    cooldown_seconds: float,
    sleep: Callable[[float], None] | None,
    skip_instagram: bool = False,
    live_from: str | None = None,
) -> None:
    """Look up unique Instagram shortcodes and YouTube videos still missing author.

    After several consecutive Instagram failures, pause for `cooldown_seconds`
    and continue. Do not abandon the rest of the shortcodes for the run.
    When `live_from` is set, only that date forward is looked up. A shortcode
    or video resolved there is copied onto older rows that cite the same URL.
    """
    instagram_resolver = resolve_instagram or resolve_instagram_shortcode
    youtube_resolver = resolve_youtube or resolve_youtube_video
    pause = sleep
    if pause is None:
        from time import sleep as pause

    if skip_instagram:
        print("Instagram live lookup skipped.", flush=True)

    instagram_cache: dict[str, str | None] = {}
    youtube_cache: dict[str, str | None] = {}
    instagram_failures = 0
    instagram_lookups = 0
    instagram_resolved = 0
    cooldown_count = 0

    for row in rows:
        if not _in_live_window(row, live_from):
            continue
        if _has_author(row.get("author")):
            continue
        url = _text(row.get("url"))
        if not url:
            continue
        host = _hostname(url, row.get("hostname"))

        if _host_matches(host, "instagram.com"):
            if skip_instagram:
                continue
            classification = classify_instagram_url(url, host)
            shortcode = classification.get("shortcode")
            if classification.get("url_type") == "Post" and shortcode:
                key = str(shortcode)
                if key not in instagram_cache:
                    handle = instagram_resolver(key)
                    instagram_cache[key] = handle
                    instagram_lookups += 1
                    if handle:
                        instagram_failures = 0
                        instagram_resolved += 1
                    else:
                        instagram_failures += 1
                        if instagram_failures >= _CONSECUTIVE_LOOKUP_FAILURES:
                            cooldown_count += 1
                            print(
                                "Instagram live lookup: "
                                f"{_CONSECUTIVE_LOOKUP_FAILURES} consecutive failures; "
                                f"cooling down {cooldown_seconds:.0f}s "
                                f"(cooldown #{cooldown_count}, "
                                f"{instagram_resolved}/{instagram_lookups} resolved so far).",
                                flush=True,
                            )
                            if cooldown_seconds > 0:
                                pause(cooldown_seconds)
                            instagram_failures = 0
                    if delay_seconds > 0:
                        pause(delay_seconds)
                    if instagram_lookups % _INSTAGRAM_PROGRESS_EVERY == 0:
                        print(
                            "Instagram live lookup progress: "
                            f"{instagram_resolved}/{instagram_lookups} unique shortcodes resolved",
                            flush=True,
                        )
                if instagram_cache[key]:
                    row["author"] = instagram_cache[key]
            continue

        if host == "youtu.be" or _host_matches(host, "youtube.com"):
            video_id = youtube_video_id(url)
            if not video_id:
                continue
            if video_id not in youtube_cache:
                youtube_cache[video_id] = youtube_resolver(url)
            if youtube_cache[video_id]:
                row["author"] = youtube_cache[video_id]

    if live_from:
        for row in rows:
            if _in_live_window(row, live_from) or _has_author(row.get("author")):
                continue
            author = _cached_live_author(row, instagram_cache, youtube_cache)
            if author:
                row["author"] = author

    if instagram_cache or youtube_cache:
        print(
            "Author live lookup: "
            f"{len(instagram_cache)} Instagram shortcodes "
            f"({instagram_resolved} resolved, {cooldown_count} cooldowns), "
            f"{len(youtube_cache)} YouTube videos",
            flush=True,
        )
