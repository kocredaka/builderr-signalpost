"""YouTube channel activity via the public channel RSS feed (AUDIT #19, WAVE-3B).

Only for YouTube handles that already passed the website identity gate:
the handle must come from the company's own verified website (a published
``social_links`` entry), never from a search. This module performs no
search, uses no API key, and never touches yt-dlp (rejected: unofficial,
terms-sensitive, undeclared dependency).

Flow per company (at most a few requests, and zero when there is no
verified YouTube handle):

1. Take the published YouTube links from the gated website evidence.
2. Resolve each link to a channel ID: ``/channel/UC...`` links carry it
   directly; ``@handle``/``/user/``/``/c/`` links are resolved from the
   public channel page (canonical link, ``og:url``, or the embedded
   ``channelId``). Resolution failure -> abstain for that handle.
3. Fetch the public feed ``https://www.youtube.com/feeds/videos.xml?channel_id=UC...``
   (no key) and parse the dated video list.
4. Match the feed's channel name against the registry legal name
   (normalized tokens; every channel-name token must appear in the legal
   name). Mismatch -> abstain: no videos are published.

What the feed provides -- and only that -- is recorded: video title,
published/updated dates, video URL, and video ID, under the SHA-256 hash
of the raw feed bytes at the evidence-record level. The RSS feed carries
NO subscriber, view, like, or comment counts, and none are invented,
estimated, or fetched from watch pages.

Evidence-module choice (documented decision): this is a batch evidence
module named ``youtube_activity``, with the same record shape, terminal
states, checkpoint/resume, and generic contract-adapter mapping as every
other module -- so the activity lands in the contract envelope with
provenance instead of living in a post-process observations file. The
``external_footprint`` observation format (one ``public_post`` per video)
remains a possible future export; it is not built here because the batch
contract is one terminal envelope per company, and per-video observations
would need their own ids/hashes without adding scored evidence.

Statuses: ``available`` (feed fetched, name matched); ``not_found``
(handle resolved but feed gone, or channel name mismatched, or handle
unresolvable -- checked, nothing publishable); ``not_applicable`` (no
verified YouTube handle, or the website identity gate did not pass --
the module's precondition is unmet, and no request is made);
``source_error``/``blocked`` (fetch/parse failure, rate limiting).
``fetch_youtube_activity`` never raises.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .evidence import evidence, utc_now
from .website import assert_public_url

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHANNEL_URL = "https://www.youtube.com/channel/{channel_id}"
CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_CHANNEL_ID_IN_TEXT_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_MAX_HANDLES = 3
_MAX_VIDEOS = 100
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.0

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"

# Mirrors the legal/generic stripping in identity._tokens (duplicated here so
# this module does not depend on a private helper in another module).
_LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _name_tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in _LEGAL_AND_GENERIC and len(token) > 1]


def channel_name_matches(legal_name: str | None, channel_name: str | None) -> tuple[bool, dict[str, Any]]:
    """Conservative channel-name check.

    Every distinctive token of the channel name must appear in the legal
    name (brand-shortened names like "Equinor" for "EQUINOR ASA" pass; a
    channel with extra unrelated tokens abstains). The handle was already
    gate-verified from the company site, so this is a second, independent
    check on the feed's own self-identification.
    """
    legal_tokens = sorted(set(_name_tokens(legal_name)))
    channel_tokens = sorted(set(_name_tokens(channel_name)))
    matched = sorted(set(legal_tokens) & set(channel_tokens))
    detail = {
        "legal_name": legal_name,
        "channel_name": channel_name,
        "matched_tokens": matched,
        "unmatched_channel_tokens": sorted(set(channel_tokens) - set(legal_tokens)),
        "method": "channel_name_tokens_subset_of_legal_name_v1",
    }
    if not legal_tokens or not channel_tokens:
        return False, {**detail, "reason": "no distinctive tokens to compare"}
    if set(channel_tokens) - set(legal_tokens):
        return False, {**detail, "reason": "channel name has tokens outside the legal name"}
    return True, detail


def extract_channel_reference(url: str) -> dict[str, str] | None:
    """Classify a normalized YouTube URL: direct channel id, handle, or legacy slug."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "youtube.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
        return {"kind": "channel_id", "value": parts[1]}
    if len(parts) >= 1 and parts[0].startswith("@") and len(parts[0]) > 1:
        return {"kind": "handle", "value": parts[0]}
    if len(parts) >= 2 and parts[0] in {"user", "c"}:
        return {"kind": "legacy", "value": "/".join(parts[:2])}
    return None


