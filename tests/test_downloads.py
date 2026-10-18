from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pco_services_mcp.client import PcoError
from pco_services_mcp.downloads import MANIFEST, Manifest, confined, safe_name


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Song - D.pdf", "Song - D.pdf"),
        ("a/b\\c.pdf", "a_b_c.pdf"),
        ('x:"y"?.pdf', "x__y__.pdf"),
        ("CON.pdf", "_CON.pdf"),
        ("lpt1", "_lpt1"),
        ("name. . ", "name"),
        ("", "file"),
        ("../../etc/passwd", ".._.._etc_passwd"),
    ],
)
def test_safe_name(raw, clean):
    assert safe_name(raw) == clean


def test_safe_name_limits_length_and_keeps_extension():
    name = safe_name("x" * 400 + ".pdf")
    assert len(name) <= 120 and name.endswith(".pdf")


@pytest.mark.parametrize("parts", [("..", "x"), ("../x",), ("/abs/path",), ("C:/abs",)])
def test_confined_refuses_escape(tmp_path, parts):
    with pytest.raises(PcoError):
        confined(tmp_path.resolve(), *parts)


def test_confined_refuses_a_link_out_of_the_root(tmp_path):
    root = (tmp_path / "root").resolve()
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("cannot create a symlink here")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        if result.returncode:
            pytest.skip("cannot create a junction here")
    with pytest.raises(PcoError):
        confined(root, "link", "f.pdf")


def test_new_attachment_gets_its_filename(tmp_path):
    path, current = Manifest.load(tmp_path).target("1", "Song.pdf", "t1")
    assert path.name == "Song.pdf" and not current


def test_unmanaged_file_is_never_overwritten(tmp_path):
    (tmp_path / "Song.pdf").write_bytes(b"mine")
    path, _ = Manifest.load(tmp_path).target("7", "Song.pdf", "t1")
    assert path.name == "Song (7).pdf"
    (tmp_path / "Song (7).pdf").write_bytes(b"also mine")
    with pytest.raises(PcoError, match="left alone"):
        Manifest.load(tmp_path).target("7", "Song.pdf", "t1")


def test_two_attachments_with_the_same_name_both_survive(tmp_path):
    manifest = Manifest.load(tmp_path)
    first, _ = manifest.target("1", "Chart.pdf", "t")
    first.write_bytes(b"a")
    manifest.record("1", first, "t", 1)
    second, _ = manifest.target("2", "Chart.pdf", "t")
    assert second.name == "Chart (2).pdf"


def test_reuse_is_decided_by_updated_at_not_size(tmp_path):
    manifest = Manifest.load(tmp_path)
    path, _ = manifest.target("1", "Chart.pdf", "t1")
    path.write_bytes(b"same size")
    manifest.record("1", path, "t1", 9)
    reloaded = Manifest.load(tmp_path)
    assert reloaded.target("1", "Chart.pdf", "t1") == (path, True)
    assert reloaded.target("1", "Chart.pdf", "t2") == (path, False)


def test_tampered_manifest_cannot_point_outside(tmp_path):
    (tmp_path / MANIFEST).write_text('{"1": {"filename": "../escape.pdf"}}', encoding="utf-8")
    with pytest.raises(PcoError):
        Manifest.load(tmp_path.resolve()).target("1", "x.pdf", None)


@pytest.mark.parametrize("name", [MANIFEST, MANIFEST.upper()])
def test_an_attachment_named_like_the_manifest_gets_its_own_file(tmp_path, name):
    manifest = Manifest.load(tmp_path)
    path, current = manifest.target("5", name, "t")
    assert path.name.lower() != MANIFEST and not current
    path.write_bytes(b"attachment")
    manifest.record("5", path, "t", 10)
    assert path.read_bytes() == b"attachment"
    assert Manifest.load(tmp_path).target("5", name, "t") == (path, True)


def test_a_managed_file_swapped_for_a_link_is_left_alone(tmp_path):
    manifest = Manifest.load(tmp_path)
    path, _ = manifest.target("1", "Chart.pdf", "t1")
    path.write_bytes(b"chart")
    manifest.record("1", path, "t1", 5)
    personal = tmp_path / "Personal.pdf"
    personal.write_bytes(b"mine")
    path.unlink()
    try:
        os.symlink(personal, path)
    except OSError:
        pytest.skip("cannot create a symlink here")
    with pytest.raises(PcoError, match="left alone"):
        Manifest.load(tmp_path).target("1", "Chart.pdf", "t2")
    (tmp_path / "New.pdf").symlink_to(tmp_path / "missing.pdf")
    assert Manifest.load(tmp_path).target("2", "New.pdf", "t")[0].name == "New (2).pdf"
