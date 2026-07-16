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

Edit `config.yaml` if saves should be restricted to a directory other than the
current user's home directory. Then start the service:

```bash
./scripts/start.sh
```

The server listens only on `127.0.0.1:8766`.

## API

- `GET /health` returns service health.
- `POST /save` accepts `{ "text": "...", "path": "notes/file.txt" }`.
- `GET /paths?prefix=notes/` returns up to 30 matching paths.

## Tests

```bash
python -m pytest tests/ -v
```
