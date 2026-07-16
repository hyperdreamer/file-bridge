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

Edit `config.yaml` to change the bind address, port, save root, or text limit.\nDefaults are safe (loopback-only). Then start the service:

```bash
./scripts/start.sh
```

The server listens on the address and port configured in `config.yaml` (default `127.0.0.1:8766`).

## API

- `GET /health` returns service health.
- `POST /save` accepts `{ "text": "...", "path": "notes/file.txt" }`.
- `GET /paths?prefix=notes/` returns up to 30 matching paths.

## Tests

```bash
python -m pytest tests/ -v
```
