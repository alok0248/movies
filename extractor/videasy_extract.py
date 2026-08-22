#!/usr/bin/env python3
"""
videasy_extract.py — Extract playable video links from Videasy (player.videasy.net)

Usage:
    python videasy_extract.py <url>
    python videasy_extract.py https://player.videasy.net/movie/969681
    python videasy_extract.py https://player.videasy.net/tv/1396/season/1/episode/1

Requires: requests  (pip install requests)
"""

import sys
import re
import json
import struct
import base64
import argparse
from urllib.parse import quote, urlparse, parse_qs

try:
    import requests
    sys.exit(1)
except ImportError:
    print("Error: 'requests' library is required. Install it with: pip install requests")


# ─── 32-bit unsigned integer helpers ────────────────────────────────────────

MASK32 = 0xFFFFFFFF


def u32(x):
    """Clamp to unsigned 32-bit."""
    return x & MASK32


# ─── Cipher primitives (ported from the Videasy player JS) ──────────────────

F = [
    1116352408, 1899447441, 3049323471, 3921009573,
    961987163, 1508970993, 2453635748, 2870763221,
    3624381080, 310598401, 607225278, 1426881987,
    1925078388, 2162078206, 2614888103, 3248222580,
]

MAGIC = bytes([109, 118, 109, 49])  # "mvm1"


def _w(e):
    """Murmur3-style avalanche (from the player source)."""
    e = u32(e)
    e ^= e >> 16
    e = u32(e * 2246822507)
    e ^= e >> 13
    e = u32(e * 3266489909)
    e ^= e >> 16
    return u32(e)


def _v(e, t):
    """Rotate-left on u32."""
    e = u32(e)
    t &= 31
    if t == 0:
        return u32(e)
    return u32((e << t) | (e >> (32 - t)))


def _b(e):
    return (e * (e + 1) & 1) == 0


def _is_odd(e):
    return (e * (e + 1) & 1) == 1


def _fnv1a(s):
    """FNV-1a hash of a string."""
    h = 2166136261
    for ch in s:
        h = u32(h ^ ord(ch))
        h = u32(h * 16777619)
    return _w(h)


def _acc_hash(s):
    """Accumulator hash used in the cipher state init."""
    h = 1732584193
    for i, ch in enumerate(s):
        h = _v(u32(h ^ u32(ord(ch) * F[15 & i])), 5)
    return _w(h)


class _CipherState:
    """Tracks which indices in S have been assigned, matching JS sparse-array 'in' semantics."""
    __slots__ = ('S', 'acc', 'assigned')

    def __init__(self):
        self.S = [0] * 61
        self.acc = 0
        self.assigned = set()  # tracks which indices have been written

    def __getitem__(self, i):
        return self.S[i]

    def __setitem__(self, i, v):
        self.S[i] = v
        self.assigned.add(i)

    def is_assigned(self, i):
        return i in self.assigned


def _init_state(response_str, seed_str):
    """
    Build the 61-element cipher state from the encrypted response string
    and the seed string, exactly as the player JS does.
    """
    # Convert seed string to a u32 (JS: t >>> 0)
    seed_num = 0
    m = re.match(r"^(\d+)", seed_str)
    if m:
        seed_num = int(m.group(1)) & MASK32

    # a = w(fnv1a(response) ^ w(seed_num ^ 2654435769))
    a = _w(u32(_fnv1a(response_str) ^ _w(u32(seed_num ^ 2654435769))))

    state = _CipherState()
    for e in range(8):
        if _b(e):
            t = a % 61
            a = _v(u32(a + 2654435769), 7 + (7 & e))
            state[t] = u32(a ^ _w(a))
            a = _w(u32(a + t))
        else:
            state[e] = F[15 & e]

    state.acc = _w(u32(2779096485 ^ a))
    return state


def _stream_byte(state, idx):
    """
    Generate one keystream word from the cipher state.
    Mutates state in-place; returns the generated u32 value.
    """
    o = state.acc
    n = o % 61
    # JS: i = 0 - Number(n in r)
    # In JS sparse arrays, 'n in r' is false for unassigned indices.
    i = 0 - (1 if state.is_assigned(n) else 0)

    d = state[n]
    a_val = u32(d ^ u32(2654435769 * u32(idx + 1)))

    s_val = o
    l = u32((s_val ^ a_val) | u32(s_val & a_val & i))

    l = u32(_v(u32(l + o), 31 & n) ^ _v(o, 31 & u32(n * 7)))

    o = _w(u32(l + 2654435769))

    state[n] = o
    state.acc = o
    return o


