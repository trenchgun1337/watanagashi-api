"""
Watanagashi Archive — Download Backend
FastAPI + yt-dlp + spotdl
Supports: Spotify, YouTube (audio + video), SoundCloud
"""

import os
import re
import uuid
import base64
import shutil
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

app = FastAPI(title="Watanagashi Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)

TMP_DIR = Path(tempfile.gettempdir()) / "wata_downloads"
TMP_DIR.mkdir(exist_ok=True)

COOKIES_FILE = TMP_DIR / "yt_cookies.txt"

AUDIO_FORMATS = {"mp3", "flac", "ogg", "opus", "aac"}
VIDEO_FORMATS = {"mp4", "webm", "mkv"}
ALL_FORMATS   = AUDIO_FORMATS | VIDEO_FORMATS


class DownloadRequest(BaseModel):
    url: str
    format: Optional[str] = "mp3"


def detect_source(url: str) -> str:
    if "spotify.com" in url:
        return "spotify"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "soundcloud.com" in url:
        return "soundcloud"
    raise ValueError("URL not recognized. Supported: Spotify, YouTube, SoundCloud.")


def has_yt_cookies() -> bool:
    return COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0


async def run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
@app.head("/")
def root():
    return {"status": "ok", "message": "Watanagashi Downloader API is running."}


@app.get("/robots.txt")
@app.head("/robots.txt")
def robots():
    return Response(content="User-agent: *\nDisallow: /", media_type="text/plain")


@app.get("/favicon.ico")
@app.head("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/yt-status")
@app.head("/yt-status")
def yt_status():
    return {"youtube_enabled": has_yt_cookies()}


# ── Download ──────────────────────────────────────────────────────────────────
@app.post("/download")
async def download(req: DownloadRequest):
    url = req.url.strip()
    fmt = req.format if req.format in ALL_FORMATS else "mp3"
    is_video = fmt in VIDEO_FORMATS

    try:
        source = detect_source(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if source == "youtube" and not has_yt_cookies():
        raise HTTPException(
            status_code=503,
            detail="YouTube downloads are not available at this time. The server administrator needs to configure authentication cookies."
        )

    if is_video and source != "youtube":
        raise HTTPException(
            status_code=400,
            detail="Video formats (mp4/webm/mkv) are only supported for YouTube URLs."
        )

    job_id = uuid.uuid4().hex
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        if source == "spotify":
            cmd = [
                "spotdl",
                "--format", fmt,
                "--output", str(job_dir),
                url,
            ]

        elif source == "youtube":
            is_playlist = "list=" in url

            # Common yt-dlp flags
            base_flags = [
                "yt-dlp",
                "--no-check-certificates",
                "--force-ipv4",
                "--cookies", str(COOKIES_FILE),
                # Use the extractor-args to pass po_token / bypass n-sig issues
                "--extractor-args", "youtube:player_client=web,default",
                # Retry on fragment errors
                "--retries", "5",
                "--fragment-retries", "5",
            ]

            playlist_flag = ["--yes-playlist"] if is_playlist else ["--no-playlist"]

            if is_video:
                # Download best video + audio and merge
                format_str = "bestvideo[ext={ext}]+bestaudio/best[ext={ext}]/bestvideo+bestaudio/best".format(ext=fmt)
                cmd = base_flags + [
                    "-f", format_str,
                    "--merge-output-format", fmt,
                    "--add-metadata",
                    "--embed-thumbnail",
                ] + playlist_flag + [
                    "-o", str(job_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
                    url,
                ]
            else:
                cmd = base_flags + [
                    "-x",
                    "--audio-format", fmt,
                    "--audio-quality", "0",
                    "--embed-thumbnail",
                    "--add-metadata",
                ] + playlist_flag + [
                    "-o", str(job_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
                    url,
                ]

        else:  # soundcloud
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--add-metadata",
                "-o", str(job_dir / "%(uploader)s - %(title)s.%(ext)s"),
                url,
            ]

        returncode, stdout, stderr = await run(cmd, cwd=str(job_dir))

        if returncode != 0:
            shutil.rmtree(job_dir, ignore_errors=True)
            # Clean up error message for end users
            err_msg = stderr[-1000:].strip()
            raise HTTPException(
                status_code=500,
                detail=f"Download failed.\n\n{err_msg}"
            )

        files = list(job_dir.glob("*"))
        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail="No files generated. Invalid or unavailable content."
            )

        # If single file and it's a video, return directly (no zip)
        if len(files) == 1 and is_video:
            file_path = files[0]
            file_size = file_path.stat().st_size
            safe_name = re.sub(r"[^\w\-]", "_", file_path.stem[:60])
            filename = f"{safe_name}.{fmt}"
            mime = {
                "mp4": "video/mp4",
                "webm": "video/webm",
                "mkv": "video/x-matroska",
            }.get(fmt, "application/octet-stream")
            return FileResponse(
                path=str(file_path),
                media_type=mime,
                filename=filename,
                headers={"Content-Length": str(file_size)},
            )

        # ZIP everything else
        zip_path = TMP_DIR / f"{job_id}.zip"
        shutil.make_archive(str(TMP_DIR / job_id), "zip", str(job_dir))
        shutil.rmtree(job_dir, ignore_errors=True)

        if not zip_path.exists():
            raise HTTPException(status_code=500, detail="Failed to create ZIP.")

        safe_name = re.sub(r"[^\w\-]", "_", url.split("/")[-1] or "download")
        filename = f"{safe_name}_{fmt}.zip"

        file_size = zip_path.stat().st_size
        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=filename,
            headers={"Content-Length": str(file_size)},
        )

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.on_event("startup")
async def setup():
    # Clean old ZIPs
    for f in TMP_DIR.glob("*.zip"):
        try:
            f.unlink()
        except Exception:
            pass

    # Decode YT cookies from env
    b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if b64:
        try:
            decoded = base64.b64decode(b64)
            COOKIES_FILE.write_bytes(decoded)
            print(f"[startup] YouTube cookies loaded ({len(decoded)} bytes).")
        except Exception as e:
            print(f"[startup] Failed to decode YT_COOKIES_B64: {e}")
    else:
        print("[startup] YT_COOKIES_B64 not set — YouTube downloads disabled.")
