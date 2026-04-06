import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from publisher import Site
from .session import MAX_VERSIONS
from .watcher import (Watcher, CLEANUP_INTERVAL, _classify_versions,
                      _magnet_is_v1_only, _read_magnet, _site_directory_ok)

FAKE_NPUB = 'npub1testfake'


def touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').close()


def _write_event(ver_dir: str, magnet: str = 'magnet:?xt=urn:btih:abc&xt=urn:btmh:1220eeff') -> None:
    os.makedirs(ver_dir, exist_ok=True)
    event = {'id': None, 'created_at': 1000, 'pubkey': 'remote',
             'tags': [['magnet', magnet]]}
    with open(os.path.join(ver_dir, 'event.json'), 'w') as f:
        json.dump(event, f)


class TestClassifyVersions(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.site_dir = os.path.join(self.tmp.name, FAKE_NPUB, 'site_a')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _torrent(self, ver: int) -> str:
        path = os.path.join(self.site_dir, str(ver), 'site.torrent')
        touch(path)
        return path

    def _incomplete(self, ver: int) -> str:
        ver_dir = os.path.join(self.site_dir, str(ver))
        _write_event(ver_dir)
        return ver_dir

    def test_returns_empty_for_missing_dir(self) -> None:
        complete, incomplete = _classify_versions('/nonexistent')
        self.assertEqual(complete, [])
        self.assertEqual(incomplete, [])

    def test_classifies_complete_version(self) -> None:
        self._torrent(1)
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertIn(1, complete)
        self.assertEqual(incomplete, [])

    def test_classifies_incomplete_version(self) -> None:
        self._incomplete(1)
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertEqual(complete, [])
        self.assertIn(1, incomplete)

    def test_ignores_non_digit_entries(self) -> None:
        touch(os.path.join(self.site_dir, 'not_a_version', 'file.txt'))
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertEqual(complete, [])
        self.assertEqual(incomplete, [])

    def test_ignores_digit_file_not_dir(self) -> None:
        os.makedirs(self.site_dir, exist_ok=True)
        open(os.path.join(self.site_dir, '1'), 'w').close()  # file named '1', not a dir
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertEqual(complete, [])
        self.assertEqual(incomplete, [])

    def test_mixed_versions(self) -> None:
        self._torrent(1)
        self._torrent(2)
        self._incomplete(3)
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertIn(1, complete)
        self.assertIn(2, complete)
        self.assertIn(3, incomplete)

    def test_skips_rejected_version(self) -> None:
        ver_dir = os.path.join(self.site_dir, '1')
        os.makedirs(ver_dir)
        touch(os.path.join(ver_dir, 'rejected'))
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertNotIn(1, complete)
        self.assertNotIn(1, incomplete)

    def test_rejected_does_not_hide_other_versions(self) -> None:
        ver_dir = os.path.join(self.site_dir, '1')
        os.makedirs(ver_dir)
        touch(os.path.join(ver_dir, 'rejected'))
        self._torrent(2)
        complete, incomplete = _classify_versions(self.site_dir)
        self.assertEqual(complete, [2])
        self.assertEqual(incomplete, [])


class TestSiteDirectoryOk(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_false_for_missing_torrent(self) -> None:
        self.assertFalse(_site_directory_ok('/nonexistent/site.torrent'))

    def test_returns_false_for_corrupt_torrent(self) -> None:
        path = os.path.join(self.tmp.name, 'site.torrent')
        with open(path, 'wb') as f:
            f.write(b'not a valid torrent')
        self.assertFalse(_site_directory_ok(path))

    def test_returns_true_when_all_files_under_site(self) -> None:
        path = os.path.join(self.tmp.name, 'site.torrent')
        open(path, 'w').close()
        with patch('daemon.watcher.torrent_manifest',
                   return_value=['site/index.html', 'site/style.css']):
            self.assertTrue(_site_directory_ok(path))

    def test_returns_false_when_file_outside_site_dir(self) -> None:
        path = os.path.join(self.tmp.name, 'site.torrent')
        open(path, 'w').close()
        with patch('daemon.watcher.torrent_manifest',
                   return_value=['index.html', 'site/style.css']):
            self.assertFalse(_site_directory_ok(path))


class TestMagnetIsV1Only(unittest.TestCase):

    def test_returns_true_when_no_btmh(self) -> None:
        """A magnet with only xt=urn:btih: is v1-only."""
        self.assertTrue(_magnet_is_v1_only('magnet:?xt=urn:btih:aabbcc&dn=site'))

    def test_returns_false_for_hybrid_magnet(self) -> None:
        """A hybrid magnet contains both xt=urn:btih: and xt=urn:btmh:."""
        self.assertFalse(_magnet_is_v1_only(
            'magnet:?xt=urn:btih:aabbcc&xt=urn:btmh:1220eeff&dn=site'
        ))

    def test_returns_false_for_v2_only_magnet(self) -> None:
        """A pure v2 magnet with only xt=urn:btmh: is not v1-only."""
        self.assertFalse(_magnet_is_v1_only('magnet:?xt=urn:btmh:1220eeff&dn=site'))


class TestReadMagnet(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_magnet_from_event_json(self) -> None:
        _write_event(self.tmp.name, magnet='magnet:?xt=urn:btih:abc')
        result = _read_magnet(self.tmp.name)
        self.assertEqual(result, 'magnet:?xt=urn:btih:abc')

    def test_returns_none_when_no_magnet_tag(self) -> None:
        event = {'id': None, 'tags': [['d', 'site']]}
        with open(os.path.join(self.tmp.name, 'event.json'), 'w') as f:
            json.dump(event, f)
        self.assertIsNone(_read_magnet(self.tmp.name))

    def test_returns_none_for_missing_file(self) -> None:
        self.assertIsNone(_read_magnet('/nonexistent'))


class TestSyncSiteSeeding(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        patcher = patch('daemon.watcher.is_v1_only', return_value=False)
        self.mock_is_v1_only = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _torrent(self, npub: str, site: str, ver: int) -> str:
        path = os.path.join(self.data_dir, 'sites', npub, site, str(ver), 'site.torrent')
        touch(path)
        return path

    def _site_dir(self, npub: str, site: str) -> str:
        return os.path.join(self.data_dir, 'sites', npub, site)

    def test_seeds_complete_version(self) -> None:
        path = self._torrent(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        session.seed.assert_called_once_with(path, os.path.dirname(path))

    def test_does_not_reseed_already_seeded(self) -> None:
        path = self._torrent(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        session.seed.assert_called_once()

    def test_removes_orphaned_version_dir(self) -> None:
        """A version dir with no marker files (no site.torrent, event.json, rejected) is removed."""
        orphan_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(orphan_dir)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='INFO') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        self.assertFalse(os.path.isdir(orphan_dir))
        session.stop_site.assert_called_once_with(orphan_dir)
        self.assertTrue(any('orphaned' in line for line in log.output))

    def test_does_not_delete_orphan_if_torrent_appears_before_rmtree(self) -> None:
        """TOCTOU: if site.torrent is written between classify and the re-check, keep the dir.

        Simulates the race by patching _classify_versions to return empty lists
        (as if it ran before site.torrent was written), while site.torrent is
        already on disk when the re-check runs.
        """
        orphan_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(orphan_dir)
        # Write site.torrent so the re-check inside _sync_site finds it.
        with open(os.path.join(orphan_dir, 'site.torrent'), 'wb') as f:
            f.write(b'fake')
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        # Patch _classify_versions to simulate the race: it returns empty lists
        # as if it ran before site.torrent was written.
        with patch('daemon.watcher._classify_versions', return_value=([], [])):
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        # The re-check must have found site.torrent and skipped deletion.
        self.assertTrue(os.path.isdir(orphan_dir))
        session.stop_site.assert_not_called()

    def test_removes_orphaned_version_dir_logs_oserror(self) -> None:
        """OSError when removing an orphaned dir is logged and does not raise."""
        orphan_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(orphan_dir)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with patch('daemon.watcher.rmtree', side_effect=OSError('disk error')), \
             self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)  # must not raise
        self.assertTrue(any('disk error' in line for line in log.output))

    def test_purges_over_cap_complete_versions(self) -> None:
        """When there are more than MAX_VERSIONS complete versions, oldest are purged."""
        for ver in range(1, MAX_VERSIONS + 2):
            self._torrent(FAKE_NPUB, 'site_a', ver)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='INFO') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        oldest_dir = os.path.join(site_dir, '1')
        self.assertFalse(os.path.isdir(oldest_dir))
        self.assertTrue(any('purging' in line for line in log.output))

    def test_purge_over_cap_logs_oserror(self) -> None:
        """OSError when purging an over-cap version is logged and does not raise."""
        for ver in range(1, MAX_VERSIONS + 2):
            self._torrent(FAKE_NPUB, 'site_a', ver)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with patch('daemon.watcher.rmtree', side_effect=OSError('no space')), \
             self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)  # must not raise
        self.assertTrue(any('no space' in line for line in log.output))

    def test_seed_failure_does_not_raise(self) -> None:
        self._torrent(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        session.seed.side_effect = RuntimeError('bad torrent')
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)  # must not raise
        self.assertTrue(any('failed to seed' in line for line in log.output))

    def test_seed_failure_unlink_error_is_ignored(self) -> None:
        """OSError when unlinking a broken torrent after seed failure is silently ignored."""
        self._torrent(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        session.seed.side_effect = RuntimeError('bad torrent')
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with patch('daemon.watcher.os.unlink', side_effect=OSError('busy')), \
             self.assertLogs('daemon.watcher', level='WARNING'):
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)  # must not raise

    def test_seed_if_new_rejects_v1_only_torrent(self) -> None:
        """_seed_if_new writes a rejected marker and does not seed a v1-only torrent."""
        path = self._torrent(FAKE_NPUB, 'site_a', 1)
        ver_dir = os.path.dirname(path)
        self.mock_is_v1_only.return_value = True
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        session.seed.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(ver_dir, 'rejected')))
        self.assertTrue(any('v1' in line.lower() for line in log.output))