def decrypt(enc_b64, seed_str, tmdb_id):
    """
    Decrypt the base64url-encoded response from the Videasy sources API.
    Returns the decrypted JSON string.
    """
    # Base64url → standard base64
    b64 = enc_b64.replace("-", "+").replace("_", "/")
    pad = (4 - len(b64) % 4) % 4
    b64 += "=" * pad
    enc_bytes = bytearray(base64.b64decode(b64))

    # Build cipher state from the raw base64url string (as the JS does)
    state, acc = _init_state(enc_b64, seed_str)

    # Generate keystream and XOR
    counter = 0
    pos = 0
    out = bytearray(len(enc_bytes))

    while pos < len(enc_bytes):
        state, acc, val = _stream_byte(state, acc, counter)
        counter += 1

        if pos < len(out):
            out[pos] = enc_bytes[pos] ^ (val & 0xFF)
            pos += 1
        if pos < len(out):
            out[pos] = enc_bytes[pos] ^ ((val >> 8) & 0xFF)
            pos += 1
        if pos < len(out):
            out[pos] = enc_bytes[pos] ^ ((val >> 16) & 0xFF)
            pos += 1
        if pos < len(out):
            out[pos] = enc_bytes[pos] ^ ((val >> 24) & 0xFF)
            pos += 1

    # Verify magic header
    if out[:4] != MAGIC:
        raise ValueError(
            f"Decryption failed (bad magic header: got {out[:4].hex()}, expected {MAGIC.hex()}). "
            "Seed may have expired — try again."
        )

    return bytes(out[4:]).decode("utf-8")


# ─── Videasy API helpers ────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://player.videasy.to",
    "Referer": "https://player.videasy.to/",
}

BASE_API = "https://api.speedracelight.com"
BASE_DB = "https://db.speedracelight.com/3"

# Server paths and their descriptions
SERVERS = [
    ("cdn",         "CDN / Yoru",           "Original audio, may have 4K"),
    ("vsrc",        "Neon",                 "Original audio"),
    ("hdmovie",     "HD Movie",             "Hindi / English dubs"),
    ("m4uhd",       "Breach (m4uhd)",       "Original audio"),
    ("downloader2", "Cypher (Downloader2)", "Original audio"),
    ("superflix",   "Superflix",            "Portuguese"),
    ("lamovie",     "LaMovie",              "Spanish"),
    ("meine",       "Meine",                "German"),
]


def parse_videasy_url(url):
    """
    Parse a Videasy URL and return (media_type, tmdb_id, extra_params).
    Examples:
        https://player.videasy.net/movie/969681
        → ("movie", "969681", {})
        https://player.videasy.net/tv/1396/season/2/episode/5
        → ("tv", "1396", {"season": "2", "episode": "5"})
    """
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]

    media_type = parts[0] if parts else "movie"
    tmdb_id = parts[1] if len(parts) > 1 else None

    extra = {}
    if media_type in ("tv", "anime") and "season" in parts:
        idx = parts.index("season")
        if idx + 1 < len(parts):
            extra["season"] = parts[idx + 1]
        if "episode" in parts and parts.index("episode") + 1 < len(parts):
            extra["episode"] = parts[parts.index("episode") + 1]

    return media_type, tmdb_id, extra


def get_movie_details(tmdb_id):
    """Fetch movie/show details from the TMDB-like API."""
    url = f"{BASE_DB}/movie/{tmdb_id}?append_to_response=external_ids&language=en"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_tv_details(tmdb_id, season=None, episode=None):
    """Fetch TV show details."""
    url = f"{BASE_DB}/tv/{tmdb_id}"
    if season and episode:
        url = f"{BASE_DB}/tv/{tmdb_id}/season/{season}/episode/{episode}?append_to_response=external_ids,credits&language=en"
    else:
        url += "?append_to_response=external_ids&language=en"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_seed(media_id):
    """Get a fresh decryption seed from the Videasy API."""
    r = requests.get(f"{BASE_API}/seed", params={"mediaId": media_id}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["seed"]


def fetch_sources(server_path, title, media_type, year, tmdb_id, imdb_id, seed):
    """Fetch and decrypt video sources from a specific server."""
    params = {
        "title": title,
        "mediaType": media_type,
        "year": str(year),
        "tmdbId": str(tmdb_id),
        "imdbId": imdb_id,
        "enc": "2",
        "seed": seed,
    }
    url = f"{BASE_API}/{server_path}/sources-with-title"
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)

    if r.status_code == 404:
        return None
    r.raise_for_status()

    resp_text = r.text
    # Some servers return JSON errors
    if resp_text.startswith("{") and "not found" in resp_text:
        return None

    try:
        decrypted = decrypt(resp_text, seed, tmdb_id)
        return json.loads(decrypted)
    except Exception as e:
        return {"error": str(e)}


