"""Package the PyInstaller build as release archives and describe them (CI and one machine alike).

    python packaging/release.py version [--ref-type tag --ref-name v0.8.2]
    python packaging/release.py package --version 0.8.2 [--triple T] [--smoke]
    python packaging/release.py extract --archive A --dest D
    python packaging/release.py mark-tests --triple T --outcome success|failure [--summary S]
    python packaging/release.py report  [--tag v0.8.2] [--require T1,T2]

`package` takes the one-folder build in dist/husk-studio/ and produces the asset the desktop shell
downloads (husk-studio-<version>-<triple>.tar.gz | .zip), after checking the folder and the archive
and (with --smoke) running the executable out of the extracted archive the way the shell does.
`extract` unpacks that archive so the binary tests can run against the exact shipped bytes.
`report` merges the per-target fragments into release-report.json (a stable schema other services can
read), RELEASE_REPORT.md and SHA256SUMS. Nothing here signs: signing happens before `package`, in CI.
Standard library only, so it runs before the project's dependencies are installed.

Ported from the backend's scripts/release.py; the checks are the same, the smoke command differs
(Studio is started with arguments, and /api/health and /health are probed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "husk-studio"
TITLE = "Husk Studio"
SCHEMA = 1
SHELL_READY_SECONDS = 20  # how long the desktop shell polls /health before it gives up
# The triples the desktop shell knows how to ask for (huskai-desktop `platform.rs`).
TRIPLES = ("aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc", "x86_64-unknown-linux-gnu")
# Every manifest that carries the product version. They are released together and must agree.
VERSION_FILES = (
    "pyproject.toml",
    "packages/husk-studio-cli/pyproject.toml",
    "packages/husk-studio-backend/pyproject.toml",
    "packages/husk-studio-sandbox/pyproject.toml",
    "packages/husk-studio-shared/pyproject.toml",
)
# Loadable native code is checked for the right CPU; bundled launcher .exe files (setuptools ships
# 32-bit and arm64 ones) are data, not code this build runs.
NATIVE_SUFFIXES = {".dylib", ".pyd", ".dll"}
# The Python runtime PyInstaller puts in _internal/: libpython3.x.dylib/.so (python-build-standalone, uv,
# Linux), python3xx.dll (Windows) or plain `Python` (a macOS framework build such as the hosted runners'
# and python.org installers).
LIBPYTHON = ("libpython3*", "python3*.dll", "Python")
LIBPYTHON_ARCHIVE = re.compile(rf"{NAME}/_internal/(libpython3.*|python3\d*\.dll|Python)")
JUNK_NAMES = {".env", ".DS_Store", ".bare-executable"}
CPU = {"macho": {0x0100000C: "arm64", 0x01000007: "x86_64"},
       "pe": {0x8664: "x86_64", 0xAA64: "arm64", 0x014C: "x86"},
       "elf": {0x3E: "x86_64", 0xB7: "arm64", 0x03: "x86"}}


# ---------------------------------------------------------------- identity

def read_versions(root: Path = ROOT) -> dict[str, str]:
    """Version of every manifest in VERSION_FILES, by relative path."""
    found: dict[str, str] = {}
    for rel in VERSION_FILES:
        try:
            data = tomllib.loads((root / rel).read_text("utf-8"))
            found[rel] = str(data["project"]["version"])
        except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
            sys.exit(f"cannot read the version from {rel}: {exc!r}")
    return found


def base_version(root: Path = ROOT) -> str:
    """The product version; refuses to continue when the manifests disagree."""
    found = read_versions(root)
    if len(set(found.values())) > 1:
        width = max(len(k) for k in found)
        lines = "\n".join(f"  {k:<{width}}  {v}" for k, v in found.items())
        sys.exit(f"version mismatch: the root and the four packages must carry the same version\n{lines}")
    return next(iter(found.values()))


def release_version(base: str, ref_type: str | None, ref_name: str | None, sha: str | None) -> str:
    """The version label of this build: the tag's, or `<base>-dev.<sha>` for anything else."""
    if ref_type == "tag" and ref_name:
        v = ref_name.removeprefix("v")
        if v != base and not v.startswith(base + "-"):
            sys.exit(f"tag {ref_name} does not match the app version {base} (expected v{base} or v{base}-<suffix>)")
        return v
    return f"{base}-dev.{(sha or 'local')[:7]}"


