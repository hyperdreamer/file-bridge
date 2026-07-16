# file-bridge

A small localhost-only FastAPI service that saves text to disk and provides
filesystem path suggestions for TextKit.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` to change the port, save root, or maximum text size. The
service always binds to `127.0.0.1`. Configuration is loaded once at startup,
so restart the service after making any changes. Then start the service:

```bash
./start.sh
```

The server listens on `127.0.0.1` using the port configured in `config.yaml`
(default `8766`).

## API

- `GET /health` returns service health.
- `POST /save` accepts `{ "text": "...", "path": "notes/file.txt" }`. The
  optional `max_text_bytes` setting limits the UTF-8 encoded text size; `0`
  means unlimited. Requests over the configured limit return HTTP 413.
- `GET /paths?prefix=notes/` returns up to 30 matching paths.

## Tests

```bash
python -m pytest tests/ -v
```