class TestSyncSiteDownload(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        patcher = patch('daemon.watcher.is_v1_only', return_value=False)
        self.mock_is_v1_only = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _site_dir(self, npub: str, site: str) -> str:
        return os.path.join(self.data_dir, 'sites', npub, site)

    def _torrent(self, npub: str, site: str, ver: int) -> None:
        path = os.path.join(self.data_dir, 'sites', npub, site, str(ver), 'site.torrent')
        touch(path)

    def _incomplete(self, npub: str, site: str, ver: int,
                    magnet: str = 'magnet:?xt=urn:btih:abc&xt=urn:btmh:1220eeff') -> None:
        ver_dir = os.path.join(self.data_dir, 'sites', npub, site, str(ver))
        _write_event(ver_dir, magnet)

    async def test_starts_download_for_highest_incomplete(self) -> None:
        self._incomplete(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        await asyncio.sleep(0)
        watcher._download.assert_called_once()
        args = watcher._download.call_args[0]
        self.assertEqual(args[2], FAKE_NPUB)  # publisher_npub
        self.assertEqual(args[3], 1)          # version

    async def test_does_not_start_duplicate_task(self) -> None:
        self._incomplete(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        # Call twice without yielding: the task is created but not yet run,
        # so existing.done() is False and no duplicate should be started.
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        await asyncio.sleep(0)
        watcher._download.assert_called_once()

    async def test_deletes_stale_lower_incomplete_dirs(self) -> None:
        self._incomplete(FAKE_NPUB, 'site_a', 1)
        self._incomplete(FAKE_NPUB, 'site_a', 2)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='INFO') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        stale_dir = os.path.join(site_dir, '1')
        self.assertFalse(os.path.isdir(stale_dir))
        self.assertTrue(any('stale' in line for line in log.output))

    async def test_cancels_old_task_when_newer_version_appears(self) -> None:
        self._incomplete(FAKE_NPUB, 'site_a', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)

        async def never_finishes(*args: object, **kwargs: object) -> None:
            await asyncio.sleep(100)

        watcher._download = never_finishes  # type: ignore[method-assign]
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')

        # Start download of v1 (task won't finish)
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        await asyncio.sleep(0)
        key = (FAKE_NPUB, 'site_a')
        self.assertEqual(watcher._task_versions[key], 1)

        # v2 appears while v1 is still in-progress
        self._incomplete(FAKE_NPUB, 'site_a', 2, magnet='magnet:?xt=urn:btih:new&xt=urn:btmh:1220aabb')
        with self.assertLogs('daemon.watcher', level='INFO'):
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)

        # Task should now be for v2
        self.assertEqual(watcher._task_versions[key], 2)
        # Clean up the long-running task
        watcher._tasks[key].cancel()
        await asyncio.sleep(0)

    async def test_skips_download_when_event_json_has_no_magnet(self) -> None:
        ver_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(ver_dir)
        with open(os.path.join(ver_dir, 'event.json'), 'w') as f:
            json.dump({'id': None, 'tags': [['d', 'site_a']]}, f)  # no magnet tag
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        watcher._download.assert_not_called()
        self.assertTrue(any('no magnet' in line for line in log.output))

    async def test_skips_download_when_magnet_is_v1_only(self) -> None:
        """A magnet URI with no xt=urn:btmh: is rejected before the download starts."""
        self._incomplete(FAKE_NPUB, 'site_a', 1, magnet='magnet:?xt=urn:btih:aabbcc')
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        ver_dir = os.path.join(site_dir, '1')
        with self.assertLogs('daemon.watcher', level='WARNING') as log:
            watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        watcher._download.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(ver_dir, 'rejected')))
        self.assertTrue(any('v1' in line.lower() for line in log.output))

    async def test_passes_prev_version_from_complete_versions(self) -> None:
        self._torrent(FAKE_NPUB, 'site_a', 1)
        self._incomplete(FAKE_NPUB, 'site_a', 2)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._download = AsyncMock()
        site_dir = self._site_dir(FAKE_NPUB, 'site_a')
        watcher._sync_site(FAKE_NPUB, 'site_a', site_dir)
        await asyncio.sleep(0)
        call_args = watcher._download.call_args[0]
        self.assertEqual(call_args[3], 2)   # version
        self.assertEqual(call_args[4], 1)   # prev_version


