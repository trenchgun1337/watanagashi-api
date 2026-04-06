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
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Watanagashi Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "X-Final-Filename", "X-Final-Mime"],
)

TMP_DIR = Path(tempfile.gettempdir()) / "wata_downloads"
TMP_DIR.mkdir(exist_ok=True)

COOKIES_FILE = TMP_DIR / "yt_cookies.txt"

_YT_COOKIES_B64: str = ""

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


def decode_cookies_b64(raw: str) -> bytes:
    cleaned = raw.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    cleaned += "=" * (-len(cleaned) % 4)
    decoded = base64.b64decode(cleaned)
    if b"\\n" in decoded:
        decoded = decoded.replace(b"\\n", b"\n")
    return decoded


def write_cookies(raw_b64: str) -> bool:
    try:
        decoded = decode_cookies_b64(raw_b64)
        TMP_DIR.mkdir(exist_ok=True)
        COOKIES_FILE.write_bytes(decoded)
        print(f"[cookies] Written {len(decoded)} bytes — {len(decoded.splitlines())} lines")
        return True
    except Exception as e:
        print(f"[cookies] Failed to decode/write: {e}")
        return False


def ensure_yt_cookies() -> bool:
    global _YT_COOKIES_B64
    if not _YT_COOKIES_B64:
        return False
    if not COOKIES_FILE.exists() or COOKIES_FILE.stat().st_size == 0:
        return write_cookies(_YT_COOKIES_B64)
    return True


def has_yt_cookies() -> bool:
    return ensure_yt_cookies()


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


# ── Debug endpoints ───────────────────────────────────────────────────────────
@app.get("/debug-yt")
async def debug_yt():
    ensure_yt_cookies()
    rc, out, err = await run([
        "yt-dlp",
        "--cookies", str(COOKIES_FILE),
        "--get-title",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ], cwd=str(TMP_DIR))
    return {"returncode": rc, "stdout": out[-1000:], "stderr": err[-1000:]}

@app.get("/debug-cookies")
async def debug_cookies():
    if not COOKIES_FILE.exists():
        return {"exists": False}
    content = COOKIES_FILE.read_text(errors="replace")
    lines = content.splitlines()
    return {
        "exists": True,
        "size_bytes": COOKIES_FILE.stat().st_size,
        "line_count": len(lines),
        "first_5_lines": lines[:5],
    }

@app.get("/debug-sp")
async def debug_sp():
    job_dir = TMP_DIR / "dbg_sp"
    job_dir.mkdir(exist_ok=True)

    async def generate():
        yield '{"status":"starting"}\n'
        proc = await asyncio.create_subprocess_exec(
            "spotdl", "download",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "--format", "mp3",
            "--output", str(job_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(job_dir),
        )
        done = asyncio.Event()
        async def waiter():
            await proc.wait()
            done.set()
        asyncio.create_task(waiter())
        while not done.is_set():
            yield '{"status":"working"}\n'
            try:
                await asyncio.wait_for(asyncio.shield(done.wait()), timeout=5)
            except asyncio.TimeoutError:
                pass
        stdout = (await proc.stdout.read()).decode(errors="replace")
        stderr = (await proc.stderr.read()).decode(errors="replace")
        import json
        yield json.dumps({"returncode": proc.returncode,
                          "stdout": stdout[-2000:],
                          "stderr": stderr[-2000:]}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


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

    if source == "youtube":
        if not ensure_yt_cookies():
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
            cmd = [
                "spotdl",
                "download",
                url,
                "--format", fmt,
                "--output", str(job_dir),
                "--save-errors", str(job_dir / "errors.txt"),
                "--audio", "youtube-music",
            ]

        # ── YouTube ──────────────────────────────────────────────────────────
        elif source == "youtube":
            is_playlist = "list=" in url

            base = [
                "yt-dlp",
                "--no-check-certificates",
                "--force-ipv4",
                "--cookies", str(COOKIES_FILE),
                "--retries", "10",
                "--fragment-retries", "10",
                "--concurrent-fragments", "4",
                "--no-warnings",
                "--newline",
            ]
            playlist_flag = ["--yes-playlist"] if is_playlist else ["--no-playlist"]
            out_tmpl = str(job_dir / "%(playlist_index)03d - %(title)s.%(ext)s")

            if is_video:
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
                # bestaudio sem restrição de codec — funciona com qualquer cliente
                cmd = base + [
                    "-f", "bestaudio/best",
                    "--extract-audio",
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
                "--retries", "5",
                "-o", str(job_dir / "%(uploader)s - %(title)s.%(ext)s"),
                url,
            ]

        returncode, stdout, stderr = await run(cmd, cwd=str(job_dir))

        if returncode != 0:
            shutil.rmtree(job_dir, ignore_errors=True)
            err_raw = re.sub(r'\x1b\[[0-9;]*m', '', stderr[-2000:]).strip()
            raise HTTPException(status_code=500,
                detail=f"Download failed.\n\n{err_raw}")

        files = [f for f in job_dir.glob("*") if f.name != "errors.txt" and f.is_file()]

        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=500,
                detail="No files generated. URL may be invalid, private, or region-locked.")

        if len(files) == 1 and is_video:
            fpath = files[0]
            mime = {"mp4":"video/mp4","webm":"video/webm","mkv":"video/x-matroska"}.get(fmt,"application/octet-stream")
            safe = re.sub(r"[^\w\-]", "_", fpath.stem[:60])
            return FileResponse(
                path=str(fpath), media_type=mime,
                filename=f"{safe}.{fmt}",
                headers={"Content-Length": str(fpath.stat().st_size)},
            )

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
    global _YT_COOKIES_B64

    for f in TMP_DIR.glob("*.zip"):
        try: f.unlink()
        except Exception: pass

    raw_b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if raw_b64:
        _YT_COOKIES_B64 = raw_b64
        ok = write_cookies(raw_b64)
        if not ok:
            print("[startup] YouTube cookies FAILED — YouTube will be disabled.")
    else:
        print("[startup] YT_COOKIES_B64 not set — YouTube downloads disabled.")
