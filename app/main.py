from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from faster_whisper import WhisperModel

from .config import AppConfig, PathMapping, load_config

# --- NVIDIA DLL パスの追加 ---
if os.name == "nt":  # Windowsの場合
    import site

    site_packages = site.getsitepackages()
    for sp in site_packages:
        nvidia_path = Path(sp) / "nvidia"
        if nvidia_path.exists():
            for bin_path in nvidia_path.glob("**/bin"):
                os.add_dll_directory(str(bin_path))
# -------------------------

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

# AIの「幻覚」として除外したいフレーズ
BLACKLIST_KEYWORDS = [
    "ご視聴ありがとうございました",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録お願いいたします",
    "視聴ありがとうございました",
    "おやすみなさい",
    "チョコレート",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "最後までご視聴ありがとうございました",
]


class TranscriptionRequest(BaseModel):
    title: str = Field(..., description="Media title")
    itemId: str = Field(..., description="Jellyfin item ID")
    downloadUrl: Optional[str] = Field(None, description="Optional direct download URL")
    filePath: str = Field(..., description="Linux absolute file path")
    overwriteExisting: bool = Field(
        False, description="Overwrite existing SRT when true"
    )


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


def write_srt(
    segments, output_path: Path, *, duration: Optional[float], item_id: str
) -> None:
    progress_step = 5
    last_percent = -progress_step

    with output_path.open("w", encoding="utf-8") as handle:
        index = 1
        for segment in segments:
            text = segment.text.strip()

            # ブラクリストに含まれる文章、または空文字はSRTに書き込まない
            if text in BLACKLIST_KEYWORDS or not text:
                continue

            start = format_timestamp(segment.start)
            end = format_timestamp(segment.end)
            handle.write(f"{index}\n{start} --> {end}\n{text}\n\n")
            index += 1

            if duration and duration > 0:
                percent = int(min(100, (segment.end / duration) * 100))
                if percent >= last_percent + progress_step:
                    last_percent = percent
                    logger.info("Progress itemId=%s: %d%%", item_id, percent)

    if duration and duration > 0 and last_percent < 100:
        logger.info("Progress itemId=%s: 100%%", item_id)


def get_srt_path(media_path: Path) -> Path:
    srt_name = f"{media_path.stem}{CONFIG.srt_suffix}"
    return media_path.with_name(srt_name)


def pick_subtitle_codec(media_path: Path) -> Optional[str]:
    ext = media_path.suffix.lower()
    if CONFIG.subtitle_codec_map:
        return CONFIG.subtitle_codec_map.get(ext)
    if ext in {".mp4", ".m4v", ".mov"}:
        return "mov_text"
    if ext in {".webm"}:
        return "webvtt"
    return "srt"


def mux_subtitle_track(media_path: Path, srt_path: Path, item_id: str) -> None:
    if not CONFIG.mux_subtitles:
        return
    if not srt_path.exists():
        logger.warning("Mux skipped for itemId=%s (missing SRT): %s", item_id, srt_path)
        return

    temp_path = media_path.with_name(f"{media_path.stem}.muxing{media_path.suffix}")
    subtitle_codec = pick_subtitle_codec(media_path)
    if subtitle_codec is None:
        logger.info(
            "Mux skipped for itemId=%s (no codec mapping): %s",
            item_id,
            media_path,
        )
        return
    cmd = [
        CONFIG.ffmpeg_path,
        "-y",
        "-i",
        str(media_path),
        "-i",
        str(srt_path),
        "-map",
        "0",
        "-map",
        "-0:s",
        "-map",
        "1:0",
        "-c",
        "copy",
        "-c:s",
        subtitle_codec,
        "-metadata:s:s:0",
        "language=jpn",
        str(temp_path),
    ]
    logger.info(
        "Muxing subtitles itemId=%s codec=%s: %s",
        item_id,
        subtitle_codec,
        media_path,
    )
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Install ffmpeg or set ffmpeg_path in config.json."
        )
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        logger.error(
            "ffmpeg failed itemId=%s rc=%s stderr=%s",
            item_id,
            exc.returncode,
            stderr,
        )
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return

    try:
        os.replace(temp_path, media_path)
    except OSError:
        logger.exception("Failed to replace media after mux itemId=%s", item_id)
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return
    logger.info("Mux completed itemId=%s: %s", item_id, media_path)


def transcribe_task(request: TranscriptionRequest, mapped_path: str) -> None:
    with _SEMAPHORE:
        media_path = Path(mapped_path)
        srt_path = get_srt_path(media_path)
        allow_overwrite = CONFIG.overwrite_existing or request.overwriteExisting

        if srt_path.exists() and not allow_overwrite:
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

            # ハルシネーション対策を強化した transcribe 呼び出し
            segments, info = model.transcribe(
                str(media_path),
                language=CONFIG.language,
                beam_size=5,
                vad_filter=True,  # 音声区間検出を有効化（無音区間の誤出力を防止）
                vad_parameters=dict(
                    min_silence_duration_ms=1000
                ),  # 1秒以上の無音を対象
                condition_on_previous_text=False,  # 直前の誤った単語に引きずられないようにする
                initial_prompt="こんにちは。これは動画の会話を正確に書き起こしたものです。",  # 役割を誘導
            )

            logger.info(
                "Detected language=%s (prob=%.2f)",
                info.language,
                info.language_probability,
            )
            duration = getattr(info, "duration", None)
            write_srt(segments, srt_path, duration=duration, item_id=request.itemId)
            mux_subtitle_track(media_path, srt_path, request.itemId)
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
    allow_overwrite = CONFIG.overwrite_existing or request.overwriteExisting
    if srt_path.exists() and not allow_overwrite:
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