class TestDownload(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        patcher = patch('daemon.watcher.is_v1_only', return_value=False)
        self.mock_is_v1_only = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_calls_session_download_and_finalize(self) -> None:
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        with patch('daemon.watcher._site_directory_ok', return_value=True), \
             patch.object(Site, 'finalize_download') as mock_finalize:
            await watcher._download('site_a', magnet, FAKE_NPUB, 1, None)
        session.download.assert_called_once()
        mock_finalize.assert_called_once_with(1, None, magnet)

    async def test_passes_prev_version_to_finalize_download(self) -> None:
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        with patch('daemon.watcher._site_directory_ok', return_value=True), \
             patch.object(Site, 'finalize_download') as mock_finalize:
            await watcher._download('site_a', magnet, FAKE_NPUB, 2, 1)
        mock_finalize.assert_called_once_with(2, 1, magnet)

    async def test_cancelled_download_skips_finalize(self) -> None:
        session = MagicMock()
        session.download = AsyncMock(side_effect=asyncio.CancelledError)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        with patch.object(Site, 'finalize_download') as mock_finalize, \
             self.assertLogs('daemon.watcher', level='INFO') as log:
            await watcher._download('site_a', 'magnet:?x', FAKE_NPUB, 1, None)
        mock_finalize.assert_not_called()
        self.assertTrue(any('cancelled' in line for line in log.output))

    async def test_task_cleared_after_completion(self) -> None:
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        key = (FAKE_NPUB, 'site_a')
        with patch.object(Site, 'finalize_download'):
            task = asyncio.create_task(
                watcher._download('site_a', 'magnet:?x', FAKE_NPUB, 1, None),
            )
            watcher._tasks[key] = task
            watcher._task_versions[key] = 1
            await task
        self.assertNotIn(key, watcher._tasks)
        self.assertNotIn(key, watcher._task_versions)

    async def test_task_cleared_after_cancellation(self) -> None:
        session = MagicMock()
        session.download = AsyncMock(side_effect=asyncio.CancelledError)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        key = (FAKE_NPUB, 'site_a')
        with self.assertLogs('daemon.watcher', level='INFO'):
            task = asyncio.create_task(
                watcher._download('site_a', 'magnet:?x', FAKE_NPUB, 1, None),
            )
            watcher._tasks[key] = task
            watcher._task_versions[key] = 1
            await task
        self.assertNotIn(key, watcher._tasks)
        self.assertNotIn(key, watcher._task_versions)

    async def test_task_not_cleared_when_superseded(self) -> None:
        """If a newer task overwrote _tasks[key], the older task must not clear it."""
        session = MagicMock()
        session.download = AsyncMock(side_effect=asyncio.CancelledError)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        key = (FAKE_NPUB, 'site_a')
        # Simulate newer task already in _tasks
        newer_task = MagicMock(spec=asyncio.Task)
        with self.assertLogs('daemon.watcher', level='INFO'):
            old_task = asyncio.create_task(
                watcher._download('site_a', 'magnet:?x', FAKE_NPUB, 1, None),
            )
            watcher._tasks[key] = newer_task  # overwrite with newer
            watcher._task_versions[key] = 2
            await old_task
        # newer_task must still be in _tasks, version still 2
        self.assertIs(watcher._tasks[key], newer_task)
        self.assertEqual(watcher._task_versions[key], 2)

    async def test_own_site_bypasses_max_site_mb(self) -> None:
        """Sites published by this machine must never be filtered."""
        own_npub = 'npub1ownpub'
        cfg = MagicMock()
        cfg.nostr.public_key = own_npub
        cfg.max_site_mb = 5
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session, config=cfg)
        with patch('daemon.watcher._site_directory_ok', return_value=True), \
             patch.object(Site, 'finalize_download'):
            await watcher._download('site_a', 'magnet:?x', own_npub, 1, None)
        kwargs = session.download.call_args[1]
        self.assertEqual(kwargs.get('max_site_mb'), 0)

    async def test_subscribed_site_uses_max_site_mb(self) -> None:
        """Sites published by others must respect max_site_mb."""
        cfg = MagicMock()
        cfg.nostr.public_key = 'npub1ownpub'
        cfg.max_site_mb = 5
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session, config=cfg)
        with patch('daemon.watcher._site_directory_ok', return_value=True), \
             patch.object(Site, 'finalize_download'):
            await watcher._download('site_a', 'magnet:?x', 'npub1otherpub', 1, None)
        kwargs = session.download.call_args[1]
        self.assertEqual(kwargs.get('max_site_mb'), 5)

    async def test_rejects_torrent_with_wrong_directory(self) -> None:
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        session.stop_site = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        # Create the version dir so _reject_version can write to it
        ver_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(ver_dir)
        with patch('daemon.watcher._site_directory_ok', return_value=False), \
             patch.object(Site, 'finalize_download') as mock_finalize, \
             self.assertLogs('daemon.watcher', level='WARNING') as log:
            await watcher._download('site_a', magnet, FAKE_NPUB, 1, None)
        mock_finalize.assert_not_called()
        session.stop_site.assert_called_once_with(ver_dir)
        self.assertTrue(os.path.exists(os.path.join(ver_dir, 'rejected')))
        self.assertTrue(any('reject' in line.lower() for line in log.output))

    async def test_accepted_torrent_calls_finalize(self) -> None:
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        with patch('daemon.watcher._site_directory_ok', return_value=True), \
             patch.object(Site, 'finalize_download') as mock_finalize:
            await watcher._download('site_a', magnet, FAKE_NPUB, 1, None)
        mock_finalize.assert_called_once_with(1, None, magnet)

    async def test_rejects_v1_only_torrent(self) -> None:
        """_download writes a rejected marker and skips finalize for a v1-only torrent."""
        self.mock_is_v1_only.return_value = True
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        session.stop_site = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        ver_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        os.makedirs(ver_dir)
        with patch.object(Site, 'finalize_download') as mock_finalize, \
             self.assertLogs('daemon.watcher', level='WARNING') as log:
            await watcher._download('site_a', magnet, FAKE_NPUB, 1, None)
        mock_finalize.assert_not_called()
        session.stop_site.assert_called_once_with(ver_dir)
        self.assertTrue(os.path.exists(os.path.join(ver_dir, 'rejected')))
        self.assertTrue(any('v1' in line.lower() for line in log.output))

    async def test_reject_version_removes_content_dir(self) -> None:
        """_reject_version deletes an existing site/ content directory."""
        magnet = 'magnet:?xt=urn:btih:abc'
        session = MagicMock()
        session.download = AsyncMock(return_value=None)
        session.stop_site = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        ver_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'site_a', '1')
        content_dir = os.path.join(ver_dir, 'site')
        os.makedirs(content_dir)
        self.mock_is_v1_only.return_value = True
        with patch.object(Site, 'finalize_download'), \
             self.assertLogs('daemon.watcher', level='WARNING'):
            await watcher._download('site_a', magnet, FAKE_NPUB, 1, None)
        self.assertFalse(os.path.isdir(content_dir))


