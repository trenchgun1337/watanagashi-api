"""
Watanagashi Archive — Download Backend
FastAPI + yt-dlp + spotdl
Supports: Spotify, YouTube, SoundCloud
"""

import re
import uuid
import shutil
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Watanagashi Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

TMP_DIR = Path(tempfile.gettempdir()) / "wata_downloads"
TMP_DIR.mkdir(exist_ok=True)


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


async def run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@app.get("/")
def root():
    return {"status": "ok", "message": "Watanagashi Downloader API is running."}


@app.post("/download")
async def download(req: DownloadRequest):
    url = req.url.strip()
    fmt = req.format if req.format in ("mp3", "flac", "ogg") else "mp3"

    job_id = uuid.uuid4().hex
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        source = detect_source(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if source == "spotify":
            cmd = [
                "spotdl",
                "--format", fmt,
                "--output", str(job_dir),
                url,
            ]

        elif source == "youtube":
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--add-metadata",
                # Contorna bloqueio de login / bot detection
                "--extractor-args", "youtube:player_client=android,ios;player_skip=web,configs",
                "--no-check-certificates",
                "--user-agent", "com.google.android.youtube/19.05.36 (Linux; U; Android 14; pt_BR; Pixel 7 Pro) gzip",
                "--force-ipv4",  # importante no Render
                "--no-playlist" if "list=" not in url else "--yes-playlist",
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

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=filename,
        )

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.on_event("startup")
async def cleanup_old_files():
    for f in TMP_DIR.glob("*.zip"):
        try:
            f.unlink()
        except Exception:
            pass
