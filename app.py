"""
Runs on Koyeb (persistent container with ffmpeg).

ENDPOINTS:
   GET /info?url=...                    -> video metadata (no download)
   GET /download?url=...&quality=720p   -> downloads + merges (ffmpeg),
                                            returns JSON with a download_url
   GET /download_audio?url=...          -> downloads + converts to mp3,
                                            returns JSON with a download_url
   GET /files/{filename}                -> serves the actual merged/converted file

Files are auto-deleted 15 minutes after creation (enough time for your
bot to fetch and forward them), via a background timer.

quality options for /download: "best", "1080p", "720p", "480p", "360p"
"""

import os
import uuid
import threading
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="yt-dlp Download API")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL_SECONDS = 15 * 60  # auto-delete after 15 min

POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL")  # optional


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


@app.get("/api/search")
def search_youtube(q: str = Query(...), limit: int = Query(19)):
    opts = base_opts({
        "skip_download": True,
        "extract_flat": "in_playlist",
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            search_result = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = search_result.get("entries", [])
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
        "creator": "ansadser",
        "total": len(results),
        "result": results,
        "server": "servr-a",
    })


@app.get("/info")
def get_info(url: str = Query(...)):
    opts = base_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return JSONResponse({
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
    })


@app.get("/download")
def download_video(request: Request, url: str = Query(...), quality: str = Query("best")):
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

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = ydl.prepare_filename(info)
            if not final_path.endswith(".mp4"):
                final_path = os.path.splitext(final_path)[0] + ".mp4"
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(final_path):
        raise HTTPException(status_code=500, detail="Merge failed, file not found")

    schedule_cleanup(final_path)
    filename = os.path.basename(final_path)
    download_url = str(request.base_url) + f"files/{filename}"

    return JSONResponse({
        "title": info.get("title"),
        "duration": info.get("duration"),
        "filesize": os.path.getsize(final_path),
        "download_url": download_url,
        "expires_in_seconds": FILE_TTL_SECONDS,
    })


@app.get("/download_audio")
def download_audio(request: Request, url: str = Query(...)):
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

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(final_path):
        raise HTTPException(status_code=500, detail="Conversion failed, file not found")

    schedule_cleanup(final_path)
    filename = os.path.basename(final_path)
    download_url = str(request.base_url) + f"files/{filename}"

    return JSONResponse({
        "title": info.get("title"),
        "filesize": os.path.getsize(final_path),
        "download_url": download_url,
        "expires_in_seconds": FILE_TTL_SECONDS,
    })


@app.get("/files/{filename}")
def serve_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found or expired")

    media_type = "video/mp4" if filename.endswith(".mp4") else "audio/mpeg"
    return FileResponse(path, media_type=media_type, filename=filename)


def _fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@app.get("/api/search")
def search_youtube(url: str = Query(..., description="Search text (not an actual URL)"), limit: int = Query(20)):
    """
    Searches YouTube by text. Param is named 'url' to match the existing
    client integration, but it's actually the search query string.
    """
    opts = base_opts({"skip_download": True, "extract_flat": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            search_result = ydl.extract_info(f"ytsearch{limit}:{url}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    entries = search_result.get("entries", []) or []
    result = []
    for e in entries:
        video_id = e.get("id")
        result.append({
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"),
            "duration": _fmt_duration(e.get("duration")),
            "imageUrl": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{video_id}/hq720.jpg" if video_id else None),
            "link": f"https://youtube.com/watch?v={video_id}" if video_id else e.get("url"),
        })

    return JSONResponse({
        "status": "success",
        "creator": "ansadser",
        "total": len(result),
        "result": result,
        "server": "servr-a",
    })
