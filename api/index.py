"""
yt-dlp API wrapper with PO Token support, deployable on Vercel.

ARCHITECTURE:
   Vercel serverless functions can't run a persistent background process,
   so the PO Token provider (bgutil, a Node.js server) CANNOT live inside
   this same deployment. Run it separately (e.g. a small Railway/Render/VPS
   instance) and point this API at it via the POT_PROVIDER_URL env var.

SETUP:

1. Deploy the PO Token provider somewhere with a persistent process
   (Railway, Render, Fly.io, or any small VPS — NOT Vercel):
       docker run -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
   This gives you a public URL, e.g. https://your-pot-provider.up.railway.app

2. In Vercel, set an environment variable:
       POT_PROVIDER_URL = https://your-pot-provider.up.railway.app

3. Install deps:
   pip install fastapi uvicorn yt-dlp bgutil-ytdlp-pot-provider

4. Run locally to test:
   POT_PROVIDER_URL=http://127.0.0.1:4416 python ytdlp_api.py
   -> visit http://127.0.0.1:8000/docs

ENDPOINTS:
   GET /info?url=...          -> video metadata + available formats
   GET /download?url=...&fmt=mp3|mp4  -> direct extracted stream URL

NOTE: even with a PO Token, Vercel's shared IPs may still get rate-limited
under heavy traffic. If that happens, hosting the whole API (not just the
PO token provider) outside Vercel is the more reliable fix.
"""

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
    # bgutil plugin reads this to know where the PO token provider server is
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

    # When a single format is resolved, yt-dlp puts the direct stream URL here
    direct_url = info.get("url")
    if not direct_url and info.get("requested_formats"):
        # merged formats (video+audio) don't have one single url
        direct_url = info["requested_formats"][0].get("url")

    if not direct_url:
        raise HTTPException(status_code=404, detail="No direct URL found for this format")

    return {
        "title": info.get("title"),
        "format": fmt,
        "url": direct_url,
        "filesize": info.get("filesize") or info.get("filesize_approx"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, hos
                t="0.0.0.0", port=8000)
