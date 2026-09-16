#!/usr/bin/env python3
"""Public release artifact audit for Vestigraph CI.

This script is intentionally small: it validates the already-public checkout and
built distributions without carrying the private staging/synchronization system.
"""
from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TEXT_SUFFIXES = {
    ".bat", ".c", ".cfg", ".cpp", ".css", ".h", ".hpp", ".html",
    ".ini", ".js", ".json", ".lym", ".md", ".ps1", ".py", ".rs",
    ".sh", ".svg", ".toml", ".txt", ".xml", ".yml", ".yaml",
}
TEXT_NAMES = {"LICENSE", "NOTICE", "PKG-INFO", "METADATA", "RECORD", "WHEEL", "entry_points.txt", "top_level.txt"}
MAX_TEXT_AUDIT_BYTES = 5_000_000
WINDOWS_ABS_RE = re.compile(r"(?i)(?:\b[a-z]:[\\/]|\\\\(?:[^\\/\s]+)\\(?:[^\\/\s]+))")
MACHINE_UNIX_ABS_RE = re.compile(r"(?<![\w/])/(?:Users|home|var/folders|tmp)/[^\s'\"<>]+")
ISO_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")
MONTH_DATE_RE = re.compile(r"(?<!\d)20\d\d-(?:0[1-9]|1[0-2])(?![-\d])")
COMPACT_DATE_RE = re.compile(r"(?<!\d)20\d{6}(?!\d)")

PRIVATE_PATHS = {
    "tests/test_release_tools.py",
    "tools/stage_public.py",
    "tools/release_sync.py",
}
PRIVATE_PREFIXES = ("release/",)
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"[ERROR] {loc} - {self.message}"


def is_text_path(path: str) -> bool:
    posix = PurePosixPath(path)
    return posix.suffix.lower() in TEXT_SUFFIXES or posix.name in TEXT_NAMES


def is_private_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized in PRIVATE_PATHS or normalized.startswith(PRIVATE_PREFIXES)


def scan_text(text: str, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if WINDOWS_ABS_RE.search(line) or MACHINE_UNIX_ABS_RE.search(line):
            findings.append(Finding(rel, lineno, "hardcoded machine absolute path"))
        if ISO_DATE_RE.search(line) or MONTH_DATE_RE.search(line) or COMPACT_DATE_RE.search(line):
            findings.append(Finding(rel, lineno, "debug or calendar date leaked"))
    return findings


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        parts = set(PurePosixPath(rel).parts)
        if parts & EXCLUDED_PARTS:
            continue
        if is_private_path(rel):
            findings.append(Finding(rel, 0, "private release control file shipped"))
            continue
        if path.is_symlink():
            findings.append(Finding(rel, 0, "symlink shipped"))
            continue
        if path.is_file() and (path.suffix in {".pyc", ".pyo"} or path.name.endswith(".egg-info")):
            findings.append(Finding(rel, 0, "junk build artifact shipped"))
        if path.is_file() and is_text_path(rel):
            if path.stat().st_size > MAX_TEXT_AUDIT_BYTES:
                findings.append(Finding(rel, 0, "text file too large for audit"))
                continue
            try:
                findings.extend(scan_text(path.read_text(encoding="utf-8"), rel))
            except UnicodeDecodeError:
                findings.append(Finding(rel, 0, "text file is not UTF-8"))
    return findings


def archive_members(path: Path):
    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                yield info.filename, zf.read(info), False
    elif path.suffixes[-2:] == [".tar", ".gz"] or path.suffix == ".tgz":
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    yield member.name, b"", True
                elif member.isfile():
                    handle = tf.extractfile(member)
                    yield member.name, handle.read() if handle else b"", False


def scan_dist(dist: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not dist.exists():
        return findings
    for artifact in sorted(dist.iterdir()):
        if not (artifact.suffix in {".whl", ".zip", ".tgz"} or artifact.suffixes[-2:] == [".tar", ".gz"]):
            continue
        for name, payload, is_link in archive_members(artifact):
            rel = f"{artifact.name}!/{name}"
            archive_name = name.replace("\\", "/")
            posix = PurePosixPath(archive_name)
            if "\\" in name or WINDOWS_ABS_RE.search(name) or posix.is_absolute() or ".." in posix.parts:
                findings.append(Finding(rel, 0, "unsafe archive member path"))
            if is_link:
                findings.append(Finding(rel, 0, "archive link member shipped"))
                continue
            parts = set(posix.parts)
            if parts & EXCLUDED_PARTS or name.endswith((".pyc", ".pyo")):
                findings.append(Finding(rel, 0, "junk build artifact in archive"))
            candidate_paths = [archive_name]
            if len(posix.parts) > 1:
                candidate_paths.append("/".join(posix.parts[1:]))
            if any(is_private_path(candidate) for candidate in candidate_paths):
                findings.append(Finding(rel, 0, "private release control file in archive"))
            if is_text_path(name):
                if len(payload) > MAX_TEXT_AUDIT_BYTES:
                    findings.append(Finding(rel, 0, "text archive member too large for audit"))
                    continue
                try:
                    findings.extend(scan_text(payload.decode("utf-8"), rel))
                except UnicodeDecodeError:
                    findings.append(Finding(rel, 0, "text archive member is not UTF-8"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit public Vestigraph checkout and distributions.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = args.root.resolve()
    dist = args.dist_dir if args.dist_dir.is_absolute() else root / args.dist_dir
    findings = scan_tree(root) + scan_dist(dist)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} ERROR")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
