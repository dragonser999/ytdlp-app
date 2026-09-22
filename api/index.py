import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import yt_dlp

app = FastAPI(title="yt-dlp PO Token API")

# PO Token Provider Server URL (Docker അല്ലെങ്കിൽ Local Server)
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://127.0.0.1:4416")

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    # YouTube Extractor Args - PO Token & Provider integration
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "mweb", "ios"],
        },
        "youtubepot-bgutilhttp": {
            "base_url": [POT_PROVIDER_URL]
        }
    },
}


def extract(url: str, extra_opts: dict | None = None) -> dict:
    # BASE_OPTS കൂടെ എക്സ്ട്രാ ഓപ്ഷൻസ് മെർജ് ചെയ്യുന്നു
    opts = {**BASE_OPTS, **(extra_opts or {})}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/")
def home():
    return {"status": "running", "message": "yt-dlp API with PO Token Provider is Active"}


@app.get("/info")
def get_info(url: str = Query(..., description="Video URL")):
    info = extract(url)
    
    formats_list = []
    for f in info.get("formats", []):
        formats_list.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "url": f.get("url")
        })

    return JSONResponse({
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "formats": formats_list,
    })


@app.get("/download")
def get_download_url(
    url: str = Query(..., description="Video URL"),
    fmt: str = Query("mp4", description="'mp3' for audio-only, 'mp4' for progressive video"),
):
    # Progressive (Audio + Video ഒന്നിച്ച്) ഉള്ള ഫീഡ് എടുക്കുന്നു
    if fmt == "mp3":
        format_selector = "bestaudio/best"
    else:
        # best[ext=mp4] / bestvideo+bestaudio
        format_selector = "best[ext=mp4][vcodec!=none][acodec!=none]/best"

    info = extract(url, {"format": format_selector})

    direct_url = info.get("url")
    
    # Direct URL ഇല്ലെങ്കിൽ requested_formats പരിശോധിക്കുന്നു
    streams = []
    if not direct_url and info.get("requested_formats"):
        for req in info["requested_formats"]:
            streams.append({
                "format_id": req.get("format_id"),
                "ext": req.get("ext"),
                "vcodec": req.get("vcodec"),
                "acodec": req.get("acodec"),
                "url": req.get("url")
            })
        direct_url = streams[0].get("url") if streams else None

    if not direct_url:
        raise HTTPException(status_code=404, detail="No direct URL found for this format")

    return {
        "title": info.get("title"),
        "format": fmt,
        "url": direct_url,
        "streams": streams if streams else None,
        "filesize": info.get("filesize") or info.get("filesize_approx"),
    }
