"""The release packaging script (packaging/release.py): what it accepts, what it refuses, what it reports.

Everything runs on synthetic folders: tiny fake Mach-O/PE headers and a stub executable, never the real
build. Ported from the backend's test_release_package.py, plus the Studio-specific parts: the version gate
over five manifests and a smoke test that starts the executable with arguments and probes /api/health and
/health.

    uv run python -m pytest packaging/tests/test_release.py -q
"""

from __future__ import annotations

import importlib.util
import json
import stat
import struct
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "husk_release", Path(__file__).resolve().parents[1] / "release.py"
)
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release
_spec.loader.exec_module(release)

ARM, X64 = 0x0100000C, 0x01000007
MAC_ARM, MAC_X64 = "aarch64-apple-darwin", "x86_64-apple-darwin"
WIN = "x86_64-pc-windows-msvc"
NAME = "husk-studio"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the stub executable is a shebang script")


def macho(cpu: int, size: int = 64) -> bytes:
    return (struct.pack("<II", 0xFEEDFACF, cpu) + b"\0" * size)[:size]


def pe(machine: int) -> bytes:
    head = bytearray(b"MZ" + b"\0" * 0x3E)
    head[0x3C:0x40] = struct.pack("<I", 0x40)
    return bytes(head) + b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 16


def build_folder(root: Path, triple: str = MAC_ARM, cpu: int = ARM) -> Path:
    folder = root / NAME
    internal = folder / "_internal"
    internal.mkdir(parents=True)
    native = pe(0x8664) if "windows" in triple else macho(cpu)
    exe = folder / release.exe_name(triple)
    exe.write_bytes(native)
    exe.chmod(0o755)
    (internal / ("python311.dll" if "windows" in triple else "libpython3.11.dylib")).write_bytes(native)
    (internal / "base_library.zip").write_bytes(b"PK")
    return folder


# ---- identifying binaries


def test_binary_archs_reads_macho_pe_elf_and_ignores_the_rest(tmp_path: Path) -> None:
    files = {
        "arm": macho(ARM), "x64": macho(X64), "win": pe(0x8664), "winarm": pe(0xAA64),
        "elf": b"\x7fELF" + b"\x02\x01\x01" + b"\0" * 11 + struct.pack("<H", 0xB7) + b"\0" * 8,
        "fat": struct.pack(">II", 0xCAFEBABE, 2) + struct.pack(">IIIII", ARM, 0, 0, 0, 0)
        + struct.pack(">IIIII", X64, 0, 0, 0, 0),
        "java": struct.pack(">II", 0xCAFEBABE, 52) + b"\0" * 40,
        "text": b"# just a script\n" * 8,
    }  # fmt: skip
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    got = {n: release.binary_archs(tmp_path / n) for n in files}
    assert got == {
        "arm": {"arm64"}, "x64": {"x86_64"}, "win": {"x86_64"}, "winarm": {"arm64"},
        "elf": {"arm64"}, "fat": {"arm64", "x86_64"}, "java": None, "text": None,
    }  # fmt: skip


# ---- the folder


def test_a_complete_folder_passes_and_counts_native_files(tmp_path: Path) -> None:
    problems, native = release.check_folder(build_folder(tmp_path), MAC_ARM)
    assert problems == []
    assert native == 2  # the executable and libpython


def test_a_folder_without__internal_is_refused(tmp_path: Path) -> None:
    """The executable alone is exactly what a bare onefile/partial copy looks like: exit 255 on start."""
    folder = tmp_path / NAME
    folder.mkdir()
    (folder / NAME).write_bytes(macho(ARM))
    problems, _ = release.check_folder(folder, MAC_ARM)
    assert any("_internal/ is missing" in p for p in problems)


def test_a_folder_without_libpython_is_refused(tmp_path: Path) -> None:
    folder = build_folder(tmp_path)
    (folder / "_internal" / "libpython3.11.dylib").unlink()
    assert any("libpython" in p for p in release.check_folder(folder, MAC_ARM)[0])


