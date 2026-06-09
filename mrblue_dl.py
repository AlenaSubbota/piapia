#!/usr/bin/env python3
"""MrBlue webtoon downloader — saves chapters as CBZ (zip of images).

Usage:
    python mrblue_dl.py --comic wt_000078468 --from-chapter 1 --to-chapter 5 \
        --cookie "MrblueAuth=...; SaveLogin=..." --out ./output

pip install requests
"""

import argparse
import http.cookiejar
import random
import string
import sys
import time
import zipfile
from pathlib import Path

import requests

VIEWER_BASE = "https://viewer.mrblue.com"
SLEEP_PAGE = 0.3
SLEEP_CHAPTER = 1.5
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session(cookie_str, cookie_file):
    s = requests.Session()
    s.headers.update({
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
    })
    if cookie_file:
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        s.cookies.update(jar)
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                for domain in (".mrblue.com", "mrblue.com", "viewer.mrblue.com", "m.mrblue.com"):
                    s.cookies.set(k, v, domain=domain)
    return s


def _auth_token() -> str:
    """Generate x-auth-token: 32 random hex chars (confirmed from viewer JS source)."""
    return ''.join(random.choices('0123456789abcdef', k=32))


def init_session(session) -> dict:
    """Call /api/v1/session to get the rolling x-authorization token."""
    import uuid as _uuid
    client_uuid = str(_uuid.uuid4())
    path = f"/api/v1/session?uuid={client_uuid}"
    session.headers["x-auth-token"] = _auth_token()
    r = session.get(f"{VIEWER_BASE}{path}", timeout=30)
    r.raise_for_status()
    x_auth = r.headers.get("x-authorization") or r.headers.get("X-Authorization")
    if x_auth:
        session.headers["x-authorization"] = x_auth
    data = r.json()
    token = (data.get("authToken") or data.get("token")
             or data.get("mrblueAuthToken") or "")
    if token:
        session.headers["mrblue-auth-token"] = token
    return data


# ---------------------------------------------------------------------------
# Chapter data
# ---------------------------------------------------------------------------

def _get_with_retry(session, url, **kwargs):
    """GET with retry/backoff on timeouts and transient connection errors."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return session.get(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            wait = 2 ** attempt
            print(f"    (timeout/conn error, retry in {wait}s…)")
            time.sleep(wait)
    raise last


def fetch_chapter_pages(session, comic_id: str, chapter_no: int) -> dict:
    """Use /api/v4/contents/access endpoint (confirmed from browser DevTools)."""
    path = f"/api/v4/contents/access/{comic_id}/{chapter_no}?channel=PC"
    session.headers["x-auth-token"] = _auth_token()
    session.headers["Referer"] = f"{VIEWER_BASE}/comics/{comic_id}/{chapter_no}?ppt=PPT01"
    r = _get_with_retry(session, f"{VIEWER_BASE}{path}")
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


def fetch_image(session, path: str, nonce_code: str) -> tuple[bytes, str]:
    session.headers["x-auth-token"] = _auth_token()
    r = session.get(f"{VIEWER_BASE}{path}", timeout=60)
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
        except requests.HTTPError as e:
            print(f"  [!] HTTP {e.response.status_code}: {e.response.text[:300]}")
            continue
        except Exception as e:
            print(f"  [!] Failed: {e}")
            continue

        # v4 API wraps data in "response" key
        resp = ch_data.get("response", ch_data)
        nonce = resp.get("nonceCode", ch_data.get("nonceCode", "0"))
        quality = args.quality
        pages = resp.get(quality) or resp.get("hd") or resp.get("sd") or []
        print(f"  [*] {len(pages)} pages | nonce={nonce}")

        if not pages:
            print(f"  [!] No pages. Full response keys: {list(ch_data.keys())} / resp keys: {list(resp.keys())}")
            continue

        images = []
        for pg in pages:
            path = pg["path"]
            pn = pg.get("pn", "?")
            print(f"    p{pn}…", end=" ", flush=True)
            try:
                img, ext = fetch_image(session, path, nonce)
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
