"""Build a single-file Windows exe with PyInstaller.

    pip install -r requirements-dev.txt
    python build.py

Output: dist/HomePod Streamer.exe
"""

from __future__ import annotations

import sys
from pathlib import Path

import PyInstaller.__main__

from config import APP_NAME, APP_VERSION

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def version_file() -> Path:
    """Windows version resource (shown in Task Manager, file properties and Windows prompts)."""
    major, minor, patch = (int(x) for x in APP_VERSION.split("."))
    BUILD.mkdir(exist_ok=True)
    path = BUILD / "version_info.txt"
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({major}, {minor}, {patch}, 0), prodvers=({major}, {minor}, {patch}, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('ProductVersion', '{APP_VERSION}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('InternalName', 'homepod-streamer')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return path


def main() -> None:
    PyInstaller.__main__.run([
        str(ROOT / "main.py"),
        "--name", APP_NAME,
        "--onefile",
        "--windowed",  # no console window
        "--noconfirm",
        "--clean",
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--version-file", str(version_file()),
        "--add-data", f"{ROOT / 'assets'}{';' if sys.platform == 'win32' else ':'}assets",
        "--collect-data", "customtkinter",  # theme JSON files
        "--hidden-import", "pystray._win32",  # backend chosen at runtime
        "--collect-submodules", "pyatv",
        "--specpath", str(BUILD),
        "--workpath", str(BUILD),
        "--distpath", str(ROOT / "dist"),
    ])
    print(f"\nBuilt: {ROOT / 'dist' / (APP_NAME + '.exe')}")


if __name__ == "__main__":
    main()
