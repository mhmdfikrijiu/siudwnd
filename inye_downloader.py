#!/usr/bin/env python3
"""
INYEDownloader — Download videos from inyeproject.biz.id

Download a single episode, a range, or ALL episodes from ANY drama link.

Usage:
    python inye_downloader.py <URL>                        # download all episodes
    python inye_downloader.py <URL> --ep 5                 # single episode
    python inye_downloader.py <URL> --range 1 20           # episode range
    python inye_downloader.py <URL> --dir "D:/dramaku"     # custom output dir
    python inye_downloader.py <URL> --check                # only verify

Examples:
    python inye_downloader.py https://inyeproject.biz.id/watch/salam-hormat-guru/1
    python inye_downloader.py https://inyeproject.biz.id/watch/drama-lain/1 --ep 3
    python inye_downloader.py https://inyeproject.biz.id/detail/salam-hormat-guru
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
MIN_VALID_BYTES = 1_000_000  # reject downloads smaller than 1 MB
MAX_RETRIES = 3
BASE_SITE = "https://inyeproject.biz.id"


def slugify(text: str) -> str:
    """Turn any text into a safe folder/filename."""
    safe = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    safe = re.sub(r"[\s_]+", "-", safe)
    return safe[:60]


def extract_slug_from_url(url: str) -> str:
    """Extract the drama slug from any inyeproject.biz.id URL."""
    # /watch/<slug>/<ep>
    m = re.search(r"/watch/([^/?#]+)", url)
    if m:
        return m.group(1)
    # /detail/<slug>
    m = re.search(r"/detail/([^/?#]+)", url)
    if m:
        return m.group(1)
    # /api/watch/<slug>
    m = re.search(r"/api/watch/([^/?#]+)", url)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot extract slug from URL: {url}")


def fetch_episode_list(slug: str) -> list[dict]:
    """
    Fetch the full episode list from the watch API.
    Returns a list of dicts with keys: number, route_episode_number, title, play_url, ...
    """
    api_url = f"{BASE_SITE}/api/watch/{slug}/1"
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT, "Referer": f"{BASE_SITE}/watch/{slug}/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    episodes = data.get("episodes", [])
    if not episodes:
        print(f"[WARN] No episodes found in API response for '{slug}'", flush=True)
    return episodes


def get_drama_title(slug: str) -> str:
    """Try to fetch a human-readable title. Fallback to slug."""
    api_url = f"{BASE_SITE}/api/watch/{slug}/1"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        # Some APIs return a 'title' field at the root
        if isinstance(data, dict):
            for key in ("title", "name", "drama_name"):
                if data.get(key):
                    return data[key]
    except Exception:
        pass
    return slug  # fallback


def stream_url(slug: str, ep_num: int) -> str:
    """Build the direct MP4 streaming URL for an episode."""
    return f"{BASE_SITE}/api/stream/{slug}/{ep_num}?src=direct"


def referer_url(slug: str, ep_num: int) -> str:
    return f"{BASE_SITE}/watch/{slug}/{ep_num}"


def is_valid_mp4(path: str) -> bool:
    """Check that the file starts with the MP4 ftyp magic bytes and is big enough."""
    try:
        size = os.path.getsize(path)
        if size < MIN_VALID_BYTES:
            return False
        with open(path, "rb") as fh:
            return fh.read(8)[4:8] == b"ftyp"
    except OSError:
        return False


def download_episode(slug: str, ep_num: int, dest: str, timeout: int = 300) -> bool:
    """Download a single episode. Returns True on success."""
    url = stream_url(slug, ep_num)
    ref = referer_url(slug, ep_num)

    if is_valid_mp4(dest):
        mb = os.path.getsize(dest) / 1_048_576
        print(f"[skip] ep {ep_num:02d} already valid ({mb:.1f} MB)", flush=True)
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Referer": ref}
            )
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            secs = time.monotonic() - start

            if is_valid_mp4(dest):
                mb = total / 1_048_576
                print(f"[ OK ] ep {ep_num:02d}  {mb:.1f} MB in {secs:.1f}s", flush=True)
                return True

            # File is too small or invalid
            print(f"[FAIL] ep {ep_num:02d} invalid ({total} bytes), retry {attempt}/{MAX_RETRIES}", flush=True)
            try:
                os.remove(dest)
            except OSError:
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[FAIL] ep {ep_num:02d} {exc}, retry {attempt}/{MAX_RETRIES}", flush=True)
            try:
                os.remove(dest)
            except OSError:
                pass
        time.sleep(1)

    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download videos from inyeproject.biz.id — supports ANY drama link"
    )
    parser.add_argument("url", help="Full watch/detail URL (e.g. https://inyeproject.biz.id/watch/salam-hormat-guru/1)")
    parser.add_argument("--ep", type=int, help="Download a single episode only")
    parser.add_argument("--range", nargs=2, type=int, metavar=("START", "END"), help="Download episode range (e.g. --range 1 10)")
    parser.add_argument("--dir", default=r"C:\Users\LENOVO\Documents\Dwn", help="Output directory (default: C:\\Users\\LENOVO\\Documents\\Dwn)")
    parser.add_argument("--check", action="store_true", help="Only verify existing files, no downloads")
    parser.add_argument("--no-sanitize", action="store_true", help="Keep raw slug as folder name instead of fetching title")
    args = parser.parse_args()

    # --- Extract slug and metadata ---
    try:
        slug = extract_slug_from_url(args.url)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Slug: {slug}", flush=True)

    # --- Get episode count ---
    episodes = fetch_episode_list(slug)

    if not episodes:
        print("ERROR: Cannot fetch episode list. Check the URL/slug.", file=sys.stderr)
        sys.exit(1)

    episode_numbers = [ep.get("route_episode_number") or ep.get("number") for ep in episodes]
    episode_numbers = [n for n in episode_numbers if n is not None]

    if not episode_numbers:
        print("ERROR: No episode numbers found in API response.", file=sys.stderr)
        sys.exit(1)

    episode_numbers.sort()
    total_eps = len(episode_numbers)
    print(f"[*] Episodes available: {min(episode_numbers)} - {max(episode_numbers)} ({total_eps} total)", flush=True)

    # --- Determine which episodes to download ---
    if args.ep is not None:
        targets = [args.ep]
    elif args.range is not None:
        start, end = args.range
        targets = [n for n in episode_numbers if start <= n <= end]
        if not targets:
            print(f"ERROR: No episodes in range {start}-{end}", file=sys.stderr)
            sys.exit(1)
    else:
        targets = episode_numbers

    # --- Output directory ---
    out_dir = os.path.abspath(args.dir)

    # Use nested subfolder if the output has multiple series
    parent_dir = os.path.dirname(out_dir.rstrip("\\/"))
    series_dir_name = os.path.basename(out_dir.rstrip("\\/"))

    # If user gave the base Dwn dir, auto-create a subfolder per title
    # If user gave a specific subdir, use it directly
    if out_dir.rstrip("\\/").lower() == r"c:\users\lenovo\documents\dwn".lower():
        title = slug if args.no_sanitize else get_drama_title(slug)
        series_dir_name = slugify(title)
        out_dir = os.path.join(out_dir.rstrip("\\/"), series_dir_name)

    os.makedirs(out_dir, exist_ok=True)
    print(f"[*] Output dir: {out_dir}", flush=True)

    # --- Check-only mode ---
    if args.check:
        valid = 0
        for n in targets:
            fpath = os.path.join(out_dir, f"Ep{n:02d}.mp4")
            if is_valid_mp4(fpath):
                valid += 1
            else:
                print(f"[MISS] Ep{n:02d}.mp4", flush=True)
        print(f"Valid MP4: {valid}/{len(targets)}", flush=True)
        return

    # --- Download loop ---
    print(f"[*] Downloading {len(targets)} episode(s)...", flush=True)
    failures = []
    for n in targets:
        fname = f"Ep{n:02d}.mp4"
        dest = os.path.join(out_dir, fname)
        if not download_episode(slug, n, dest):
            failures.append(n)
        time.sleep(0.5)  # be gentle

    # --- Summary ---
    print("")
    if failures:
        print(f"FAILED EPISODES: {', '.join(map(str, failures))}", file=sys.stderr)
        sys.exit(1)
    print(f"ALL {len(targets)} EPISODE(S) DOWNLOADED to: {out_dir}", flush=True)


if __name__ == "__main__":
    main()