# ─── Main ───────────────────────────────────────────────────────────────────

def extract(url, servers=None, best_only=False):
    """
    Main extraction function. Returns a dict with movie info and sources.
    """
    media_type, tmdb_id, extra = parse_videasy_url(url)
    if not tmdb_id:
        raise ValueError(f"Could not parse TMDB ID from URL: {url}")

    print(f"[*] Media type: {media_type} | TMDB ID: {tmdb_id}")

    # Fetch details
    if media_type == "movie":
        details = get_movie_details(tmdb_id)
        title = details.get("title") or details.get("original_title", "")
        year = details.get("release_date", "")[:4]
        imdb_id = details.get("imdb_id", "")
    else:
        details = get_tv_details(tmdb_id, extra.get("season"), extra.get("episode"))
        title = details.get("name") or details.get("original_name", "")
        year = (details.get("first_air_date") or "")[:4]
        imdb_id = ""
        if "external_ids" in details:
            imdb_id = details["external_ids"].get("imdb_id", "")

    print(f"   Title: {title} ({year}) | IMDB: {imdb_id}")

    # Get seed
    seed = get_seed(tmdb_id)
    print(f"   Seed: {seed}")

    # Try each server
    results = []
    server_list = servers if servers else SERVERS

    for path, name, note in server_list:
        print(f"\n>> {name} ({note})...", end=" ", flush=True)
        try:
            data = fetch_sources(path, title, media_type, year, tmdb_id, imdb_id, seed)
            if data is None:
                print("not available")
                continue
            if "error" in data:
                print(f"error: {data['error'][:60]}")
                continue

            sources = data.get("sources", [])
            subtitles = data.get("subtitles", [])
            if not sources:
                print("no sources")
                continue

            print(f"✅ {len(sources)} source(s)")
            for src in sources:
                q = src.get("quality", "?")
                t = src.get("type", "unknown")
                u = src.get("url", "N/A")
                print(f"   [{q}] {t}: {u[:120]}{'...' if len(u) > 120 else ''}")
            if subtitles:
                print(f"   subs: {len(subtitles)} subtitle(s)")

            results.append({
                "server": name,
                "sources": sources,
                "subtitles": subtitles,
            })
        except Exception as e:
            print(f"error: {e}")

    # Find best URL
    best_url = None
    for r in results:
        for src in r["sources"]:
            q = src.get("quality", "")
            if "1080" in q:
                best_url = src["url"]
                break
        if best_url:
            break
    if not best_url and results:
        best_url = results[0]["sources"][0].get("url")

    if best_url:
        print(f"\n{'='*60}")
        print(f"[+] Best playable link:")
        print(f"   {best_url}")
        print(f"{'='*60}")

    return {
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "best_url": best_url,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract playable video links from Videasy",
        epilog="Example: python videasy_extract.py https://player.videasy.net/movie/969681",
    )
    parser.add_argument("url", help="Videasy URL to extract from")
    parser.add_argument("--server", "-s", action="append",
                        help="Server name(s) to try (default: all). Options: cdn, vsrc, hdmovie, m4uhd, downloader2, superflix, lamovie, meine")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output results as JSON")

    args = parser.parse_args()

    # Filter servers if specified
    server_filter = None
    if args.server:
        server_filter = []
        for s in args.server:
            for path, name, note in SERVERS:
                if s.lower() in (path.lower(), name.lower()):
                    server_filter.append((path, name, note))
                    break
        if not server_filter:
            print(f"Unknown server: {args.server}")
            print(f"Available: {', '.join(p for p, _, _ in SERVERS)}")
            sys.exit(1)

    try:
        result = extract(args.url, servers=server_filter)
        if args.json:
            # Remove non-serializable items
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"\n[!] Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
