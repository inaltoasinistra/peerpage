import asyncio
import datetime
import json
import logging
import os
import time

import libtorrent as lt

from fileutil import (CONTENT_DIR, atomic_open, initial_priorities,
                      list_version_dirs, rmtree as _rmtree)
from snapshot import torrent_manifest

logger = logging.getLogger(__name__)

# File priority states stored in file_priorities.json at the site level.
# The UI and download logic use these to distinguish automatic from manual.
AUTO_SKIP = 0  # excluded by budget algorithm
AUTO_PICK = 1  # included by budget algorithm
SKIP = 2       # manually excluded by user
PICK = 3       # manually included by user


def _is_pad_file(path: str) -> bool:
    """Return True for libtorrent pad files (/.pad/ directories).

    Pad files are used for piece-boundary alignment and cannot be selected
    by users — libtorrent silently resets their priority to 0 regardless of
    what is requested.
    """
    return '/.pad/' in path


def _site_dir(torrent_path: str) -> str:
    """Return the site directory (grandparent of torrent_path).

    torrent_path: DATA_DIR/sites/<npub>/<site>/<ver>/site.torrent
    site_dir:     DATA_DIR/sites/<npub>/<site>
    """
    return os.path.dirname(os.path.dirname(torrent_path))


def _load_file_priorities(site_dir: str) -> dict[str, int]:
    """Load file_priorities.json from site_dir.  Returns {} if not found."""
    path = os.path.join(site_dir, 'file_priorities.json')
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _save_file_priorities(site_dir: str, prios: dict[str, int]) -> None:
    """Save file_priorities.json to site_dir atomically."""
    path = os.path.join(site_dir, 'file_priorities.json')
    os.makedirs(site_dir, exist_ok=True)
    with atomic_open(path) as f:
        json.dump(prios, f, indent=2)


def _compute_new_version_priorities(new_info: lt.torrent_info, site_dir: str,
                                     max_site_mb: int) -> list[int]:
    """Compute file priorities for a new version, respecting manual overrides.

    Manual states (SKIP/PICK) from file_priorities.json are preserved.
    All other files are re-evaluated by the budget algorithm against the full
    max_site_mb budget.  Manual picks are in addition to the budget (they do
    not reduce the space available for automatic selection).

    Updates file_priorities.json with the new states and returns a libtorrent
    priority list (0 = skip, 1 = download).
    """
    fs = new_info.files()
    num = fs.num_files()
    json_prios = _load_file_priorities(site_dir)

    all_files = [
        {'index': i, 'path': fs.file_path(i).replace('\\', '/'), 'size': fs.file_size(i)}
        for i in range(num)
    ]
    real_files = [f for f in all_files if not _is_pad_file(f['path'])]

    # Run the budget algorithm on real files only (pad files are excluded
    # because libtorrent ignores priorities set on them).
    # initial_priorities requires contiguous 0-based indices, so we re-index
    # real_files and then expand the result back to the full lt index space.
    lt_prios = [0] * num
    if max_site_mb > 0:
        compact = [{'index': j, 'path': f['path'], 'size': f['size']}
                   for j, f in enumerate(real_files)]
        compact_prios = initial_priorities(compact, max_site_mb * 1024 * 1024)
        for j, f in enumerate(real_files):
            lt_prios[f['index']] = compact_prios[j]
    else:
        for f in real_files:
            lt_prios[f['index']] = 1  # no budget: download everything

    auto_lt_prios = list(lt_prios)  # snapshot before manual overrides

    # Apply manual overrides on top of the automatic selection.
    new_json: dict[str, int] = {}
    for f in real_files:
        path = f['path']
        state = json_prios.get(path)
        if state == SKIP:
            lt_prios[f['index']] = 0
            new_json[path] = SKIP
        elif state == PICK:
            lt_prios[f['index']] = 1
            new_json[path] = PICK
        else:
            # Auto file: record what the budget algorithm decided.
            new_json[path] = AUTO_PICK if auto_lt_prios[f['index']] == 1 else AUTO_SKIP

    _save_file_priorities(site_dir, new_json)
    return lt_prios


