"""
Watanagashi Archive — Download Backend
FastAPI + yt-dlp + spotdl
Supports: Spotify, YouTube, SoundCloud
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="Watanagashi Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

TMP_DIR = Path(tempfile.gettempdir()) / "wata_downloads"
TMP_DIR.mkdir(exist_ok=True)

COOKIES_FILE = TMP_DIR / "yt_cookies.txt"


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


# ── Health check — responde GET e HEAD ──────────────────────────────────────
@app.get("/")
@app.head("/")
def root():
    return {"status": "ok", "message": "Watanagashi Downloader API is running."}


@app.get("/yt-status")
@app.head("/yt-status")
def yt_status():
    """Returns whether YouTube cookies are configured on the server."""
    return {"youtube_enabled": has_yt_cookies()}


@app.post("/download")
async def download(req: DownloadRequest):
    url = req.url.strip()
    fmt = req.format if req.format in ("mp3", "flac", "ogg") else "mp3"

    try:
        source = detect_source(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if source == "youtube" and not has_yt_cookies():
        raise HTTPException(
            status_code=503,
            detail="YouTube downloads are not available at this time. The server administrator needs to configure authentication cookies."
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
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--add-metadata",
                "--cookies", str(COOKIES_FILE),
                "--no-check-certificates",
                "--force-ipv4",
                "--yes-playlist" if is_playlist else "--no-playlist",
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
            raise HTTPException(
                status_code=500,
                detail=f"Download failed.\n\n{stderr[-800:]}"
            )

        files = list(job_dir.glob("*"))
        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail="No files generated. Invalid or unavailable content."
            )

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
    # Limpa ZIPs antigos
    for f in TMP_DIR.glob("*.zip"):
        try:
            f.unlink()
        except Exception:
            pass

    # Decodifica cookies do env e salva em arquivo
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
