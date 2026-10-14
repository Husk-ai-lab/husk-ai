# Packaging the Husk Studio binary

```bash
uv run python packaging/build.py            # -> dist/husk-studio/husk-studio  (onedir)
uv run python -m pytest packaging/tests -q  # boots the built binary and exercises it
```

`--onefile` builds a single executable instead of a folder, for people who want one file. **Never
ship it to the desktop shell**: the shell needs the whole one-folder build and refuses a bare
executable, so CI builds onedir only. Binaries are platform-native: PyInstaller does not
cross-compile, so build on each OS you ship to. Needs Node 20+ the first time (to build the Studio
UI that gets baked in); the resulting binary needs nothing.

## What CI does with it

`.github/workflows/desktop-build.yml` builds, packages, smoke-tests and reports one archive per OS.
The steps, and how to cut a release, are in [RELEASING.md](RELEASING.md). To run the same steps by
hand on this machine:

```bash
uv run python packaging/build.py
LABEL=$(python3 packaging/release.py version | cut -d= -f2)          # <version>-dev.<sha>
python3 packaging/release.py package --version "$LABEL" --smoke --out dist
BIN=$(python3 packaging/release.py extract --archive dist/husk-studio-$LABEL-*.tar.gz --dest dist/extracted)
HUSK_STUDIO_BINARY="$BIN" uv run python -m pytest packaging/tests/test_binary.py -q
```

The last line matters: the archive is what ships, so the binary tests also run against the
*extracted archive*, not only against `dist/`.

## Contract with the desktop shell

What Studio does when the shell starts it as
`husk-studio start --port 7654 --host 127.0.0.1 --no-open-browser`.

| Input | Behaviour |
|---|---|
| `--port N` | **Exactly N.** If N is busy the process exits 1 with `error: port N is already in use …`. It never moves to another port. "Busy" means a live listener: sockets still in `TIME_WAIT` from a run that just stopped do not count (POSIX; Windows never blocked on them), so a stop followed by an immediate start on the same port works. |
| no `--port` | Starts on 7654; if busy, takes the next free port up to 7664 and logs `husk-studio starting on http://HOST:PORT`. The bound port is also written to `<HUSK_HOME>/port`. Interactive use only: a supervisor should always pass `--port`. |
| `--host`, `--no-open-browser` | Honoured. |
| `HUSK_HOME` | Honoured. The data directory (database, runs, cassettes, secrets); created if missing. |
| `HUSK_NO_BROWSER=1` | Honoured. Never opens a browser. |
| `HUSK_PORT`, `HUSK_DATA_DIR`, `HUSK_ENV`, `HUSK_LAUNCH_SECRET`, `STUDIO_TOKEN_URL` | **Not implemented; ignored.** Passing them is harmless (CI passes them on purpose). There is no launch-secret or token handshake in Studio; do not build on one. |
| `GET /health`, `GET /api/health` | `200 application/json`, `{"ok": true, "service": "husk-studio-backend", "version": "<version>"}`. `/healthz` is an older alias that returns only `{"ok": true}`. |
| SIGTERM | uvicorn shuts down gracefully (exit 0 or `-15`) and the one-folder build leaves no child process. On Windows the shell's stop is `TerminateProcess` (no graceful path); SQLite runs in WAL mode, so a hard kill is crash-safe. |
| Ingest | Loopback callers only, as always. Nothing here changes it. |

The MCP server (`husk-studio mcp --transport http`) uses 7655 and is not started by `start`.

## Known limit: replay inside the bundle

Replay executes the user's graph inside the bundled interpreter, so it can import only what the
bundle ships (OpenTelemetry, `husk_studio_shared`, the standard library). An agent with other
dependencies should be run from a normal `uv` install. The report lists this under `known_gaps`.
