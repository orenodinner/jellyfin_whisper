from __future__ import annotations

import os
import socket
from pathlib import Path

import uvicorn

from app.config import load_config


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("JELLYFINORED_CONFIG", ROOT_DIR / "config.json")).expanduser()


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "0.0.0.0"


def main() -> None:
    config = load_config(CONFIG_PATH)
    host = config.host or get_local_ip()
    port = config.port
    print(f"Starting server on http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
