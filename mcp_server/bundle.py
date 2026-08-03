"""Bundle OpenSCAD and the BOSL2 library for the fusion-mcp server.

On first use, this module downloads and installs:

  * the OpenSCAD executable  -> ~/.fusion-mcp/bundle/openscad/
  * the BOSL2 library        -> ~/.fusion-mcp/bundle/BOSL2/

Only the Python standard library is used (urllib, zipfile, shutil,
subprocess), so the bundler works with zero extra pip dependencies.

Public API:
    get_openscad_path() -> str
    get_bosl2_path() -> str

Both functions are idempotent: they return immediately when the target is
already installed, and raise FileNotFoundError (with manual-install
instructions) if an automatic download/extract fails.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

BUNDLE_ROOT = os.path.expanduser("~/.fusion-mcp/bundle")
OPENSCAD_DIR = os.path.join(BUNDLE_ROOT, "openscad")
BOSL2_DIR = os.path.join(BUNDLE_ROOT, "BOSL2")

_OPENSCAD_BASE_URL = "https://files.openscad.org/"
_OPENSCAD_SNAPSHOTS_URL = _OPENSCAD_BASE_URL + "snapshots/"
# Known-good pinned fallbacks used when the snapshots directory cannot be
# fetched/parsed (all verified to exist as of 2026-08-04).
_OPENSCAD_PINNED_WINDOWS = _OPENSCAD_BASE_URL + "OpenSCAD-2021.01-x86-64.zip"
_OPENSCAD_PINNED_MACOS = _OPENSCAD_BASE_URL + "OpenSCAD-2021.01.dmg"
_OPENSCAD_PINNED_LINUX = _OPENSCAD_BASE_URL + "OpenSCAD-2021.01-x86_64.AppImage"
# IMPORTANT: the BOSL2 repo's default branch is `master`, NOT `main`.
# `refs/heads/main.zip` returns 404; this URL returns 200.
_BOSL2_ZIP_URL = "https://github.com/BelfrySCAD/BOSL2/archive/refs/heads/master.zip"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "fusion-mcp-bundler/1.0"
)

# Per-platform snapshot filename patterns: OpenSCAD-YYYY.MM.DD-<suffix>.
# These only match dated snapshot builds (never OpenSCAD-Tests-* or other
# non-dated artifacts).
_SNAPSHOT_PATTERNS: dict[str, str] = {
    "windows": r"OpenSCAD-(\d{4})\.(\d{2})\.(\d{2})-x86-64\.zip",
    "macos": r"OpenSCAD-(\d{4})\.(\d{2})\.(\d{2})\.dmg",
    "linux": r"OpenSCAD-(\d{4})\.(\d{2})\.(\d{2})-x86_64\.AppImage",
}


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Low-level helpers (stdlib only)
# ---------------------------------------------------------------------------
def _download(url: str, dest: str) -> None:
    """Download ``url`` to ``dest``, streaming with a browser-like User-Agent."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        with open(dest, "wb") as out_file:
            shutil.copyfileobj(response, out_file, length=1024 * 256)


