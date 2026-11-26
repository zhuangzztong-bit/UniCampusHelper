"""Build the Windows x64 download from a Git commit and verified wheels."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile, ZIP_DEFLATED


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.14.10"
PYTHON_ABI = "cp314"
PYTHON_ZIP = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
ASSET_NAME = "UniCampusHelper-Windows-x64.zip"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(output_dir: Path, ref: str, index_url: str, runtime_archive: Path | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=ROOT).decode().strip()
    with tempfile.TemporaryDirectory(prefix="uc-", ignore_cleanup_errors=True) as scratch:
        stage = Path(scratch)
        app = stage / "app"
        source = stage / "source.zip"
        subprocess.run(["git", "archive", "--format=zip", f"--output={source}", commit], cwd=ROOT, check=True)
        with ZipFile(source) as archive:
            archive.extractall(app)

        if runtime_archive is None:
            runtime_archive = stage / PYTHON_ZIP
            url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ZIP}"
            with urlopen(url, timeout=30) as response, runtime_archive.open("wb") as target:
                shutil.copyfileobj(response, target)
        if sha256(runtime_archive) != PYTHON_SHA256:
            raise ValueError(f"Python archive SHA-256 does not match the python.org value: {runtime_archive.name}")
        runtime = app / ".runtime"
        with ZipFile(runtime_archive) as archive:
            archive.extractall(runtime)

        libraries = runtime / "pkgs"
        subprocess.run([
            sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--only-binary=:all:",
            "--require-hashes", "--no-compile", "--retries", "2", "--timeout", "20",
            "--platform", "win_amd64", "--implementation", "cp", "--abi", PYTHON_ABI,
            "--python-version", PYTHON_VERSION, "--target", str(libraries), "--index-url", index_url,
            "-r", str(app / "requirements-windows-x64.lock"),
        ], cwd=stage, check=True)
        (runtime / "python314._pth").write_text("python314.zip\n.\npkgs\n..\n", encoding="ascii")
        for command in app.glob("*.cmd"):
            command.write_text(command.read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n")

        if sys.platform == "win32":
            subprocess.run([str(runtime / "python.exe"), "-B", "-c",
                            "import selenium, openai, numpy; from PySide6 import QtCore, QtQml, QtQuick"],
                           cwd=app, check=True)
        asset = output_dir / ASSET_NAME
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".pending", delete=False) as stream:
                pending = Path(stream.name)
                with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=5) as archive:
                    archive.comment = commit.encode("ascii")
                    for path in sorted(app.rglob("*")):
                        if not path.is_file() or path.is_relative_to(libraries / "bin") or path.is_relative_to(libraries / "Scripts"):
                            continue
                        if path.suffix == ".obj" and path.is_relative_to(libraries / "PySide6"):
                            continue  # Developer build objects are not loaded by Qt Quick.
                        archive.write(path, f"UniCampusHelper/{path.relative_to(app).as_posix()}")
            os.replace(pending, asset)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    (output_dir / f"{ASSET_NAME}.sha256").write_text(f"{sha256(asset)}  {asset.name}\n", encoding="ascii")
    return asset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--index-url", default="https://pypi.org/simple")
    parser.add_argument("--runtime-archive", type=Path, help="Use a locally cached python.org ZIP after checksum validation")
    args = parser.parse_args()
    print(build(args.output_dir.resolve(), args.ref, args.index_url, args.runtime_archive))


if __name__ == "__main__":
    main()
