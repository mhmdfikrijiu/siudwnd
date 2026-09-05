#!/usr/bin/env python3
import os
import re
import uuid
import time
import json
import tempfile
import zipfile
import pathlib
import concurrent.futures
import threading
import traceback
from flask import Flask, request, jsonify, send_file, render_template, after_this_request

import nartodrama_downloader as nd

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

CACHE = pathlib.Path(os.environ.get("NARTO_CACHE") or pathlib.Path(tempfile.gettempdir()) / "narto_cache")
CACHE.mkdir(exist_ok=True, parents=True)

CACHE_TTL = 24 * 3600
CACHE_SWEEP_EVERY = 3600
CACHE_MAX_BYTES = int(os.environ.get("NARTO_CACHE_MAX_BYTES") or 10 * 1024**3)
try:
    if os.environ.get("NARTO_CACHE_MAX_MB"):
        CACHE_MAX_BYTES = int(os.environ["NARTO_CACHE_MAX_MB"]) * 1024 * 1024
except Exception:
    pass

SLUG_RE = re.compile(r"^[a-z0-9-]+$")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _cache_sweep_once() -> tuple[int, int]:
    now = time.time()
    deleted = 0
    freed = 0
    kept: list[tuple[pathlib.Path, int, float]] = []
    try:
        for p in CACHE.rglob("Ep*.mp4"):
            if p.name.endswith(".tmp.mp4"):
                continue
            try:
                st = p.stat()
                if now - st.st_mtime > CACHE_TTL:
                    freed += st.st_size
                    p.unlink()
                    deleted += 1
                else:
                    kept.append((p, st.st_size, st.st_mtime))
            except Exception:
                pass
        total = sum(s for _, s, _ in kept)
        if total > CACHE_MAX_BYTES and kept:
            kept.sort(key=lambda x: x[2])
            for p, sz, _ in kept:
                if total <= CACHE_MAX_BYTES:
                    break
                try:
                    p.unlink()
                    deleted += 1
                    freed += sz
                    total -= sz
                except Exception:
                    pass
        for d in list(CACHE.iterdir()):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass
    except Exception:
        pass
    return deleted, freed


def _cache_sweeper_loop():
    while True:
        time.sleep(CACHE_SWEEP_EVERY)
        try:
            deleted, freed = _cache_sweep_once()
            if deleted:
                print(f"[CACHE] sweep {deleted} file(s) {freed/1_048_576:.1f} MB (TTL {CACHE_TTL//3600}h)", flush=True)
        except Exception as e:
            print(f"[CACHE] sweep err: {e}", flush=True)

_EP_CACHE_APP: dict[str, tuple[float, tuple[list[dict], str, str]]] = {}
_EP_CACHE_TTL = 600


def _get_episodes(slug: str) -> tuple[list[dict], str, str]:
    now = time.time()
    if slug in _EP_CACHE_APP:
        ts, v = _EP_CACHE_APP[slug]
        if now - ts < _EP_CACHE_TTL:
            return v
    last: Exception | None = None
    for i in range(3):
        try:
            v = nd.fetch_episode_list(slug)
            _EP_CACHE_APP[slug] = (time.time(), v)
            return v
        except Exception as e:
            last = e
            msg = str(e)
            is_5xx = "502" in msg or "503" in msg or "500" in msg or "Bad Gateway" in msg
            if is_5xx and i < 2:
                time.sleep(2 * (i + 1))
                continue
            raise
    assert last is not None
    raise last


def _touch(p: pathlib.Path):
    try:
        os.utime(p, None)
    except Exception:
        pass


def ensure_mp4(slug: str, n: int, episodes: list[dict] | None = None) -> pathlib.Path:
    d = CACHE / slug
    d.mkdir(exist_ok=True, parents=True)
    dest = d / f"Ep{n:02d}.mp4"
    if nd.is_valid_mp4(str(dest)):
        _touch(dest)
        return dest
    if episodes is None:
        episodes, _, _ = _get_episodes(slug)
    ep = next((e for e in episodes if (e.get("route_episode_number") == n or e.get("number") == n)), None)
    if not ep:
        raise FileNotFoundError(f"Episode {n} tidak ada di {slug}")
    m3u8 = ep.get("direct_play_url") or ep.get("play_url")
    if not m3u8:
        raise RuntimeError(f"Episode {n} tidak punya play URL")
    referer = ep.get("watch_url") or f"{nd.BASE_SITE}/detail/watch/{slug}/{n}?lang=id-ID"
    ok = nd.download_episode_hls(m3u8, str(dest), referer)
    if not ok or not nd.is_valid_mp4(str(dest)):
        raise RuntimeError(f"Gagal download Ep{n:02d}")
    return dest


def _job_set(job_id: str, **kw):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j is not None:
            j.update(kw)
            j["updated"] = time.time()


