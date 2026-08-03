"""Headless unit tests for mcp_server/bundle.py.

Covers (plan Todo 17 acceptance):
  * path-resolution logic in ``_resolve_openscad_url`` with the network
    fetcher monkeypatched away (newest-dated snapshot, offline fallback,
    non-matching-directory fallback, non-dated artifacts filtered)
  * unit conversion mm <-> cm <-> in via ``DEFAULT_UNITS`` from
    scad_translator.py (1 OpenSCAD unit == 1 mm -> 0.1 cm, 1 in -> 2.54 cm)
  * the ``_BOSL2_ZIP_URL`` / ``_OPENSCAD_SNAPSHOTS_URL`` constants exist
  * idempotent get_*_path() resolution WITHOUT downloading (skipped when the
    bundle is not already installed -- tests never trigger a download)
  * the plan's QA-failure requirement: prove the assertions are REAL by
    demonstrating that a deliberately-wrong expectation raises
    AssertionError instead of silently passing

No network is used: the only entry points that touch urllib are the
monkeypatched-off ``_fetch_text`` and the downloader (which the idempotency
tests never reach because the bundle is pre-installed, and which are skipped
otherwise).
"""

import os

import pytest

from conftest import TrapRoot
from mcp_server import bundle as b
from mcp_server.scad_translator import DEFAULT_UNITS, translate_to_fusion_commands


def _snapshot_name(version):
    """Build a dated snapshot filename matching this platform's pattern."""
    if b._is_windows():
        return "OpenSCAD-%s-x86-64.zip" % version
    if b._is_macos():
        return "OpenSCAD-%s.dmg" % version
    return "OpenSCAD-%s-x86_64.AppImage" % version


def _pinned_fallback():
    if b._is_windows():
        return b._OPENSCAD_PINNED_WINDOWS
    if b._is_macos():
        return b._OPENSCAD_PINNED_MACOS
    return b._OPENSCAD_PINNED_LINUX


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

def test_bundle_constants_exist():
    assert b._OPENSCAD_SNAPSHOTS_URL.startswith("https://files.openscad.org/")
    assert b._BOSL2_ZIP_URL.startswith("https://github.com/BelfrySCAD/BOSL2")
    assert ".fusion-mcp" in b.BUNDLE_ROOT
    assert b.BOSL2_DIR
    assert b.OPENSCAD_DIR
    assert set(b._SNAPSHOT_PATTERNS) == {"windows", "macos", "linux"}


# ---------------------------------------------------------------------------
# unit conversion mm <-> cm <-> in (DEFAULT_UNITS from scad_translator)
# ---------------------------------------------------------------------------

def test_default_units_scale_to_cm():
    # 1 OpenSCAD unit == 1 mm -> 0.1 cm; cm is identity; 1 in -> 2.54 cm.
    assert DEFAULT_UNITS["mm"] == 0.1
    assert DEFAULT_UNITS["cm"] == 1.0
    assert DEFAULT_UNITS["in"] == 2.54


def test_unit_conversions_consistent():
    # 10 mm per cm (Fusion's internal unit).
    assert 1.0 / DEFAULT_UNITS["mm"] == 10.0
    # 1 in == 25.4 mm exactly.
    assert abs(DEFAULT_UNITS["in"] / DEFAULT_UNITS["mm"] - 25.4) < 1e-9
    # round-trip: in -> cm -> in.
    assert abs(DEFAULT_UNITS["in"] * (1.0 / DEFAULT_UNITS["in"]) - 1.0) < 1e-9
    # 1 cm == 1 / 2.54 in (0.3937007874...).
    assert abs(1.0 / DEFAULT_UNITS["in"] - 0.3937007874) < 1e-6


def test_unknown_units_fall_back_to_mm_without_keyerror():
    # An unknown units string must fall back (mm scale) instead of KeyError:
    # the empty tree validates, then the executor's adsk import raises the
    # RuntimeError -- reaching that point proves the units lookup succeeded.
    with pytest.raises(RuntimeError) as excinfo:
        translate_to_fusion_commands([], TrapRoot(), None, units="furlong")
    assert "adsk is not available" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _resolve_openscad_url: path resolution with the network fetcher mocked