class TestSync(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        patcher = patch('daemon.watcher.is_v1_only', return_value=False)
        self.mock_is_v1_only = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _torrent(self, npub: str, site: str, ver: int) -> None:
        path = os.path.join(self.data_dir, 'sites', npub, site, str(ver), 'site.torrent')
        touch(path)

    async def test_sync_seeds_torrents_across_multiple_publishers(self) -> None:
        self._torrent(FAKE_NPUB, 'site_a', 1)
        self._torrent('npub1other', 'site_b', 1)
        session = MagicMock()
        watcher = Watcher(self.sites_dir, self.data_dir, session)
        watcher._sync()
        self.assertEqual(session.seed.call_count, 2)

    def test_sync_returns_when_data_dir_missing(self) -> None:
        session = MagicMock()
        watcher = Watcher(self.sites_dir, '/nonexistent', session)
        watcher._sync()  # must not raise
        session.seed.assert_not_called()

    def test_non_dir_entry_in_sites_base_is_skipped(self) -> None:
        sites_base = os.path.join(self.data_dir, 'sites')
        os.makedirs(sites_base)
        open(os.path.join(sites_base, 'not_a_dir'), 'w').close()
        watcher = Watcher(self.sites_dir, self.data_dir, MagicMock())
        watcher._sync()  # must not raise

    def test_non_dir_entry_in_npub_dir_is_skipped(self) -> None:
        npub_dir = os.path.join(self.data_dir, 'sites', FAKE_NPUB)
        os.makedirs(npub_dir)
        open(os.path.join(npub_dir, 'not_a_dir'), 'w').close()
        watcher = Watcher(self.sites_dir, self.data_dir, MagicMock())
        watcher._sync()  # must not raise


class TestMaybeCleanup(unittest.TestCase):

    def test_cleanup_runs_when_interval_elapsed(self) -> None:
        session = MagicMock()
        watcher = Watcher('/s', '/d', session)
        watcher._last_cleanup = 0.0
        with patch('daemon.watcher.time.monotonic', return_value=CLEANUP_INTERVAL + 1):
            watcher._maybe_cleanup()
        session.cleanup_old_versions.assert_called_once()

    def test_cleanup_skipped_when_interval_not_elapsed(self) -> None:
        session = MagicMock()
        watcher = Watcher('/s', '/d', session)
        watcher._last_cleanup = 0.0
        with patch('daemon.watcher.time.monotonic', return_value=1.0):
            watcher._maybe_cleanup()
        session.cleanup_old_versions.assert_not_called()


class TestRun(unittest.IsolatedAsyncioTestCase):

    async def test_run_calls_sync_and_cleanup_each_iteration(self) -> None:
        watcher = Watcher('/nonexistent', '/nonexistent', MagicMock())
        watcher._sync = MagicMock()
        watcher._maybe_cleanup = MagicMock()
        with patch('asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await watcher.run()
        watcher._sync.assert_called_once()
        watcher._maybe_cleanup.assert_called_once()


if __name__ == '__main__':
    unittest.main()
