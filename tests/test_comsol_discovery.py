"""COMSOL install discovery (`comsol_support._default_comsol_path`).

Order: the platform's vendor-default roots, then the install that owns a
`comsol` launcher on PATH, then the first conventional root as a
placeholder. No site-specific root is ever probed — a site that installs
COMSOL elsewhere exports COMSOL_PATH or puts `comsol` on PATH.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import comsol_support


def _install(root: Path, launcher: str = "bin/comsol", plugins: bool = True) -> Path:
    exe = root / launcher
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    if plugins:
        (root / "plugins").mkdir()
    return exe


@pytest.fixture
def no_vendor_roots(monkeypatch, tmp_path):
    """Make every vendor-default root absent, whatever the platform."""
    missing = str(tmp_path / "nowhere" / "multiphysics")
    monkeypatch.setattr(comsol_support, "_DEFAULT_CANDIDATES",
                        {k: [missing] for k in ("Linux", "Windows", "Darwin")})
    return missing


def test_vendor_root_wins_over_launcher(monkeypatch, tmp_path):
    vendor = tmp_path / "opt" / "comsol64" / "multiphysics"
    vendor.mkdir(parents=True)
    other = _install(tmp_path / "elsewhere")
    monkeypatch.setattr(comsol_support, "_DEFAULT_CANDIDATES",
                        {k: [str(vendor)] for k in ("Linux", "Windows", "Darwin")})
    monkeypatch.setattr(comsol_support.shutil, "which", lambda name: str(other))
    assert comsol_support._default_comsol_path() == str(vendor)


def test_launcher_on_path_locates_install(no_vendor_roots, monkeypatch, tmp_path):
    root = tmp_path / "site" / "comsol64" / "multiphysics"
    exe = _install(root)
    monkeypatch.setattr(comsol_support.shutil, "which",
                        lambda name: str(exe) if name == "comsol" else None)
    assert comsol_support._default_comsol_path() == str(root)


def test_launcher_symlink_is_resolved(no_vendor_roots, monkeypatch, tmp_path):
    root = tmp_path / "site" / "multiphysics"
    exe = _install(root)
    link = tmp_path / "usr-local-bin" / "comsol"
    link.parent.mkdir()
    try:
        os.symlink(exe, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    monkeypatch.setattr(comsol_support.shutil, "which", lambda name: str(link))
    assert comsol_support._default_comsol_path() == str(root)


def test_windows_launcher_layout(no_vendor_roots, monkeypatch, tmp_path):
    root = tmp_path / "COMSOL64" / "Multiphysics"
    exe = _install(root, launcher="bin/win64/comsol.exe")
    monkeypatch.setattr(comsol_support.shutil, "which", lambda name: str(exe))
    assert comsol_support._default_comsol_path() == str(root)


def test_unrelated_comsol_script_is_rejected(no_vendor_roots, monkeypatch, tmp_path):
    """A `comsol` wrapper script whose parent tree has no plugins/ is not an install."""
    exe = _install(tmp_path / "wrappers", plugins=False)
    monkeypatch.setattr(comsol_support.shutil, "which", lambda name: str(exe))
    assert comsol_support._default_comsol_path() == no_vendor_roots


def test_no_install_falls_back_to_conventional_root(no_vendor_roots, monkeypatch):
    monkeypatch.setattr(comsol_support.shutil, "which", lambda name: None)
    assert comsol_support._default_comsol_path() == no_vendor_roots
