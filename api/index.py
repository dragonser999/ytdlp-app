import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import yt_dlp

app = FastAPI(title="yt-dlp API")

POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://127.0.0.1:4416")

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extractor_args": {
        "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}
    },
}


def extract(url: str, extra_opts: dict | None = None) -> dict:
    opts = {**BASE_OPTS, **(extra_opts or {})}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/info")
def get_info(url: str = Query(..., description="Video URL")):
    info = extract(url)
    return JSONResponse({
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "formats": [
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            }
            for f in info.get("formats", [])
        ],
    })


@app.get("/download")
def get_download_url(
    url: str = Query(..., description="Video URL"),
    fmt: str = Query("mp4", description="'mp3' for audio-only, 'mp4' for video"),
):
    if fmt == "mp3":
        format_selector = "bestaudio/best"
    else:
        format_selector = "best[ext=mp4]/best"

    info = extract(url, {"format": format_selector})

    direct_url = info.get("url")
    if not direct_url and info.get("requested_formats"):
        direct_url = info["requested_formats"][0].get("url")

    if not direct_url:
        raise HTTPException(status_code=404, detail="No direct URL found for this format")

    return {
        "title": info.get("title"),
        "format": fmt,
        "url": direct_url,
        "filesize": info.get("filesize") or info.get("filesize_approx"),
    }
