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

AUDIO_FMTS = {"mp3", "flac", "ogg", "opus", "aac"}
VIDEO_FMTS = {"mp4", "webm", "mkv"}
ALL_FMTS   = AUDIO_FMTS | VIDEO_FMTS


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


# ── Health / robots / favicon ────────────────────────────────────────────────
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
    fmt = req.format if req.format in ALL_FMTS else "mp3"
    is_video = fmt in VIDEO_FMTS

    try:
        source = detect_source(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if source == "youtube" and not has_yt_cookies():
        raise HTTPException(status_code=503,
            detail="YouTube downloads are not available. Server cookies not configured.")

    if is_video and source != "youtube":
        raise HTTPException(status_code=400,
            detail="Video formats (mp4/webm/mkv) are only supported for YouTube URLs.")

    job_id  = uuid.uuid4().hex
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Spotify ──────────────────────────────────────────────────────────
        if source == "spotify":
            # spotdl handles tracks, albums, playlists, artist pages natively
            # Use --save-errors to avoid crashing on single bad tracks in albums
            cmd = [
                "spotdl",
                "--format", fmt,
                "--output", str(job_dir),
                "--save-errors", str(job_dir / "errors.txt"),
                url,
            ]

        # ── YouTube ──────────────────────────────────────────────────────────
        elif source == "youtube":
            is_playlist = "list=" in url

            # Base flags — use ios client to bypass n-sig / JS challenge
            base = [
                "yt-dlp",
                "--no-check-certificates",
                "--force-ipv4",
                "--cookies", str(COOKIES_FILE),
                # ios client avoids the n-challenge that breaks web player
                "--extractor-args", "youtube:player_client=ios,web",
                "--retries", "10",
                "--fragment-retries", "10",
                "--concurrent-fragments", "4",
                "--no-warnings",
            ]
            playlist_flag = ["--yes-playlist"] if is_playlist else ["--no-playlist"]
            out_tmpl = str(job_dir / "%(playlist_index)03d - %(title)s.%(ext)s")

            if is_video:
                # Best available video+audio merged to target container
                fmt_sel = (
                    f"bestvideo[ext={fmt}]+bestaudio[ext=m4a]"
                    f"/bestvideo[ext={fmt}]+bestaudio"
                    f"/bestvideo+bestaudio"
                    f"/best"
                )
                cmd = base + [
                    "-f", fmt_sel,
                    "--merge-output-format", fmt,
                    "--add-metadata",
                    "--embed-thumbnail",
                ] + playlist_flag + ["-o", out_tmpl, url]
            else:
                cmd = base + [
                    "-x",
                    "--audio-format", fmt,
                    "--audio-quality", "0",
                    "--embed-thumbnail",
                    "--add-metadata",
                ] + playlist_flag + ["-o", out_tmpl, url]

        # ── SoundCloud ───────────────────────────────────────────────────────
        else:
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--add-metadata",
                "--no-warnings",
                "-o", str(job_dir / "%(uploader)s - %(title)s.%(ext)s"),
                url,
            ]

        returncode, stdout, stderr = await run(cmd, cwd=str(job_dir))

        # Non-zero return is a hard error
        if returncode != 0:
            shutil.rmtree(job_dir, ignore_errors=True)
            # Show last 1000 chars of stderr, strip ANSI
            err_raw = re.sub(r'\x1b\[[0-9;]*m', '', stderr[-1000:]).strip()
            raise HTTPException(status_code=500,
                detail=f"Download failed.\n\n{err_raw}")

        # Collect generated files (exclude our errors.txt helper)
        files = [f for f in job_dir.glob("*") if f.name != "errors.txt" and f.is_file()]

        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=500,
                detail="No files generated. URL may be invalid, private, or region-locked.")

        # ── Single video: return the file directly (no ZIP) ──────────────────
        if len(files) == 1 and is_video:
            fpath = files[0]
            mime = {"mp4":"video/mp4","webm":"video/webm","mkv":"video/x-matroska"}.get(fmt,"application/octet-stream")
            safe = re.sub(r"[^\w\-]", "_", fpath.stem[:60])
            return FileResponse(
                path=str(fpath), media_type=mime,
                filename=f"{safe}.{fmt}",
                headers={"Content-Length": str(fpath.stat().st_size)},
            )

        # ── Single audio track: return as-is (no ZIP) ────────────────────────
        if len(files) == 1:
            fpath = files[0]
            mime_map = {"mp3":"audio/mpeg","flac":"audio/flac","ogg":"audio/ogg",
                        "opus":"audio/opus","aac":"audio/aac"}
            mime = mime_map.get(fmt, "application/octet-stream")
            safe = re.sub(r"[^\w\-]", "_", fpath.stem[:80])
            return FileResponse(
                path=str(fpath), media_type=mime,
                filename=f"{safe}.{fmt}",
                headers={"Content-Length": str(fpath.stat().st_size)},
            )

        # ── Multiple files: ZIP ───────────────────────────────────────────────
        zip_path = TMP_DIR / f"{job_id}.zip"
        shutil.make_archive(str(TMP_DIR / job_id), "zip", str(job_dir))
        shutil.rmtree(job_dir, ignore_errors=True)

        if not zip_path.exists():
            raise HTTPException(status_code=500, detail="Failed to create ZIP archive.")

        safe_name = re.sub(r"[^\w\-]", "_", url.split("/")[-1] or "download")
        return FileResponse(
            path=str(zip_path), media_type="application/zip",
            filename=f"{safe_name}_{fmt}.zip",
            headers={"Content-Length": str(zip_path.stat().st_size)},
        )

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.on_event("startup")
async def setup():
    # Clean stale ZIPs from previous runs
    for f in TMP_DIR.glob("*.zip"):
        try: f.unlink()
        except Exception: pass

    # Load YouTube cookies from env
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
