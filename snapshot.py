import libtorrent as lt

from fileutil import atomic_open


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
