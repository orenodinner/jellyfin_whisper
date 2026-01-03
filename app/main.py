from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from faster_whisper import WhisperModel

from .config import AppConfig, PathMapping, load_config

# --- NVIDIA DLL パスの追加 (ここから追加) ---
if os.name == "nt":  # Windowsの場合
    import site

    # 仮想環境内またはシステムのsite-packagesからnvidia関連のbinディレクトリを探す
    site_packages = site.getsitepackages()
    for sp in site_packages:
        nvidia_path = Path(sp) / "nvidia"
        if nvidia_path.exists():
            # cublasやcudnnのbinフォルダを探してDLLディレクトリとして追加
            for bin_path in nvidia_path.glob("**/bin"):
                os.add_dll_directory(str(bin_path))
# --- NVIDIA DLL パスの追加 (ここまで) ---


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(
    os.getenv("JELLYFINORED_CONFIG", ROOT_DIR / "config.json")
).expanduser()
CONFIG: AppConfig = load_config(CONFIG_PATH)

logger = logging.getLogger("jellyfinored")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_MODEL: Optional[WhisperModel] = None
_MODEL_LOCK = threading.Lock()
_SEMAPHORE = threading.Semaphore(CONFIG.max_concurrent_jobs)


class TranscriptionRequest(BaseModel):
    title: str = Field(..., description="Media title")
    itemId: str = Field(..., description="Jellyfin item ID")
    downloadUrl: Optional[str] = Field(None, description="Optional direct download URL")
    filePath: str = Field(..., description="Linux absolute file path")


class TranscriptionResponse(BaseModel):
    accepted: bool
    message: str
    mappedPath: str


app = FastAPI(title="JellyfinORED Transcription Server")


def get_model() -> WhisperModel:
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                logger.info(
                    "Loading model '%s' (device=%s, compute_type=%s)",
                    CONFIG.model,
                    CONFIG.device,
                    CONFIG.compute_type,
                )
                _MODEL = WhisperModel(
                    CONFIG.model, device=CONFIG.device, compute_type=CONFIG.compute_type
                )
    return _MODEL


def map_path(input_path: str, mappings: list[PathMapping]) -> str:
    for mapping in mappings:
        if mapping.regex:
            updated = re.sub(mapping.source, mapping.target, input_path)
            if updated != input_path:
                return updated
        else:
            if input_path.startswith(mapping.source):
                suffix = input_path[len(mapping.source) :].lstrip("/\\")
                return str(Path(mapping.target) / suffix)
    return input_path


def format_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000.0))
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments, output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        for index, segment in enumerate(segments, start=1):
            start = format_timestamp(segment.start)
            end = format_timestamp(segment.end)
            text = segment.text.strip()
            handle.write(f"{index}\n{start} --> {end}\n{text}\n\n")


def get_srt_path(media_path: Path) -> Path:
    srt_name = f"{media_path.stem}{CONFIG.srt_suffix}"
    return media_path.with_name(srt_name)


def transcribe_task(request: TranscriptionRequest, mapped_path: str) -> None:
    with _SEMAPHORE:
        media_path = Path(mapped_path)
        srt_path = get_srt_path(media_path)

        if srt_path.exists() and not CONFIG.overwrite_existing:
            logger.info(
                "Skipping existing SRT for itemId=%s: %s", request.itemId, srt_path
            )
            return

        if not media_path.exists():
            logger.error(
                "Media file not found for itemId=%s: %s", request.itemId, media_path
            )
            return

        try:
            model = get_model()
            logger.info("Transcribing itemId=%s path=%s", request.itemId, media_path)
            segments, info = model.transcribe(str(media_path), language=CONFIG.language)
            logger.info(
                "Detected language=%s (prob=%.2f)",
                info.language,
                info.language_probability,
            )
            write_srt(segments, srt_path)
            logger.info("SRT written for itemId=%s: %s", request.itemId, srt_path)
        except Exception:
            logger.exception("Transcription failed for itemId=%s", request.itemId)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe(
    request: TranscriptionRequest, background_tasks: BackgroundTasks
) -> TranscriptionResponse:
    mapped_path = map_path(request.filePath, CONFIG.path_mappings)
    media_path = Path(mapped_path)

    if not media_path.exists():
        raise HTTPException(
            status_code=404, detail=f"File not found after mapping: {mapped_path}"
        )

    srt_path = get_srt_path(media_path)
    if srt_path.exists() and not CONFIG.overwrite_existing:
        return TranscriptionResponse(
            accepted=False,
            message=f"SRT already exists, skipping: {srt_path}",
            mappedPath=str(media_path),
        )

    background_tasks.add_task(transcribe_task, request, str(media_path))
    return TranscriptionResponse(
        accepted=True,
        message="Transcription started",
        mappedPath=str(media_path),
    )