def _run_full_job(job_id: str, slug: str, nums: list[int], workers: int = 4):
    try:
        workers = max(1, min(int(workers), 8))
        _job_set(job_id, status="running", total=len(nums), current=0, message=f"Mulai download {len(nums)} episode ({workers} workers)")
        try:
            episodes, _, _ = _get_episodes(slug)
        except Exception as e:
            episodes = []
            print(f"[WARN] preload episodes fail: {e}", flush=True)
        parts_map: dict[int, str] = {}
        failed: list[int] = []
        lock = threading.Lock()
        done = 0

        def _one(n: int):
            nonlocal done
            try:
                p = ensure_mp4(slug, n, episodes if episodes else None)
                with lock:
                    parts_map[n] = str(p)
            except Exception as e:
                print(f"[WARN] Ep{n:02d} skip: {e}", flush=True)
                with lock:
                    failed.append(n)
            with lock:
                done += 1
                cur = done
            _job_set(job_id, current=cur, progress=int(cur / len(nums) * 85), message=f"Download Ep{n:02d} ({cur}/{len(nums)})")

        if workers == 1:
            for n in nums:
                _one(n)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_one, nums))
        parts = [parts_map[n] for n in sorted(parts_map)]
        skipped_msg = [f"Ep{n:02d}" for n in sorted(failed)]

        if not parts:
            raise RuntimeError(f"Semua episode gagal ({', '.join(map(str, failed))}) — server 502? Coba retry.")
        _job_set(job_id, message="Gabung jadi 1 FULL…", progress=90)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"-{slug}-FULL.mp4")
        tmp.close()
        ok = nd.concat_mp4s(parts, tmp.name)
        if not ok:
            try:
                os.remove(tmp.name)
            except Exception:
                pass
            raise RuntimeError("Gagal concat FULL — codec tidak konsisten atau ffmpeg error")
        with JOBS_LOCK:
            JOBS[job_id]["result_path"] = tmp.name
            if not failed:
                JOBS[job_id]["result_name"] = f"{slug}-FULL-Ep{nums[0]:02d}-{nums[-1]:02d}.mp4"
            else:
                JOBS[job_id]["result_name"] = f"{slug}-FULL-partial-{len(parts)}eps.mp4"
                JOBS[job_id]["failed"] = failed
        msg = "Siap download" if not failed else f"Siap (partial {len(parts)}/{len(nums)} — gagal: {', '.join(skipped_msg)} — retry gagal ep untuk lengkap)"
        _job_set(job_id, status="done", progress=100, message=msg)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[JOB {job_id} FULL ERR]\n{tb}", flush=True)
        _job_set(job_id, status="error", error=f"{e}", traceback=tb, message=f"Error: {e}")


def _run_zip_job(job_id: str, slug: str, nums: list[int], workers: int = 4):
    try:
        workers = max(1, min(int(workers), 8))
        _job_set(job_id, status="running", total=len(nums), current=0, message=f"Siapin {len(nums)} episode ({workers} workers)")
        try:
            episodes, _, _ = _get_episodes(slug)
        except Exception as e:
            episodes = []
            print(f"[WARN] preload episodes fail: {e}", flush=True)
        failed: list[int] = []
        ok_nums: list[int] = []
        lock = threading.Lock()
        done = 0

        def _one(n: int):
            nonlocal done
            try:
                ensure_mp4(slug, n, episodes if episodes else None)
                with lock:
                    ok_nums.append(n)
            except Exception as e:
                print(f"[WARN] Ep{n:02d} skip: {e}", flush=True)
                with lock:
                    failed.append(n)
            with lock:
                done += 1
                cur = done
            _job_set(job_id, current=cur, progress=int(cur / len(nums) * 85), message=f"Download Ep{n:02d} ({cur}/{len(nums)})")

        if workers == 1:
            for n in nums:
                _one(n)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_one, nums))
        ok_nums.sort()
        failed.sort()

        if not ok_nums:
            raise RuntimeError(f"Semua episode gagal ({', '.join(map(str, failed))}) — server 502? Coba retry.")
        _job_set(job_id, message="Bikin ZIP…", progress=90)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"-{slug}.zip")
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_STORED) as z:
            for n in ok_nums:
                p = CACHE / slug / f"Ep{n:02d}.mp4"
                if p.exists():
                    z.write(p, arcname=f"{slug}/Ep{n:02d}.mp4")
        with JOBS_LOCK:
            JOBS[job_id]["result_path"] = tmp.name
            JOBS[job_id]["result_name"] = f"{slug}-Ep{ok_nums[0]:02d}-{ok_nums[-1]:02d}.zip" if len(ok_nums) > 1 else f"{slug}-Ep{ok_nums[0]:02d}.zip"
            if failed:
                JOBS[job_id]["failed"] = failed
        msg = "Siap download" if not failed else f"Siap (partial {len(ok_nums)}/{len(nums)} — gagal: {', '.join(map(str, failed))})"
        _job_set(job_id, status="done", progress=100, message=msg)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[JOB {job_id} ZIP ERR]\n{tb}", flush=True)
        _job_set(job_id, status="error", error=f"{e}", traceback=tb, message=f"Error: {e}")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/resolve")
