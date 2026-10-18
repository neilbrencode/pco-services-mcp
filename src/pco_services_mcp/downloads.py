"""Where attachment files land, and how a re-download is recognised.

Nothing here takes a path from the model. The root comes from the user's
configuration; folder and file names come from Planning Center and are
sanitised, then the resolved path must still sit under the root (resolving
follows symlinks and junctions, so a link out of the root is caught).

Each plan folder keeps `.pco-attachments.json`, mapping attachment id to the
file written for it and the attachment's `updated_at`. That record, not file
size, decides whether a file is current, and only files it lists are ever
replaced.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pco_services_mcp.client import PcoError

ENV_ROOT = "PCO_DOWNLOAD_DIR"
DEFAULT_ROOT = Path.home() / "Documents" / "Planning Center"
MANIFEST = ".pco-attachments.json"
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def root() -> Path:
    return Path(os.environ.get(ENV_ROOT) or DEFAULT_ROOT).expanduser().resolve()


def safe_name(name: str, *, limit: int = 120) -> str:
    name = _UNSAFE.sub("_", name).strip().rstrip(". ")
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem and ext.isalnum() and len(ext) <= 10):
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    stem = stem[: max(1, limit - len(ext) - 1)].rstrip(". ") or "file"
    return f"{stem}.{ext}" if ext else stem


def confined(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts).resolve()
    if not path.is_relative_to(base):
        raise PcoError("Refused to write outside the download folder.")
    return path


def _left_alone(name: str) -> PcoError:
    return PcoError(f"{name} already exists and wasn't downloaded by this tool; left alone.")


@dataclass
class Manifest:
    folder: Path
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Path) -> Manifest:
        try:
            entries = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            entries = {}
        return cls(folder, entries if isinstance(entries, dict) else {})

    def target(self, att_id: str, filename: str, updated_at: str | None) -> tuple[Path, bool]:
        """The path for this attachment, and whether the file there is already current."""
        entry = self.entries.get(att_id)
        if entry and entry["filename"].lower() != MANIFEST:
            path = self._own(entry["filename"])
            return path, entry.get("updated_at") == updated_at and path.is_file()
        # The manifest's own name is never an attachment's, or saving one would replace the other.
        taken = {e["filename"].lower() for e in self.entries.values()} | {MANIFEST}
        name = safe_name(filename)
        # lexists: a link counts as taken even when what it points at is missing.
        if name.lower() in taken or os.path.lexists(self.folder / name):
            stem, dot, ext = name.rpartition(".")
            name = f"{stem} ({att_id}).{ext}" if dot else f"{name} ({att_id})"
            if name.lower() in taken or os.path.lexists(self.folder / name):
                raise _left_alone(name)
        return self._own(name), False

    def _own(self, name: str) -> Path:
        # A file swapped for a link would redirect the write onto whatever the link points at.
        if (self.folder / name).is_symlink():
            raise _left_alone(name)
        return confined(self.folder, name)

    def record(self, att_id: str, path: Path, updated_at: str | None, size: int) -> None:
        self.entries[att_id] = {"filename": path.name, "updated_at": updated_at, "size": size}
        fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=self.folder)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(self.entries, fh, indent=1)
        Path(tmp).replace(self.folder / MANIFEST)
