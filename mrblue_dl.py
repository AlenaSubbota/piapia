#!/usr/bin/env python3
"""MrBlue webtoon downloader — saves chapters as CBZ (zip of images).

Usage:
    python mrblue_dl.py --comic wt_000078468 --from-chapter 1 --to-chapter 5 \
        --cookie "MrblueAuth=...; SaveLogin=..." --out ./output

Requirements:
  * Python: pip install "httpx[http2]"
  * Node.js (for image decryption), plus these files in the same folder as the
    script: mrblue_decode.mjs, bee.wasm, wasm_exec.js

Notes:
  * MrBlue's API tarpits plain HTTP/1.1 and only answers over HTTP/2 (like the
    browser), so this uses httpx with http2=True.
  * Images are encrypted by a WASM module (bee.wasm). We download the raw bytes
    and decrypt them with the site's own decode() via the Node helper.
"""

import argparse
import http.cookiejar
import random
import shutil
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
#
# MrBlue encrypts images with a position-permutation + per-byte transform
# implemented in bee.wasm. Re-implementing it in Python is fragile (and breaks
# whenever MrBlue ships a new bee.wasm), so we download the raw encrypted bytes
# and hand them to the real WASM via a tiny Node helper (mrblue_decode.mjs).


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


def fetch_encrypted(session, path: str, is_hd: bool) -> bytes:
    """Download the raw (still-encrypted) image bytes."""
    r = _get_with_retry(session, image_url(path, is_hd),
                        headers={"x-auth-token": _auth_token()})
    r.raise_for_status()
    return r.content


def decode_dir(node_bin: str, decoder: Path, work_dir: Path) -> None:
    """Run the Node/WASM helper to decrypt every *.enc in work_dir."""
    import subprocess
    proc = subprocess.run(
        [node_bin, str(decoder), str(work_dir)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"decoder failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )


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

def write_cbz(decoded: list[tuple[int, bytes]], out_path: Path) -> None:
    """Zip decoded pages (page_number, bytes) into a CBZ, named by reading order."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for idx, (_pn, img) in enumerate(sorted(decoded), 1):
            zf.writestr(f"{idx:04d}.{_ext(img)}", img)


def merge_cbz(cbz_paths: list[Path], out_path: Path) -> None:
    """Merge multiple CBZ files into one, re-numbering pages sequentially."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as out_zf:
        idx = 1
        for cbz in cbz_paths:
            with zipfile.ZipFile(cbz, "r") as in_zf:
                for name in sorted(in_zf.namelist()):
                    data = in_zf.read(name)
                    ext = Path(name).suffix
                    out_zf.writestr(f"{idx:04d}{ext}", data)
                    idx += 1


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
    ap.add_argument("--node", default="node", help="Path to the node binary")
    ap.add_argument("--decoder", default=None,
                    help="Path to mrblue_decode.mjs (default: next to this script)")
    ap.add_argument("--keep-temp", action="store_true",
                    help="Keep the per-chapter temp dir with raw/decoded images")
    ap.add_argument("--bundle", type=int, default=0, metavar="N",
                    help="Merge every N chapters into one CBZ (e.g. --bundle 5)")
    args = ap.parse_args()

    if not args.cookies and not args.cookie:
        ap.error("Provide --cookies <file> or --cookie <string>")

    decoder = Path(args.decoder) if args.decoder else Path(__file__).with_name("mrblue_decode.mjs")
    if not decoder.exists():
        ap.error(f"decoder not found: {decoder} (download mrblue_decode.mjs, bee.wasm, wasm_exec.js)")

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

    pending_cbz: list[tuple[int, Path]] = []  # (ch_no, path) awaiting bundle

    def flush_bundle(force=False):
        if not args.bundle or not pending_cbz:
            return
        if not force and len(pending_cbz) < args.bundle:
            return
        first, last = pending_cbz[0][0], pending_cbz[-1][0]
        paths = [p for _, p in pending_cbz]
        merged = out_dir / f"{args.comic}_ch{first:04d}-ch{last:04d}.cbz"
        merge_cbz(paths, merged)
        print(f"\n[+] Bundle {merged.name} ({merged.stat().st_size:,} bytes, {len(paths)} chapters)")
        for p in paths:
            p.unlink()
        pending_cbz.clear()

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

        # 1) Download all encrypted pages into a temp dir.
        work = out_dir / f".tmp_ch{ch_no:04d}"
        work.mkdir(parents=True, exist_ok=True)
        n_ok = 0
        for i, pg in enumerate(pages, 1):
            pn = pg.get("pn", i)
            print(f"    p{pn}…", end=" ", flush=True)
            try:
                enc = fetch_encrypted(session, pg["path"], is_hd)
                (work / f"{i:04d}.enc").write_bytes(enc)
                print(f"{len(enc):,}B")
                n_ok += 1
            except Exception as e:
                print(f"fail: {e}")
            time.sleep(SLEEP_PAGE)

        if n_ok == 0:
            print("  [!] No pages downloaded.")
            if not args.keep_temp:
                shutil.rmtree(work, ignore_errors=True)
            continue

        # 2) Decrypt them all in one WASM/Node pass.
        print(f"  [*] Decrypting {n_ok} page(s) via {decoder.name}…")
        try:
            decode_dir(args.node, decoder, work)
        except Exception as e:
            print(f"  [!] Decode failed: {e}")
            print(f"      (raw pages kept in {work})")
            continue

        # 3) Collect decoded pages and zip to CBZ.
        decoded = []
        for enc_file in sorted(work.glob("*.enc")):
            dec_file = enc_file.with_suffix("")
            if dec_file.exists():
                decoded.append((int(dec_file.stem), dec_file.read_bytes()))
        if decoded:
            cbz_path = out_dir / f"{args.comic}_ch{ch_no:04d}.cbz"
            write_cbz(decoded, cbz_path)
            print(f"  [+] {cbz_path} ({cbz_path.stat().st_size:,} bytes, {len(decoded)} pages)")
        if not args.keep_temp:
            shutil.rmtree(work, ignore_errors=True)

        if args.bundle and decoded:
            pending_cbz.append((ch_no, cbz_path))
            flush_bundle()

        time.sleep(args.sleep)

    flush_bundle(force=True)  # merge any leftover chapters


if __name__ == "__main__":
    main()
