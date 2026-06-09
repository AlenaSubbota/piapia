#!/usr/bin/env python3
"""MrBlue webtoon downloader — saves chapters as CBZ (zip of images).

Usage:
    python mrblue_dl.py --comic wt_000078468 --from-chapter 1 --to-chapter 5 \
        --cookie "MrblueAuth=...; SaveLogin=..." --out ./output

pip install "httpx[http2]"

NOTE: MrBlue's server tarpits plain HTTP/1.1 API requests and only responds
over HTTP/2 (like the browser), so this uses httpx with http2=True.
"""

import argparse
import http.cookiejar
import random
import time
import zipfile
from pathlib import Path

import httpx

VIEWER_BASE = "https://viewer.mrblue.com"
# Image hosts (from Main.js): SD / HD / V2 (legacy /v2/ paths)
SD_BASE = "https://comics-c.mrblue.com"
HD_BASE = "https://comicshd-c.mrblue.com"
V2_BASE = "https://comics.mrblue.com"
SLEEP_PAGE = 0.3
SLEEP_CHAPTER = 1.5
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session(cookie_str, cookie_file):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": VIEWER_BASE,
        "Referer": f"{VIEWER_BASE}/",
        "x-client-agent": "daddy-desktop/2.40.3",
        "x-wasm-support": "Y",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    # http2=True is essential: the server tarpits HTTP/1.1 API calls.
    s = httpx.Client(http2=True, headers=headers, timeout=REQUEST_TIMEOUT,
                     follow_redirects=True)
    if cookie_file:
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        for c in jar:
            s.cookies.set(c.name, c.value, domain=c.domain or ".mrblue.com")
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".mrblue.com")
    return s


def _auth_token() -> str:
    """Generate x-auth-token: 32 random hex chars (confirmed from viewer JS source)."""
    return ''.join(random.choices('0123456789abcdef', k=32))


# ---------------------------------------------------------------------------
# Chapter data
# ---------------------------------------------------------------------------

