# Known Non-Issues

This document records audit findings that were evaluated and determined to be
non-issues under this project's threat model.

## Threat Model

file-bridge is a **localhost-only** (127.0.0.1) FastAPI service for **personal
developer use on a trusted single-user machine**. Local code execution is
assumed to be under the user's control — if an attacker has local code
execution, filesystem-level defenses are moot.

## #1 — Symlinks Under Save Root Are Always Valid

**What:** As of commit `75be3df`, `APPLICATION_ROOT` resolved-target
containment checks have been removed from `_resolve_save_path()` and
`list_paths()`. Symlinks that exist under `save_root` are valid
regardless of where they point — including into the application directory.

**Why it's not an issue:** Exploiting this requires local filesystem control
(creating symlinks). On a trusted single-user machine, the user can already
create or modify those files directly. The code intentionally preserves symlink
usability — e.g., `~/Ramdisk` → `/ramdisk` — which is a deliberate design
choice documented here.

The remaining containment checks are lexical only: `_expand_user_path()` uses
`os.path.normpath()` to prevent `../` traversal, and the `/paths` endpoint
still skips entries whose resolved targets fall outside `save_root`. But
symlinks within `save_root` that point to any location (including
`APPLICATION_ROOT`) are accepted.

**When it would matter:** If `save_root` contains untrusted content, or the
service is deployed on a shared multi-user host, or strict "writes never leave
this directory" semantics are required for correctness.

## #2 — No Authentication

**What:** The `/save` endpoint has no token, authorization header, or `Host`
validation. Loopback binding alone is the only access control.

**Why it's not an issue:** The service binds exclusively to `127.0.0.1`.
- A malicious webpage cannot read responses (browser same-origin policy).
- The JSON-only `/save` endpoint limits ordinary form-based CSRF.
- Any local process with network access to loopback already has filesystem
  access to the user's files, making an API token redundant.

Authentication is an intentional design choice — the threat model treats local
clients as trusted.

**When it would matter:** If the deployment changes — binding to `0.0.0.0`,
exposure through a proxy/tunnel/container port mapping, running on a shared
host, or treating browser content/extensions as untrusted principals.
