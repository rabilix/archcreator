"""Build the two distributions of archcreator.

    python packaging/build.py            # both
    python packaging/build.py --wheel    # dist/archcreator-<v>-py3-none-any.whl (needs Python + pip on the target)
    python packaging/build.py --exe      # dist/archcreator/archcreator.exe + dist/archcreator-<v>-win64.zip (no Python needed)

The executable folder carries libclang and the central profiles (dist/archcreator/profiles); a machine can still point
ARCHCREATOR_PROFILES at a shared profile folder, which is searched first. Run from the repository root with the venv
that has the `build` extra installed (pip install -e .[build]).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
sys.path.insert(0, str(ROOT / "src"))
from archcreator import __version__  # noqa: E402


def run(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def build_wheel() -> Path:
    run(sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(DIST))
    return DIST / f"archcreator-{__version__}-py3-none-any.whl"


def build_exe() -> Path:
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "archcreator",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        "--paths", str(ROOT / "src"),
        # libclang.dll ships inside the clang package; lizard loads its extensions and languages by name.
        "--collect-binaries", "clang",
        "--collect-submodules", "lizard_ext",
        "--collect-submodules", "lizard_languages",
        "--copy-metadata", "libclang",
        str(ROOT / "packaging" / "archcreator_entry.py"))
    target = DIST / "archcreator"
    profiles = target / "profiles"
    shutil.rmtree(profiles, ignore_errors=True)
    profiles.mkdir()
    for f in sorted((ROOT / "profiles").glob("*.yaml")):
        shutil.copy2(f, profiles / f.name)
    archive = shutil.make_archive(str(DIST / f"archcreator-{__version__}-win64"), "zip", DIST, "archcreator")
    return Path(archive)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", action="store_true")
    parser.add_argument("--exe", action="store_true")
    args = parser.parse_args()
    both = not (args.wheel or args.exe)
    built = []
    if args.wheel or both:
        built.append(build_wheel())
    if args.exe or both:
        built.append(build_exe())
    for b in built:
        print("built", b.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