def _fetch_text(url: str) -> str:
    """Fetch ``url`` and return its body as text."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def _extract_zip(zip_path: str, dest_dir: str) -> None:
    """Extract the zip archive at ``zip_path`` into ``dest_dir``."""
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest_dir)


def _remove_quietly(path: str) -> None:
    """Best-effort removal of a downloaded temp file."""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _resolve_openscad_url() -> str:
    """Return the newest matching OpenSCAD snapshot URL for this platform.

    Parses the files.openscad.org snapshots directory for the newest dated
    build matching the current platform, falling back to a pinned,
    known-good release URL when the directory cannot be fetched/parsed.
    """
    if _is_windows():
        fallback, pattern = _OPENSCAD_PINNED_WINDOWS, _SNAPSHOT_PATTERNS["windows"]
    elif _is_macos():
        fallback, pattern = _OPENSCAD_PINNED_MACOS, _SNAPSHOT_PATTERNS["macos"]
    elif _is_linux():
        fallback, pattern = _OPENSCAD_PINNED_LINUX, _SNAPSHOT_PATTERNS["linux"]
    else:
        raise FileNotFoundError(_openscad_manual_install_msg())

    def _date_key(name: str) -> tuple[int, int, int]:
        match = re.search(pattern, name)
        if match is None:
            return (-1, -1, -1)
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))

    try:
        html = _fetch_text(_OPENSCAD_SNAPSHOTS_URL)
    except OSError:
        return fallback

    names = [
        name
        for name in re.findall(r'href="([^"]+)"', html)
        if _date_key(name) != (-1, -1, -1)
    ]
    if not names:
        return fallback
    newest = max(names, key=_date_key)
    if newest.startswith("http"):
        return newest
    return _OPENSCAD_SNAPSHOTS_URL + newest


# ---------------------------------------------------------------------------
# Manual-install instructions
# ---------------------------------------------------------------------------
def _openscad_manual_install_msg() -> str:
    """Platform-specific manual-install instructions for OpenSCAD."""
    if _is_windows():
        return (
            "Failed to download/extract the bundled OpenSCAD automatically.\n"
            "Manual install: download the latest x86-64 zip from "
            f"{_OPENSCAD_SNAPSHOTS_URL} (fallback: {_OPENSCAD_PINNED_WINDOWS}), "
            f"extract it, and make sure openscad.com ends up under {OPENSCAD_DIR}."
        )
    if _is_macos():
        return (
            "Failed to download/extract the bundled OpenSCAD automatically.\n"
            "Manual install: download the OpenSCAD DMG from "
            f"{_OPENSCAD_SNAPSHOTS_URL} (fallback: {_OPENSCAD_PINNED_MACOS}), "
            f"mount it, and copy OpenSCAD.app into {OPENSCAD_DIR}."
        )
    if _is_linux():
        return (
            "Failed to download the bundled OpenSCAD automatically.\n"
            "Manual install: download the OpenSCAD AppImage from "
            f"{_OPENSCAD_SNAPSHOTS_URL} (fallback: {_OPENSCAD_PINNED_LINUX}) and place it at "
            f"{os.path.join(OPENSCAD_DIR, 'openscad.AppImage')} (chmod +x)."
        )
    return (
        "OpenSCAD bundling is not supported on this platform. "
        f"Install OpenSCAD manually so its executable is under {OPENSCAD_DIR}."
    )


def _bosl2_manual_install_msg() -> str:
    """Manual-install instructions for BOSL2."""
    return (
        "Failed to download/extract the bundled BOSL2 library automatically.\n"
        f"Manual install: download {_BOSL2_ZIP_URL}, extract it, and make sure "
        f"std.scad is at {os.path.join(BOSL2_DIR, 'std.scad')}."
    )


# ---------------------------------------------------------------------------
# OpenSCAD install
# ---------------------------------------------------------------------------
def _install_openscad_windows() -> None:
    """Download the Windows x86-64 zip and extract it under OPENSCAD_DIR."""
    zip_path = os.path.join(BUNDLE_ROOT, "_openscad_download.zip")
    try:
        _download(_resolve_openscad_url(), zip_path)
        shutil.rmtree(OPENSCAD_DIR, ignore_errors=True)
        os.makedirs(OPENSCAD_DIR, exist_ok=True)
        _extract_zip(zip_path, OPENSCAD_DIR)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileNotFoundError(_openscad_manual_install_msg()) from exc
    finally:
        _remove_quietly(zip_path)


def _install_openscad_macos() -> None:
    """Download the DMG, mount it, copy OpenSCAD.app, then unmount.

    NOTE: this branch is implemented by construction — it follows the
    standard hdiutil workflow (attach -> copy -> detach) used by OpenSCAD's
    own macOS distribution, but cannot be executed on the Windows dev
    machine. The DMG mounts at a local mount point; OpenSCAD.app is copied
    out and the DMG is detached in the ``finally`` block so nothing stays
    mounted on failure.
    """
    dmg_path = os.path.join(BUNDLE_ROOT, "_openscad.dmg")
    mount_point = os.path.join(BUNDLE_ROOT, "_openscad_mount")
    app_dest = os.path.join(OPENSCAD_DIR, "OpenSCAD.app")
    try:
        _download(_resolve_openscad_url(), dmg_path)
        os.makedirs(mount_point, exist_ok=True)
        subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-mountpoint", mount_point],
            check=True,
            capture_output=True,
        )
        source_app = os.path.join(mount_point, "OpenSCAD.app")
        if not os.path.isdir(source_app):
            raise FileNotFoundError(_openscad_manual_install_msg())
        shutil.rmtree(OPENSCAD_DIR, ignore_errors=True)
        shutil.copytree(source_app, app_dest)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FileNotFoundError(_openscad_manual_install_msg()) from exc
    finally:
        try:
            subprocess.run(
                ["hdiutil", "detach", mount_point], check=False, capture_output=True
            )
        except OSError:
            pass
        _remove_quietly(dmg_path)
        shutil.rmtree(mount_point, ignore_errors=True)


def _install_openscad_linux() -> None:
    """Download the AppImage and make it executable."""
    appimage = os.path.join(OPENSCAD_DIR, "openscad.AppImage")
    try:
        os.makedirs(OPENSCAD_DIR, exist_ok=True)
        _download(_resolve_openscad_url(), appimage)
        os.chmod(appimage, 0o755)
    except OSError as exc:
        _remove_quietly(appimage)
        raise FileNotFoundError(_openscad_manual_install_msg()) from exc


def _install_openscad() -> None:
    """Platform-dispatching OpenSCAD installer."""
    if _is_windows():
        _install_openscad_windows()
    elif _is_macos():
        _install_openscad_macos()
    elif _is_linux():
        _install_openscad_linux()
    else:
        raise FileNotFoundError(_openscad_manual_install_msg())


def _find_openscad_com() -> str | None:
    """Return the path of ``openscad.com`` under OPENSCAD_DIR, or None."""
    if not os.path.isdir(OPENSCAD_DIR):
        return None
    for root, _dirs, files in os.walk(OPENSCAD_DIR):
        for name in files:
            if name.lower() == "openscad.com":
                return os.path.join(root, name)
    return None


def _find_installed_openscad() -> str | None:
    """Return the platform's OpenSCAD executable path if already installed."""
    if _is_windows():
        return _find_openscad_com()
    if _is_macos():
        candidate = os.path.join(
            OPENSCAD_DIR, "OpenSCAD.app", "Contents", "MacOS", "OpenSCAD"
        )
        return candidate if os.path.isfile(candidate) else None
    if _is_linux():
        candidate = os.path.join(OPENSCAD_DIR, "openscad.AppImage")
        return candidate if os.path.isfile(candidate) else None
    return None


