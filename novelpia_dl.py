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

def build_session(cookie_str, cookie_file, login_at=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3.1 Safari/605.1.15"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://global.novelpia.com",
        "Referer": "https://global.novelpia.com/",
    })
    if login_at:
        s.headers["login-at"] = login_at.strip()
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
                for domain in (".novelpia.com", "novelpia.com", "global.novelpia.com", "api-global.novelpia.com"):
                    s.cookies.set(k, v, domain=domain)
    return s


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class AuthError(RuntimeError):
    """Raised when the server rejects us for not being logged in (expired login-at)."""


class AdvertisementEpisode(RuntimeError):
    """Raised when the episode requires watching an ad to unlock."""


def api_get(session, path, **params):
    r = session.get(f"{API_BASE}{path}", params=params, timeout=30)
    if r.status_code >= 400:
        body = r.text
        if "logged in" in body or "AUTH_ERROR" in body:
            raise AuthError(f"login-at expired/invalid on {path}")
        if "ADVERTISEMENT_EPISODE" in body or "0008" in body:
            raise AdvertisementEpisode()
        raise RuntimeError(f"HTTP {r.status_code} on {path}: {body[:300]}")
    data = r.json()
    if str(data.get("code", "0000")) != "0000":
        raise RuntimeError(f"API error {data.get('code')} on {path}: {data.get('errmsg')}")
    return data


def refresh_login_at(session):
    """Prompt the user to paste a fresh login-at token from the browser."""
    print(
        "\n[!] The login-at token expired (it only lives ~15 min).\n"
        "    Grab a fresh one: in the browser DevTools → Network, open any chapter,\n"
        "    click the /v1/novel/episode request, copy the value of the 'login-at'\n"
        "    request header, and paste it below.\n"
    )
    token = input("    Paste fresh login-at (or press Enter to abort): ").strip()
    if not token:
        raise SystemExit("[!] Aborted — no token provided.")
    session.headers["login-at"] = token
    print("[*] Token updated, retrying…\n")


def fetch_episode_meta(session, episode_no):
    """Fetch episode metadata; returns the full result dict. Refreshes token on auth failure."""
    try:
        data = api_get(session, "/v1/novel/episode", episode_no=episode_no)
    except AuthError:
        refresh_login_at(session)
        data = api_get(session, "/v1/novel/episode", episode_no=episode_no)
    except AdvertisementEpisode:
        raise
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
    # Try several parameter names — the global API is inconsistent
    for param in ({"novel_no": novel_no}, {"novelNo": novel_no}, {"id": novel_no}):
        try:
            data = session.get(f"{API_BASE}/v1/novel", params=param, timeout=30)
            if data.status_code < 400:
                j = data.json()
                if str(j.get("code", "0000")) == "0000":
                    result = j.get("result", {})
                    if result:
                        return result
        except Exception:
            continue
    # Fallback: try the novel page on global.novelpia.com to scrape basic metadata
    try:
        r = session.get(
            f"https://global.novelpia.com/novel/{novel_no}", timeout=30
        )
        if r.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")
            info: dict = {"novel_no": novel_no}
            # Title
            og_title = soup.find("meta", property="og:title")
            if og_title:
                info["title"] = og_title.get("content", "").strip()
            # Description
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                info["description"] = og_desc.get("content", "").strip()
            # Cover image
            og_img = soup.find("meta", property="og:image")
            if og_img:
                info["cover_url"] = og_img.get("content", "").strip()
            # Author from structured data or page
            author_tag = soup.find("span", class_=lambda c: c and "author" in c.lower())
            if author_tag:
                info["writer_name"] = author_tag.get_text(strip=True)
            if info.get("title"):
                return info
    except Exception as e:
        print(f"    · novel page scrape failed: {e}")
    return {"novel_no": novel_no}


