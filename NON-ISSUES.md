# Known Non-Issues

This document records audit findings that were evaluated and determined to be
non-issues under this project's threat model.

## Threat Model

file-bridge is a **localhost-only** (127.0.0.1) FastAPI service for **personal
developer use on a trusted single-user machine**. Local code execution is
assumed to be under the user's control — if an attacker has local code
execution, filesystem-level defenses are moot.

## #1 — Lexical Containment; Symlinks Are Trusted Same-User Entries

**What:** Containment is enforced **lexically** via `os.path.normpath()` and
`Path.relative_to()`. Resolved-path (realpath) containment is intentionally not
enforced. Symlinks that exist under `save_root` are valid regardless of where
they resolve — including outside `save_root` or into the application directory.

**Why it's not an issue:** Exploiting this requires local filesystem control
(creating symlinks). On a trusted single-user machine, the user can already
create or modify those files directly. The code intentionally preserves symlink
usability — e.g., `~/Ramdisk` → `/ramdisk` — which is a deliberate design
choice documented here. In-root symlinks are trusted same-user filesystem
entries.

**When it would matter:** If `save_root` contains untrusted content, or the
service is deployed on a shared multi-user host, or strict "writes never leave
this directory" semantics are required for correctness.

## #2 — No Token Authentication

**What:** The `/save` endpoint has no bearer token, API key, or credential
storage. Loopback binding plus Host validation are the only access
controls.

**Why it's not an issue:** The service binds exclusively to `127.0.0.1` and
validates the HTTP `Host` header is a loopback address (`127.0.0.1`, `localhost`,
or `[::1]`). Origin headers are intentionally not validated to preserve
compatibility with browser extensions. Any local process with network access to
loopback already has filesystem access to the user's files, making an API token
redundant.

Authentication is an intentional design choice — the threat model treats local
clients as trusted.

**When it would matter:** If the deployment changes — binding to `0.0.0.0`,
exposure through a proxy/tunnel/container port mapping, running on a shared
host, or treating browser content/extensions as untrusted principals.
