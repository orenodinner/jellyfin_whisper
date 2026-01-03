# JellyfinORED 文字起こしサーバー

文字起こしリクエストを受け取り、Linux パスを Windows パスへ変換し、faster-whisper で解析してメディアと同じフォルダに SRT を保存する FastAPI サーバーです。

## セットアップ

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 起動

設定ファイルの `host` と `port` を使って起動します。

```bash
python run_server.py
```

直接指定して起動したい場合:

```bash
uvicorn app.main:app --host 192.168.0.11 --port 9876
```

## 設定

リポジトリ直下の `config.json` を編集します。主なオプション:

- `path_mappings`: パス変換用の `{source, target, regex}` の配列。
- `model`: faster-whisper のモデル名（例: `medium`）。
- `language`: `ja` 固定、または `null` で自動判定。
- `device`: `cuda` または `cpu`。
- `compute_type`: 例: `float16` (GPU) / `int8` (CPU)。
- `overwrite_existing`: `false` の場合、既存 SRT があればスキップ。
- `srt_suffix`: ファイル名の末尾（例: `.ja.srt`）。
- `max_concurrent_jobs`: 同時実行数の上限。
- `host`: サーバーの待ち受け IP（例: `192.168.0.11`）。
- `port`: サーバーの待ち受けポート（例: `9876`）。

設定ファイルの場所は環境変数で上書きできます:

```bash
set JELLYFINORED_CONFIG=C:\path\to\config.json
```

## API

- `POST /transcribe`

```json
{
  "title": "Example",
  "itemId": "123",
  "downloadUrl": "http://jellyfin/...",
  "filePath": "/mnt/Priscilla/dnow/vid.mp4"
}
```

レスポンス:

```json
{
  "accepted": true,
  "message": "Transcription started",
  "mappedPath": "P:\\dnow\\vid.mp4"
}
```

- `GET /health`

```json
{"status":"ok"}
```
