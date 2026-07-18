# file-bridge

A small localhost-only FastAPI service that saves text to disk and provides
filesystem path suggestions for TextKit.

## Requirements

- Python 3.12+ on a POSIX system
- Bash (for `start.sh`)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
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
`8964`). `start.sh` checks that the virtual environment interpreter is
executable, the configuration file is present and readable, and that
`--check-config` succeeds (validating config syntax, port range, and save root
accessibility). If dependencies are missing, `--check-config` will fail with an
import error.

## Configuration and limits

The default `max_text_bytes` is 1 MiB. It limits the UTF-8 encoded text after
JSON decoding. Setting `max_text_bytes` to `0` disables the decoded-text check.
Regardless of this setting, the raw request body is always bounded by a hard
transport safety cap of 128 MiB.

The server validates the HTTP `Host` header is a loopback address (missing or
non-loopback values are rejected with HTTP 421). This guards against
DNS-rebinding attacks when the service is accessed through a browser context.
Origin headers are intentionally **not** validated — this preserves
compatibility with browser extensions (e.g. `chrome-extension://<id>`) that
send a non-loopback Origin. The binding to `127.0.0.1`, the loopback-only
Host check, and the requirement that the service must never be exposed
through `0.0.0.0`, reverse proxies, port forwarding, or tunnels together
constitute the security model.

Atomic overwrites preserve existing Unix mode bits. New parent directories,
file data, permission changes, and the final directory entry are synced for
crash durability. Other metadata such as timestamps, ACLs, and extended
attributes is not preserved. A successful response may include a warning if a
filesystem does not support a requested durability sync.

Containment is enforced **lexically** (via `os.path.normpath()` and
`Path.relative_to()`). Resolved-path containment is intentionally not enforced:
symlinks within `save_root` are trusted same-user filesystem entries and may
resolve outside the root. This preserves usability for patterns such as
`~/Ramdisk` → `/ramdisk`.

## Threat model

file-bridge is a **trusted-user localhost utility**. It must:

- Bind exclusively to `127.0.0.1` — never expose it on `0.0.0.0`.
- Not be placed behind a reverse proxy, port forwarding, or tunneling service.
- Rely on Host-header validation (HTTP 421 for missing/non-loopback hosts)
  to mitigate DNS-rebinding attacks in browser contexts.

Origin filtering is intentionally absent: browser extensions such as
`chrome-extension://<id>` send non-loopback Origin values and would be
unnecessarily blocked. The Host check, combined with the hard `127.0.0.1`
bind, provides the necessary rebinding defence.

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

`/paths` uses lexical containment — symlinks under `save_root` may resolve
outside the root or into the application directory (see the containment
discussion above). Request bodies reject unknown fields. Empty and
whitespace-only save paths are invalid; leading or trailing whitespace on a
non-empty filename is preserved rather than silently stripped. Invalid paths,
including destination type conflicts, return HTTP 400. Oversized requests
return HTTP 413. Misdirected requests with a missing or non-loopback `Host` header return HTTP 421.

Every response includes `X-Request-ID`. Clients may supply an ID containing
ASCII letters, digits, `.`, `_`, or `-`; otherwise the service generates one.
Application logs are newline-delimited JSON and include the request ID, status,
latency, and event name. Error responses do not disclose internal absolute
filesystem paths.

## Development and tests

Runtime and test dependencies, including transitive dependencies, are pinned
with artifact hashes. Install the test tools with:

```bash
pip install --require-hashes -r requirements-dev.lock
python -m pytest tests/ -v
```

`requirements.txt` and `requirements-dev.txt` are the human-edited inputs.
After changing either file, regenerate the locks using Python 3.12 (the minimum
supported version) to keep the generated platform tags compatible:

```bash
pip-compile --allow-unsafe --generate-hashes --strip-extras \
  --index-url https://pypi.org/simple \
  --output-file requirements.lock requirements.txt
pip-compile --allow-unsafe --generate-hashes --strip-extras \
  --index-url https://pypi.org/simple \
  --output-file requirements-dev.lock requirements-dev.txt
```
