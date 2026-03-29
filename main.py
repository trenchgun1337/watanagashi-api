"""
Watanagashi Archive — Download Backend
FastAPI + yt-dlp + spotdl
Suporta: Spotify, YouTube, SoundCloud
"""

import os
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

# ── CORS: permite seu site Vercel chamar o backend ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # troque pelo seu domínio Vercel em produção
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Pasta temporária para os ZIPs gerados
TMP_DIR = Path(tempfile.gettempdir()) / "wata_downloads"
TMP_DIR.mkdir(exist_ok=True)


class DownloadRequest(BaseModel):
    url: str
    format: Optional[str] = "mp3"   # mp3 | flac | ogg


def detect_source(url: str) -> str:
    """Detecta se a URL é Spotify, YouTube ou SoundCloud."""
    if "spotify.com" in url:
        return "spotify"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "soundcloud.com" in url:
        return "soundcloud"
    raise ValueError("URL não reconhecida. Suportamos Spotify, YouTube e SoundCloud.")


async def run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """Executa um subprocesso de forma assíncrona."""
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

    # Pasta única para esta requisição
    job_id = uuid.uuid4().hex
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        source = detect_source(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if source == "spotify":
            # spotDL — baixa do Spotify via YouTube Music
            cmd = [
                "spotdl",
                "--format", fmt,
                "--output", str(job_dir),
                url,
            ]
        elif source == "youtube":
            # yt-dlp — extrai áudio do YouTube
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", fmt,
                "--audio-quality", "0",
                "--embed-thumbnail",
                "--add-metadata",
                "-o", str(job_dir / "%(playlist_index)s - %(title)s.%(ext)s"),
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
            # Limpa e retorna erro legível
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail=f"Erro ao baixar. Verifique a URL.\n\n{stderr[-800:]}"
            )

        # Verifica se algum arquivo foi gerado
        files = list(job_dir.glob("*"))
        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Nenhum arquivo gerado. URL inválida ou conteúdo indisponível.")

        # Comprime tudo em ZIP
        zip_path = TMP_DIR / f"{job_id}.zip"
        shutil.make_archive(str(TMP_DIR / job_id), "zip", str(job_dir))

        # Limpa pasta individual, mantém só o ZIP
        shutil.rmtree(job_dir, ignore_errors=True)

        if not zip_path.exists():
            raise HTTPException(status_code=500, detail="Falha ao criar ZIP.")

        # Gera nome limpo para o arquivo
        safe_name = re.sub(r"[^\w\-]", "_", url.split("/")[-1] or "download")
        filename = f"{safe_name}_{fmt}.zip"

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=filename,
            background=None,  # o arquivo é deletado após envio abaixo
        )

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@app.on_event("startup")
async def cleanup_old_files():
    """Limpa ZIPs antigos ao iniciar (evita acúmulo no disco)."""
    for f in TMP_DIR.glob("*.zip"):
        try:
            f.unlink()
        except Exception:
            pass
