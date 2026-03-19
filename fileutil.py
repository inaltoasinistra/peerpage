import contextlib
import os
import shutil
import tempfile
from collections.abc import Generator, Iterator
from typing import IO


CONTENT_DIR = 'site'


def rmtree(path: str) -> None:
    """Remove *path* recursively, making read-only entries writable as needed."""
    def _onerror(func, p, excinfo):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def last_complete_version(site_data: str) -> int | None:
    """Return the highest version whose site.torrent and site/ directory both exist."""
    versions = [
        v for v in list_version_dirs(site_data)
        if os.path.isfile(os.path.join(site_data, str(v), 'site.torrent'))
        and os.path.isdir(os.path.join(site_data, str(v), CONTENT_DIR))
    ]
    return max(versions) if versions else None


def list_version_dirs(path: str) -> list[int]:
    """Return unsorted list of integer version directory numbers under *path*."""
    if not os.path.isdir(path):
        return []
    return [
        int(e) for e in os.listdir(path)
        if e.isdigit() and os.path.isdir(os.path.join(path, e))
    ]


def iter_sites(sites_base: str) -> Iterator[tuple[str, str, str]]:
    """Yield (npub, site_name, site_dir) for every site directory under sites_base."""
    if not os.path.isdir(sites_base):
        return
    for npub in os.listdir(sites_base):
        npub_dir = os.path.join(sites_base, npub)
        if not os.path.isdir(npub_dir):
            continue
        for site_name in os.listdir(npub_dir):
            site_dir = os.path.join(npub_dir, site_name)
            if not os.path.isdir(site_dir):
                continue
            yield npub, site_name, site_dir


def get_tag(event: dict, name: str, default: str | None = None) -> str | None:
    """Return the first value of tag *name* in a Nostr event dict, or *default*."""
    return next(
        (t[1] for t in event.get('tags', [])
         if isinstance(t, list) and len(t) >= 2 and t[0] == name),
        default,
    )


def initial_priorities(files: list[dict], budget_bytes: int) -> list[int]:
    """Compute initial file priorities using a greedy recursive budget algorithm.

    At each directory level, all direct files are treated as a single atomic
    group: either all are selected or all are skipped.  Subdirectories are
    treated as individual items.  Direct-file groups and subdirectories are
    sorted by total size ascending; items that fit the remaining budget are
    selected in full.  Directories that exceed the budget are recursed into,
    applying the same rule to their contents.  This continues recursively
    until the budget is exhausted or all files have been considered.

    files: list of {index, path, size}  (same format as TorrentSession.file_list())
    budget_bytes: maximum total bytes to select

    Returns a list of priorities (0=skip, 1=download), indexed by file.index.
    """
    if not files or budget_bytes <= 0:
        return [0] * len(files)

    # Strip the leading 'site/' prefix that all peerpage torrents use.
    strip = 'site/' if all(f['path'].startswith('site/') for f in files) else ''

    priorities: list[int] = [0] * len(files)

    def _children(fs: list[dict], prefix_len: int) -> list[dict]:
        """Partition *fs* into direct-file items and sub-directory items."""
        dirs: dict[str, dict] = {}
        items: list[dict] = []
        for f in fs:
            rel = f['path'][prefix_len:]
            sep = rel.find('/')
            if sep == -1:
                items.append({'kind': 'file', 'size': f['size'], 'index': f['index']})
            else:
                name = rel[:sep]
                if name not in dirs:
                    dirs[name] = {
                        'kind': 'dir', 'size': 0, 'files': [],
                        'prefix_len': prefix_len + len(name) + 1,
                    }
                dirs[name]['files'].append(f)
                dirs[name]['size'] += f['size']
        items.extend(dirs.values())
        return items

    def _fill(fs: list[dict], prefix_len: int, budget: int) -> int:
        children = _children(fs, prefix_len)
        direct_files = [c for c in children if c['kind'] == 'file']
        subdirs = [c for c in children if c['kind'] == 'dir']
        items: list[dict] = subdirs[:]
        if direct_files:
            group_size = sum(f['size'] for f in direct_files)
            items.append({'kind': 'file_group', 'size': group_size, 'files': direct_files})
        for item in sorted(items, key=lambda x: x['size']):
            if item['size'] <= budget:
                for f in item['files']:
                    priorities[f['index']] = 1
                budget -= item['size']
            elif item['kind'] == 'dir':
                budget = _fill(item['files'], item['prefix_len'], budget)
            # file_group that exceeds budget: all remain 0
        return budget

    _fill(files, len(strip), budget_bytes)
    return priorities


@contextlib.contextmanager
def atomic_open(path: str, mode: str = 'w', **kwargs):
    """Context manager: write to a temp file then atomically replace *path* on success.

    The temp file is created in the same directory as *path* so that
    os.replace() is guaranteed to be atomic (same filesystem).
    On exception the temp file is cleaned up and *path* is left untouched.
    """
    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    try:
        with os.fdopen(fd, mode, **kwargs) as f:
            yield f
        os.replace(tmp_path, path)
    except:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
