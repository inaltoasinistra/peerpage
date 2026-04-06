import libtorrent as lt

from fileutil import atomic_open

_ZERO_ROOT = bytes(32)


def is_v1_only(torrent_path: str) -> bool:
    """Return True if the torrent has no v2 per-file SHA-256 roots.

    A hybrid v1+v2 or pure v2 torrent has a non-zero SHA-256 Merkle root for
    every real (non-pad) file.  A v1-only torrent returns all-zero roots or
    raises ValueError.  Pure v1 torrents are rejected by peerpage because
    per-file SHA-256 roots are required for cross-version deduplication.
    """
    info = lt.torrent_info(torrent_path)
    files = info.files()
    for i in range(files.num_files()):
        try:
            root = bytes.fromhex(str(files.root(i)))
        except ValueError:
            return True  # root() not available → v1-only
        if root != _ZERO_ROOT:
            return False  # found a real v2 root → not v1-only
    return True  # all roots were zero (pad files only, or v1-only)


def torrent_manifest(torrent_path: str) -> dict[str, bytes]:
    """Return {rel_path: sha256_root} for each file in a hybrid v2 torrent."""
    info = lt.torrent_info(torrent_path)
    files = info.files()
    manifest: dict[str, bytes] = {}
    for i in range(files.num_files()):
        rel_path = files.file_path(i).replace('\\', '/')
        manifest[rel_path] = bytes.fromhex(str(files.root(i)))
    return manifest


def diff_manifests(
    previous: dict[str, str], current: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    new_files = sorted(k for k in current if k not in previous)
    modified  = sorted(k for k in current if k in previous and current[k] != previous[k])
    deleted   = sorted(k for k in previous if k not in current)
    return new_files, modified, deleted


def write_changelog(
    path: str, new_files: list[str], modified: list[str], deleted: list[str],
    magnet_uri: str,
) -> None:
    with atomic_open(path) as f:
        f.write(f'magnet: {magnet_uri}\n\n')
        if new_files:
            f.write('new files:\n')
            for name in new_files:
                f.write(f'  {name}\n')
            f.write('\n')
        if modified:
            f.write('modified files:\n')
            for name in modified:
                f.write(f'  {name}\n')
            f.write('\n')
        if deleted:
            f.write('deleted files:\n')
            for name in deleted:
                f.write(f'  {name}\n')
            f.write('\n')