def _prepopulate(new_info: lt.torrent_info, site_dir: str,
                 new_version_dir: str,
                 only_indices: set[int] | None = None) -> int:
    """Hard-link files from any previous version dir where the SHA-256 Merkle root matches.

    Searches all version directories inside site_dir that have a complete
    site.torrent, newest first.  This handles the case where a file was
    excluded in an immediately preceding version but still exists in an older
    one (the user previously had it selected and now wants it again).

    only_indices: if given, only attempt to restore files whose lt index is in
    this set.  Used when re-selecting previously deselected files so that
    priority-0 files are not accidentally restored.

    Returns the number of files hard-linked.
    libtorrent will verify these on startup and skip downloading them.
    """
    new_files = new_info.files()

    # Collect files we need to find in previous versions, keyed by SHA-256 Merkle root.
    _ZERO_ROOT = bytes(32)
    needed: dict[str, bytes] = {}
    for i in range(new_files.num_files()):
        if only_indices is not None and i not in only_indices:
            continue
        try:
            root = bytes.fromhex(str(new_files.root(i)))
        except ValueError:
            continue  # malformed root — skip
        if root != _ZERO_ROOT:  # skip pad files (zero root)
            rel_path = new_files.file_path(i).replace('\\', '/')
            needed[rel_path] = root

    if not needed:
        return 0

    # Collect previous complete version dirs, newest first.
    # The current version dir has no site.torrent yet (download in progress),
    # so it is naturally excluded by the isfile() check below.
    prev_version_dirs: list[str] = []
    for ver in sorted(list_version_dirs(site_dir), reverse=True):
        ver_dir = os.path.join(site_dir, str(ver))
        if os.path.isfile(os.path.join(ver_dir, 'site.torrent')):
            prev_version_dirs.append(ver_dir)

    count = 0
    remaining = dict(needed)

    for prev_dir in prev_version_dirs:
        if not remaining:
            break
        torrent_file = os.path.join(prev_dir, 'site.torrent')
        try:
            prev_roots = torrent_manifest(torrent_file)
        except Exception:
            continue

        for rel_path, expected_root in list(remaining.items()):
            if prev_roots.get(rel_path) != expected_root:
                continue
            src = os.path.join(prev_dir, rel_path)
            if not os.path.exists(src):
                continue
            dst = os.path.join(new_version_dir, rel_path)
            try:
                dst_ino = os.stat(dst).st_ino
            except FileNotFoundError:
                dst_ino = None
            if dst_ino == os.stat(src).st_ino:
                del remaining[rel_path]
                continue  # already the same inode
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if dst_ino is not None:
                os.unlink(dst)  # remove libtorrent's zero-filled placeholder
            os.link(src, dst)
            count += 1
            del remaining[rel_path]

    return count


ALERT_POLL_INTERVAL = 1.0  # seconds
KEEP_DURATION = datetime.timedelta(
    seconds=int(os.environ.get('PEERPAGE_KEEP_SECONDS', 24 * 3600))
)
MAX_VERSIONS = 5
# last_upload values before this date indicate "never uploaded" (libtorrent sentinel)
_UPLOADED_THRESHOLD = datetime.datetime(2000, 1, 1)