def fetch_cover(session, novel_info) -> bytes | None:
    """Download cover image bytes, trying known URL patterns."""
    url = novel_info.get("cover_url") or novel_info.get("cover_img")
    if not url:
        novel_no = novel_info.get("novel_no", "")
        candidates = [
            f"https://img.novelpia.com/novel/{novel_no}/thumbnail.jpg",
            f"https://img.novelpia.com/novel/{novel_no}/cover.jpg",
            f"https://cover.novelpia.com/{novel_no}.jpg",
        ]
    else:
        candidates = [url]
    for u in candidates:
        try:
            r = session.get(u, timeout=20)
            if r.status_code == 200 and r.content[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\x89PNG"):
                return r.content
        except Exception:
            continue
    return None


def fetch_episode_list(session, novel_no):
    episodes = []
    page = 1
    while True:
        try:
            data = api_get(session, "/v1/novel/episode/list", novel_no=novel_no, page=page)
        except RuntimeError as e:
            # "The episode does not exist" on an out-of-range page = normal end of list
            if "does not exist" in str(e) or "0002" in str(e):
                break
            raise
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


def fetch_episode_content(session, episode_no, title_hint=None):
    try:
        meta = fetch_episode_meta(session, episode_no)
    except AdvertisementEpisode:
        return None  # silent skip
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
    # Title can live in the content payload, the episode meta, or the list entry.
    title = (
        inner.get("epi_title")
        or meta.get("epi_title")
        or meta.get("subject")
        or (title_hint or f"Episode {episode_no}")
    )
    return {
        "epi_title": title,
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


def build_epub(novel_info, chapters, out_path, cover_bytes=None):
    book = epub.EpubBook()
    title = novel_info.get("title") or novel_info.get("novel_name") or f"novel_{novel_info.get('novel_no', 0)}"
    author = novel_info.get("writer_name") or novel_info.get("author") or "Unknown"
    description = novel_info.get("description") or novel_info.get("intro") or ""

    book.set_identifier(f"novelpia-{novel_info.get('novel_no', 0)}")
    book.set_title(title)
    book.set_language("ko")
    book.add_author(author)
    if description:
        book.add_metadata("DC", "description", description)

    if cover_bytes:
        # Detect image type
        ext = "jpg" if cover_bytes[:2] == b"\xff\xd8" else "png"
        mime = "image/jpeg" if ext == "jpg" else "image/png"
        cover_item = epub.EpubItem(
            uid="cover-image",
            file_name=f"cover.{ext}",
            media_type=mime,
            content=cover_bytes,
        )
        book.add_item(cover_item)
        book.set_cover(f"cover.{ext}", cover_bytes)

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
    ap.add_argument("--login-at", dest="login_at",
                    help="The 'login-at' request header (JWT) copied from the browser. "
                         "Required for logged-in/free-after-login chapters.")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN)
    ap.add_argument("--episodes", help="Comma-separated episode_no list to fetch instead of all")
    args = ap.parse_args()

    if not args.cookies and not args.cookie:
        ap.error("Provide --cookies <file> or --cookie <string>")

    session = build_session(args.cookie, args.cookies, args.login_at)
    cookie_names = [c.name for c in session.cookies]
    print(f"[*] Loaded cookies: {cookie_names}")

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
        novel_info = {"novel_no": novel_no}

    title = novel_info.get("title") or novel_info.get("novel_name") or f"novel_{novel_no}"
    author = novel_info.get("writer_name") or novel_info.get("author") or "?"
    description = novel_info.get("description") or novel_info.get("intro") or ""
    print(f"[*] Title      : {title}")
    print(f"[*] Author     : {author}")
    if description:
        print(f"[*] Description: {description[:120]}{'…' if len(description) > 120 else ''}")

    print(f"[*] Fetching cover image…")
    cover_bytes = fetch_cover(session, novel_info)
    if cover_bytes:
        print(f"[*] Cover      : {len(cover_bytes):,} bytes")
    else:
        print(f"[*] Cover      : not found")

    # Episode list
    title_by_ep = {}
    if args.episodes:
        episode_nos = [int(x.strip()) for x in args.episodes.split(",")]
        print(f"[*] Using {len(episode_nos)} specified episodes.")
    else:
        print("[*] Fetching episode list…")
        try:
            ep_list = fetch_episode_list(session, novel_no)
        except Exception as e:
            sys.exit(f"[!] Could not fetch episode list: {e}")
        episode_nos = []
        for ep in ep_list:
            en = ep.get("episode_no") or ep.get("epi_no")
            if not en:
                continue
            episode_nos.append(en)
            title_by_ep[en] = (
                ep.get("epi_title") or ep.get("title") or ep.get("subject")
            )
        print(f"[*] Found {len(episode_nos)} episodes.")

    # Fetch content
    chapters = []
    ok = skip = 0
    auth_failures = 0
    AUTH_FAIL_LIMIT = 5
    out_path = Path(args.out) if args.out else Path(f"{title}.epub")

    def save():
        if not chapters:
            print("[!] Nothing collected — nothing to save.")
            return
        print(f"\n[*] Building EPUB ({len(chapters)} chapters) → {out_path}…")
        build_epub(novel_info, chapters, out_path, cover_bytes=cover_bytes)
        print(f"[+] Saved: {out_path} ({out_path.stat().st_size:,} bytes)")

    try:
        for i, ep_no in enumerate(episode_nos, 1):
            print(f"  [{i}/{len(episode_nos)}] ep {ep_no}…", end=" ", flush=True)
            ch = fetch_episode_content(session, ep_no, title_hint=title_by_ep.get(ep_no))
            if ch is None or not ch["epi_content"].strip():
                print("skipped")
                skip += 1
                auth_failures += 1
                if auth_failures >= AUTH_FAIL_LIMIT and ok == 0:
                    print(
                        f"\n[!] {AUTH_FAIL_LIMIT} consecutive failures with no successes — "
                        "likely a missing/expired login-at token.\n"
                        "    Pass a fresh --login-at value from the browser."
                    )
                    break
            else:
                print(f"ok — {ch['epi_title']!r}")
                chapters.append(ch)
                ok += 1
                auth_failures = 0
            time.sleep(args.sleep)
    except KeyboardInterrupt:
        print(f"\n[*] Interrupted after {ok} chapters.")

    print(f"\n[*] {ok} fetched, {skip} skipped.")
    save()


if __name__ == "__main__":
    main()
