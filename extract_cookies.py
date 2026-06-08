#!/usr/bin/env python3
"""Extract Novelpia session cookies from the Mac Catalyst app's Cookies.binarycookies.

Usage:
    python extract_cookies.py /path/to/Data/Library/Cookies/Cookies.binarycookies

Prints the --cookie string for novelpia_dl.py and also writes novelpia_cookies.txt
in Netscape format.

pip install (no extra deps — uses stdlib only)
"""

import struct
import sys
import datetime
from pathlib import Path

# Apple epoch: seconds since 2001-01-01
APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc).timestamp()

NOVELPIA_KEYS = {"USERKEY", "TKEY", "LOGINKEY"}


def read_binarycookies(path: Path) -> list[dict]:
    data = path.read_bytes()
    magic = data[:4]
    if magic != b"cook":
        raise ValueError(f"Not a binarycookies file (magic={magic!r})")

    num_pages = struct.unpack_from(">I", data, 4)[0]
    page_sizes = [struct.unpack_from(">I", data, 8 + i * 4)[0] for i in range(num_pages)]

    offset = 8 + num_pages * 4
    cookies = []

    for page_size in page_sizes:
        page = data[offset : offset + page_size]
        offset += page_size

        if page[:4] != b"\x00\x00\x01\x00":
            continue

        num_cookies = struct.unpack_from("<I", page, 4)[0]
        cookie_offsets = [struct.unpack_from("<I", page, 8 + i * 4)[0] for i in range(num_cookies)]

        for co in cookie_offsets:
            # Each cookie record: size(4) flags(4) pad(4) domain_off(4) name_off(4)
            #                      path_off(4) value_off(4) end(4) expire(8) create(8)
            size = struct.unpack_from("<I", page, co)[0]
            flags = struct.unpack_from("<I", page, co + 4)[0]
            domain_off = struct.unpack_from("<I", page, co + 16)[0]
            name_off = struct.unpack_from("<I", page, co + 20)[0]
            path_off = struct.unpack_from("<I", page, co + 24)[0]
            value_off = struct.unpack_from("<I", page, co + 28)[0]
            expire_ts = struct.unpack_from("<d", page, co + 40)[0]
            # create_ts = struct.unpack_from("<d", page, co + 48)[0]

            def cstr(off: int) -> str:
                end = page.index(b"\x00", co + off)
                return page[co + off : end].decode("utf-8", errors="replace")

            cookies.append(
                {
                    "domain": cstr(domain_off),
                    "name": cstr(name_off),
                    "path": cstr(path_off),
                    "value": cstr(value_off),
                    "secure": bool(flags & 1),
                    "httponly": bool(flags & 4),
                    "expires": int(APPLE_EPOCH + expire_ts),
                }
            )

    return cookies


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: extract_cookies.py <path/to/Cookies.binarycookies>")

    path = Path(sys.argv[1])
    cookies = read_binarycookies(path)

    novelpia = [c for c in cookies if "novelpia" in c["domain"]]
    if not novelpia:
        print("[!] No novelpia.com cookies found.")
    else:
        print(f"[+] Found {len(novelpia)} novelpia.com cookie(s):\n")
        for c in novelpia:
            print(f"    {c['name']} = {c['value'][:60]}{'…' if len(c['value']) > 60 else ''}")

    key_cookies = {c["name"]: c for c in novelpia if c["name"] in NOVELPIA_KEYS}
    if key_cookies:
        cookie_str = "; ".join(f"{k}={key_cookies[k]['value']}" for k in NOVELPIA_KEYS if k in key_cookies)
        print(f"\n[+] --cookie string:\n    {cookie_str}\n")

        # Write Netscape file
        out = Path("novelpia_cookies.txt")
        lines = ["# Netscape HTTP Cookie File"]
        for c in novelpia:
            secure = "TRUE" if c["secure"] else "FALSE"
            lines.append(
                f"{c['domain']}\tTRUE\t{c['path']}\t{secure}\t{c['expires']}\t{c['name']}\t{c['value']}"
            )
        out.write_text("\n".join(lines) + "\n")
        print(f"[+] Wrote Netscape cookie file → {out}")
        print(f"\n[*] Now run:\n    python novelpia_dl.py --novel <novel_no> --cookies {out} --out MyNovel.epub")
    else:
        print("[!] USERKEY/TKEY/LOGINKEY not found — make sure you're logged in in the app.")


if __name__ == "__main__":
    main()