def channel_for(version: str) -> str:
    return "beta" if "-" in version else "stable"


def host_triple() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    system = {"Darwin": "apple-darwin", "Windows": "pc-windows-msvc", "Linux": "unknown-linux-gnu"}.get(platform.system())
    if not system:
        sys.exit(f"unsupported operating system: {platform.system()}")
    return f"{arch}-{system}"


def exe_name(triple: str) -> str:
    return NAME + (".exe" if "windows" in triple else "")


def archive_name(version: str, triple: str) -> str:
    return f"{NAME}-{version}-{triple}." + ("zip" if "windows" in triple else "tar.gz")


def expected_arch(triple: str) -> str:
    return "arm64" if triple.startswith("aarch64") else "x86_64"


def is_emulated(flag: bool) -> bool:
    """True when this build ran through an emulator (Rosetta, Windows-on-ARM), so it is not proof of a
    real machine of that architecture."""
    if flag:
        return True
    if platform.system() == "Darwin":
        r = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True)
        return r.stdout.strip() == "1"
    return False


# ---------------------------------------------------------------- inspecting binaries

def binary_archs(path: Path) -> set[str] | None:
    """CPU architectures inside a Mach-O (thin or fat), PE or ELF file; None when it is none of them."""
    try:
        with path.open("rb") as f:
            head = f.read(512)
            if len(head) < 8:
                return None
            magic_le, magic_be = struct.unpack("<I", head[:4])[0], struct.unpack(">I", head[:4])[0]
            if magic_le in (0xFEEDFACF, 0xFEEDFACE):
                return {CPU["macho"].get(struct.unpack("<I", head[4:8])[0], "other")}
            if magic_be in (0xCAFEBABE, 0xCAFEBABF):
                n, stride = struct.unpack(">I", head[4:8])[0], (32 if magic_be == 0xCAFEBABF else 20)
                if not 0 < n <= 8:  # a Java class file shares the magic; its "count" is a version
                    return None
                return {CPU["macho"].get(struct.unpack(">I", head[8 + i * stride:12 + i * stride])[0], "other")
                        for i in range(n)}
            if head[:4] == b"\x7fELF":
                return {CPU["elf"].get(struct.unpack("<H", head[18:20])[0], "other")}
            if head[:2] == b"MZ" and len(head) >= 0x40:
                f.seek(struct.unpack("<I", head[0x3C:0x40])[0])
                pe = f.read(6)
                if pe[:4] == b"PE\0\0" and len(pe) == 6:
                    return {CPU["pe"].get(struct.unpack("<H", pe[4:6])[0], "other")}
    except OSError:
        return None
    return None


def check_folder(folder: Path, triple: str) -> tuple[list[str], int]:
    """Problems that would make the shell install broken, plus how many native files were arch-checked."""
    problems: list[str] = []
    exe, internal = folder / exe_name(triple), folder / "_internal"
    if not exe.is_file():
        problems.append(f"{exe.name} is missing from {folder}")
    if not internal.is_dir():
        problems.append("_internal/ is missing: the executable cannot start without it")
    elif not any(f.is_file() for pat in LIBPYTHON for f in internal.glob(pat)):
        problems.append("_internal/ holds no libpython (the Python runtime)")
    want, wrong, checked = expected_arch(triple), [], 0
    for p in sorted(folder.rglob("*")) if folder.is_dir() else []:
        if p.name.startswith("._") or p.name in JUNK_NAMES:
            problems.append(f"unwanted file in the bundle: {p.relative_to(folder)}")
        if p.is_symlink() or not p.is_file():
            continue
        if p == exe or p.suffix in NATIVE_SUFFIXES or ".so" in p.name or p.name == "Python":
            archs = binary_archs(p)
            if archs is not None:
                checked += 1
                if want not in archs:
                    wrong.append(f"{p.relative_to(folder)} ({'/'.join(sorted(archs))})")
    if wrong:
        problems.append(f"{len(wrong)} native file(s) are not {want}: " + ", ".join(wrong[:5]))
    return problems, checked


# ---------------------------------------------------------------- the archive

def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_archive(folder: Path, dest: Path) -> None:
    """The whole folder under one top directory. tarfile does not add macOS AppleDouble files."""
    if dest.suffix == ".zip":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(folder.rglob("*")):
                z.write(p, f"{NAME}/{p.relative_to(folder).as_posix()}")
    else:
        with tarfile.open(dest, "w:gz") as t:
            t.add(folder, arcname=NAME, filter=_anonymous)  # symlinks stay symlinks, owner names are dropped