# ---------------------------------------------------------------------------

def test_resolve_openscad_url_picks_newest_dated_snapshot(monkeypatch):
    html = ('<a href="%s">older</a><a href="%s">newer</a>'
            % (_snapshot_name("2026.06.10"), _snapshot_name("2026.08.01")))
    monkeypatch.setattr(b, "_fetch_text", lambda url: html)
    url = b._resolve_openscad_url()
    assert url == b._OPENSCAD_SNAPSHOTS_URL + _snapshot_name("2026.08.01")


def test_resolve_openscad_url_filters_non_dated_artifacts(monkeypatch):
    html = ('<a href="OpenSCAD-Tests-2026.08.01-x86-64.zip">tests</a>'
            '<a href="OpenSCAD-2021.01-x86-64.zip">release</a>'
            '<a href="%s">snapshot</a>'
            % _snapshot_name("2026.08.01"))
    monkeypatch.setattr(b, "_fetch_text", lambda url: html)
    url = b._resolve_openscad_url()
    # Only the dated snapshot matches the per-platform pattern.
    assert url == b._OPENSCAD_SNAPSHOTS_URL + _snapshot_name("2026.08.01")


def test_resolve_openscad_url_offline_falls_back_to_pinned(monkeypatch):
    def _offline(url):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(b, "_fetch_text", _offline)
    assert b._resolve_openscad_url() == _pinned_fallback()


def test_resolve_openscad_url_no_match_falls_back_to_pinned(monkeypatch):
    monkeypatch.setattr(b, "_fetch_text",
                        lambda url: "<html>no snapshot links here</html>")
    assert b._resolve_openscad_url() == _pinned_fallback()


def test_resolve_openscad_url_invalid_directory_falls_back(monkeypatch):
    # Invalid resolver input: a bogus snapshots directory.  The offline
    # fetcher raises before any match, so the pinned URL must be returned --
    # and it must be asserted, not silently tolerated.
    monkeypatch.setattr(
        b, "_OPENSCAD_SNAPSHOTS_URL",
        "https://files.openscad.org/definitely-not-a-real-dir/")

    def _offline(url):
        raise OSError("cannot reach %r" % url)
    monkeypatch.setattr(b, "_fetch_text", _offline)
    assert b._resolve_openscad_url() == _pinned_fallback()


# ---------------------------------------------------------------------------
# idempotent get_*_path() (never downloads in tests)
# ---------------------------------------------------------------------------

def test_get_bosl2_path_resolves_without_network_when_installed():
    std_scad = os.path.join(b.BOSL2_DIR, "std.scad")
    if not os.path.isfile(std_scad):
        pytest.skip("BOSL2 bundle not installed -- resolving would download")
    path = b.get_bosl2_path()
    assert path == b.BOSL2_DIR
    assert os.path.isfile(os.path.join(path, "std.scad"))


def test_get_openscad_path_resolves_without_network_when_installed():
    if b._find_installed_openscad() is None:
        pytest.skip("OpenSCAD bundle not installed -- resolving would download")
    path = b.get_openscad_path()
    assert os.path.isfile(path)
    assert path == b._find_installed_openscad()


def test_find_installed_openscad_windows_shape():
    if not b._is_windows():
        pytest.skip("windows-specific layout")
    exe = b._find_installed_openscad()
    # None when not installed, otherwise the openscad.com executable.
    assert exe is None or exe.lower().endswith("openscad.com")


# ---------------------------------------------------------------------------
# plan QA failure: prove the assertions are real
# ---------------------------------------------------------------------------

def test_assertions_are_real_deliberately_wrong_expectation_fails():
    # The actual value the module uses -- the assertion below must PASS:
    assert DEFAULT_UNITS["mm"] == 0.1
    # A deliberately-wrong expectation must FAIL cleanly with AssertionError
    # (plan QA failure mode: changing the expected value breaks the test
    # loudly instead of silently passing).
    with pytest.raises(AssertionError):
        assert DEFAULT_UNITS["mm"] == 0.12345  # deliberately wrong
