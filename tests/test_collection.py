"""Tests for safe command artifact collection."""

from __future__ import annotations

import os
import pickle
import tarfile
from pathlib import Path

import pytest

import rcm.collection as collection_module
from rcm.collection import TarCollector, _expand_pattern, _sanitize_member
from rcm.config import CollectPathSpec, CollectSpec


def _spec(*paths: CollectPathSpec) -> CollectSpec:
    return CollectSpec(paths=paths)


def test_collection_package_preserves_legacy_api() -> None:
    assert collection_module.TarCollector is collection_module.TarGzipCollector
    assert isinstance(
        collection_module.DEFAULT_COLLECTOR,
        collection_module.TarGzipCollector,
    )
    assert collection_module._expand_pattern is _expand_pattern
    assert collection_module._sanitize_member is _sanitize_member

    exported_types = (
        "ArchiveResult",
        "ArchiveWriter",
        "ArtifactCollector",
        "ChangeDetector",
        "CollectionOutcome",
        "CollectionPreparation",
        "EntitySnapshot",
        "MemberMetadata",
        "MemberSnapshot",
        "MetadataSha256ChangeDetector",
        "PathMatcher",
        "PosixGlobMatcher",
        "RulePreparation",
        "SnapshotError",
        "TarGzipArchiveWriter",
        "TarGzipCollector",
    )
    assert set(exported_types) <= set(collection_module.__all__)
    assert all(
        getattr(collection_module, name).__module__ == "rcm.collection"
        for name in exported_types
    )
    assert _expand_pattern.__module__ == "rcm.collection"
    assert _sanitize_member.__module__ == "rcm.collection"
    outcome = collection_module.CollectionOutcome(published=False)
    assert pickle.loads(pickle.dumps(outcome)) == outcome


def test_leading_recursive_glob_does_not_create_a_dot_archive_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "artifact.txt").write_text("x", encoding="utf-8")

    matches = [
        path.relative_to(tmp_path).as_posix()
        for path in _expand_pattern(tmp_path, "**")
    ]

    assert matches == ["output", "output/artifact.txt"]


@pytest.mark.asyncio
async def test_tar_collector_anonymizes_ownership_and_preserves_tree(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "报告.txt").write_text("result", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (output / "outside-link").symlink_to(outside)
    destination = tmp_path / "collect.tar.gz"

    outcome = await TarCollector().collect(
        _spec(CollectPathSpec("output")),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
    )

    assert outcome.published is True
    assert destination.read_bytes().startswith(b"\x1f\x8b")
    with tarfile.open(destination, "r:gz") as archive:
        members = archive.getmembers()
        assert {member.name for member in members} == {
            "output",
            "output/outside-link",
            "output/报告.txt",
        }
        member_file = archive.extractfile("output/报告.txt")
        assert member_file is not None
        assert member_file.read() == b"result"
        assert archive.getmember("output/outside-link").issym()
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.uname == "" and member.gname == "" for member in members)
        assert all(
            not ({"uid", "gid", "uname", "gname"} & set(member.pax_headers))
            for member in members
        )


def test_tar_member_filter_removes_vendor_ownership_headers() -> None:
    info = tarfile.TarInfo("artifact")
    info.uid = 501
    info.gid = 20
    info.uname = "developer"
    info.gname = "staff"
    info.pax_headers = {
        "uid": "501",
        "SCHILY.gid": "20",
        "vendor.uname": "developer",
        "path": "artifact",
    }

    sanitized = _sanitize_member(info, protected_members=())

    assert sanitized is not None
    assert (sanitized.uid, sanitized.gid) == (0, 0)
    assert (sanitized.uname, sanitized.gname) == ("", "")
    assert sanitized.pax_headers == {"path": "artifact"}


@pytest.mark.asyncio
async def test_optional_missing_path_warns_and_publishes_remaining_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "present.txt").write_text("present", encoding="utf-8")
    destination = tmp_path / "collect.tar.gz"

    outcome = await TarCollector().collect(
        _spec(
            CollectPathSpec("missing.txt"),
            CollectPathSpec("present.txt"),
        ),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
    )

    assert outcome.published is True
    assert outcome.warnings[0]["code"] == "collect_path_missing"
    assert outcome.warnings[0]["required"] is False
    with tarfile.open(destination, "r:gz") as archive:
        assert archive.getnames() == ["present.txt"]


@pytest.mark.asyncio
async def test_required_missing_path_prevents_partial_archive(tmp_path: Path) -> None:
    (tmp_path / "present.txt").write_text("present", encoding="utf-8")
    destination = tmp_path / "collect.tar.gz"

    outcome = await TarCollector().collect(
        _spec(
            CollectPathSpec("missing.txt", required=True),
            CollectPathSpec("present.txt"),
        ),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
    )

    assert outcome.published is False
    assert not destination.exists()
    assert outcome.warnings[0]["required"] is True


@pytest.mark.asyncio
async def test_collector_excludes_nested_protected_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "artifact.txt").write_text("artifact", encoding="utf-8")
    protected = project / "commands.yaml"
    protected.write_text("secret", encoding="utf-8")
    destination = tmp_path / "collect.tar.gz"

    outcome = await TarCollector().collect(
        _spec(CollectPathSpec("project")),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(protected,),
    )

    assert outcome.published is True
    with tarfile.open(destination, "r:gz") as archive:
        assert "project/artifact.txt" in archive.getnames()
        assert "project/commands.yaml" not in archive.getnames()


@pytest.mark.asyncio
async def test_configured_symlink_cannot_escape_working_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)

    outcome = await TarCollector().collect(
        _spec(CollectPathSpec("escape", required=True)),
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
    )

    assert outcome.published is False
    assert outcome.warnings[0]["code"] == "collect_path_unsafe"