def test_a_macos_framework_python_is_accepted(tmp_path: Path) -> None:
    """Hosted macOS runners use python.org framework builds: PyInstaller names the runtime `Python`."""
    folder = build_folder(tmp_path)
    (folder / "_internal" / "libpython3.11.dylib").rename(folder / "_internal" / "Python")
    assert release.check_folder(folder, MAC_ARM)[0] == []
    archive = tmp_path / "a.tar.gz"
    release.make_archive(folder, archive)
    assert release.inspect_archive(archive, MAC_ARM)[0] == []
    # ... and its architecture is checked like any other native file
    (folder / "_internal" / "Python").write_bytes(macho(X64))
    assert any("not arm64" in p for p in release.check_folder(folder, MAC_ARM)[0])


def test_a_directory_named_python_is_not_the_runtime(tmp_path: Path) -> None:
    folder = build_folder(tmp_path)
    (folder / "_internal" / "libpython3.11.dylib").unlink()
    (folder / "_internal" / "Python").mkdir()
    assert any("libpython" in p for p in release.check_folder(folder, MAC_ARM)[0])


def test_a_wrong_architecture_is_refused_but_launcher_exes_are_not(tmp_path: Path) -> None:
    folder = build_folder(tmp_path, MAC_X64, X64)
    (folder / "_internal" / "libwrong.dylib").write_bytes(macho(ARM))
    problems, _ = release.check_folder(folder, MAC_X64)
    assert any("not x86_64" in p and "libwrong.dylib" in p for p in problems)
    # a setuptools-style 32-bit launcher is data, not code the build runs
    win = build_folder(tmp_path / "w", WIN)
    (win / "_internal" / "cli-32.exe").write_bytes(pe(0x014C))
    assert release.check_folder(win, WIN)[0] == []


@pytest.mark.parametrize("junk", ["._husk-studio", ".env", ".bare-executable", ".DS_Store"])
def test_junk_files_are_refused(tmp_path: Path, junk: str) -> None:
    folder = build_folder(tmp_path)
    (folder / junk).write_bytes(b"x")
    assert any("unwanted file" in p for p in release.check_folder(folder, MAC_ARM)[0])


# ---- the archive


def test_tar_archive_roundtrip_keeps_one_top_folder_and_no_owner(tmp_path: Path) -> None:
    folder = build_folder(tmp_path)
    archive = tmp_path / release.archive_name("0.8.2", MAC_ARM)
    release.make_archive(folder, archive)
    assert archive.name == "husk-studio-0.8.2-aarch64-apple-darwin.tar.gz"
    problems, entries, unpacked = release.inspect_archive(archive, MAC_ARM)
    assert problems == []
    assert entries >= 4 and unpacked > 0
    with tarfile.open(archive) as t:
        members = t.getmembers()
    assert {m.name.split("/")[0] for m in members} == {NAME}
    assert all(m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" for m in members)
    exe = next(m for m in members if m.name == f"{NAME}/{NAME}")
    assert exe.mode & stat.S_IXUSR


def test_zip_archive_for_windows(tmp_path: Path) -> None:
    folder = build_folder(tmp_path, WIN)
    archive = tmp_path / release.archive_name("0.8.2", WIN)
    assert archive.name == "husk-studio-0.8.2-x86_64-pc-windows-msvc.zip"
    release.make_archive(folder, archive)
    assert release.inspect_archive(archive, WIN)[0] == []
    with zipfile.ZipFile(archive) as z:
        assert f"{NAME}/{NAME}.exe" in z.namelist()


def test_a_bare_executable_archive_is_refused(tmp_path: Path) -> None:
    """A onefile build: the shell refuses it, so it must never reach a release."""
    archive = tmp_path / "bare.tar.gz"
    exe = tmp_path / NAME
    exe.write_bytes(macho(ARM))
    with tarfile.open(archive, "w:gz") as t:
        t.add(exe, arcname=NAME)
    problems, _, _ = release.inspect_archive(archive, MAC_ARM)
    assert any("_internal" in p for p in problems) or any("libpython" in p for p in problems)


def test_an_archive_with_escaping_paths_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    src = tmp_path / "f"
    src.write_bytes(b"x")
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname=f"{NAME}/../evil")
    assert any("parent-relative" in p for p in release.inspect_archive(archive, MAC_ARM)[0])


