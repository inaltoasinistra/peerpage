import asyncio
import json
import logging
import os
import time

from fileutil import CONTENT_DIR, get_tag, iter_sites, list_version_dirs, rmtree
from publisher import Site
from snapshot import is_v1_only, torrent_manifest
from .session import TorrentSession, KEEP_DURATION, MAX_VERSIONS

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0        # seconds
CLEANUP_INTERVAL = 3600.0  # seconds (1 hour)


class Watcher:
    """Drives downloads and seeding based on the state of version directories on disk.

    Each version directory under DATA_DIR/sites/<npub>/<site>/<ver>/ is in one
    of two states:

    - *Complete*: contains a ``site.torrent`` file → seed it.
    - *Incomplete*: contains ``event.json`` but no ``.torrent`` → download it.

    On every poll cycle ``_sync()`` enforces these rules:

    1. Seed every complete version that has not been seeded yet.
    2. Among incomplete versions for a site keep only the highest; delete the
       rest (they are stale from a previous download attempt).
    3. Download the highest incomplete version.  If a newer incomplete version
       appears while a download is in-progress, cancel it and start the new one.
    """

    def __init__(self, sites_dir: str, data_dir: str,
                 session: TorrentSession, config=None) -> None:
        self._sites_dir = sites_dir
        self._data_dir = data_dir
        self._session = session
        self._config = config
        self._seen: set[str] = set()
        self._last_cleanup: float = 0.0
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._task_versions: dict[tuple[str, str], int] = {}

    def _sync(self) -> None:
        """Scan all site version dirs and enforce the seed/download rules."""
        sites_base = os.path.join(self._data_dir, 'sites')
        for npub, site_name, site_dir in iter_sites(sites_base):
            self._sync_site(npub, site_name, site_dir)

    def _sync_site(self, npub: str, site_name: str, site_dir: str) -> None:
        """Enforce seed/download rules for a single site."""
        complete, incomplete = _classify_versions(site_dir)

        # Remove numeric dirs with no recognised marker (orphaned by a crash etc.)
        # Re-check the markers just before deleting: the publisher may have written
        # site.torrent between _classify_versions and this loop (TOCTOU).
        known = set(complete) | set(incomplete)
        for ver in list_version_dirs(site_dir):
            if ver not in known:
                ver_dir = os.path.join(site_dir, str(ver))
                if not os.path.isfile(os.path.join(ver_dir, 'rejected')):
                    if (os.path.isfile(os.path.join(ver_dir, 'site.torrent')) or
                            os.path.isfile(os.path.join(ver_dir, 'event.json'))):
                        continue  # marker appeared since classify; not orphaned
                    logger.info('%s: removing orphaned version dir v%d', site_name, ver)
                    self._session.stop_site(ver_dir)
                    try:
                        rmtree(ver_dir)
                    except OSError as e:
                        logger.warning('failed to remove orphaned dir %s: %s', ver_dir, e)

        sorted_complete = sorted(complete)
        for ver in sorted_complete[-MAX_VERSIONS:]:
            self._seed_if_new(site_dir, ver)
        for ver in sorted_complete[:-MAX_VERSIONS]:
            version_dir = os.path.join(site_dir, str(ver))
            if os.path.isdir(version_dir):
                self._session.stop_site(version_dir)
                logger.info('%s: purging over-cap version %d', site_name, ver)
                try:
                    rmtree(version_dir)
                except OSError as e:
                    logger.warning('failed to purge %s: %s', version_dir, e)

        if not incomplete:
            return

        highest = max(incomplete)
        active_version = self._manage_task(npub, site_name, highest)

        self._delete_stale(site_dir, incomplete, highest, active_version)

        if self._task_running_for(npub, site_name, highest):
            return

        self._start_download(npub, site_name, site_dir, highest, complete)

    def _seed_if_new(self, site_dir: str, ver: int) -> None:
        torrent_path = os.path.join(site_dir, str(ver), 'site.torrent')
        if torrent_path in self._seen:
            return
        ver_dir = os.path.join(site_dir, str(ver))
        if is_v1_only(torrent_path):
            logger.warning('%s v%d: rejecting — v1-only torrent (hybrid v1+v2 or v2 required)',
                           os.path.basename(site_dir), ver)
            self._reject_version(ver_dir, torrent_path, 'v1-only torrent')
            return
        self._seen.add(torrent_path)
        try:
            self._session.seed(torrent_path, ver_dir)
        except Exception as e:
            logger.warning('failed to seed %s: %s — deleting and retrying next cycle', torrent_path, e)
            self._seen.discard(torrent_path)
            try:
                os.unlink(torrent_path)
            except OSError:
                pass

    def _manage_task(self, npub: str, site_name: str, highest: int) -> int | None:
        """Cancel the running task if it is not for *highest*.

        Returns the version the (still-running) task is downloading, or None
        if no task is running or it was just cancelled.
        """
        key = (npub, site_name)
        existing = self._tasks.get(key)
        if not existing or existing.done():
            return None
        active = self._task_versions.get(key)
        if active != highest:
            logger.info('newer version %d available for %s; cancelling v%d download',
                        highest, site_name, active)
            existing.cancel()
            # Return the old version so _delete_stale doesn't race with the task.
            return active
        return active

    @staticmethod
    def _delete_stale(site_dir: str, incomplete: list[int], highest: int,
                      active_version: int | None) -> None:
        """Delete stale lower incomplete dirs, but skip one still being downloaded."""
        for ver in incomplete:
            if ver != highest and ver != active_version:
                stale_dir = os.path.join(site_dir, str(ver))
                logger.info('deleting stale incomplete dir: %s', stale_dir)
                if os.path.isdir(stale_dir):
                    rmtree(stale_dir)

    def _task_running_for(self, npub: str, site_name: str, version: int) -> bool:
        key = (npub, site_name)
        existing = self._tasks.get(key)
        return bool(existing and not existing.done()
                    and self._task_versions.get(key) == version)

    def _start_download(self, npub: str, site_name: str, site_dir: str,
                        highest: int, complete: list[int]) -> None:
        ver_dir = os.path.join(site_dir, str(highest))
        magnet = _read_magnet(ver_dir)
        if magnet is None:
            logger.warning('no magnet tag in event.json for %s/%s v%d — rejecting',
                           npub, site_name, highest)
            self._reject_version(ver_dir, os.path.join(ver_dir, 'site.torrent'),
                                 'no magnet tag in event.json')
            return
        prev_version = max(complete) if complete else None
        key = (npub, site_name)
        task = asyncio.create_task(
            self._download(site_name, magnet, npub, highest, prev_version),
        )
        self._tasks[key] = task
        self._task_versions[key] = highest

    async def _download(self, site_name: str, magnet_uri: str, publisher_npub: str,
                        version: int, prev_version: int | None = None) -> None:
        """Download *magnet_uri* into an already-created version directory."""
        key = (publisher_npub, site_name)
        try:
            site = Site(site_name, sites_dir=self._sites_dir, data_dir=self._data_dir,
                        npub=publisher_npub)
            version_dir = os.path.join(site.data_path, str(version))
            torrent_path = os.path.join(version_dir, 'site.torrent')
            max_site_mb = 0
            if self._config is not None and publisher_npub != self._config.nostr.public_key:
                max_site_mb = self._config.max_site_mb
            try:
                await self._session.download(magnet_uri, version_dir, torrent_path,
                                             max_site_mb=max_site_mb)
            except asyncio.CancelledError:
                logger.info('%s: download cancelled', site_name)
                return
            if is_v1_only(torrent_path):
                logger.warning(
                    '%s: rejecting v%d — v1-only torrent (hybrid v1+v2 or v2 required)',
                    site_name, version,
                )
                self._reject_version(version_dir, torrent_path, 'v1-only torrent')
                return
            if not _site_directory_ok(torrent_path):
                logger.warning(
                    '%s: rejecting v%d — torrent top-level directory is not "site"',
                    site_name, version,
                )
                self._reject_version(version_dir, torrent_path,
                                     'top-level directory is not "site"')
                return
            site.finalize_download(version, prev_version, magnet_uri)
            logger.info('%s: version %d downloaded', site_name, version)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                del self._tasks[key]
                self._task_versions.pop(key, None)

    def _reject_version(self, version_dir: str, torrent_path: str, reason: str) -> None:
        """Permanently reject a version, writing a 'rejected' marker with *reason*.

        Removes the torrent from the session, deletes site.torrent and the
        downloaded site/ content, and writes a 'rejected' marker file so that
        _classify_versions skips this directory on future polls.
        """
        self._session.stop_site(version_dir)
        if os.path.exists(torrent_path):
            os.unlink(torrent_path)
        content_dir = os.path.join(version_dir, 'site')
        if os.path.isdir(content_dir):
            rmtree(content_dir)
        os.makedirs(version_dir, exist_ok=True)
        with open(os.path.join(version_dir, 'rejected'), 'w') as f:
            f.write(reason + '\n')

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup >= min(CLEANUP_INTERVAL, KEEP_DURATION.total_seconds()):
            self._session.cleanup_old_versions()
            self._last_cleanup = now

    async def run(self) -> None:
        while True:
            self._sync()
            self._maybe_cleanup()
            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _next_version(site_data: str) -> int:
    """Return the next available version number (max existing dir + 1)."""
    versions = list_version_dirs(site_data)
    return (max(versions) + 1) if versions else 1


