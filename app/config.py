from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class PathMapping(BaseModel):
    source: str
    target: str
    regex: bool = False


class AppConfig(BaseModel):
    path_mappings: List[PathMapping] = Field(default_factory=list)
    model: str = "medium"
    language: Optional[str] = "ja"
    device: str = "cuda"
    compute_type: str = "float16"
    overwrite_existing: bool = False
    srt_suffix: str = ".ja.srt"
    max_concurrent_jobs: int = 1
    host: str = "0.0.0.0"
    port: int = 9876
    mux_subtitles: bool = True
    ffmpeg_path: str = "ffmpeg"
    subtitle_codec_map: Dict[str, str] = Field(
        default_factory=lambda: {
            ".mp4": "mov_text",
            ".m4v": "mov_text",
            ".mov": "mov_text",
            ".webm": "webvtt",
        }
    )

    def normalized(self) -> "AppConfig":
        if self.max_concurrent_jobs < 1:
            self.max_concurrent_jobs = 1
        if not self.srt_suffix.startswith("."):
            self.srt_suffix = "." + self.srt_suffix
        if not (1 <= self.port <= 65535):
            self.port = 9876
        return self


def load_config(config_path: Path) -> AppConfig:
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return AppConfig.parse_obj(data).normalized()
    return AppConfig().normalized()
