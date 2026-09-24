"""
Runs on Koyeb (persistent container with ffmpeg).

STRATEGY:
   1. Try Cobalt API (public instances) first — they already solve
      YouTube's bot-detection robustly and often return an instant
      direct download link.
   2. If Cobalt fails, fall back to local yt-dlp (with PO token
      provider + proxy + optional cookies, all configured via env vars).

ENDPOINTS:
   GET /api/search?q=...&limit=19
   GET /api/info?url=...
   GET /api/fetch?url=...&quality=720p&format=mp4
   GET /api/download?url=...
   GET /api/audio?url=...
   GET /files/{filename}
"""

import os
import re
import uuid
import base64
import asyncio
import threading
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="yt-dlp Download API")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL_SECONDS = 15 * 60
CREATOR = "ansadser"

POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL")
PROXY_URL = os.environ.get("PROXY_URL")

COOKIES_FILE_PATH = "/tmp/cookies.txt"
COOKIES_B64 = os.environ.get("COOKIES_B64")
if COOKIES_B64:
    try:
        with open(COOKIES_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
    except Exception as e:
        print(f"Failed to decode COOKIES_B64: {e}")
        COOKIES_B64 = None

QUALITIES = ["1080p", "720p", "480p", "360p", "144p"]

COBALT_APIS = [
    "https://cobalt.api.scity.gov.mn",
    "https://co.wuk.sh",
    "https://cobalt.tools",
    "https://nuko-c.meowing.de",
    "https://subito-c.meowing.de",
    "https://melon.clxxped.lol",
    "https://api-cobalt.eversiege.network",
    "https://api.qwkuns.me",
    "https://kitty.tame.gg",
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def clean_media_url(raw_url: str) -> str:
    if not raw_url:
        return raw_url
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", raw_url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return raw_url.split("?")[0]


# ---------- yt-dlp fallback helpers ----------

def base_opts(extra: dict) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        **extra,
    }
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}
        }
    if COOKIES_B64 and os.path.exists(COOKIES_FILE_PATH):
        opts["cookiefile"] = COOKIES_FILE_PATH
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


def schedule_cleanup(path: str, delay: int = FILE_TTL_SECONDS):
    def _delete():
        try:
            os.remove(path)
        except OSError:
            pass
    timer = threading.Timer(delay, _delete)
    timer.daemon = True
    timer.start()