def _classify_versions(site_dir: str) -> tuple[list[int], list[int]]:
    """Return (complete_versions, incomplete_versions) for a site dir.

    Complete: has a ``site.torrent`` file.
    Incomplete: has ``event.json`` but no ``site.torrent``.
    """
    complete: list[int] = []
    incomplete: list[int] = []
    for ver in list_version_dirs(site_dir):
        ver_dir = os.path.join(site_dir, str(ver))
        if os.path.isfile(os.path.join(ver_dir, 'rejected')):
            continue
        if os.path.isfile(os.path.join(ver_dir, 'site.torrent')):
            complete.append(ver)
        elif os.path.isfile(os.path.join(ver_dir, 'event.json')):
            incomplete.append(ver)
    return complete, incomplete


def _site_directory_ok(torrent_path: str) -> bool:
    """Return True iff every file in the torrent lives under a top-level 'site/' directory."""
    try:
        manifest = torrent_manifest(torrent_path)
    except Exception:
        return False
    return all(rel.startswith(CONTENT_DIR + '/') for rel in manifest)


def _read_magnet(ver_dir: str) -> str | None:
    """Read the magnet URI from event.json in *ver_dir*, or return None."""
    try:
        with open(os.path.join(ver_dir, 'event.json')) as f:
            event = json.load(f)
    except Exception as e:
        logger.warning('could not read event.json in %s: %s', ver_dir, e)
        return None
    return get_tag(event, 'magnet')