def inspect_archive(archive: Path, triple: str) -> tuple[list[str], int, int]:
    """Check what is actually shipped: (problems, entries, unpacked bytes)."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            members = [(i.filename, i.file_size, None) for i in z.infolist()]
    else:
        with tarfile.open(archive) as t:
            members = [(m.name, m.size, m.mode) for m in t.getmembers()]
    problems: list[str] = []
    names = [n for n, _, _ in members]
    if any(n.startswith("/") or ".." in Path(n).parts for n in names):
        problems.append("the archive has absolute or parent-relative paths (the shell rejects it)")
    if any(not (n == NAME or n.startswith(NAME + "/")) for n in names):
        problems.append(f"every entry must be under a single top folder `{NAME}/`")
    for need in (f"{NAME}/{exe_name(triple)}", f"{NAME}/_internal"):
        if not any(n.rstrip("/") == need or n.startswith(need + "/") for n in names):
            problems.append(f"the archive has no {need}")
    if not any(LIBPYTHON_ARCHIVE.fullmatch(n) for n in names):
        problems.append("the archive has no libpython")
    if any(Path(n).name.startswith("._") or Path(n).name in JUNK_NAMES for n in names):
        problems.append("the archive has AppleDouble or junk files")
    exe = next((m for m in members if m[0] == f"{NAME}/{exe_name(triple)}"), None)
    if exe and exe[2] is not None and not exe[2] & 0o111:
        problems.append("the executable has no execute bit")
    return problems, len(members), sum(s for _, s, _ in members)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, dest: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(dest, filter="data")  # rejects absolute and escaping links, like the shell
            else:
                t.extractall(dest)


# ---------------------------------------------------------------- smoke test

def _get_json(opener: urllib.request.OpenerDirector, url: str) -> tuple[str, dict]:
    """(content type, parsed body); raises ValueError when the body is not JSON (e.g. the UI's index.html)."""
    with opener.open(url, timeout=2) as r:
        ctype = r.headers.get_content_type()
        return ctype, json.loads(r.read().decode())


def smoke(archive: Path, triple: str, *, expect_version: str | None = None, timeout: float = 60) -> dict:
    """Run the executable out of the extracted archive the way the shell does.

    Same command line and environment the shell uses (`start --port … --host 127.0.0.1
    --no-open-browser`, HUSK_HOME = its data dir). The variables Studio does not read
    (HUSK_PORT, HUSK_DATA_DIR, HUSK_ENV, HUSK_LAUNCH_SECRET) are passed too, to show they are harmless.
    Passing needs `/api/health` to answer `{ok: true, version}` and `/health` to answer the same as JSON.
    """
    started = time.monotonic()
    result: dict = {"passed": False, "health_version": None, "seconds": None, "detail": None}
    with tempfile.TemporaryDirectory(prefix="husk-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        extract(archive, root / "bin")
        folder, data, log = root / "bin" / NAME, root / "data", root / "husk-studio.log"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {k: v for k, v in os.environ.items()
               if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "OTEL_EXPORTER_OTLP_ENDPOINT")}
        env.update(HUSK_PORT=str(port), HUSK_DATA_DIR=str(data), HUSK_HOME=str(data), HUSK_ENV="prod",
                   HUSK_LAUNCH_SECRET=secrets.token_hex(32), HUSK_NO_BROWSER="1",
                   PYTHONIOENCODING="utf-8")
        cmd = [str(folder / exe_name(triple)), "start", "--port", str(port), "--host", "127.0.0.1",
               "--no-open-browser"]
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with log.open("wb") as out:
            proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            while time.monotonic() - started < timeout:
                if proc.poll() is not None:
                    tail = log.read_text("utf-8", "replace").strip().splitlines()[-6:]
                    result["detail"] = f"exited with code {proc.returncode}: " + " | ".join(tail)
                    break
                try:
                    _, body = _get_json(opener, f"{base}/api/health")
                except (OSError, ValueError):
                    time.sleep(0.5)
                    continue
                if body.get("ok") is not True:
                    result["detail"] = f"/api/health answered without ok=true: {body!r}"
                    break
                result["health_version"] = body.get("version")
                if expect_version and body.get("version") != expect_version:
                    result["detail"] = (f"/api/health reports version {body.get('version')!r}, "
                                        f"the build is {expect_version!r}")
                    break
                try:
                    ctype, alias = _get_json(opener, f"{base}/health")
                except (OSError, ValueError):
                    result["detail"] = "/health is not JSON (the UI fallback answers it?)"
                    break
                if ctype != "application/json" or alias != body:
                    result["detail"] = f"/health must equal /api/health as application/json, got {ctype} {alias!r}"
                    break
                result["passed"] = True
                break
            else:
                result["detail"] = f"/api/health did not answer within {timeout:.0f} s"
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


# ---------------------------------------------------------------- package

def cmd_package(a: argparse.Namespace) -> int:
    triple = a.triple or host_triple()
    if triple not in TRIPLES:
        sys.exit(f"unknown target triple {triple}; the shell asks for one of: {', '.join(TRIPLES)}")
    folder, out = Path(a.folder), Path(a.out)
    (out / "report").mkdir(parents=True, exist_ok=True)
    name = archive_name(a.version, triple)
    try:
        from importlib.metadata import version as dist_version
        pyinstaller = dist_version("pyinstaller")
    except Exception:  # noqa: BLE001 - informational only
        pyinstaller = None
    fragment: dict = {
        "triple": triple, "version": a.version, "status": "failed", "asset_name": name, "size_bytes": None,
        "sha256": None, "unpacked_bytes": None, "entries": None, "python": platform.python_version(),
        "pyinstaller": pyinstaller, "arch_check": {"expected": expected_arch(triple), "native_files": 0},
        "smoke": None, "binary_tests": None, "signed": {"kind": a.signed, "notarised": a.notarised},
        "verified_on_real_hardware": not is_emulated(a.emulated), "problems": [],
    }
    problems, native = check_folder(folder, triple)
    fragment["arch_check"]["native_files"] = native
    archive = out / name
    if not problems:
        make_archive(folder, archive)
        problems, entries, unpacked = inspect_archive(archive, triple)
        fragment.update(size_bytes=archive.stat().st_size, sha256=sha256(archive), entries=entries,
                        unpacked_bytes=unpacked)
    if not problems and a.smoke:
        # `0.8.2-dev.abc1234` and `0.8.2-alpha.1` both report the base version at runtime.
        fragment["smoke"] = smoke(archive, triple, expect_version=a.version.split("-", 1)[0])
        if not fragment["smoke"]["passed"]:
            problems.append("smoke test failed: " + str(fragment["smoke"]["detail"]))
    fragment["problems"] = problems
    fragment["status"] = "failed" if problems else "built"
    (out / "report" / f"{triple}.json").write_text(json.dumps(fragment, indent=2) + "\n", "utf-8")
    if problems:
        print(f"{triple}: FAILED", *problems, sep="\n  - ", file=sys.stderr)
        return 1
    print(f"{triple}: {name} {fragment['size_bytes'] / 1e6:.1f} MB sha256 {fragment['sha256'][:16]}… "
          f"({native} native files are {expected_arch(triple)}"
          + (f", /health {fragment['smoke']['health_version']} in {fragment['smoke']['seconds']} s" if a.smoke else "")
          + ")")
    return 0


def cmd_extract(a: argparse.Namespace) -> int:
    archive, dest = Path(a.archive), Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    extract(archive, dest)
    triple = a.triple or host_triple()
    exe = dest / NAME / exe_name(triple)
    if not exe.is_file():
        print(f"{exe} is missing after extracting {archive}", file=sys.stderr)
        return 1
    # The value for HUSK_STUDIO_BINARY. Absolute: the tests launch it from other working directories.
    print(os.path.abspath(exe))
    return 0


def cmd_mark_tests(a: argparse.Namespace) -> int:
    """Record the binary tests' outcome on the target's fragment; a failure fails the target."""
    path = Path(a.reports) / f"{a.triple}.json"
    fragment = json.loads(path.read_text("utf-8"))
    ok = a.outcome == "success"
    fragment["binary_tests"] = {"passed": ok, "outcome": a.outcome, "summary": a.summary}
    if not ok:
        fragment["problems"].append(f"binary tests did not pass against the extracted archive (outcome: {a.outcome})"
                                    + (f": {a.summary}" if a.summary else ""))
        fragment["status"] = "failed"
    path.write_text(json.dumps(fragment, indent=2) + "\n", "utf-8")
    return 0


# ---------------------------------------------------------------- report

def junit_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    return {"passed": total - failed - skipped, "failed": failed, "skipped": skipped}


def vitest_summary(path: Path) -> dict:
    d = json.loads(path.read_text("utf-8"))
    return {"passed": d.get("numPassedTests", 0), "failed": d.get("numFailedTests", 0),
            "skipped": d.get("numPendingTests", 0)}


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def build_report(fragments: list[dict], *, tag: str | None, quality: dict, min_version: str | None,
                 required: list[str], env: dict[str, str] | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    versions = {f["version"] for f in fragments}
    if len(versions) > 1:
        sys.exit(f"the targets were built with different versions: {sorted(versions)}")
    version = versions.pop() if versions else base_version()
    if tag and tag.removeprefix("v") != version:
        sys.exit(f"tag {tag} does not match the packaged version {version}")
    repo, run_id = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    built = {f["triple"]: f for f in fragments if f["status"] == "built"}
    runner = env.get("RUNNER_ENVIRONMENT")
    ref_type = env.get("GITHUB_REF_TYPE")
    gaps: list[str] = []
    for triple in TRIPLES:
        if triple not in built:
            gaps.append(f"no build for {triple}")
    if unsigned := [t for t, f in built.items() if f["signed"]["kind"] == "none"]:
        gaps.append("not code-signed: " + ", ".join(unsigned))
    if emulated := [t for t, f in built.items() if not f["verified_on_real_hardware"]]:
        gaps.append("only run under emulation, never on a real machine of that CPU: " + ", ".join(emulated))
    if slow := [t for t, f in built.items() if f["smoke"] and f["smoke"]["seconds"] > SHELL_READY_SECONDS]:
        gaps.append(f"start-up took longer than the shell's {SHELL_READY_SECONDS} s readiness wait: " + ", ".join(slow))
    if untested := [t for t, f in built.items() if not (f.get("binary_tests") or {}).get("passed")]:
        gaps.append("binary tests were not recorded as passing against the archive: " + ", ".join(untested))
    if any("windows" in t for t in built):
        gaps.append("Windows: the shell's stop is TerminateProcess, so there is no graceful shutdown to rely on")
    gaps.append("replay runs a graph inside the bundled interpreter and can only import what the bundle ships")
    gaps.append("the desktop shell has not been shown installing and starting these assets end to end")
    return {
        "schema": SCHEMA, "component": NAME, "version": version, "tag": tag,
        "commit": env.get("GITHUB_SHA") or git("rev-parse", "HEAD"),
        "branch": env.get("GITHUB_REF_NAME") if ref_type == "branch" else git("rev-parse", "--abbrev-ref", "HEAD"),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "channel": channel_for(version), "min_version": min_version,
        "builder": {"kind": runner or "local", "os": platform.system(), "arch": platform.machine(),
                    "run_url": f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"
                    if repo and run_id else None},
        "quality": quality,
        "required": required,
        "targets": sorted(fragments, key=lambda f: f["triple"]),
        "assets": [{"triple": t, "name": f["asset_name"], "size": f["size_bytes"], "sha256": f["sha256"],
                    "url": f"https://github.com/{repo}/releases/download/{tag}/{f['asset_name']}" if repo and tag else None}
                   for t, f in sorted(built.items())],
        "known_gaps": gaps,
    }


def render_markdown(r: dict) -> str:
    L = [f"# {TITLE} {r['version']}", "",
         f"Commit `{(r['commit'] or 'unknown')[:10]}` · {r['built_at']} · channel **{r['channel']}**"
         + (f" · [run]({r['builder']['run_url']})" if r["builder"]["run_url"] else "")
         + f" · built on {r['builder']['kind']} ({r['builder']['os']} {r['builder']['arch']})", "",
         "| Target | Result | Asset | Size | Smoke | Binary tests | Signed |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in r["targets"]:
        s, bt = t["smoke"], t.get("binary_tests")
        size = f"{t['size_bytes'] / 1e6:.1f} MB" if t["size_bytes"] else "—"
        smoke_cell = f"ok, {s['seconds']} s" if s and s["passed"] else ("failed" if s else "not run")
        tests_cell = ("passed" if bt["passed"] else "failed") if bt else "not run"
        asset = t["asset_name"] if t["status"] == "built" else "—"
        L.append(f"| `{t['triple']}` | {t['status']} | {asset} | {size} | {smoke_cell} | {tests_cell} "
                 f"| {t['signed']['kind']} |")
    if r["quality"]:
        L += ["", "## Tests", ""]
        L += [f"- {k}: " + (", ".join(f"{n} {v}" for v, n in d.items()) if isinstance(d, dict) else str(d))
              for k, d in r["quality"].items()]
    failed = [t for t in r["targets"] if t["problems"]]
    if failed:
        L += ["", "## Problems", ""] + [f"- `{t['triple']}`: {p}" for t in failed for p in t["problems"]]
    L += ["", "## Known gaps", ""] + [f"- {g}" for g in r["known_gaps"]]
    L += ["", "## Checksums (SHA-256)", ""] + [f"- `{a['sha256']}`  {a['name']}" for a in r["assets"]]
    return "\n".join(L) + "\n"


def cmd_report(a: argparse.Namespace) -> int:
    reports, out = Path(a.reports), Path(a.out)
    fragments = [json.loads(p.read_text("utf-8")) for p in sorted(reports.glob("*.json"))]
    quality: dict = {}
    if a.pytest_xml and Path(a.pytest_xml).is_file():
        quality["pytest"] = junit_summary(Path(a.pytest_xml))
    if a.vitest_json and Path(a.vitest_json).is_file():
        quality["vitest"] = vitest_summary(Path(a.vitest_json))
    for item in a.check:
        k, _, v = item.partition("=")
        quality[k] = v
    required = [t for t in a.require.split(",") if t]
    report = build_report(fragments, tag=a.tag, quality=quality, min_version=a.min_version, required=required)
    out.mkdir(parents=True, exist_ok=True)
    (out / "release-report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    (out / "RELEASE_REPORT.md").write_text(render_markdown(report), "utf-8")
    (out / "SHA256SUMS").write_text("".join(f"{x['sha256']}  {x['name']}\n" for x in report["assets"]), "utf-8")
    print(f"wrote {out / 'release-report.json'} ({len(report['assets'])} asset(s))")
    missing = [t for t in required if t not in {x["triple"] for x in report["assets"]}]
    if missing:
        print("required target(s) not built: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("version", help="print version=<label> for this build (for $GITHUB_OUTPUT); the version gate")
    v.add_argument("--ref-type"), v.add_argument("--ref-name"), v.add_argument("--sha")

    p = sub.add_parser("package", help="archive, check and (optionally) smoke-test dist/husk-studio/")
    p.add_argument("--version", required=True)
    p.add_argument("--triple")
    p.add_argument("--folder", default=str(ROOT / "dist" / NAME))
    p.add_argument("--out", default=str(ROOT / "dist"))
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--signed", choices=["none", "developer-id", "authenticode"], default="none")
    p.add_argument("--notarised", action="store_true")
    p.add_argument("--emulated", action="store_true", help="this build ran under an emulator")

    e = sub.add_parser("extract", help="unpack an archive and print the path of its executable")
    e.add_argument("--archive", required=True)
    e.add_argument("--dest", required=True)
    e.add_argument("--triple")

    m = sub.add_parser("mark-tests", help="record the binary tests' outcome on a target's fragment")
    m.add_argument("--triple", required=True)
    m.add_argument("--outcome", required=True, help="GitHub step outcome: success, failure, cancelled, skipped")
    m.add_argument("--summary", default="")
    m.add_argument("--reports", default=str(ROOT / "dist" / "report"))

    r = sub.add_parser("report", help="merge per-target results into release-report.json")
    r.add_argument("--reports", default=str(ROOT / "dist" / "report"))
    r.add_argument("--out", default=str(ROOT / "dist"))
    r.add_argument("--tag")
    r.add_argument("--min-version")
    r.add_argument("--require", default="", help="comma-separated triples that must have built")
    r.add_argument("--pytest-xml"), r.add_argument("--vitest-json")
    r.add_argument("--check", action="append", default=[], help="name=outcome, repeatable")

    a = ap.parse_args(argv)
    if a.cmd == "version":
        print("version=" + release_version(base_version(), a.ref_type, a.ref_name, a.sha))
        return 0
    return {"package": cmd_package, "extract": cmd_extract, "mark-tests": cmd_mark_tests,
            "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