def resolve_channel_id(page_html: str, page_url: str) -> str | None:
    """Extract a channel ID from a public channel page (pure; no fetching).

    Preference order: canonical link, og:url, embedded ``channelId`` JSON,
    any ``/channel/UC...`` link. Returns None when nothing unambiguous is
    found; the caller abstains.
    """
    text = str(page_html or "")
    canonical = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        text, re.I,
    ) or re.search(
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
        text, re.I,
    )
    candidates: list[str] = []
    if canonical:
        candidates.append(canonical.group(1))
    og_url = re.search(
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
        text, re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
        text, re.I,
    )
    if og_url:
        candidates.append(og_url.group(1))
    for candidate in candidates:
        match = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", candidate)
        if match and CHANNEL_ID_RE.match(match.group(1)):
            return match.group(1)
    json_match = re.search(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"', text)
    if json_match:
        return json_match.group(1)
    any_match = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", text)
    if any_match:
        return any_match.group(1)
    direct = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", str(page_url or ""))
    if direct:
        return direct.group(1)
    return None


def _entry_text(entry: ET.Element, tag: str) -> str | None:
    node = entry.find(f"{{{ATOM_NS}}}{tag}")
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def parse_rss_feed(raw: bytes) -> dict[str, Any]:
    """Parse a YouTube channel Atom feed (pure). Raises ValueError if unusable.

    Returns channel id/name plus the dated video list. No metrics exist in
    this feed and none are produced here.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Unparseable YouTube RSS feed: {exc}") from exc
    if root.tag != f"{{{ATOM_NS}}}feed":
        raise ValueError("YouTube RSS response is not an Atom feed")
    channel_id = None
    channel_node = root.find(f"{{{YT_NS}}}channelId")
    if channel_node is not None and channel_node.text:
        candidate = channel_node.text.strip()
        if CHANNEL_ID_RE.match(candidate):
            channel_id = candidate
    author_name = None
    author = root.find(f"{{{ATOM_NS}}}author")
    if author is not None:
        name_node = author.find(f"{{{ATOM_NS}}}name")
        if name_node is not None and name_node.text:
            author_name = name_node.text.strip() or None
    feed_title = _entry_text(root, "title")
    videos: list[dict[str, Any]] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = None
        video_node = entry.find(f"{{{YT_NS}}}videoId")
        if video_node is not None and video_node.text:
            video_id = video_node.text.strip() or None
        entry_channel = entry.find(f"{{{YT_NS}}}channelId")
        if channel_id is None and entry_channel is not None and entry_channel.text:
            candidate = entry_channel.text.strip()
            if CHANNEL_ID_RE.match(candidate):
                channel_id = candidate
        url = None
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.get("rel") in (None, "alternate") and link.get("href"):
                url = link.get("href")
                break
        if url is None and video_id:
            # Deterministic construction from the feed's own video id, not
            # invented data: the canonical watch URL for that id.
            url = f"https://www.youtube.com/watch?v={video_id}"
        published = _entry_text(entry, "published")
        updated = _entry_text(entry, "updated")
        title = _entry_text(entry, "title")
        if video_id is None and url is None:
            continue
        videos.append({
            "video_id": video_id,
            "title": title,
            "published": published,
            "updated": updated,
            "url": url,
        })
        if len(videos) >= _MAX_VIDEOS:
            break
    if channel_id is None:
        raise ValueError("YouTube RSS feed carries no channel id")
    return {
        "channel_id": channel_id,
        "channel_name": author_name or feed_title,
        "feed_title": feed_title,
        "videos": videos,
    }


def _http_get(url: str, headers: dict[str, str], *, timeout: float, max_bytes: int) -> tuple[int, bytes, dict[str, str]]:
    assert_public_url(url)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            return response.status, raw, response_headers
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_bytes + 1)
        response_headers = {str(key).lower(): str(value) for key, value in (exc.headers or {}).items()}
        return exc.code, raw, response_headers


def _fetch(
    fetch: Callable[..., tuple[int, bytes, dict[str, str]]],
    url: str,
    headers: dict[str, str],
    *,
    timeout: float,
    max_bytes: int,
    attempts: int,
    sleeper: Callable[[float], None],
    metrics: dict[str, Any],
) -> tuple[int | None, bytes, str | None]:
    """One URL with a single retry on transient failures only.

    404/410 (gone), 403/429 (blocked/limited) and 2xx are terminal: no
    retry, to stay polite to YouTube. Returns (status, body, error).
    """
    last_error: str | None = "request failed"
    body = b""
    for attempt in range(max(1, attempts)):
        started = time.monotonic()
        try:
            status, body, _headers = fetch(url, headers, timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 1
            metrics["bytes"] += len(body)
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        except Exception as exc:
            metrics["requests"] += 1
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            if attempt + 1 < max(1, attempts):
                sleeper(_RETRY_DELAY_SECONDS)
            continue
        if status == 200 or status in {403, 404, 410, 429}:
            return status, body, None
        last_error = f"HTTP {status}"
        if status >= 500 and attempt + 1 < max(1, attempts):
            sleeper(_RETRY_DELAY_SECONDS)
            continue
        return status, body, last_error
    return None, body, last_error


def _record(
    org: str,
    status: str,
    source_url: str,
    *,
    value: Any = None,
    note: str | None = None,
    content_sha256: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    return evidence(
        "youtube_activity",
        status,  # type: ignore[arg-type]
        "youtube_channel_rss",
        source_url,
        value=value,
        note=note,
        content_sha256=content_sha256,
        retrieved_at=retrieved_at,
        source_row_key=org,
    )


def fetch_youtube_activity(
    profile: dict[str, Any],
    *,
    fetcher: Callable[..., tuple[int, bytes, dict[str, str]]] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    attempts: int = _DEFAULT_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch dated YouTube activity for verified handles only. Never raises.

    Reads the *published* (post-gate) YouTube links from the website
    evidence. Companies without a verified handle cost zero requests.
    """
    org = str(profile.get("organisation_number") or "")
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    retrieved_at = utc_now()
    website = (profile.get("evidence") or {}).get("website") or {}
    website_value = website.get("value") or {}
    website_url = website.get("source_url") or "https://www.youtube.com/feeds/videos.xml"
    try:
        return _fetch_youtube_activity(profile, org, website, website_value, website_url, fetcher, timeout, attempts, sleeper, metrics, retrieved_at)
    except Exception as exc:  # belt and braces: a bug here must never kill a run
        return _record(
            org, "source_error", website_url,
            note=f"YouTube activity check failed unexpectedly ({type(exc).__name__}); no claims published.",
            retrieved_at=retrieved_at,
        ), metrics


def _fetch_youtube_activity(
    profile: dict[str, Any],
    org: str,
    website: dict[str, Any],
    website_value: dict[str, Any],
    website_url: str,
    fetcher: Callable[..., tuple[int, bytes, dict[str, str]]] | None,
    timeout: float,
    attempts: int,
    sleeper: Callable[[float], None],
    metrics: dict[str, Any],
    retrieved_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if website.get("status") != "available" or not (website_value.get("identity_assessment") or {}).get("publishable"):
        return _record(
            org, "not_applicable", website_url,
            note="YouTube activity requires a website identity gate pass; no verified handle can be trusted.",
            retrieved_at=retrieved_at,
        ), metrics
    handles = sorted({
        str(link.get("url") or "")
        for link in website_value.get("social_links") or []
        if isinstance(link, dict) and link.get("platform") == "youtube" and link.get("url")
    })
    if not handles:
        return _record(
            org, "not_applicable", website_url,
            note="No verified YouTube handle on the company website; nothing fetched.",
            retrieved_at=retrieved_at,
        ), metrics
    fetch = fetcher or _http_get
    outcomes: list[dict[str, Any]] = []
    saw_blocked = False
    saw_error = False
    for handle_url in handles[:_MAX_HANDLES]:
        reference = extract_channel_reference(handle_url)
        if reference is None:
            outcomes.append({"handle_url": handle_url, "outcome": "unparseable_handle_url"})
            continue
        channel_id: str | None = None
        page_url = handle_url
        if reference["kind"] == "channel_id":
            channel_id = reference["value"]
        else:
            status, body, error = _fetch(
                fetch, page_url,
                {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=timeout, max_bytes=3_000_000, attempts=attempts, sleeper=sleeper, metrics=metrics,
            )
            if status in {403, 429}:
                saw_blocked = True
                outcomes.append({"handle_url": handle_url, "outcome": f"channel_page_blocked_http_{status}"})
                continue
            if status != 200:
                saw_error = True
                outcomes.append({"handle_url": handle_url, "outcome": f"channel_page_fetch_failed:{error or status}"})
                continue
            channel_id = resolve_channel_id(body.decode("utf-8", errors="replace"), page_url)
            if channel_id is None:
                outcomes.append({"handle_url": handle_url, "outcome": "channel_id_unresolvable"})
                continue
        feed_url = FEED_URL.format(channel_id=channel_id)
        status, raw, error = _fetch(
            fetch, feed_url,
            {"User-Agent": USER_AGENT, "Accept": "application/atom+xml,application/xml"},
            timeout=timeout, max_bytes=2_000_000, attempts=attempts, sleeper=sleeper, metrics=metrics,
        )
        if status in {403, 429}:
            saw_blocked = True
            outcomes.append({"handle_url": handle_url, "outcome": f"feed_blocked_http_{status}"})
            continue
        if status in {404, 410}:
            outcomes.append({"handle_url": handle_url, "outcome": "feed_not_found"})
            continue
        if status != 200:
            saw_error = True
            outcomes.append({"handle_url": handle_url, "outcome": f"feed_fetch_failed:{error or status}"})
            continue
        if len(raw) > 2_000_000:
            saw_error = True
            outcomes.append({"handle_url": handle_url, "outcome": "feed_oversized"})
            continue
        try:
            parsed = parse_rss_feed(raw)
        except ValueError as exc:
            saw_error = True
            outcomes.append({"handle_url": handle_url, "outcome": f"feed_unparseable:{exc}"})
            continue
        if parsed["channel_id"] != channel_id:
            outcomes.append({"handle_url": handle_url, "outcome": "feed_channel_id_mismatch"})
            continue
        name_ok, name_detail = channel_name_matches(profile.get("name"), parsed["channel_name"])
        if not name_ok:
            outcomes.append({
                "handle_url": handle_url,
                "outcome": f"channel_name_mismatch:{name_detail.get('reason')}",
                "channel_name": parsed["channel_name"],
            })
            continue
        value = {
            "organisation_number": org,
            "channel_id": channel_id,
            "channel_url": CHANNEL_URL.format(channel_id=channel_id),
            "channel_name": parsed["channel_name"],
            "handle_url": handle_url,
            "handle_source": "company_website_social_link",
            "name_match": name_detail,
            "feed_url": feed_url,
            "videos": parsed["videos"],
            "video_count": len(parsed["videos"]),
            "handles_attempted": [item["handle_url"] for item in outcomes] + [handle_url],
        }
        return _record(
            org, "available", feed_url,
            value=value,
            note="Dated video list from the public YouTube channel RSS feed. No subscriber/view metrics exist in this feed.",
            content_sha256=hashlib.sha256(raw).hexdigest(),
            retrieved_at=utc_now(),
        ), metrics
    summary = "; ".join(f"{item['handle_url']} -> {item['outcome']}" for item in outcomes) or "no handles attempted"
    if saw_blocked:
        return _record(
            org, "blocked", website_url,
            note=f"YouTube fetch blocked/limited; no claims published. Attempts: {summary}",
            retrieved_at=retrieved_at,
        ), metrics
    if saw_error:
        return _record(
            org, "source_error", website_url,
            note=f"YouTube feed fetch failed; no claims published. Attempts: {summary}",
            retrieved_at=retrieved_at,
        ), metrics
    return _record(
        org, "not_found", website_url,
        note=f"No publishable YouTube activity for the verified handle(s); abstaining. Attempts: {summary}",
        retrieved_at=retrieved_at,
    ), metrics