def format_duration(seconds):
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ytdlp_get_info(url: str) -> dict:
    opts = base_opts({"skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def ytdlp_merge(url: str, quality: str) -> tuple:
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    if quality == "best":
        format_selector = "bestvideo+bestaudio/best"
    else:
        height = quality.replace("p", "")
        format_selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    opts = base_opts({
        "format": format_selector,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_path = ydl.prepare_filename(info)
        if not final_path.endswith(".mp4"):
            final_path = os.path.splitext(final_path)[0] + ".mp4"
    return final_path, info.get("title", "video")


def ytdlp_audio(url: str) -> tuple:
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    opts = base_opts({
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
    return final_path, info.get("title", "audio")


# ---------- Cobalt (primary strategy) ----------

async def cobalt_resolve(url: str, mode: str) -> dict | None:
    """Ask all Cobalt instances in parallel, return the first one that
    resolves a direct URL, or None if all fail."""
    target = clean_media_url(url)
    payload = {"url": target, "videoQuality": "720"}
    if mode == "audio":
        payload["downloadMode"] = "audio"
        payload["audioFormat"] = "mp3"

    async def call(api: str):
        async with httpx.AsyncClient(timeout=9) as client:
            r = await client.post(
                api, json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
            )
            data = r.json()
            if data and data.get("status") in ("tunnel", "redirect") and data.get("url"):
                return data
            raise ValueError("no usable url")

    tasks = [asyncio.create_task(call(api)) for api in COBALT_APIS]
    for coro in asyncio.as_completed(tasks):
        try:
            result = await coro
            for t in tasks:
                t.cancel()
            return result
        except Exception:
            continue
    return None


async def cobalt_download_file(direct_url: str, ext_hint: str) -> str:
    file_id = str(uuid.uuid4())
    final_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{ext_hint}")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", direct_url, headers={"User-Agent": USER_AGENT}) as resp:
            with open(final_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
    if not os.path.exists(final_path) or os.path.getsize(final_path) < 100:
        raise HTTPException(status_code=500, detail="Cobalt download produced an empty file")
    return final_path


async def get_media(url: str, mode: str, quality: str = "720p") -> tuple:
    """
    Tries Cobalt first, then falls back to local yt-dlp.
    Returns (file_path, title).
    """
    # 1) Try Cobalt
    try:
        resolved = await cobalt_resolve(url, mode)
        if resolved:
            ext = "mp3" if mode == "audio" else "mp4"
            fname = resolved.get("filename", "")
            if fname and "." in fname:
                ext = fname.rsplit(".", 1)[-1]
            path = await cobalt_download_file(resolved["url"], ext)
            title = os.path.splitext(resolved.get("filename", "media"))[0]
            return path, title
    except Exception as e:
        print(f"Cobalt failed: {e}")

    # 2) Fallback to yt-dlp
    if mode == "audio":
        return await asyncio.to_thread(ytdlp_audio, url)
    return await asyncio.to_thread(ytdlp_merge, url, quality)


# ---------- SEARCH ----------

@app.get("/api/search")
def search_youtube(q: str = Query(...), limit: int = Query(19)):
    opts = base_opts({"skip_download": True, "extract_flat": "in_playlist"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            search_result = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = search_result.get("entries", []) or []
    results = []
    for e in entries:
        if not e:
            continue
        video_id = e.get("id")
        thumbnails = e.get("thumbnails") or []
        image_url = thumbnails[-1]["url"] if thumbnails else (
            f"https://i.ytimg.com/vi/{video_id}/hq720.jpg" if video_id else None
        )
        results.append({
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"),
            "duration": format_duration(e.get("duration")),
            "imageUrl": image_url,
            "link": f"https://youtube.com/watch?v={video_id}" if video_id else e.get("url"),
        })

    return JSONResponse({
        "status": "success",
        "creator": CREATOR,
        "total": len(results),
        "result": results,
        "server": "servr-a",
    })


# ---------- INFO ----------

@app.get("/api/info")
async def api_info(request: Request, url: str = Query(...)):
    try:
        info = await asyncio.to_thread(ytdlp_get_info, url)
    except yt_dlp.utils.DownloadError:
        info = None

    base = str(request.base_url)
    downloads = []
    for q in QUALITIES:
        downloads.append({
            "quality": q,
            "format": "mp4",
            "url": f"{base}api/fetch?url={url}&quality={q}&format=mp4",
        })
    downloads.append({
        "quality": "Audio (192kbps)",
        "format": "mp3",
        "url": f"{base}api/fetch?url={url}&format=mp3",
    })

    if info:
        return JSONResponse({
            "status": True,
            "result": {
                "title": info.get("title"),
                "videoId": info.get("id"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "cached": False,
                "downloads": downloads,
            },
        })

    # metadata blocked, but downloads may still work via Cobalt
    return JSONResponse({
        "status": True,
        "result": {
            "title": None,
            "videoId": None,
            "duration": None,
            "thumbnail": None,
            "cached": False,
            "downloads": downloads,
        },
    })


# ---------- FETCH (does the real work, streams file back) ----------

@app.get("/api/fetch")
async def api_fetch(url: str = Query(...), quality: str = Query("720p"), format: str = Query("mp4")):
    mode = "audio" if format == "mp3" else "video"
    try:
        final_path, title = await get_media(url, mode, quality)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(final_path):
        raise HTTPException(status_code=500, detail="Processing failed, file not found")

    schedule_cleanup(final_path)
    media_type = "audio/mpeg" if format == "mp3" else "video/mp4"
    ext = "mp3" if format == "mp3" else "mp4"
    return FileResponse(final_path, media_type=media_type, filename=f"{title}.{ext}")


# ---------- DOWNLOAD (lists all quality links) ----------

@app.get("/api/download")
async def api_download(request: Request, url: str = Query(...)):
    try:
        info = await asyncio.to_thread(ytdlp_get_info, url)
    except yt_dlp.utils.DownloadError:
        info = {}

    base = str(request.base_url)
    downloads = []
    for q in QUALITIES:
        downloads.append({
            "quality": q,
            "format": "mp4",
            "url": f"{base}api/fetch?url={url}&quality={q}&format=mp4",
        })

    return JSONResponse({
        "status": "success",
        "creator": CREATOR,
        "title": info.get("title") if info else None,
        "duration": info.get("duration") if info else None,
        "downloads": downloads,
    })


# ---------- AUDIO ----------

@app.get("/api/audio")
async def api_audio(request: Request, url: str = Query(...)):
    try:
        final_path, title = await get_media(url, "audio")
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(final_path):
        raise HTTPException(status_code=500, detail="Conversion failed, file not found")

    schedule_cleanup(final_path)
    filename = os.path.basename(final_path)
    link = str(request.base_url) + f"files/{filename}"

    return JSONResponse({
        "status": "success",
        "Audio_url": link,
        "creator": CREATOR,
    })


# ---------- FILE SERVE ----------

@app.get("/files/{filename}")
def serve_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    media_type = "video/mp4" if filename.endswith(".mp4") else "audio/mpeg"
    return FileResponse(path, media_type=media_type, filename=filename)
