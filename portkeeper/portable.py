"""Fixed launchers for release assembly; never executed by the build worker."""
from .safety import Invalid, confined


def prepare_dusklight(package, target, disc):
    confined(package, disc, must_exist=True)
    if target == "windows":
        confined(package, "dusklight.exe", must_exist=True)
        name = "Launch Dusklight.cmd"
        content = ('@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
                   'if not exist "data" mkdir "data"\r\n'
                   f'"%~dp0dusklight.exe" --dvd "{disc}" --user-dir "%~dp0data" %*\r\n')
    else:
        confined(package, "Dusklight.AppImage", must_exist=True)
        name = "Launch Dusklight.sh"
        content = ('#!/bin/sh\nset -eu\n'
                   'cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                   'mkdir -p data\n'
                   'chmod +x Dusklight.AppImage\n'
                   'export APPIMAGE_EXTRACT_AND_RUN=1\n'
                   f'exec ./Dusklight.AppImage --dvd "{disc}" --user-dir "$PWD/data" "$@"\n')
    readme = "PORTABLE-README.txt"
    for filename in (name, readme):
        if confined(package, filename).exists():
            raise Invalid(f"Portable launcher destination already exists: {filename}")
    launcher = confined(package, name)
    launcher.write_bytes(content.encode("utf-8"))
    launcher.chmod(0o755 if target != "windows" else 0o644)
    confined(package, readme).write_text(
        f"Extract this entire ZIP into a writable folder, then run {name}.\n"
        "Linux: if your file manager opens the script as text, run it with sh.\n"
        "The launcher selects the included owned disc automatically.\n"
        "Saves, settings, and user data stay in the data folder beside the game.\n"
        "Move the whole folder to retain them; carry data forward when updating.\n"
        "Game files are read directly from the disc image; no disc extraction is needed.\n"
        "The package has been checked for structure; gameplay may be untested.\n",
        encoding="utf-8")