def test_extract_command_unpacks_and_prints_the_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "a.tar.gz"
    release.make_archive(build_folder(tmp_path / "src"), archive)
    dest = tmp_path / "out"
    assert release.main(["extract", "--archive", str(archive), "--dest", str(dest), "--triple", MAC_ARM]) == 0
    assert capsys.readouterr().out.strip() == str(dest / NAME / NAME)
    # an archive without the executable is an error, not a silent empty directory
    bad = tmp_path / "bad.tar.gz"
    stray = tmp_path / "stray.txt"
    stray.write_text("x")
    with tarfile.open(bad, "w:gz") as t:
        t.add(stray, arcname=f"{NAME}/stray.txt")
    assert release.main(["extract", "--archive", str(bad), "--dest", str(tmp_path / "o2"), "--triple", MAC_ARM]) == 1


# ---- running it like the shell


def stub_build(tmp_path: Path, body: str) -> Path:
    """A folder whose 'executable' is a Python script (POSIX only)."""
    folder = build_folder(tmp_path)
    exe = folder / NAME
    exe.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    exe.chmod(0o755)
    return folder


# Serves /api/health, and /health either as the same JSON or as the SPA's HTML, like the real app did.
SERVER = """
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    argv = sys.argv[1:]
    assert argv[0] == "start", argv
    assert "--no-open-browser" in argv and argv[argv.index("--host") + 1] == "127.0.0.1", argv
    port = int(argv[argv.index("--port") + 1])
    assert os.environ["HUSK_ENV"] == "prod" and len(os.environ["HUSK_LAUNCH_SECRET"]) == 64
    assert os.environ["HUSK_NO_BROWSER"] == "1" and os.path.isabs(os.environ["HUSK_HOME"])
    HEALTH_IS_JSON = __HEALTH_IS_JSON__
    BODY = {"ok": True, "service": "husk-studio-backend", "version": "__VERSION__"}
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/health" or (self.path == "/health" and HEALTH_IS_JSON):
                body, ctype = json.dumps(BODY).encode(), "application/json"
            else:
                body, ctype = b"<!doctype html><div id=root></div>", "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def server_body(*, health_is_json: bool = True, version: str = "0.8.2") -> str:
    return SERVER.replace("__HEALTH_IS_JSON__", str(health_is_json)).replace("__VERSION__", version)


@posix_only
def test_smoke_runs_the_extracted_archive_with_the_shell_arguments_and_environment(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    release.make_archive(stub_build(tmp_path, server_body()), archive)
    result = release.smoke(archive, MAC_ARM, expect_version="0.8.2", timeout=20)
    assert result["passed"], result
    assert result["health_version"] == "0.8.2"


@posix_only
def test_smoke_fails_when_health_is_the_ui_fallback(tmp_path: Path) -> None:
    """The bug this guards: `/health` answered 200 text/html, so a dead backend looked alive."""
    archive = tmp_path / "a.tar.gz"
    release.make_archive(stub_build(tmp_path, server_body(health_is_json=False)), archive)
    result = release.smoke(archive, MAC_ARM, expect_version="0.8.2", timeout=20)
    assert not result["passed"]
    assert "/health is not JSON" in result["detail"]


@posix_only
def test_smoke_fails_on_a_version_the_build_does_not_have(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    release.make_archive(stub_build(tmp_path, server_body(version="0.0.0+unknown")), archive)
    result = release.smoke(archive, MAC_ARM, expect_version="0.8.2", timeout=20)
    assert not result["passed"]
    assert "0.0.0+unknown" in result["detail"]


@posix_only
def test_smoke_reports_an_early_exit(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    release.make_archive(stub_build(tmp_path, "import sys; print('no libpython'); sys.exit(3)"), archive)
    result = release.smoke(archive, MAC_ARM, timeout=20)
    assert not result["passed"]
    assert "code 3" in result["detail"] and "no libpython" in result["detail"]


@posix_only
def test_package_command_writes_the_fragment_and_fails_on_a_broken_folder(tmp_path: Path) -> None:
    out = tmp_path / "out"
    build_folder(tmp_path / "ok")
    argv = ["package", "--version", "0.8.2-alpha.1", "--triple", MAC_ARM, "--out", str(out)]
    assert release.main([*argv, "--folder", str(tmp_path / "ok" / NAME)]) == 0
    fragment = json.loads((out / "report" / f"{MAC_ARM}.json").read_text())
    assert fragment["status"] == "built" and fragment["sha256"] and fragment["signed"]["kind"] == "none"
    assert (out / fragment["asset_name"]).is_file()

    broken = tmp_path / "broken" / NAME
    broken.mkdir(parents=True)
    (broken / NAME).write_bytes(macho(ARM))
    assert release.main([*argv, "--folder", str(broken)]) == 1
    assert json.loads((out / "report" / f"{MAC_ARM}.json").read_text())["status"] == "failed"


@posix_only
def test_package_smoke_expects_the_base_version_for_a_suffixed_label(tmp_path: Path) -> None:
    """`0.8.2-dev.abc1234` is a label; the app inside reports `0.8.2`."""
    out = tmp_path / "out"
    folder = stub_build(tmp_path / "b", server_body(version="0.8.2"))
    argv = ["package", "--version", "0.8.2-dev.abc1234", "--triple", MAC_ARM, "--out", str(out),
            "--folder", str(folder), "--smoke"]  # fmt: skip
    assert release.main(argv) == 0
    assert json.loads((out / "report" / f"{MAC_ARM}.json").read_text())["smoke"]["health_version"] == "0.8.2"


# ---- versions, tags and the gate


def write_manifests(root: Path, **versions: str) -> None:
    """A fake checkout: the root and the four packages, all 0.8.2 unless overridden by path key."""
    for rel in release.VERSION_FILES:
        key = rel.replace("/", "_").replace("-", "_").replace(".", "_")
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[project]\nname = "x"\nversion = "{versions.get(key, "0.8.2")}"\n')


def test_the_real_repo_versions_agree() -> None:
    """The gate on the actual checkout: one version across the root and the four packages."""
    assert len(release.read_versions()) == 5
    assert release.base_version()  # exits non-zero if they disagree


def test_version_gate_accepts_matching_manifests(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    assert release.base_version(tmp_path) == "0.8.2"


def test_version_gate_fails_and_prints_every_value_when_one_package_disagrees(tmp_path: Path) -> None:
    write_manifests(tmp_path, packages_husk_studio_backend_pyproject_toml="0.8.1")
    with pytest.raises(SystemExit) as exc:
        release.base_version(tmp_path)
    message = str(exc.value)
    assert "version mismatch" in message
    for rel in release.VERSION_FILES:  # all five are listed, not just the odd one out
        assert rel in message
    assert "0.8.1" in message and "0.8.2" in message


def test_version_gate_fails_on_an_unreadable_manifest(tmp_path: Path) -> None:
    write_manifests(tmp_path)
    (tmp_path / "packages/husk-studio-cli/pyproject.toml").write_text("[project]\nname = 'x'\n")
    with pytest.raises(SystemExit):
        release.base_version(tmp_path)


def test_release_version_follows_the_tag_or_is_a_dev_build() -> None:
    assert release.release_version("0.8.2", "tag", "v0.8.2-alpha.1", "abcdef1234") == "0.8.2-alpha.1"
    assert release.release_version("0.8.2", "tag", "v0.8.2", None) == "0.8.2"
    assert release.release_version("0.8.2", "branch", "main", "abcdef1234") == "0.8.2-dev.abcdef1"
    with pytest.raises(SystemExit):
        release.release_version("0.8.2", "tag", "v0.9", None)  # the stale-release label
    with pytest.raises(SystemExit):
        release.release_version("0.8.2", "tag", "v0.8.20", None)  # a prefix is not a match
    assert release.channel_for("0.8.2-alpha.1") == "beta"
    assert release.channel_for("0.8.2") == "stable"


def test_version_command_prints_the_label(capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["version", "--ref-type", "branch", "--ref-name", "main", "--sha", "abcdef1234"]) == 0
    assert capsys.readouterr().out.strip() == f"version={release.base_version()}-dev.abcdef1"
    with pytest.raises(SystemExit):
        release.main(["version", "--ref-type", "tag", "--ref-name", "v99.0.0"])


# ---- the report


def fragment(triple: str, **over: object) -> dict:
    base = {"triple": triple, "version": "0.8.2-alpha.1", "status": "built",
            "asset_name": release.archive_name("0.8.2-alpha.1", triple), "size_bytes": 1000, "sha256": "ab" * 32,
            "unpacked_bytes": 2000, "entries": 9, "python": "3.11.15", "pyinstaller": "6.22",
            "arch_check": {"expected": "arm64", "native_files": 2},
            "smoke": {"passed": True, "health_version": "0.8.2", "seconds": 3.1, "detail": None},
            "binary_tests": {"passed": True, "outcome": "success", "summary": ""},
            "signed": {"kind": "none", "notarised": False}, "verified_on_real_hardware": True, "problems": []}  # fmt: skip
    return {**base, **over}


ENV = {"GITHUB_REPOSITORY": "Husk-ai-lab/husk-ai", "GITHUB_RUN_ID": "42", "GITHUB_SHA": "c0ffee" * 7,
       "GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.8.2-alpha.1", "RUNNER_ENVIRONMENT": "github-hosted"}  # fmt: skip


def test_report_lists_assets_gaps_and_the_husk_releases_shape() -> None:
    r = release.build_report(
        [fragment(MAC_ARM), fragment(MAC_X64, verified_on_real_hardware=False)],
        tag="v0.8.2-alpha.1", quality={"pytest": {"passed": 5, "failed": 0, "skipped": 0}},
        min_version=None, required=[MAC_ARM], env=ENV,
    )  # fmt: skip
    assert r["schema"] == release.SCHEMA and r["component"] == "husk-studio" and r["channel"] == "beta"
    assert r["builder"]["kind"] == "github-hosted"
    assert r["builder"]["run_url"] == "https://github.com/Husk-ai-lab/husk-ai/actions/runs/42"
    assert [a["triple"] for a in r["assets"]] == [MAC_ARM, MAC_X64]
    assert r["assets"][0]["url"].endswith(
        "/v0.8.2-alpha.1/husk-studio-0.8.2-alpha.1-aarch64-apple-darwin.tar.gz"
    )
    assert set(r["assets"][0]) == {"triple", "name", "size", "sha256", "url"}
    gaps = " ".join(r["known_gaps"])
    assert "no build for x86_64-pc-windows-msvc" in gaps and "not code-signed" in gaps
    assert "emulation" in gaps and MAC_X64 in gaps
    assert "bundled interpreter" in gaps and "end to end" in gaps


def test_report_flags_a_start_slower_than_the_shell_waits() -> None:
    slow = fragment(MAC_ARM, smoke={"passed": True, "health_version": "0.8.2", "seconds": 21.5, "detail": None})
    r = release.build_report([slow], tag=None, quality={}, min_version=None, required=[], env={})
    assert any("readiness wait" in g and MAC_ARM in g for g in r["known_gaps"])
    fast = release.build_report([fragment(MAC_ARM)], tag=None, quality={}, min_version=None, required=[], env={})
    assert not any("readiness wait" in g for g in fast["known_gaps"])


def test_report_flags_binary_tests_that_never_ran_against_the_archive() -> None:
    r = release.build_report([fragment(MAC_ARM, binary_tests=None)], tag=None, quality={}, min_version=None,
                             required=[], env={})  # fmt: skip
    assert any("binary tests" in g and MAC_ARM in g for g in r["known_gaps"])


def test_report_notes_the_windows_stop_semantics_only_when_windows_is_built() -> None:
    win = release.build_report([fragment(WIN)], tag=None, quality={}, min_version=None, required=[], env={})
    assert any("TerminateProcess" in g for g in win["known_gaps"])
    mac = release.build_report([fragment(MAC_ARM)], tag=None, quality={}, min_version=None, required=[], env={})
    assert not any("TerminateProcess" in g for g in mac["known_gaps"])


def test_report_refuses_a_tag_that_does_not_match_the_build() -> None:
    with pytest.raises(SystemExit):
        release.build_report([fragment(MAC_ARM)], tag="v0.9", quality={}, min_version=None, required=[], env=ENV)
    with pytest.raises(SystemExit):
        release.build_report([fragment(MAC_ARM), fragment(MAC_X64, version="0.8.3")], tag=None, quality={},
                             min_version=None, required=[], env=ENV)  # fmt: skip


def test_mark_tests_failure_turns_a_built_target_into_a_failed_one(tmp_path: Path) -> None:
    reports = tmp_path / "report"
    reports.mkdir()
    (reports / f"{MAC_ARM}.json").write_text(json.dumps(fragment(MAC_ARM, binary_tests=None)))
    base = ["mark-tests", "--triple", MAC_ARM, "--reports", str(reports)]
    assert release.main([*base, "--outcome", "failure", "--summary", "2 failed"]) == 0
    data = json.loads((reports / f"{MAC_ARM}.json").read_text())
    assert data["status"] == "failed"
    assert data["binary_tests"]["passed"] is False
    assert any("did not pass" in p and "failure" in p and "2 failed" in p for p in data["problems"])
    # ... so it is not an asset, and a required target that failed its tests fails the report
    out = tmp_path / "out"
    argv = ["report", "--reports", str(reports), "--out", str(out), "--require", MAC_ARM]
    assert release.main(argv) == 1
    assert json.loads((out / "release-report.json").read_text())["assets"] == []


def test_mark_tests_success_records_it_and_keeps_the_target(tmp_path: Path) -> None:
    reports = tmp_path / "report"
    reports.mkdir()
    (reports / f"{MAC_ARM}.json").write_text(json.dumps(fragment(MAC_ARM, binary_tests=None)))
    assert release.main(["mark-tests", "--triple", MAC_ARM, "--reports", str(reports),
                         "--outcome", "success"]) == 0  # fmt: skip
    data = json.loads((reports / f"{MAC_ARM}.json").read_text())
    assert data["status"] == "built" and data["binary_tests"]["passed"] is True


def test_report_command_writes_the_files_and_fails_when_a_required_target_is_missing(tmp_path: Path) -> None:
    reports = tmp_path / "report"
    reports.mkdir()
    (reports / f"{MAC_ARM}.json").write_text(json.dumps(fragment(MAC_ARM)))
    failed = fragment(WIN, status="failed", sha256=None, size_bytes=None, problems=["smoke test failed: boom"])
    (reports / f"{WIN}.json").write_text(json.dumps(failed))
    junit = tmp_path / "pytest.xml"
    junit.write_text('<testsuites><testsuite tests="10" failures="1" errors="0" skipped="2"/></testsuites>')
    common = ["report", "--reports", str(reports), "--out", str(tmp_path), "--pytest-xml", str(junit),
              "--check", "verify=success"]  # fmt: skip
    assert release.main([*common, "--require", MAC_ARM]) == 0
    assert release.main([*common, "--require", f"{MAC_ARM},{WIN}"]) == 1
    data = json.loads((tmp_path / "release-report.json").read_text())
    assert data["quality"]["pytest"] == {"passed": 7, "failed": 1, "skipped": 2}
    assert data["quality"]["verify"] == "success"
    assert (tmp_path / "SHA256SUMS").read_text() == f"{'ab' * 32}  husk-studio-0.8.2-alpha.1-{MAC_ARM}.tar.gz\n"
    md = (tmp_path / "RELEASE_REPORT.md").read_text()
    assert "smoke test failed: boom" in md and "Known gaps" in md and md.startswith("# Husk Studio 0.8.2-alpha.1")
