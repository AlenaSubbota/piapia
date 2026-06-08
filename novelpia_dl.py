#!/usr/bin/env python3
"""Novelpia offline collector.

Fetches all accessible episodes of a novel and packs them into an EPUB.
Requires your session cookies from the browser or the Mac app.

Usage:
    python novelpia_dl.py --novel 12345 --cookies cookies.txt --out MyNovel.epub

Cookie file format (Netscape/curl, one line per cookie):
    .novelpia.com  TRUE  /  FALSE  0  USERKEY  abc123
    .novelpia.com  TRUE  /  FALSE  0  TKEY     def456
    .novelpia.com  TRUE  /  FALSE  0  LOGINKEY ghi789

Or pass cookies directly:
    python novelpia_dl.py --novel 12345 --cookie "USERKEY=abc; TKEY=def; LOGINKEY=ghi"

pip install requests ebooklib beautifulsoup4 lxml
"""

import argparse
import http.cookiejar
import json
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

API_BASE = "https://api-global.novelpia.com"
SLEEP_BETWEEN = 1.5  # seconds between episode fetches


# ---------------------------------------------------------------------------
# Session setup
# ---------------------------------------------------------------------------

def build_session(cookie_str: str | None, cookie_file: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://global.novelpia.com",
        "Referer": "https://global.novelpia.com/",
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
                s.cookies.set(k.strip(), v.strip(), domain=".novelpia.com")

    return s


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(session: requests.Session, path: str, **params) -> dict:
    r = session.get(f"{API_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if str(data.get("code", "0000")) != "0000":
        raise RuntimeError(f"API error on {path}: {data}")
    return data


def fetch_novel_info(session: requests.Session, novel_no: int) -> dict:
    data = api_get(session, "/v1/novel", novel_no=novel_no)
    return data["result"]


def fetch_episode_list(session: requests.Session, novel_no: int) -> list[dict]:
    """Returns all episodes across all pages."""
    episodes = []
    page = 0
    while True:
        data = api_get(session, "/v1/novel/episode/list", novel_no=novel_no, page=page)
        result = data.get("result", {})
        page_eps = result.get("episode", [])
        if not page_eps:
            break
        episodes.extend(page_eps)
        if len(page_eps) < result.get("limit", 20):
            break
        page += 1
    return episodes


def fetch_episode_content(session: requests.Session, episode_no: int) -> dict | None:
    """Returns dict with keys: epi_title, epi_content (HTML string), or None if inaccessible."""
    try:
        meta = api_get(session, "/v1/novel/episode", episode_no=episode_no)
    except Exception as e:
        print(f"    ! meta fetch failed for ep {episode_no}: {e}")
        return None

    jwt = meta.get("result", {}).get("_t")
    if not jwt:
        # episode locked / not purchased
        return None

    try:
        content_data = api_get(session, "/v1/novel/episode/content", _t=jwt)
    except Exception as e:
        print(f"    ! content fetch failed for ep {episode_no}: {e}")
        return None

    inner = content_data.get("result", {}).get("data", {})
    return {
        "epi_title": inner.get("epi_title", f"Episode {episode_no}"),
        "epi_content": inner.get("epi_content", ""),
        "episode_no": episode_no,
    }


# ---------------------------------------------------------------------------
# EPUB builder
# ---------------------------------------------------------------------------

def html_chapter(title: str, body_html: str) -> str:
    safe = body_html.replace("&", "&amp;") if not body_html.strip().startswith("<") else body_html
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"'
        ' "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head>"
        f'<title>{title}</title>'
        '<style>body{{font-family:serif;line-height:1.7;margin:2em;}} p{{margin:.6em 0;}}</style>'
        "</head>"
        f"<body><h2>{title}</h2>{body_html}</body>"
        "</html>"
    )


def build_epub(
    novel_info: dict,
    chapters: list[dict],
    out_path: Path,
) -> None:
    book = epub.EpubBook()
    title = novel_info.get("title") or novel_info.get("novel_name", "Unknown")
    author = novel_info.get("writer_name", "Unknown")

    book.set_identifier(f"novelpia-{novel_info.get('novel_no', 0)}")
    book.set_title(title)
    book.set_language("ko")
    book.add_author(author)

    epub_chapters = []
    toc = []

    for idx, ch in enumerate(chapters, 1):
        ch_title = ch["epi_title"]
        content = html_chapter(ch_title, ch["epi_content"])
        ep = epub.EpubHtml(
            title=ch_title,
            file_name=f"chap_{idx:04d}.xhtml",
            lang="ko",
        )
        ep.content = content.encode("utf-8")
        book.add_item(ep)
        epub_chapters.append(ep)
        toc.append(epub.Link(ep.file_name, ch_title, f"chap{idx}"))

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    epub.write_epub(str(out_path), book)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Download a Novelpia novel to EPUB.")
    ap.add_argument("--novel", required=True, type=int, help="novel_no (from the URL)")
    ap.add_argument("--out", help="Output .epub path (default: <title>.epub)")
    ap.add_argument("--cookies", help="Path to Netscape cookie file")
    ap.add_argument("--cookie", help='Cookie string, e.g. "USERKEY=x; TKEY=y; LOGINKEY=z"')
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN, help="Seconds between requests")
    ap.add_argument(
        "--episodes",
        help="Comma-separated episode_no list to fetch (default: all)",
    )
    args = ap.parse_args()

    if not args.cookies and not args.cookie:
        ap.error("Provide --cookies <file> or --cookie <string>")

    session = build_session(args.cookie, args.cookies)

    print(f"[*] Fetching novel info for novel_no={args.novel}…")
    try:
        novel_info = fetch_novel_info(session, args.novel)
    except Exception as e:
        sys.exit(f"[!] Could not fetch novel info: {e}")

    title = novel_info.get("title") or novel_info.get("novel_name", f"novel_{args.novel}")
    print(f"[*] Title : {title}")
    print(f"[*] Author: {novel_info.get('writer_name', '?')}")

    if args.episodes:
        episode_nos = [int(x.strip()) for x in args.episodes.split(",")]
        print(f"[*] Fetching {len(episode_nos)} specified episodes…")
    else:
        print("[*] Fetching episode list…")
        try:
            ep_list = fetch_episode_list(session, args.novel)
        except Exception as e:
            sys.exit(f"[!] Could not fetch episode list: {e}")
        episode_nos = [ep["episode_no"] for ep in ep_list]
        print(f"[*] Found {len(episode_nos)} episodes.")

    chapters = []
    ok = skip = fail = 0
    for i, ep_no in enumerate(episode_nos, 1):
        print(f"  [{i}/{len(episode_nos)}] episode {ep_no}…", end=" ", flush=True)
        ch = fetch_episode_content(session, ep_no)
        if ch is None:
            print("skipped (locked or no JWT)")
            skip += 1
        elif not ch["epi_content"].strip():
            print("skipped (empty content)")
            skip += 1
        else:
            print(f"ok — {ch['epi_title']!r}")
            chapters.append(ch)
            ok += 1
        time.sleep(args.sleep)

    print(f"\n[*] {ok} fetched, {skip} skipped, {fail} failed.")

    if not chapters:
        sys.exit("[!] No chapters collected — check your cookies or novel_no.")

    out_path = Path(args.out) if args.out else Path(f"{title}.epub")
    print(f"[*] Building EPUB → {out_path}…")
    build_epub(novel_info, chapters, out_path)
    print(f"[+] Done: {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
