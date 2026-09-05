#!/usr/bin/env python3
"""
NartoDrama Downloader - Download HLS videos from narto-drama.com
Pattern mirip inye_downloader.py tapi untuk HLS (m3u8 -> mp4 via ffmpeg)

Debug findings (2026-09-05):
- Detail page /detail/watch/<slug> TIDAK ada video URL (hanya poster + list episode href).
- Episode page /detail/watch/<slug>/1 berisi `const episodeItemsRaw = [{68 eps}]` -> SEMUA episode
  jadi cukup fetch 1 halaman (ep 1) untuk dapat 68 m3u8 URL.
- Tiap episode: direct_play_url = https://v-mps.crazymaplestudios.com/vod-.../*.m3u8
  -> MEDIA playlist (13 segmen .ts, ~64s, 960x540 H264 + HE-AAC), tanpa token/encrypt, public.
- ffmpeg -c copy langsung remux jadi mp4 (tested 5s -> 965KB OK). Butuh ffmpeg di PATH.

Usage:
    python nartodrama_downloader.py <URL>                        # download all 68
    python nartodrama_downloader.py <URL> --ep 5                 # single
    python nartodrama_downloader.py <URL> --range 1 10           # range
    python nartodrama_downloader.py <URL> --dir "D:/dramaku"     # custom dir
    python nartodrama_downloader.py <URL> --check                # verify only
    python nartodrama_downloader.py <URL> --dry-run              # list URLs only

Examples:
    python nartodrama_downloader.py https://narto-drama.com/detail/watch/guruku-yang-terkuat-pura-pura-lemah-s2
    python nartodrama_downloader.py https://narto-drama.com/detail/watch/guruku-yang-terkuat-pura-pura-lemah-s2/1 --ep 1 --dir "D:/narto"
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
BASE_SITE = "https://narto-drama.com"
MIN_VALID_BYTES = 500_000  # HLS episode ~8-12MB, reject <0.5MB
FFMPEG_TIMEOUT = 300
_EP_CACHE: dict[str, tuple[float, tuple[list[dict], str, str]]] = {}  # slug -> (ts, result)
_EP_CACHE_TTL = 600

def slugify(text: str) -> str:
    safe = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    safe = re.sub(r"[\s_]+", "-", safe)
    return safe[:60] or "narto-drama"

def extract_slug_from_url(url: str) -> str:
    # /detail/watch/<slug>/1 or /detail/watch/<slug>
    m = re.search(r"/detail/watch/([^/?#]+)", url)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot extract slug from URL: {url} (expected /detail/watch/<slug>)")

def fetch_html(url: str, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE_SITE + "/"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            last_err = e
            if 500 <= e.code < 600 and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"[!] HTTP {e.code} {url} -> retry {attempt+1}/{retries} in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"[!] fetch err {e} -> retry {attempt+1}/{retries} in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    assert last_err is not None
    raise last_err

def fetch_episode_list(slug: str) -> tuple[list[dict], str, str]:
    now = time.time()
    if slug in _EP_CACHE:
        ts, cached = _EP_CACHE[slug]
        if now - ts < _EP_CACHE_TTL:
            return cached
    page_url = f"{BASE_SITE}/detail/watch/{slug}/1?lang=id-ID"
    print(f"[*] Fetch {page_url}", flush=True)
    html = fetch_html(page_url)

    title = slug
    m_title = re.search(r"<title>(.*?)</title>", html, re.S)
    if m_title:
        title = m_title.group(1).split(" - ")[0].split(" Episode")[0].strip()

    poster = ""
    m_post = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    if m_post:
        poster = m_post.group(1).strip()
        if poster.startswith("/"):
            poster = BASE_SITE + poster

    m = re.search(r"const episodeItemsRaw\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        html2 = fetch_html(f"{BASE_SITE}/detail/watch/{slug}?lang=id-ID")
        m = re.search(r"const episodeItemsRaw\s*=\s*(\[.*?\]);", html2, re.S)
        if not m:
            raise RuntimeError("episodeItemsRaw not found. Page structure changed or slug invalid.")
        html = html2
        if not poster:
            m_post = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
            if m_post:
                poster = m_post.group(1).strip()
                if poster.startswith("/"):
                    poster = BASE_SITE + poster
        if title == slug:
            m_title = re.search(r"<title>(.*?)</title>", html, re.S)
            if m_title:
                title = m_title.group(1).split(" - ")[0].split(" Episode")[0].strip()

    raw = m.group(1)
    episodes = json.loads(raw)
    if not episodes:
        raise RuntimeError("No episodes in episodeItemsRaw")
    result = (episodes, title, poster)  # ponytail: in-mem cache cukup; upgrade ke file cache jika mau persist antar restart
    _EP_CACHE[slug] = (time.time(), result)
    return result

def is_valid_mp4(path: str) -> bool:
    try:
        if os.path.getsize(path) < MIN_VALID_BYTES:
            return False
        with open(path, "rb") as fh:
            head = fh.read(8192)
            return b"ftyp" in head  # HLS remux always has ftyp
    except OSError:
        return False

def concat_mp4s(parts: list[str], out_path: str) -> bool:
    if shutil.which("ffmpeg") is None:
        print("[ERR] ffmpeg not found", file=sys.stderr); return False
    lst = out_path + ".concat.txt"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in parts:
                s = p.replace("'", "'\\''")
                f.write(f"file '{s}'\n")
        cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",lst,"-c","copy", out_path]
        print(f"[*] Concat {len(parts)} episode -> {os.path.basename(out_path)} ...", flush=True)
        r = subprocess.run(cmd, timeout=600, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[FAIL] concat: {r.stderr[:600]}", flush=True); return False
        return is_valid_mp4(out_path)
    finally:
        try: os.remove(lst)
        except: pass

def download_episode_hls(m3u8_url: str, dest: str, referer: str) -> bool:
    """Remux HLS to mp4 via ffmpeg. Returns True on success."""
    if is_valid_mp4(dest):
        mb = os.path.getsize(dest) / 1_048_576
        print(f"[skip] {os.path.basename(dest)} already valid ({mb:.1f} MB)", flush=True)
        return True

    if shutil.which("ffmpeg") is None:
        print("[ERR] ffmpeg not found in PATH. Install via: winget install Gyan.FFmpeg", file=sys.stderr)
        return False

    tmp = dest + ".tmp.mp4"
    # Remove stale tmp
    try: os.remove(tmp)
    except OSError: pass

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_url,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        tmp
    ]
    # ponytail: single ffmpeg copy is ceiling; upgrade to manual .ts concat + parallel fetch if CDN throttles
    print(f"  -> ffmpeg {os.path.basename(dest)} ...", flush=True)
    try:
        result = subprocess.run(cmd, timeout=FFMPEG_TIMEOUT, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[FAIL] ffmpeg: {result.stderr.strip()[:400]}", flush=True)
            try: os.remove(tmp)
            except OSError: pass
            return False
        if not is_valid_mp4(tmp):
            print(f"[FAIL] output invalid ({os.path.getsize(tmp) if os.path.exists(tmp) else 0} bytes)", flush=True)
            try: os.remove(tmp)
            except OSError: pass
            return False
        os.replace(tmp, dest)
        mb = os.path.getsize(dest) / 1_048_576
        print(f"[ OK ] {os.path.basename(dest)} {mb:.1f} MB", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print("[FAIL] ffmpeg timeout", flush=True)
        try: os.remove(tmp)
        except OSError: pass
        return False
    except Exception as e:
        print(f"[FAIL] {e}", flush=True)
        try: os.remove(tmp)
        except OSError: pass
        return False

def main() -> None:
    parser = argparse.ArgumentParser(description="Download HLS videos from narto-drama.com (via ffmpeg)")
    parser.add_argument("url", help="Detail or episode URL, e.g. https://narto-drama.com/detail/watch/guruku-yang-terkuat-pura-pura-lemah-s2")
    parser.add_argument("--ep", type=int, help="Single episode number")
    parser.add_argument("--range", nargs=2, type=int, metavar=("START","END"), help="Episode range")
    parser.add_argument("--dir", default=r"C:\Users\Muhamad Fikri\Documents\Python\downloads", help="Output directory")
    parser.add_argument("--check", action="store_true", help="Only verify existing files")
    parser.add_argument("--dry-run", action="store_true", help="List episodes/URLs without downloading")
    parser.add_argument("--no-sanitize", action="store_true", help="Keep raw slug as folder name")
    parser.add_argument("--full", action="store_true", help="Gabung semua target jadi 1 file FULL.mp4 (concat copy, tanpa re-encode)")
    parser.add_argument("--keep-parts", action="store_true", help="Jika --full, tetap simpan Ep*.mp4")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers (default 4, 1=serial)")
    args = parser.parse_args()
    args.workers = max(1, min(args.workers, 8))

    try:
        slug = extract_slug_from_url(args.url)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)

    print(f"[*] Slug: {slug}", flush=True)
    try:
        episodes, title, _poster = fetch_episode_list(slug)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)

    # normalize episode dicts
    # episodes already sorted by number, but ensure
    episodes.sort(key=lambda e: e.get("route_episode_number") or e.get("number") or 0)
    nums = [e.get("route_episode_number") or e.get("number") for e in episodes]
    print(f"[*] Title: {title}", flush=True)
    print(f"[*] Episodes: {min(nums)}-{max(nums)} ({len(nums)} total) | Source: v-mps.crazymaplestudios.com HLS .m3u8", flush=True)

    if args.ep is not None:
        targets = [e for e in episodes if (e.get("route_episode_number")==args.ep or e.get("number")==args.ep)]
        if not targets: print(f"ERROR: Episode {args.ep} not found", file=sys.stderr); sys.exit(1)
    elif args.range is not None:
        s,e = args.range
        targets = [ep for ep in episodes if s <= (ep.get("route_episode_number") or ep.get("number") or 0) <= e]
        if not targets: print(f"ERROR: No episodes in range {s}-{e}", file=sys.stderr); sys.exit(1)
    else:
        targets = episodes

    # output dir
    out_dir = os.path.abspath(args.dir)
    # auto subfolder per drama if user gave generic downloads folder
    base_downloads = os.path.abspath(r"C:\Users\Muhamad Fikri\Documents\Python\downloads")
    if os.path.abspath(out_dir) == base_downloads and not args.no_sanitize:
        out_dir = os.path.join(out_dir, slugify(title))
    os.makedirs(out_dir, exist_ok=True)
    print(f"[*] Output: {out_dir}", flush=True)

    if args.dry_run:
        for ep in targets:
            n = ep.get("route_episode_number") or ep.get("number")
            print(f"Ep{n:02d} | {ep.get('direct_play_url') or ep.get('play_url')} | is_hls={ep.get('direct_play_is_hls')}")
        return

    if args.check:
        ok=0
        for ep in targets:
            n = ep.get("route_episode_number") or ep.get("number")
            p = os.path.join(out_dir, f"Ep{n:02d}.mp4")
            if is_valid_mp4(p): ok+=1
            else: print(f"[MISS] Ep{n:02d}.mp4", flush=True)
        print(f"Valid: {ok}/{len(targets)}", flush=True)
        return

    failures: list[int] = []
    if args.workers == 1:
        for ep in targets:
            n = ep.get("route_episode_number") or ep.get("number")
            m3u8 = ep.get("direct_play_url") or ep.get("play_url")
            if not m3u8:
                print(f"[FAIL] Ep{n:02d} no play_url", flush=True); failures.append(n); continue
            dest = os.path.join(out_dir, f"Ep{n:02d}.mp4")
            referer = ep.get("watch_url") or f"{BASE_SITE}/detail/watch/{slug}/{n}?lang=id-ID"
            if not download_episode_hls(m3u8, dest, referer):
                failures.append(n)
            time.sleep(0.3)
    else:
        def _one(ep: dict) -> tuple[int, bool]:
            n = ep.get("route_episode_number") or ep.get("number")
            m3u8 = ep.get("direct_play_url") or ep.get("play_url")
            if not m3u8:
                print(f"[FAIL] Ep{n:02d} no play_url", flush=True)
                return n, False
            dest = os.path.join(out_dir, f"Ep{n:02d}.mp4")
            referer = ep.get("watch_url") or f"{BASE_SITE}/detail/watch/{slug}/{n}?lang=id-ID"
            ok = download_episode_hls(m3u8, dest, referer)
            return n, ok
        print(f"[*] Parallel {args.workers} workers", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, ep): ep for ep in targets}
            for fut in concurrent.futures.as_completed(futs):
                n, ok = fut.result()
                if not ok:
                    failures.append(n)

    print("")
    if failures:
        print(f"FAILED: {', '.join(map(str, failures))}", file=sys.stderr); sys.exit(1)

    if args.full and len(targets) > 1:
        parts = [os.path.join(out_dir, f"Ep{(e.get('route_episode_number') or e.get('number')):02d}.mp4") for e in targets]
        parts.sort()
        full_name = f"{slugify(title)}-FULL.mp4" if not args.no_sanitize else f"{slug}-FULL.mp4"
        full_path = os.path.join(out_dir, full_name)
        if concat_mp4s(parts, full_path):
            mb = os.path.getsize(full_path) / 1_048_576
            print(f"FULL {mb:.1f} MB -> {full_path}", flush=True)
            if not args.keep_parts:
                for p in parts:
                    try: os.remove(p)
                    except: pass
                print("[*] Parts dihapus (--keep-parts untuk simpan Ep*.mp4)", flush=True)
        else:
            print("FAILED concat FULL", file=sys.stderr); sys.exit(1)
    else:
        if args.full and len(targets)==1:
            print("[*] --full butuh >=2 episode, skip concat", flush=True)
        print(f"ALL {len(targets)} EPISODE(S) DONE -> {out_dir}", flush=True)

if __name__ == "__main__":
    main()