@pytest.mark.asyncio
async def test_archive_failure_is_non_fatal_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "artifact.txt").write_text("artifact", encoding="utf-8")

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile, "open", fail_open)
    destination = tmp_path / "collect.tar.gz"
    outcome = await TarCollector().collect(
        _spec(CollectPathSpec("artifact.txt")),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
    )

    assert outcome.published is False
    assert outcome.warnings[-1]["code"] == "collect_archive_failed"
    assert not destination.exists()
    assert not (tmp_path / ".collect.tar.gz.tmp").exists()


@pytest.mark.asyncio
async def test_glob_matches_files_and_directories_but_not_implicit_hidden_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "base" / "sub").mkdir(parents=True)
    (tmp_path / "base" / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "base" / "sub" / "two.txt").write_text("two", encoding="utf-8")
    (tmp_path / "base" / ".hidden.txt").write_text("hidden", encoding="utf-8")

    destination = tmp_path / "collect.tar.gz"
    outcome = await TarCollector().collect(
        _spec(CollectPathSpec("base/**/*")),
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
    )

    assert outcome.published is True
    with tarfile.open(destination, "r:gz") as archive:
        names = set(archive.getnames())
    assert names == {
        "base/one.txt",
        "base/sub",
        "base/sub/two.txt",
    }


@pytest.mark.asyncio
async def test_changed_file_uses_content_fingerprint_when_metadata_is_unchanged(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("old", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("artifact.txt"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())

    before = artifact.stat()
    artifact.write_text("new", encoding="utf-8")
    os.chmod(artifact, before.st_mode)
    os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))
    destination = tmp_path / "collect.tar.gz"
    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is True
    with tarfile.open(destination, "r:gz") as archive:
        member = archive.extractfile("artifact.txt")
        assert member is not None
        assert member.read() == b"new"


@pytest.mark.asyncio
async def test_new_file_is_collected_in_changed_mode(tmp_path: Path) -> None:
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("new.txt"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")

    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is True


@pytest.mark.asyncio
async def test_changed_mode_detects_a_new_symlink_target(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("same", encoding="utf-8")
    second.write_text("same", encoding="utf-8")
    link = tmp_path / "latest.txt"
    link.symlink_to(first.name)
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("latest.txt"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())
    link.unlink()
    link.symlink_to(second.name)

    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is True


@pytest.mark.asyncio
async def test_unchanged_path_is_omitted_in_changed_mode(tmp_path: Path) -> None:
    (tmp_path / "artifact.txt").write_text("same", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("artifact.txt"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())

    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is False
    assert outcome.warnings == ()


@pytest.mark.asyncio
async def test_unchanged_required_path_is_not_an_availability_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "artifact.txt").write_text("same", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("artifact.txt", required=True),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())

    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is False
    assert outcome.warnings == ()


@pytest.mark.asyncio
async def test_changed_directory_is_archived_as_a_complete_unit(tmp_path: Path) -> None:
    directory = tmp_path / "output"
    directory.mkdir()
    (directory / "changed.txt").write_text("before", encoding="utf-8")
    (directory / "unchanged.txt").write_text("same", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(paths=(CollectPathSpec("output"),), mode="changed")
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())

    (directory / "changed.txt").write_text("after", encoding="utf-8")
    destination = tmp_path / "collect.tar.gz"
    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is True
    with tarfile.open(destination, "r:gz") as archive:
        assert set(archive.getnames()) == {
            "output",
            "output/changed.txt",
            "output/unchanged.txt",
        }


@pytest.mark.asyncio
async def test_glob_matched_changed_directory_includes_its_unchanged_members(
    tmp_path: Path,
) -> None:
    subdirectory = tmp_path / "base" / "sub"
    subdirectory.mkdir(parents=True)
    (tmp_path / "base" / "top.txt").write_text("top", encoding="utf-8")
    (subdirectory / "changed.txt").write_text("before", encoding="utf-8")
    (subdirectory / "unchanged.txt").write_text("same", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("base/**/*"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())

    (subdirectory / "changed.txt").write_text("after", encoding="utf-8")
    destination = tmp_path / "collect.tar.gz"
    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=destination,
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is True
    with tarfile.open(destination, "r:gz") as archive:
        assert set(archive.getnames()) == {
            "base/sub",
            "base/sub/changed.txt",
            "base/sub/unchanged.txt",
        }


@pytest.mark.asyncio
async def test_deleted_changed_path_warns_without_publishing_empty_archive(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("before", encoding="utf-8")
    collector = TarCollector()
    spec = CollectSpec(
        paths=(CollectPathSpec("artifact.txt"),),
        mode="changed",
    )
    prepared = await collector.prepare(spec, cwd=tmp_path, protected_paths=())
    artifact.unlink()

    outcome = await collector.collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        prepared=prepared,
    )

    assert outcome.published is False
    assert {warning["code"] for warning in outcome.warnings} == {
        "collect_path_deleted",
        "collect_path_missing",
    }


@pytest.mark.asyncio
async def test_on_exit_always_collects_after_nonzero_exit(tmp_path: Path) -> None:
    (tmp_path / "failure.log").write_text("diagnostic", encoding="utf-8")
    spec = CollectSpec(
        paths=(CollectPathSpec("failure.log"),),
        on_exit="always",
    )

    outcome = await TarCollector().collect(
        spec,
        cwd=tmp_path,
        destination=tmp_path / "collect.tar.gz",
        protected_paths=(),
        returncode=2,
    )

    assert outcome.published is True
