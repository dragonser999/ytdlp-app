"""
Runs on Koyeb (persistent container with ffmpeg) — not Vercel.

ENDPOINTS:
   GET /info?url=...                        -> video metadata + formats (no download)
   GET /download?url=...&quality=720p        -> downloads + merges (ffmpeg) + returns the mp4 file
   GET /download_audio?url=...               -> downloads + converts to mp3 + returns the file

quality options for /download: "best", "1080p", "720p", "480p", "360p"

Files are written to a temp folder, sent back as the response body, then
deleted right after — nothing is kept between requests.
"""

import os
import uuid
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="yt-dlp Download API")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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


def cleanup(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


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
def download_video(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    quality: str = Query("best"),
):
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

    background_tasks.add_task(cleanup, final_path)
    title = info.get("title", "video")
    return FileResponse(
        final_path,
        media_type="video/mp4",
        filename=f"{title}.mp4",
        background=background_tasks,
    )


@app.get("/download_audio")
def download_audio(background_tasks: BackgroundTasks, url: str = Query(...)):
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

    background_tasks.add_task(cleanup, final_path)
    title = info.get("title", "audio")
    return FileResponse(
        final_path,
        media_type="audio/mpeg",
        filename=f"{title}.mp3",
        background=background_tasks,
    )