class TorrentSession:

    def __init__(self) -> None:
        self._session = lt.session({
            'alert_mask': lt.alert.category_t.status_notification
                        | lt.alert.category_t.error_notification
                        | lt.alert.category_t.tracker_notification,
        })
        self._handles: dict[str, lt.torrent_handle] = {}
        self._pending_resume: dict[lt.torrent_handle, str] = {}
        self._pending_remove: set[lt.torrent_handle] = set()

    def seed(self, torrent_path: str, save_path: str) -> None:
        """Add a completed torrent to the session for seeding.

        If the content directory is present, resume data (or seed_mode) is
        used so libtorrent trusts the existing files.  If the content
        directory is missing, the torrent is added without any optimistic
        flags so libtorrent re-downloads it from scratch.
        """
        if torrent_path in self._handles:
            return
        content_dir = os.path.join(save_path, CONTENT_DIR)
        if os.path.isdir(content_dir):
            resume_path = torrent_path.replace('.torrent', '.resume')
            if os.path.isfile(resume_path):
                with open(resume_path, 'rb') as f:
                    params = lt.read_resume_data(f.read())
            else:
                params = lt.add_torrent_params()
                params.flags |= lt.torrent_flags.seed_mode
            logger.info('seeding %s', torrent_path)
        else:
            params = lt.add_torrent_params()
            logger.warning('content missing for %s, re-downloading', torrent_path)
        params.ti = lt.torrent_info(torrent_path)
        params.save_path = save_path
        self._handles[torrent_path] = self._session.add_torrent(params)

    async def download(self, magnet_uri: str, version_dir: str, torrent_path: str,
                       max_site_mb: int = 0) -> None:
        params = lt.parse_magnet_uri(magnet_uri)
        params.save_path = version_dir
        handle = self._session.add_torrent(params)
        self._handles[torrent_path] = handle  # register so stop_site() can cancel
        logger.info('downloading to %s', version_dir)

        try:
            while not handle.has_metadata():
                await asyncio.sleep(0.5)
                self._process_alerts()
                if not handle.is_valid():
                    logger.info('download cancelled: %s', version_dir)
                    raise asyncio.CancelledError

            # Metadata received.  Pause briefly, apply file priorities and
            # pre-populate hard-links from previous versions, force-recheck so
            # libtorrent recognises the hard-linked files, then resume.
            handle.pause()
            for _ in range(20):  # up to 10 s for the pause to take effect
                if handle.status().paused:
                    break
                await asyncio.sleep(0.5)
                self._process_alerts()
                if not handle.is_valid():
                    logger.info('download cancelled: %s', version_dir)
                    raise asyncio.CancelledError

            info = handle.torrent_file()
            site_dir = _site_dir(torrent_path)
            if info is not None and max_site_mb > 0:
                prios = _compute_new_version_priorities(info, site_dir, max_site_mb)
                if any(p == 0 for p in prios):
                    handle.prioritize_files(prios)
                    logger.info('file selection: %d/%d files selected',
                                sum(p > 0 for p in prios), len(prios))
            if info is not None:
                n = _prepopulate(info, site_dir, version_dir)
                if n > 0:
                    logger.info('pre-populated %d file(s) from previous versions', n)
                    handle.force_recheck()
            handle.resume()

            while True:
                if handle.is_seed():
                    break
                if str(handle.status().state).split('.')[-1] == 'finished':
                    break
                await asyncio.sleep(1)
                self._process_alerts()
                if not handle.is_valid():
                    logger.info('download cancelled: %s', version_dir)
                    raise asyncio.CancelledError
            info = handle.torrent_file()
            with atomic_open(torrent_path, 'wb') as f:
                f.write(lt.bencode({b'info': lt.bdecode(info.info_section())}))
            # v1/v2 hybrid torrents: pieces can span file boundaries, so
            # libtorrent may write stub data for priority-0 files.  Delete
            # any such stubs so they don't appear as "changed" in comparisons.
            prios = handle.get_file_priorities()
            if any(p == 0 for p in prios):
                fs = info.files()
                for i, prio in enumerate(prios):
                    if prio == 0 and i < fs.num_files():
                        fp = os.path.join(version_dir,
                                          fs.file_path(i).replace('\\', '/'))
                        if os.path.isfile(fp):
                            try:
                                os.unlink(fp)
                                logger.info('removed priority-0 stub: %s', fp)
                            except OSError as e:
                                logger.warning('could not remove stub %s: %s',
                                               fp, e)
            logger.info('download complete: %s', version_dir)
        except BaseException:
            self._handles.pop(torrent_path, None)
            if handle.is_valid():
                handle.pause()
                # Libtorrent 2.0.11 races between piece_picker::completed_hash_job
                # (network thread) and mmap_storage::release_files (triggered by
                # remove_torrent).  We must not call remove_torrent() until the
                # torrent is fully paused; torrent_paused_alert signals that the
                # network thread has processed the pause and will dispatch no more
                # disk jobs for this torrent.  _handle_alert() performs the actual
                # removal when that alert arrives.
                self._pending_remove.add(handle)
            raise

    def _handle_resume_alert(self, alert: lt.alert,
                             pending: dict[str, lt.torrent_handle]) -> None:
        if isinstance(alert, lt.save_resume_data_alert):
            for torrent_path, handle in list(pending.items()):
                if alert.handle == handle:
                    resume_path = torrent_path.replace('.torrent', '.resume')
                    with atomic_open(resume_path, 'wb') as f:
                        f.write(lt.write_resume_data_buf(alert.params))
                    logger.info('resume data saved: %s', resume_path)
                    del pending[torrent_path]
                    break
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            logger.warning('[%s] could not save resume data', alert.torrent_name)
            for torrent_path, handle in list(pending.items()):
                if alert.handle == handle:
                    del pending[torrent_path]
                    break
        else:
            self._handle_alert(alert)

    async def shutdown(self) -> None:
        """Save resume data for all torrents then pause the session."""
        pending = {path: handle for path, handle in self._handles.items()
                   if handle.is_valid()}
        for handle in pending.values():
            handle.save_resume_data()
        deadline = time.monotonic() + max(5.0, len(pending) * 1.0)
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            for alert in self._session.pop_alerts():
                self._handle_resume_alert(alert, pending)
        self._session.pause()
        self._session = None  # block until libtorrent's internal threads finish
        logger.info('shutdown complete')

    @staticmethod
    def _disk_stats(version_dir: str) -> tuple[int, int]:
        """Return (total_bytes, exclusive_bytes) for all files in version_dir.

        total_bytes:     sum of all file sizes (actual disk usage).
        exclusive_bytes: sum of files not hard-linked to any other version
                         (nlink == 1); represents bytes unique to this version.
        """
        total = 0
        exclusive = 0
        for root, _dirs, files in os.walk(version_dir):
            for name in files:
                st = os.stat(os.path.join(root, name))
                total += st.st_size
                if st.st_nlink == 1:
                    exclusive += st.st_size
        return total, exclusive

    def _site_info(self, torrent_path: str, handle: lt.torrent_handle) -> dict:
        s = handle.status()
        version_dir = os.path.dirname(torrent_path)
        version = int(os.path.basename(version_dir))
        site_dir = os.path.dirname(version_dir)
        site_name = os.path.basename(site_dir)
        npub = os.path.basename(os.path.dirname(site_dir))
        identifier = f'{site_name}.{npub[-5:]}'
        url_identifier = f'{site_name}.{npub}'
        info = handle.torrent_file()
        if info is not None:
            fs = info.files()
            site_total_bytes = sum(
                fs.file_size(i) for i in range(fs.num_files())
                if not _is_pad_file(fs.file_path(i).replace('\\', '/'))
            )
        else:
            site_total_bytes = 0
        if os.path.isdir(version_dir):
            disk_bytes, exclusive_bytes = self._disk_stats(version_dir)
        else:
            disk_bytes, exclusive_bytes = 0, 0
        return {
            'identifier': identifier,
            'url_identifier': url_identifier,
            'version': version,
            'state': str(s.state).split('.')[-1],
            'upload_rate': s.upload_rate,
            'download_rate': s.download_rate,
            'total_upload': s.total_upload,
            'disk_bytes': disk_bytes,
            'exclusive_bytes': exclusive_bytes,
            'site_total_bytes': site_total_bytes,
            'num_peers': s.num_peers,
        }

    def file_list(self, torrent_path: str) -> tuple[list[dict], int] | None:
        """Return (files, total_file_count) for a torrent.

        files: [{index, path, size, priority, state}] — pad files excluded.
        total_file_count: full libtorrent file count including pad files (needed
          by the UI to build a correctly-indexed priorities array).

        priority: 0 (skip) or 1 (download) — the libtorrent priority.
        state: 0–3 (AUTO_SKIP/AUTO_PICK/SKIP/PICK) from file_priorities.json.

        Returns None if the torrent is not loaded or has no metadata yet.
        """
        handle = self._handles.get(torrent_path)
        if handle is None or not handle.is_valid():
            return None
        info = handle.torrent_file()
        if info is None:
            return None
        fs = info.files()
        total = fs.num_files()
        prios = handle.get_file_priorities()
        site_dir = _site_dir(torrent_path)
        json_prios = _load_file_priorities(site_dir)
        result = []
        for i in range(total):
            path = fs.file_path(i).replace('\\', '/')
            if _is_pad_file(path):
                continue
            prio = prios[i] if i < len(prios) else 1
            state = json_prios.get(path, AUTO_PICK if prio > 0 else AUTO_SKIP)
            result.append({'index': i, 'path': path, 'size': fs.file_size(i),
                           'priority': prio, 'state': state})
        return result, total

    def set_file_priorities(self, torrent_path: str, priorities: list[int]) -> bool:
        """Set per-file download priorities for a torrent.

        Detects which files changed vs their current state in file_priorities.json
        and updates that file accordingly:
          - A file whose effective priority changed to 1 → PICK (manual include)
          - A file whose effective priority changed to 0 → SKIP (manual exclude)
          - Files with no effective change keep their existing AUTO_SKIP/AUTO_PICK/SKIP/PICK state.

        Files set to priority 0 are deleted from disk if they exist.
        Returns False if the torrent handle is not found or invalid.
        """
        handle = self._handles.get(torrent_path)
        if handle is None or not handle.is_valid():
            return False
        info = handle.torrent_file()
        if info is not None:
            fs = info.files()
            site_dir = _site_dir(torrent_path)
            json_prios = _load_file_priorities(site_dir)
            # Start from existing JSON but drop any stale pad file entries.
            new_json = {k: v for k, v in json_prios.items() if not _is_pad_file(k)}
            for i, new_lt_prio in enumerate(priorities):
                if i >= fs.num_files():
                    break
                path = fs.file_path(i).replace('\\', '/')
                if _is_pad_file(path):
                    continue
                current_state = json_prios.get(path, AUTO_PICK)
                # Effective libtorrent priority from the stored state.
                current_lt = 1 if current_state in (AUTO_PICK, PICK) else 0
                if new_lt_prio != current_lt:
                    new_json[path] = PICK if new_lt_prio else SKIP
            _save_file_priorities(site_dir, new_json)

            save_path = handle.status().save_path
            for i, prio in enumerate(priorities):
                if prio == 0 and i < fs.num_files():
                    file_path = os.path.join(save_path, fs.file_path(i).replace('\\', '/'))
                    if os.path.isfile(file_path):
                        try:
                            os.unlink(file_path)
                            logger.info('deleted priority-0 file: %s', file_path)
                        except OSError as e:
                            logger.warning('could not delete %s: %s', file_path, e)

            # Restore priority-1 files that are missing from disk by hard-linking
            # from a previous version.  This handles the deselect-then-reselect
            # case: the file was deleted above (or in a previous call), so
            # libtorrent would otherwise try to download it from peers.
            version_dir = os.path.dirname(torrent_path)
            missing_selected = {
                i for i, prio in enumerate(priorities)
                if prio > 0 and i < fs.num_files()
                and not _is_pad_file(fs.file_path(i).replace('\\', '/'))
                and not os.path.isfile(
                    os.path.join(save_path, fs.file_path(i).replace('\\', '/'))
                )
            }
            if missing_selected:
                n = _prepopulate(info, site_dir, version_dir,
                                 only_indices=missing_selected)
                if n > 0:
                    handle.force_recheck()
                    logger.info('restored %d file(s) from previous versions', n)
        handle.prioritize_files(priorities)
        resume_path = torrent_path.replace('.torrent', '.resume')
        self._pending_resume[handle] = resume_path
        handle.save_resume_data()
        return True

    def reset_file_priorities(self, torrent_path: str, max_site_mb: int = 0) -> bool:
        """Reset all file priorities to automatic (re-run budget algorithm).

        Removes all manual SKIP/PICK states, re-computes priorities using the
        budget algorithm, updates file_priorities.json, and applies the result
        to libtorrent.  Files that become excluded are deleted from disk.
        Returns False if the torrent handle is not found, invalid, or has no
        metadata.
        """
        handle = self._handles.get(torrent_path)
        if handle is None or not handle.is_valid():
            return False
        info = handle.torrent_file()
        if info is None:
            return False
        fs = info.files()
        num = fs.num_files()
        all_files = [
            {'index': i, 'path': fs.file_path(i).replace('\\', '/'), 'size': fs.file_size(i)}
            for i in range(num)
        ]
        real_files = [f for f in all_files if not _is_pad_file(f['path'])]
        prios = [0] * num
        if max_site_mb > 0:
            compact = [{'index': j, 'path': f['path'], 'size': f['size']}
                       for j, f in enumerate(real_files)]
            compact_prios = initial_priorities(compact, max_site_mb * 1024 * 1024)
            for j, f in enumerate(real_files):
                prios[f['index']] = compact_prios[j]
        else:
            for f in real_files:
                prios[f['index']] = 1
        new_json = {
            f['path']: (AUTO_PICK if prios[f['index']] == 1 else AUTO_SKIP)
            for f in real_files
        }
        site_dir = _site_dir(torrent_path)
        _save_file_priorities(site_dir, new_json)

        save_path = handle.status().save_path
        for f in all_files:
            if prios[f['index']] == 0:
                file_path = os.path.join(save_path, f['path'])
                if os.path.isfile(file_path):
                    try:
                        os.unlink(file_path)
                        logger.info('deleted excluded file after reset: %s', file_path)
                    except OSError as e:
                        logger.warning('could not delete %s: %s', file_path, e)
        # Restore priority-1 files that are missing from disk by hard-linking
        # from a previous version.  After a reset the budget may include files
        # that were previously excluded and deleted; restore them from an older
        # version rather than downloading from peers.
        version_dir = os.path.dirname(torrent_path)
        missing_selected = {
            f['index'] for f in real_files
            if prios[f['index']] > 0
            and not os.path.isfile(os.path.join(save_path, f['path']))
        }
        if missing_selected:
            n = _prepopulate(info, site_dir, version_dir,
                             only_indices=missing_selected)
            if n > 0:
                handle.force_recheck()
                logger.info('restored %d file(s) from previous versions', n)

        handle.prioritize_files(prios)
        resume_path = torrent_path.replace('.torrent', '.resume')
        self._pending_resume[handle] = resume_path
        handle.save_resume_data()
        logger.info('file priorities reset for %s: %d/%d files selected',
                    torrent_path, sum(p > 0 for p in prios), num)
        return True

    def sites_info(self) -> list[dict]:
        result = []
        for torrent_path, handle in list(self._handles.items()):
            if not handle.is_valid():
                continue
            try:
                result.append(self._site_info(torrent_path, handle))
            except Exception as e:
                logger.warning('skipping %s: %s', torrent_path, e)
        result.sort(key=lambda s: (s['url_identifier'], s['version']))
        return result

    def stats(self) -> dict:
        sites = self.sites_info()
        return {
            'upload_rate': sum(s['upload_rate'] for s in sites),
            'download_rate': sum(s['download_rate'] for s in sites),
            'num_sites': len(sites),
        }

    def _group_by_site(self) -> dict[str, list[tuple[int, str]]]:
        by_site: dict[str, list[tuple[int, str]]] = {}
        for torrent_path in list(self._handles):
            version_dir = os.path.dirname(torrent_path)
            ver_name = os.path.basename(version_dir)
            if not ver_name.isdigit():
                continue
            site_dir = os.path.dirname(version_dir)
            by_site.setdefault(site_dir, []).append((int(ver_name), torrent_path))
        return by_site

    @staticmethod
    def _version_age(torrent_path: str, handle: lt.torrent_handle,
                     now: datetime.datetime) -> datetime.timedelta:
        last_upload = handle.status().last_upload
        if last_upload is not None and last_upload > _UPLOADED_THRESHOLD:
            return now - last_upload
        try:
            mtime = os.path.getmtime(torrent_path)
        except OSError:
            return datetime.timedelta.max
        return now - datetime.datetime.fromtimestamp(mtime)

    def stop_site(self, site_data_dir: str) -> int:
        """Remove all torrents whose torrent file lives under site_data_dir.

        Returns the number of torrents removed.
        """
        count = 0
        prefix = site_data_dir + os.sep
        for torrent_path in list(self._handles):
            if torrent_path.startswith(prefix):
                handle = self._handles.pop(torrent_path)
                if handle.is_valid():
                    self._session.remove_torrent(handle)
                count += 1
        return count

    def cleanup_old_versions(self) -> None:
        """Remove old versions of each site.

        The latest version per site is always kept. Old versions are only
        removed once the latest version is fully seeding — never while it is
        still downloading or checking.

        An old version is removed if either:
        - it exceeds the MAX_VERSIONS cap (oldest first, regardless of age), or
        - it has had no uploads for longer than KEEP_DURATION.

        For age measurement: last_upload is used if the torrent has ever been
        uploaded, otherwise the torrent file mtime.
        """
        now = datetime.datetime.now()
        for versions in self._group_by_site().values():
            if len(versions) <= 1:
                continue
            latest = max(v for v, _ in versions)
            latest_path = next(tp for v, tp in versions if v == latest)
            latest_handle = self._handles[latest_path]
            if not latest_handle.is_valid() or not latest_handle.is_seed():
                continue
            # Sort old versions ascending so we can identify the excess oldest ones.
            old_versions = sorted(
                [(v, tp) for v, tp in versions if v != latest],
                key=lambda x: x[0],
            )
            excess = max(0, len(old_versions) - (MAX_VERSIONS - 1))
            for i, (_, torrent_path) in enumerate(old_versions):
                handle = self._handles[torrent_path]
                if not handle.is_valid():
                    continue
                over_cap = i < excess
                if over_cap or self._version_age(torrent_path, handle, now) > KEEP_DURATION:
                    self._remove_version(torrent_path, handle)

    def _remove_version(self, torrent_path: str, handle: lt.torrent_handle) -> None:
        self._session.remove_torrent(handle)
        del self._handles[torrent_path]
        version_dir = os.path.dirname(torrent_path)
        if os.path.isdir(version_dir):
            _rmtree(version_dir)
        logger.info('removed old version: %s', torrent_path)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(ALERT_POLL_INTERVAL)
            self._process_alerts()

    def _process_alerts(self) -> None:
        for alert in self._session.pop_alerts():
            self._handle_alert(alert)

    def _handle_alert(self, alert: lt.alert) -> None:
        if isinstance(alert, lt.torrent_paused_alert):
            if alert.handle in self._pending_remove:
                self._pending_remove.discard(alert.handle)
                if alert.handle.is_valid():
                    self._session.remove_torrent(alert.handle)
        elif isinstance(alert, lt.save_resume_data_alert):
            resume_path = self._pending_resume.pop(alert.handle, None)
            if resume_path is not None:
                with atomic_open(resume_path, 'wb') as f:
                    f.write(lt.write_resume_data_buf(alert.params))
                logger.info('resume data saved: %s', resume_path)
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            self._pending_resume.pop(alert.handle, None)
            logger.warning('[%s] could not save resume data', alert.torrent_name)
        elif isinstance(alert, lt.torrent_error_alert):
            logger.error('[%s] %s', alert.torrent_name, alert.message())
        elif isinstance(alert, lt.torrent_finished_alert):
            try:
                sp = alert.handle.save_path()
                ver = os.path.basename(sp)
                site = os.path.basename(os.path.dirname(sp))
                logger.info('%s v%s: ready to seed', site, ver)
            except Exception:
                logger.info('[%s] ready to seed', alert.torrent_name)
        else:
            logger.debug('%s', alert.message())
