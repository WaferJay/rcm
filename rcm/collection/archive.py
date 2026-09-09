"""Deterministic and ownership-anonymized tar.gz archive writing."""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path

from ..artifacts import sha256_file
from .models import ArchiveResult


class TarGzipArchiveWriter:
    """Write a deterministic-header, level-6 gzip-compressed PAX tar."""

    def write(
        self,
        destination: Path,
        roots: list[tuple[str, Path]],
        *,
        protected_members: tuple[str, ...],
    ) -> ArchiveResult:
        with destination.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=6,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                    dereference=False,
                ) as archive:
                    for relative, source in roots:
                        archive.add(
                            source,
                            arcname=relative,
                            recursive=True,
                            filter=lambda info: _sanitize_member(
                                info,
                                protected_members=protected_members,
                            ),
                        )
        return ArchiveResult(
            bytes=destination.stat().st_size,
            sha256=sha256_file(destination),
        )


def _sanitize_member(
    info: tarfile.TarInfo,
    *,
    protected_members: tuple[str, ...],
) -> tarfile.TarInfo | None:
    name = info.name.removeprefix("./").rstrip("/")
    for protected in protected_members:
        if name == protected or name.startswith(f"{protected}/"):
            return None
    if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
        return None

    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {
        key: value
        for key, value in info.pax_headers.items()
        if key.rsplit(".", 1)[-1].lower() not in {"uid", "gid", "uname", "gname"}
    }
    return info
