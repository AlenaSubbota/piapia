#!/usr/bin/env python3
"""Novelpia offline collector.

Fetches all accessible episodes of a novel and packs them into an EPUB.
Requires your session cookies from the browser or the Mac app.

Usage (viewer URL, e.g. global.novelpia.com/viewer/167202):
    python novelpia_dl.py --viewer 167202 --cookie "USERKEY=x; TKEY=y" --out Novel.epub

Or if you know the novel_no:
    python novelpia_dl.py --novel 12345 --cookie "USERKEY=x; TKEY=y" --out Novel.epub

pip install requests ebooklib
"""

import argparse
import http.cookiejar
import sys
import time
from pathlib import Path

import requests
from ebooklib import epub

API_BASE = "https://api-global.novelpia.com"
SLEEP_BETWEEN = 1.5


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session(cookie_str, cookie_file):
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://global.novelpia.com",
        "Referer": "https://global.novelpia.com/",
        "X-Requested-With": "XMLHttpRequest",
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
# API
# ---------------------------------------------------------------------------

def api_get(session, path, **params):
    r = session.get(f"{API_BASE}{path}", params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} on {path}: {r.text[:300]}")
    data = r.json()
    if str(data.get("code", "0000")) != "0000":
        raise RuntimeError(f"API error {data.get('code')} on {path}: {data.get('errmsg')}")
    return data


def fetch_episode_meta(session, episode_no):
    """Fetch episode metadata; returns the full result dict."""
    data = api_get(session, "/v1/novel/episode", episode_no=episode_no)
    return data.get("result", {})


def novel_no_from_episode(session, episode_no):
    """Resolve novel_no from any known episode_no."""
    meta = fetch_episode_meta(session, episode_no)
    novel_no = (
        meta.get("novel_no")
        or meta.get("novelNo")
        or (meta.get("data") or {}).get("novel_no")
    )
    if not novel_no:
        print(f"[!] Raw episode meta (for debugging): {meta}")
        raise RuntimeError("Could not find novel_no in episode metadata.")
    return int(novel_no)


def fetch_novel_info(session, novel_no):
    data = api_get(session, "/v1/novel", novel_no=novel_no)
    return data["result"]


def fetch_episode_list(session, novel_no):
    episodes = []
    page = 0
    while True:
        data = api_get(session, "/v1/novel/episode/list", novel_no=novel_no, page=page)
        result = data.get("result", {})
        page_eps = result.get("episode") or result.get("episodes") or result.get("list") or []
        if not page_eps:
            break
        episodes.extend(page_eps)
        limit = result.get("limit", len(page_eps))
        if len(page_eps) < limit:
            break
        page += 1
    return episodes


def fetch_episode_content(session, episode_no):
    try:
        meta = fetch_episode_meta(session, episode_no)
    except Exception as e:
        print(f"    ! meta fail ep {episode_no}: {e}")
        return None

    jwt = meta.get("_t")
    if not jwt:
        return None  # locked / not purchased

    try:
        d = api_get(session, "/v1/novel/episode/content", _t=jwt)
    except Exception as e:
        print(f"    ! content fail ep {episode_no}: {e}")
        return None

    inner = d.get("result", {}).get("data", {})
    return {
        "epi_title": inner.get("epi_title", f"Episode {episode_no}"),
        "epi_content": inner.get("epi_content", ""),
        "episode_no": episode_no,
    }


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

def html_chapter(title, body_html):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"'
        ' "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f'<title>{title}</title>'
        '<style>body{font-family:serif;line-height:1.7;margin:2em;} p{margin:.6em 0;}</style>'
        f'</head><body><h2>{title}</h2>{body_html}</body></html>'
    )


def build_epub(novel_info, chapters, out_path):
    book = epub.EpubBook()
    title = novel_info.get("title") or novel_info.get("novel_name", "Unknown")
    author = novel_info.get("writer_name", "Unknown")
    book.set_identifier(f"novelpia-{novel_info.get('novel_no', 0)}")
    book.set_title(title)
    book.set_language("ko")
    book.add_author(author)
    eps, toc = [], []
    for idx, ch in enumerate(chapters, 1):
        ep = epub.EpubHtml(title=ch["epi_title"], file_name=f"chap_{idx:04d}.xhtml", lang="ko")
        ep.content = html_chapter(ch["epi_title"], ch["epi_content"]).encode("utf-8")
        book.add_item(ep)
        eps.append(ep)
        toc.append(epub.Link(ep.file_name, ch["epi_title"], f"chap{idx}"))
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + eps
    epub.write_epub(str(out_path), book)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--viewer", type=int, metavar="EPISODE_NO",
                       help="Episode number from viewer URL (e.g. /viewer/167202)")
    group.add_argument("--novel", type=int, metavar="NOVEL_NO",
                       help="Novel number if known")
    ap.add_argument("--out")
    ap.add_argument("--cookies")
    ap.add_argument("--cookie")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN)
    ap.add_argument("--episodes", help="Comma-separated episode_no list to fetch instead of all")
    args = ap.parse_args()

    if not args.cookies and not args.cookie:
        ap.error("Provide --cookies <file> or --cookie <string>")

    session = build_session(args.cookie, args.cookies)

    # Resolve novel_no
    if args.viewer:
        print(f"[*] Resolving novel_no from viewer episode {args.viewer}…")
        try:
            novel_no = novel_no_from_episode(session, args.viewer)
            print(f"[*] novel_no = {novel_no}")
        except Exception as e:
            sys.exit(f"[!] {e}")
    else:
        novel_no = args.novel

    # Novel metadata
    print(f"[*] Fetching novel info…")
    try:
        novel_info = fetch_novel_info(session, novel_no)
    except Exception as e:
        print(f"[!] Could not fetch novel info: {e}\n[*] Continuing without metadata.")
        novel_info = {"novel_no": novel_no, "title": f"novel_{novel_no}"}

    title = novel_info.get("title") or novel_info.get("novel_name", f"novel_{novel_no}")
    print(f"[*] Title : {title}")
    print(f"[*] Author: {novel_info.get('writer_name', '?')}")

    # Episode list
    if args.episodes:
        episode_nos = [int(x.strip()) for x in args.episodes.split(",")]
        print(f"[*] Using {len(episode_nos)} specified episodes.")
    else:
        print("[*] Fetching episode list…")
        try:
            ep_list = fetch_episode_list(session, novel_no)
        except Exception as e:
            sys.exit(f"[!] Could not fetch episode list: {e}")
        episode_nos = [
            ep.get("episode_no") or ep.get("epi_no")
            for ep in ep_list
            if ep.get("episode_no") or ep.get("epi_no")
        ]
        print(f"[*] Found {len(episode_nos)} episodes.")

    # Fetch content
    chapters = []
    ok = skip = 0
    for i, ep_no in enumerate(episode_nos, 1):
        print(f"  [{i}/{len(episode_nos)}] ep {ep_no}…", end=" ", flush=True)
        ch = fetch_episode_content(session, ep_no)
        if ch is None or not ch["epi_content"].strip():
            print("skipped")
            skip += 1
        else:
            print(f"ok — {ch['epi_title']!r}")
            chapters.append(ch)
            ok += 1
        time.sleep(args.sleep)

    print(f"\n[*] {ok} fetched, {skip} skipped.")
    if not chapters:
        sys.exit("[!] Nothing collected — check cookies or novel_no.")

    out_path = Path(args.out) if args.out else Path(f"{title}.epub")
    print(f"[*] Building EPUB → {out_path}…")
    build_epub(novel_info, chapters, out_path)
    print(f"[+] Done: {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
