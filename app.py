"""
Runs on Koyeb (persistent container with ffmpeg).

ENDPOINTS:
   GET /api/search?q=...&limit=19
   GET /api/info?url=...
       -> lists available qualities with URLs pointing back to /api/fetch
          (actual download+merge happens lazily when that URL is opened)
   GET /api/fetch?url=...&quality=720p&format=mp4
       -> does the real download+merge (or audio extraction) and streams
          the file back directly (this is what the links in /api/info
          "downloads" point to)
   GET /api/download?url=...&quality=best
       -> downloads + merges now, returns {"status","download","creator"}
   GET /api/audio?url=...
       -> downloads + converts to mp3 now, returns {"status","Audio_url","creator"}
   GET /files/{filename}
       -> serves an already-merged file (used internally by /api/download, /api/audio)

Files in /tmp/downloads are auto-deleted 15 minutes after creation.
"""

import os
import uuid
import base64
import threading
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="yt-dlp Download API")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL_SECONDS = 15 * 60
CREATOR = "ansadser"

POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL")  # optional

# Cookies (optional, base64-encoded in the COOKIES_B64 env var so the
# real cookies.txt never has to be committed to the public repo).
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


def base_opts(extra: dict) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        **extra,
    }
    # Let yt-dlp pick the best client automatically (its defaults handle
    # this better than forcing one) — just supply the PO token provider
    # so it can complete whichever client needs a token.
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
            "youtube": {"player_client": ["android", "web"]},
        }
    else:
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
    if COOKIES_B64 and os.path.exists(COOKIES_FILE_PATH):
        opts["cookiefile"] = COOKIES_FILE_PATH
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


def do_merge(url: str, quality: str) -> tuple:
    """Downloads + merges video, returns (final_path, info)."""
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    if quality == "best":
        format_selector = "bestvideo+bestaudio/best"
    else:
        height = quality.replace("p", "")
        format_selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"

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
    return final_path, info


def do_audio(url: str) -> tuple:
    """Downloads + converts to mp3, returns (final_path, info)."""
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
    return final_path, info


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


# ---------- INFO (lists qualities, lazy links) ----------

@app.get("/api/info")
def api_info(request: Request, url: str = Query(...)):
    opts = base_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

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


# ---------- FETCH (actually does the work, streams file back) ----------

@app.get("/api/fetch")
def api_fetch(url: str = Query(...), quality: str = Query("best"), format: str = Query("mp4")):
    try:
        if format == "mp3":
            final_path, info = do_audio(url)
            media_type = "audio/mpeg"
        else:
            final_path, info = do_merge(url, quality)
            media_type = "video/mp4"
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(final_path):
        raise HTTPException(status_code=500, detail="Processing failed, file not found")

    schedule_cleanup(final_path)
    title = info.get("title", "file")
    ext = "mp3" if format == "mp3" else "mp4"
    return FileResponse(final_path, media_type=media_type, filename=f"{title}.{ext}")


# ---------- DOWNLOAD (lists all quality links together) ----------

@app.get("/api/download")
def api_download(request: Request, url: str = Query(...)):
    opts = base_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
        "title": info.get("title"),
        "duration": info.get("duration"),
        "downloads": downloads,
    })


# ---------- AUDIO (JSON with a link) ----------

@app.get("/api/audio")
def api_audio(request: Request, url: str = Query(...)):
    try:
        final_path, info = do_audio(url)
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