def resolve():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="Paste link drama dulu"), 400
    try:
        slug = nd.extract_slug_from_url(url)
        episodes, title, poster = nd.fetch_episode_list(slug)
        episodes = sorted(episodes, key=lambda x: x.get("route_episode_number") or x.get("number") or 0)
        out = []
        for e in episodes:
            out.append({
                "n": e.get("route_episode_number") or e.get("number"),
                "title": e.get("title") or f"Episode {e.get('number')}",
                "hls": e.get("direct_play_url") or e.get("play_url") or "",
            })
        return jsonify(slug=slug, title=title, poster=poster, total=len(out), episodes=out)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)), 400


@app.get("/api/mp4/<slug>/<int:n>")
def mp4(slug, n):
    if not SLUG_RE.fullmatch(slug):
        return jsonify(error="slug invalid"), 400
    try:
        p = ensure_mp4(slug, n)
        return send_file(p, mimetype="video/mp4", as_attachment=True, download_name=f"{slug}-Ep{n:02d}.mp4")
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)), 500


@app.post("/api/zip")
def zip_eps():
    data = request.get_json(force=True, silent=True) or {}
    slug = (data.get("slug") or "").strip()
    nums = data.get("episodes") or []
    workers = int(data.get("workers") or 4)
    workers = max(1, min(workers, 8))
    if not slug or not nums:
        return jsonify(error="slug / episodes kosong"), 400
    if not SLUG_RE.fullmatch(slug):
        return jsonify(error="slug invalid"), 400
    nums = sorted(set(int(x) for x in nums))
    if len(nums) > 80:
        return jsonify(error="Maks 80 episode per ZIP — bagi jadi 2 ZIP"), 400
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "type": "zip", "slug": slug, "nums": nums, "workers": workers, "status": "queued", "progress": 5, "current": 0, "total": len(nums), "message": "Antri…", "created": time.time()}
    t = threading.Thread(target=_run_zip_job, args=(job_id, slug, nums, workers), daemon=True)
    t.start()
    return jsonify(job_id=job_id)


@app.post("/api/full")
def full_mp4():
    data = request.get_json(force=True, silent=True) or {}
    slug = (data.get("slug") or "").strip()
    nums = data.get("episodes") or []
    workers = int(data.get("workers") or 4)
    workers = max(1, min(workers, 8))
    if not slug or not nums:
        return jsonify(error="slug / episodes kosong"), 400
    if not SLUG_RE.fullmatch(slug):
        return jsonify(error="slug invalid"), 400
    nums = sorted(set(int(x) for x in nums))
    if len(nums) < 2:
        return jsonify(error="FULL butuh minimal 2 episode"), 400
    if len(nums) > 100:
        return jsonify(error="Maks 100 episode per FULL"), 400
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "type": "full", "slug": slug, "nums": nums, "workers": workers, "status": "queued", "progress": 5, "current": 0, "total": len(nums), "message": "Antri…", "created": time.time()}
    t = threading.Thread(target=_run_full_job, args=(job_id, slug, nums, workers), daemon=True)
    t.start()
    return jsonify(job_id=job_id)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify(error="job tidak ditemukan"), 404
        return jsonify({k: v for k, v in j.items() if k not in ("result_path",)})


@app.get("/api/download/<job_id>")
def download_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify(error="job tidak ditemukan"), 404
        if j.get("status") != "done":
            return jsonify(error=f"job belum siap: {j.get('status')}"), 400
        path = j.get("result_path")
        name = j.get("result_name") or f"{job_id}.bin"
    if not path or not pathlib.Path(path).exists():
        return jsonify(error="file sudah dihapus / expired"), 410
    mimetype = "video/mp4" if path.endswith(".mp4") else "application/zip"

    @after_this_request
    def cleanup(response):
        try:
            os.remove(path)
        except Exception:
            pass
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        return response

    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=name)


@app.get("/health")
def health():
    return jsonify(ok=True, cache=str(CACHE), ttl_hours=CACHE_TTL // 3600, max_mb=round(CACHE_MAX_BYTES / 1_048_576, 1))


@app.get("/api/cache/status")
def cache_status():
    total = 0
    count = 0
    try:
        for p in CACHE.rglob("Ep*.mp4"):
            try:
                if p.name.endswith(".tmp.mp4"):
                    continue
                total += p.stat().st_size
                count += 1
            except Exception:
                pass
    except Exception:
        pass
    return jsonify(cache=str(CACHE), files=count, bytes=total, mb=round(total / 1_048_576, 1), ttl_hours=CACHE_TTL // 3600, max_mb=round(CACHE_MAX_BYTES / 1_048_576, 1))


@app.post("/api/cache/clean")
def cache_clean():
    deleted, freed = _cache_sweep_once()
    return jsonify(deleted=deleted, freed_mb=round(freed / 1_048_576, 1))


if not getattr(app, "_sweeper_started", False):
    app._sweeper_started = True
    threading.Thread(target=_cache_sweeper_loop, daemon=True).start()
    try:
        _cache_sweep_once()
    except Exception:
        pass

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False, threaded=True)
