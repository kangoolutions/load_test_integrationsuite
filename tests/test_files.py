import os
from pathlib import Path

import pytest

from cpiload.files import DirCache, PathError, list_folder, preview, scan_files


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in ["a/msg_2.xml", "a/msg_10.xml", "a/msg_1.XML", "a/note.txt", "a/.DS_Store", "a/sub/msg_3.xml", "b/x.json"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


def test_list_root(tree: Path):
    result = list_folder(tree, "")
    assert result["path"] == ""
    assert result["parent"] is None
    assert [d["name"] for d in result["dirs"]] == ["a", "b"]


def test_list_subfolder_counts_files_without_hidden(tree: Path):
    result = list_folder(tree, "a")
    assert result["files"] == 4
    assert result["parent"] == ""
    assert result["dirs"] == [{"name": "sub", "path": "a/sub"}]


def test_scan_natural_sort_and_case_insensitive_pattern(tree: Path):
    assert scan_files(tree, "a", "*.xml") == ["a/msg_1.XML", "a/msg_2.xml", "a/msg_10.xml"]


def test_scan_recursive_and_multiple_patterns(tree: Path):
    assert scan_files(tree, "a", "*.xml, *.txt", recursive=True) == [
        "a/msg_1.XML", "a/msg_2.xml", "a/msg_10.xml", "a/note.txt", "a/sub/msg_3.xml",
    ]


def test_scan_limit_applies_after_sort(tree: Path):
    assert scan_files(tree, "a", "*.xml", limit=2) == ["a/msg_1.XML", "a/msg_2.xml"]


def test_path_traversal_rejected(tree: Path):
    with pytest.raises(PathError):
        list_folder(tree / "a", "../b")


def test_missing_folder(tree: Path):
    with pytest.raises(PathError):
        scan_files(tree, "gibtsnicht")


def test_preview(tree: Path):
    result = preview(tree, "a", "*.xml", False, 2)
    assert result == {"matches": 3, "effective": 2, "sample": ["a/msg_1.XML", "a/msg_2.xml", "a/msg_10.xml"]}


def test_dircache_reuses_listing_until_folder_changes(tree: Path, monkeypatch):
    cache = DirCache(tree)
    calls = []
    real_scandir = os.scandir
    monkeypatch.setattr("cpiload.files.os.scandir", lambda p: calls.append(p) or real_scandir(p))

    assert list_folder(tree, "a", cache)["files"] == 4
    assert scan_files(tree, "a", "*.xml", cache=cache) == ["a/msg_1.XML", "a/msg_2.xml", "a/msg_10.xml"]
    assert preview(tree, "a", "*", False, 0, cache)["matches"] == 4
    assert len(calls) == 1  # ein Scan für Browser, Scan und Vorschau

    (tree / "a" / "msg_11.xml").write_text("x")
    os.utime(tree / "a", ns=(0, (tree / "a").stat().st_mtime_ns + 1_000_000))
    assert list_folder(tree, "a", cache)["files"] == 5
    assert len(calls) == 2

    list_folder(tree, "a", cache, refresh=True)
    assert len(calls) == 3


def test_dircache_recursive_uses_cache_per_subfolder(tree: Path):
    cache = DirCache(tree)
    first = scan_files(tree, "a", "*.xml", recursive=True, cache=cache)
    assert first == scan_files(tree, "a", "*.xml", recursive=True)
    assert first[-1] == "a/sub/msg_3.xml"
