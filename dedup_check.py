#!/usr/bin/env python3
"""Compare two version directories and report deduplication opportunities.

For every file present in both directories with identical content
(same SHA-256 Merkle root from the .torrent, or SHA-1 fallback):

  LINKED   – same inode, already deduplicated.
  MISSED   – different inodes; one or both have nlink=1 (wasted disk).

Usage:
    python3 dedup_check.py <dir_a> <dir_b>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snapshot import torrent_manifest
from publisher import Site

_ZERO_ROOT = bytes(32)


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _torrent_path(version_dir: str) -> str | None:
    """Return site.torrent inside version_dir, or None."""
    p = os.path.join(version_dir, 'site.torrent')
    return p if os.path.isfile(p) else None


def _manifest(version_dir: str) -> dict[str, bytes]:
    """Return {rel_path: hash_bytes} for all real (non-pad) files.

    Prefers the SHA-256 Merkle roots stored in the .torrent (no I/O per
    file).  Falls back to SHA-1 content hashes when no .torrent exists.
    """
    torrent = _torrent_path(version_dir)
    if torrent:
        return {
            path: root
            for path, root in torrent_manifest(torrent).items()
            if root != _ZERO_ROOT
        }
    # Fallback: walk the directory and compute SHA-1
    return {
        path: bytes.fromhex(sha1)
        for path, sha1 in Site.file_manifest(version_dir).items()
    }


# ---------------------------------------------------------------------------
# Per-file stat helpers
# ---------------------------------------------------------------------------

def _stat(version_dir: str, rel_path: str):
    try:
        return os.stat(os.path.join(version_dir, rel_path))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------

def compare(dir_a: str, dir_b: str) -> int:
    """Compare dir_a and dir_b.  Returns the number of missed hard-links."""
    print(f'A: {dir_a}')
    print(f'B: {dir_b}')
    print()

    manifest_a = _manifest(dir_a)
    manifest_b = _manifest(dir_b)

    linked: list[str] = []
    missed: list[tuple[str, int, int, int]] = []  # (rel_path, nlink_a, nlink_b, size)

    for rel_path, hash_a in manifest_a.items():
        if manifest_b.get(rel_path) != hash_a:
            continue  # absent or different content — not a dedup candidate

        st_a = _stat(dir_a, rel_path)
        st_b = _stat(dir_b, rel_path)
        if st_a is None or st_b is None:
            continue  # file missing on disk (e.g. incomplete download)

        if st_a.st_ino == st_b.st_ino:
            linked.append(rel_path)
        else:
            missed.append((rel_path, st_a.st_nlink, st_b.st_nlink, st_a.st_size))

    print(f'Equal files already hard-linked : {len(linked):>5}')
    print(f'Equal files NOT hard-linked     : {len(missed):>5}  (dedup opportunities)')
    print()

    if not missed:
        return 0

    wasted = sum(size for _, _, _, size in missed)
    col = 52
    print(f'{"FILE":<{col}}  {"NL_A":>4}  {"NL_B":>4}  {"BYTES":>12}')
    print('-' * (col + 26))
    for rel_path, nl_a, nl_b, size in sorted(missed):
        print(f'{rel_path:<{col}}  {nl_a:>4}  {nl_b:>4}  {size:>12,}')
    print('-' * (col + 26))
    print(f'{"Total wasted bytes:":<{col}}  {"":>4}  {"":>4}  {wasted:>12,}')

    return len(missed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('dir_a', help='First version directory')
    parser.add_argument('dir_b', help='Second version directory')
    args = parser.parse_args()
    missed = compare(args.dir_a, args.dir_b)
    sys.exit(0 if missed == 0 else 1)


if __name__ == '__main__':
    main()