def _get_with_retry(session, url, headers=None):
    """GET with retry/backoff on timeouts and transient connection errors."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return session.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            wait = 2 ** attempt
            print(f"    ({type(e).__name__}: {e}; retry in {wait}s…)")
            time.sleep(wait)
    raise last


def fetch_chapter_pages(session, comic_id: str, chapter_no: int) -> dict:
    """Use /api/v4/contents/access endpoint (confirmed from browser DevTools)."""
    path = f"/api/v4/contents/access/{comic_id}/{chapter_no}?channel=PC"
    headers = {
        "x-auth-token": _auth_token(),
        "Referer": f"{VIEWER_BASE}/comics/{comic_id}/{chapter_no}?ppt=PPT01",
    }
    r = _get_with_retry(session, f"{VIEWER_BASE}{path}", headers=headers)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Image fetch + decryption
# ---------------------------------------------------------------------------

def decrypt_image(data: bytes, nonce_code: str) -> bytes:
    """Decrypt image bytes — algorithm confirmed from bee.wasm: bitwise NOT of each byte."""
    if _is_image(data):
        return data
    candidate = bytes(b ^ 0xff for b in data)
    if _is_image(candidate):
        return candidate
    return data


def _is_image(data: bytes) -> bool:
    return (len(data) > 4 and (
        data[:2] == b"\xff\xd8"      # JPEG
        or data[:4] == b"\x89PNG"    # PNG
        or data[:4] == b"RIFF"       # WebP
        or data[:6] in (b"GIF87a", b"GIF89a")  # GIF
    ))


def _ext(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"RIFF":
        return "webp"
    return "bin"


def image_url(path: str, is_hd: bool) -> str:
    """Build full image URL — host depends on path/quality (from Main.js getImageUrl)."""
    if "/v2/" in path:
        base = V2_BASE
    elif is_hd:
        base = HD_BASE
    else:
        base = SD_BASE
    return base + path


def fetch_image(session, path: str, nonce_code: str, is_hd: bool) -> tuple[bytes, str]:
    r = _get_with_retry(session, image_url(path, is_hd),
                        headers={"x-auth-token": _auth_token()})
    r.raise_for_status()
    img = decrypt_image(r.content, nonce_code)
    ext = _ext(img)
    return img, ext


# ---------------------------------------------------------------------------
# Chapter list from detail page
# ---------------------------------------------------------------------------

def fetch_chapter_list(session, comic_id: str) -> list[dict]:
    """Fetch available chapter list from the detail API."""
    # Try the mobile web API
    for url in [
        f"https://m.mrblue.com/webtoon/detail/{comic_id}",
        f"https://m.mrblue.com/api/v1/content/{comic_id}/episodes",
        f"https://viewer.mrblue.com/home/api/v1/content/{comic_id}/episodes",
    ]:
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get("content-type", "")
                if "json" in ct:
                    data = r.json()
                    # Try common response shapes
                    items = (data.get("episodes") or data.get("items")
                             or data.get("list") or data.get("data") or [])
                    if items:
                        return items
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# CBZ writer
# ---------------------------------------------------------------------------

def write_cbz(pages: list[tuple[bytes, str]], out_path: Path) -> None:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for i, (img, ext) in enumerate(pages, 1):
            zf.writestr(f"{i:04d}.{ext}", img)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Download MrBlue webtoon chapters as CBZ.")
    ap.add_argument("--comic", required=True, help="Comic ID, e.g. wt_000078468")
    ap.add_argument("--chapter", type=int, help="Single chapter number to download")
    ap.add_argument("--from-chapter", type=int, default=1, dest="from_ch")
    ap.add_argument("--to-chapter", type=int, dest="to_ch")
    ap.add_argument("--quality", choices=["sd", "hd"], default="hd")
    ap.add_argument("--out", default=".", help="Output directory")
    ap.add_argument("--cookies", help="Netscape cookie file")
    ap.add_argument("--cookie", help='Cookie string: "MrblueAuth=...; SaveLogin=..."')
    ap.add_argument("--sleep", type=float, default=SLEEP_CHAPTER)
    ap.add_argument("--debug", action="store_true", help="Dump raw first image to disk for inspection")
    args = ap.parse_args()

    if not args.cookies and not args.cookie:
        ap.error("Provide --cookies <file> or --cookie <string>")

    session = build_session(args.cookie, args.cookies)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Note: the browser flow hits /api/v4/contents/access directly with cookies +
    # a random x-auth-token. No separate session-init call is required.

    # Resolve chapter range
    if args.chapter:
        chapters = [args.chapter]
    elif args.to_ch:
        chapters = range(args.from_ch, args.to_ch + 1)
    else:
        chapters = [args.from_ch]

    for ch_no in chapters:
        print(f"\n[*] Chapter {ch_no}…")
        try:
            ch_data = fetch_chapter_pages(session, args.comic, ch_no)
        except httpx.HTTPStatusError as e:
            print(f"  [!] HTTP {e.response.status_code}: {e.response.text[:300]}")
            continue
        except Exception as e:
            print(f"  [!] Failed: {e}")
            continue

        # v4 API wraps data in "response" key
        resp = ch_data.get("response", ch_data)
        nonce = resp.get("nonceCode", ch_data.get("nonceCode", "0"))
        # Pick requested quality, falling back to whichever is available.
        used_quality = None
        for q in (args.quality, "hd", "sd"):
            if resp.get(q):
                used_quality, pages = q, resp[q]
                break
        else:
            pages = []
        is_hd = used_quality == "hd"
        print(f"  [*] {len(pages)} pages | quality={used_quality} | nonce={nonce}")

        if not pages:
            print(f"  [!] No pages. Full response keys: {list(ch_data.keys())} / resp keys: {list(resp.keys())}")
            continue

        images = []
        for pg in pages:
            path = pg["path"]
            pn = pg.get("pn", "?")
            print(f"    p{pn}…", end=" ", flush=True)
            try:
                img, ext = fetch_image(session, path, nonce, is_hd)
                if ext == "bin" and args.debug:
                    dbg = out_dir / f"debug_ch{ch_no}_p{pn}_raw.bin"
                    dbg.write_bytes(img)
                    print(f"encrypted? saved raw→{dbg}")
                else:
                    print(f"{ext} {len(img):,}B")
                images.append((img, ext))
            except Exception as e:
                print(f"fail: {e}")
            time.sleep(SLEEP_PAGE)

        if images:
            cbz_path = out_dir / f"{args.comic}_ch{ch_no:04d}.cbz"
            write_cbz(images, cbz_path)
            print(f"  [+] {cbz_path} ({cbz_path.stat().st_size:,} bytes)")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
