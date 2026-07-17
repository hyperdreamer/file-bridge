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

Edit `config.yaml` before starting the service. The file is required, is loaded
once at startup, and rejects unknown or duplicate YAML keys. `save_root` must be
non-empty and must already be an accessible, readable, writable directory. A
relative `save_root` is resolved relative to the configuration file.

Start the service with:

```bash
./start.sh
```

The server always binds to `127.0.0.1` and uses the configured port (default
`8766`). `start.sh` checks the virtual environment, configuration file, and
save root first, then prints actionable diagnostics if startup cannot proceed.

## Configuration and limits

The default `max_text_bytes` is 1 MiB. It limits the UTF-8 encoded text after
JSON decoding. The server also rejects an oversized raw request body before
JSON parsing; its allowance includes bounded space for JSON escaping and the
request envelope. Setting `max_text_bytes` to `0` disables only the decoded-text
check. The raw-body cap still uses the default 1 MiB decoded-text allowance in
its calculation: six times that allowance for JSON escaping plus 64 KiB of
envelope overhead, or about 6.1 MiB of raw HTTP body bytes.

Atomic overwrites preserve existing Unix mode bits. New parent directories,
file data, permission changes, and the final directory entry are synced for
crash durability. Other metadata such as timestamps, ACLs, and extended
attributes is not preserved. A successful response may include a warning if a
filesystem does not support a requested durability sync.

## API

- `GET /health` is a liveness check and returns `200` while the process serves
  requests.
- `GET /ready` verifies that the configured save root remains accessible and
  returns `503` when the service is not ready.
- `POST /save` accepts `{ "text": "...", "path": "notes/file.txt" }`.
- `GET /paths?prefix=notes/` returns up to 30 matching paths. To keep completion
  latency bounded, each request inspects at most 300 directory entries; results
  are the sorted first 30 matches found within that scan and may therefore be
  truncated in very large directories.

`/paths` never follows suggestions outside `save_root` or into the file-bridge
application directory. Request bodies reject unknown fields. Empty and
whitespace-only save paths are invalid; leading or trailing whitespace on a
non-empty filename is preserved rather than silently stripped. Invalid paths,
including destination type conflicts, return HTTP 400. Oversized requests
return HTTP 413.

Every response includes `X-Request-ID`. Clients may supply an ID containing
ASCII letters, digits, `.`, `_`, or `-`; otherwise the service generates one.
Application logs are newline-delimited JSON and include the request ID, status,
latency, and event name. Error responses do not disclose internal absolute
filesystem paths.

## Development and tests

Runtime and test dependencies are pinned to exact versions. Install the test
tools with:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```
