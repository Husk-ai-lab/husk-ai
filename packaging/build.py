"""Build the Husk Studio binary.

    uv run python packaging/build.py             # onedir  -> dist/husk-studio/
    uv run python packaging/build.py --onefile   # single executable -> dist/onefile/husk-studio
    uv run python packaging/build.py --rebuild-studio

Steps: (1) build the Studio UI if apps/studio/dist is missing, (2) sync a *clean*
environment (.venv-build, no dev/examples groups, so langchain & co. never end up in
the bundle), (3) run PyInstaller against husk-studio.spec.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--onefile", action="store_true", help="build a single executable")
    ap.add_argument("--rebuild-studio", action="store_true", help="rebuild the Studio UI first")
    args = ap.parse_args()

    studio_index = ROOT / "apps" / "studio" / "dist" / "index.html"
    if args.rebuild_studio or not studio_index.is_file():
        corepack = shutil.which("corepack")
        if corepack is None:
            print("error: Node 20+ (corepack) is needed to build the Studio UI.", file=sys.stderr)
            return 1
        run([corepack, "pnpm", "install", "--frozen-lockfile"])
        run([corepack, "pnpm", "--filter", "studio", "build"])

    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv-build")
    env.pop("VIRTUAL_ENV", None)  # an activated dev venv would only make uv warn and ignore it
    if args.onefile:
        env["HUSK_STUDIO_ONEFILE"] = "1"
    run(["uv", "sync", "--frozen", "--all-packages", "--no-dev", "--group", "build"], env=env)
    # onefile writes a file called `husk-studio`, which would collide with the onedir folder.
    distpath = ROOT / "dist" / ("onefile" if args.onefile else "")
    run(
        ["uv", "run", "--no-sync", "pyinstaller", "husk-studio.spec", "--noconfirm", "--clean"]
        + ["--distpath", str(distpath)],
        env=env,
    )

    exe = "husk-studio.exe" if os.name == "nt" else "husk-studio"
    out = distpath / exe if args.onefile else distpath / "husk-studio" / exe
    print(f"\nBuilt: {out}")
    return 0 if out.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