def get_openscad_path() -> str:
    """Return the absolute path to the bundled OpenSCAD executable.

    Downloads and installs OpenSCAD on first use (idempotent afterwards).
    Raises FileNotFoundError with platform-specific manual-install
    instructions if the download/extract fails.
    """
    existing = _find_installed_openscad()
    if existing is not None:
        return existing
    _install_openscad()
    installed = _find_installed_openscad()
    if installed is None:
        raise FileNotFoundError(_openscad_manual_install_msg())
    return installed


# ---------------------------------------------------------------------------
# BOSL2 install
# ---------------------------------------------------------------------------
def _install_bosl2() -> None:
    """Download the BOSL2 archive and normalize it to BUNDLE_ROOT/BOSL2.

    The GitHub archive extracts to a single top-level folder named
    ``BOSL2-master/``; it is renamed to ``BOSL2/`` so callers get a stable,
    version-independent path.
    """
    zip_path = os.path.join(BUNDLE_ROOT, "_bosl2_download.zip")
    try:
        os.makedirs(BUNDLE_ROOT, exist_ok=True)
        _download(_BOSL2_ZIP_URL, zip_path)
        _extract_zip(zip_path, BUNDLE_ROOT)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileNotFoundError(_bosl2_manual_install_msg()) from exc
    finally:
        _remove_quietly(zip_path)

    extracted = os.path.join(BUNDLE_ROOT, "BOSL2-master")
    if not os.path.isdir(extracted):
        # Tolerate a differently-named top-level folder (e.g. BOSL2-main)
        # as long as exactly one candidate exists.
        candidates = [
            os.path.join(BUNDLE_ROOT, name)
            for name in os.listdir(BUNDLE_ROOT)
            if name.startswith("BOSL2")
            and os.path.isdir(os.path.join(BUNDLE_ROOT, name))
        ]
        if len(candidates) == 1:
            extracted = candidates[0]
        else:
            raise FileNotFoundError(_bosl2_manual_install_msg())
    os.rename(extracted, BOSL2_DIR)


def get_bosl2_path() -> str:
    """Return the path to the BOSL2 library directory.

    Downloads and installs BOSL2 on first use (idempotent afterwards).
    Raises FileNotFoundError with manual-install instructions on failure.
    """
    std_scad = os.path.join(BOSL2_DIR, "std.scad")
    if os.path.isfile(std_scad):
        return BOSL2_DIR
    if os.path.isdir(BOSL2_DIR):
        shutil.rmtree(BOSL2_DIR, ignore_errors=True)
    _install_bosl2()
    if not os.path.isfile(std_scad):
        raise FileNotFoundError(_bosl2_manual_install_msg())
    return BOSL2_DIR
