import asyncio
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import libtorrent as lt

from .session import (TorrentSession, KEEP_DURATION, MAX_VERSIONS,
                      _UPLOADED_THRESHOLD, _prepopulate,
                      AUTO_SKIP, AUTO_PICK, SKIP, PICK,
                      _load_file_priorities, _save_file_priorities,
                      _compute_new_version_priorities, _site_dir)
from publisher import Site

FAKE_NPUB = 'npub1testfake'


def make_torrent(tmp: str) -> tuple[str, str]:
    """Create a minimal site and return (torrent_path, version_dir)."""
    sites_dir = os.path.join(tmp, 'sites')
    data_dir = os.path.join(tmp, 'data')
    os.makedirs(os.path.join(sites_dir, 'site'))
    with open(os.path.join(sites_dir, 'site', 'file.txt'), 'w') as f:
        f.write('hello')
    t = Site('site', sites_dir=sites_dir, data_dir=data_dir, npub=FAKE_NPUB)
    t.create()
    return t.torrent_path, os.path.dirname(t.torrent_path)


class TestSeed(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.torrent_path, self.save_path = make_torrent(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seed_adds_handle(self) -> None:
        session = TorrentSession()
        session.seed(self.torrent_path, self.save_path)
        self.assertIn(self.torrent_path, session._handles)

    def test_seed_is_idempotent(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        session.seed(self.torrent_path, self.save_path)
        session.seed(self.torrent_path, self.save_path)
        session._session.add_torrent.assert_called_once()

    def test_seed_logs_warning_when_content_missing(self) -> None:
        """When content dir is absent, a warning is logged and no seed_mode is set."""
        content_dir = os.path.join(self.save_path, 'site')
        shutil.rmtree(content_dir)  # remove the content directory
        session = TorrentSession()
        session._session = MagicMock()
        with self.assertLogs('daemon.session', level='WARNING') as log:
            session.seed(self.torrent_path, self.save_path)
        self.assertTrue(any('content missing' in line for line in log.output))
        params = session._session.add_torrent.call_args[0][0]
        self.assertFalse(params.flags & lt.torrent_flags.seed_mode)

    def test_seed_uses_seed_mode_when_content_exists_without_resume(self) -> None:
        """seed_mode flag must be set when content dir exists and no resume file is present."""
        session = TorrentSession()
        session._session = MagicMock()
        session.seed(self.torrent_path, self.save_path)
        params = session._session.add_torrent.call_args[0][0]
        self.assertTrue(params.flags & lt.torrent_flags.seed_mode)

    def test_seed_uses_resume_data_when_available(self) -> None:
        resume_path = self.torrent_path.replace('.torrent', '.resume')
        with open(resume_path, 'wb') as f:
            f.write(b'resume')
        mock_params = MagicMock()
        session = TorrentSession()
        session._session = MagicMock()
        with patch('libtorrent.read_resume_data', return_value=mock_params):
            session.seed(self.torrent_path, self.save_path)
        self.assertEqual(mock_params.save_path, self.save_path)
        session._session.add_torrent.assert_called_once_with(mock_params)


class TestStopSite(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.torrent_path, self.save_path = make_torrent(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stop_site_removes_handle(self) -> None:
        session = TorrentSession()
        session.seed(self.torrent_path, self.save_path)
        site_data_dir = os.path.dirname(os.path.dirname(self.torrent_path))
        count = session.stop_site(site_data_dir)
        self.assertEqual(count, 1)
        self.assertNotIn(self.torrent_path, session._handles)

    def test_stop_site_returns_zero_for_unknown_dir(self) -> None:
        session = TorrentSession()
        session.seed(self.torrent_path, self.save_path)
        count = session.stop_site('/nonexistent/path')
        self.assertEqual(count, 0)
        self.assertIn(self.torrent_path, session._handles)

    def test_stop_site_only_removes_matching_dir(self) -> None:
        session = TorrentSession()
        session.seed(self.torrent_path, self.save_path)
        other_dir = os.path.join(self.tmp.name, 'other')
        count = session.stop_site(other_dir)
        self.assertEqual(count, 0)
        self.assertIn(self.torrent_path, session._handles)


class TestHandleAlert(unittest.TestCase):

    def setUp(self) -> None:
        self.session = TorrentSession()

    def test_error_alert_logs_at_error_level(self) -> None:
        alert = MagicMock(spec=lt.torrent_error_alert)
        alert.torrent_name = 'test'
        alert.message.return_value = 'something went wrong'
        with self.assertLogs('daemon.session', level='ERROR') as log:
            self.session._handle_alert(alert)
        self.assertTrue(any('something went wrong' in line for line in log.output))

    def test_finished_alert_logs_at_info_level(self) -> None:
        alert = MagicMock(spec=lt.torrent_finished_alert)
        alert.torrent_name = 'test'
        with self.assertLogs('daemon.session', level='INFO') as log:
            self.session._handle_alert(alert)
        self.assertTrue(any('ready to seed' in line for line in log.output))

    def test_other_alert_does_not_raise(self) -> None:
        alert = MagicMock(spec=lt.alert)
        alert.message.return_value = 'some info'
        self.session._handle_alert(alert)  # should not raise


class TestRun(unittest.IsolatedAsyncioTestCase):

    async def test_run_polls_alerts_until_cancelled(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        call_count = 0

        def fake_process() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        session._process_alerts = fake_process
        with self.assertRaises(asyncio.CancelledError):
            await session.run()
        self.assertGreaterEqual(call_count, 2)


class TestProcessAlerts(unittest.TestCase):

    def test_process_alerts_dispatches_each_alert(self) -> None:
        session = TorrentSession()
        alerts = [MagicMock(spec=lt.alert), MagicMock(spec=lt.alert)]
        for a in alerts:
            a.message.return_value = 'ok'
        session._session = MagicMock()
        session._session.pop_alerts.return_value = alerts
        handled = []
        session._handle_alert = lambda a: handled.append(a)
        session._process_alerts()
        self.assertEqual(handled, alerts)


class TestDownload(unittest.IsolatedAsyncioTestCase):

    async def test_download_waits_until_seeding(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_seed.side_effect = [False, False, True]
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake_info_section'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertTrue(any('download complete' in line for line in log.output))
        self.assertEqual(handle.is_seed.call_count, 3)

    async def test_download_completes_on_finished_state(self) -> None:
        """finished state (partial seed, some files skipped) must also end the loop."""
        session = TorrentSession()
        handle = MagicMock()
        handle.is_seed.return_value = False
        mock_status_downloading = MagicMock()
        mock_status_downloading.state.__str__ = lambda s: 'torrent_status.states.downloading'
        mock_status_downloading.paused = True
        mock_status_finished = MagicMock()
        mock_status_finished.state.__str__ = lambda s: 'torrent_status.states.finished'
        mock_status_finished.paused = True
        handle.status.side_effect = [
            mock_status_downloading,  # pause check
            mock_status_downloading,  # first loop iteration state check
            mock_status_finished,     # second iteration → finished → break
        ]
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertTrue(any('download complete' in line for line in log.output))

    async def test_download_force_rechecks_when_prepopulated(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_seed.return_value = True
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '2')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock), \
                 patch('daemon.session._prepopulate', return_value=2):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)
        handle.force_recheck.assert_called_once()

    async def test_download_removes_priority_zero_stubs(self) -> None:
        """Priority-0 files written as v1 piece-boundary stubs must be deleted."""
        session = TorrentSession()
        handle = MagicMock()
        handle.is_seed.return_value = True
        handle.get_file_priorities.return_value = [1, 0]

        mock_fs = MagicMock()
        mock_fs.num_files.return_value = 2
        mock_fs.file_path.side_effect = lambda i: ['site/index.html', 'site/big.mp4'][i]
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        mock_info.files.return_value = mock_fs
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(os.path.join(version_dir, 'site'), exist_ok=True)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            stub = os.path.join(version_dir, 'site', 'big.mp4')
            with open(stub, 'wb') as f:
                f.write(b'\x00' * 1024)
            with open(os.path.join(version_dir, 'site', 'index.html'), 'w') as f:
                f.write('<h1>hi</h1>')

            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertFalse(os.path.exists(stub))
        self.assertTrue(any('priority-0 stub' in line for line in log.output))

    async def test_download_registers_handle_immediately(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_seed.return_value = True
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertIn(torrent_path, session._handles)
        self.assertIs(session._handles[torrent_path], handle)

    async def test_download_raises_cancelled_during_metadata_wait(self) -> None:
        """Cancellation is detected in the metadata-wait loop, not only after."""
        session = TorrentSession()
        handle = MagicMock()
        # has_metadata() returns False then True — but is_valid() returns False
        # on the first poll, so we expect CancelledError before metadata arrives.
        handle.has_metadata.side_effect = [False, False]
        handle.is_valid.side_effect = [True, False, False]

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    with self.assertRaises(asyncio.CancelledError):
                        await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertTrue(any('cancelled' in line for line in log.output))

    async def test_download_raises_cancelled_when_handle_removed(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_valid.side_effect = [True, False, False]
        handle.is_seed.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    with self.assertRaises(asyncio.CancelledError):
                        await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        self.assertTrue(any('cancelled' in line for line in log.output))

    async def test_download_skips_recheck_when_nothing_prepopulated(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_seed.return_value = True
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '2')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock), \
                 patch('daemon.session._prepopulate', return_value=0):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)
        handle.force_recheck.assert_not_called()

    async def test_download_resumes_after_prepopulate(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_seed.return_value = True
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        handle.resume.assert_called_once()

    async def test_download_does_not_set_stop_when_ready(self) -> None:
        """stop_when_ready must never be set — pausing is handled manually after metadata."""
        session = TorrentSession()
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_seed.return_value = True
        mock_info = MagicMock()
        mock_info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = mock_info

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, '1.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path)

        handle.unset_flags.assert_not_called()


# ---------------------------------------------------------------------------
# file_priorities.json helpers
# ---------------------------------------------------------------------------

class TestLoadSaveFilePriorities(unittest.TestCase):

    def test_returns_empty_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_load_file_priorities(tmp), {})

    def test_round_trips_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = {'site/index.html': SKIP, 'site/big.mp4': PICK}
            _save_file_priorities(tmp, data)
            self.assertEqual(_load_file_priorities(tmp), data)

    def test_creates_directory_if_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_dir = os.path.join(tmp, 'new', 'dir')
            _save_file_priorities(site_dir, {'f': AUTO_PICK})
            self.assertTrue(os.path.isfile(os.path.join(site_dir, 'file_priorities.json')))

    def test_returns_empty_on_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'file_priorities.json'), 'w') as f:
                f.write('not json')
            self.assertEqual(_load_file_priorities(tmp), {})


# ---------------------------------------------------------------------------
# _site_dir helper
# ---------------------------------------------------------------------------

class TestSiteDir(unittest.TestCase):

    def test_returns_grandparent(self) -> None:
        tp = '/data/sites/npub1abc/mysite/3/site.torrent'
        self.assertEqual(_site_dir(tp), '/data/sites/npub1abc/mysite')


# ---------------------------------------------------------------------------
# _compute_new_version_priorities
# ---------------------------------------------------------------------------

class TestComputeNewVersionPriorities(unittest.TestCase):

    def _make_info(self, files: list[tuple[str, int]]) -> MagicMock:
        info = MagicMock()
        fs = MagicMock()
        fs.num_files.return_value = len(files)
        fs.file_path.side_effect = [f[0] for f in files] * 20
        fs.file_size.side_effect = [f[1] for f in files] * 20
        info.files.return_value = fs
        return info

    def test_writes_file_priorities_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = self._make_info([('site/a.html', 10)])
            _compute_new_version_priorities(info, tmp, max_site_mb=100)
            self.assertTrue(os.path.isfile(os.path.join(tmp, 'file_priorities.json')))

    def test_all_auto_pick_when_under_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = self._make_info([('site/a.html', 10), ('site/b.html', 20)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=100)
            self.assertEqual(prios, [1, 1])
            stored = _load_file_priorities(tmp)
            self.assertEqual(stored['site/a.html'], AUTO_PICK)
            self.assertEqual(stored['site/b.html'], AUTO_PICK)

    def test_budget_excludes_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 1 MB budget: small.html (100 B) is alone at root → its group fits.
            # huge.bin lives in media/ (10 MB subdir) → recurse, file group too large.
            info = self._make_info([('site/small.html', 100),
                                    ('site/media/huge.bin', 10 * 1024 * 1024)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=1)
            self.assertEqual(prios[0], 1)   # small file: included
            self.assertEqual(prios[1], 0)   # huge file: excluded
            stored = _load_file_priorities(tmp)
            self.assertEqual(stored['site/small.html'], AUTO_PICK)
            self.assertEqual(stored['site/media/huge.bin'], AUTO_SKIP)

    def test_manual_skip_overrides_budget(self) -> None:
        """A SKIP file is always excluded even if the budget would include it."""
        with tempfile.TemporaryDirectory() as tmp:
            _save_file_priorities(tmp, {'site/a.html': SKIP})
            info = self._make_info([('site/a.html', 10), ('site/b.html', 20)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=100)
            self.assertEqual(prios[0], 0)   # SKIP → always 0
            self.assertEqual(prios[1], 1)
            stored = _load_file_priorities(tmp)
            self.assertEqual(stored['site/a.html'], SKIP)  # manual state preserved

    def test_manual_pick_overrides_budget(self) -> None:
        """A PICK file is always included even if the budget would exclude it."""
        with tempfile.TemporaryDirectory() as tmp:
            _save_file_priorities(tmp, {'site/media/huge.bin': PICK})
            info = self._make_info([('site/small.html', 100),
                                    ('site/media/huge.bin', 10 * 1024 * 1024)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=1)
            self.assertEqual(prios[0], 1)   # small: included by budget
            self.assertEqual(prios[1], 1)   # PICK: always 1 despite exceeding budget
            stored = _load_file_priorities(tmp)
            self.assertEqual(stored['site/media/huge.bin'], PICK)  # manual state preserved

    def test_new_file_goes_through_budget(self) -> None:
        """A file not in file_priorities.json is treated as auto."""
        with tempfile.TemporaryDirectory() as tmp:
            _save_file_priorities(tmp, {'site/existing.html': AUTO_PICK})
            info = self._make_info([('site/existing.html', 100), ('site/new.html', 100)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=100)
            self.assertEqual(prios, [1, 1])
            stored = _load_file_priorities(tmp)
            self.assertIn('site/new.html', stored)

    def test_no_budget_includes_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = self._make_info([('site/a.html', 10), ('site/b.html', 10 * 1024 * 1024)])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=0)
            self.assertEqual(prios, [1, 1])

    def test_budget_with_pad_file_between_real_files(self) -> None:
        """Budget algorithm works correctly when a pad file sits between real files.

        libtorrent assigns non-contiguous indices when pad files are present
        (e.g. real files at indices 0 and 2, pad at index 1).  Both real files
        must fit in the budget so that initial_priorities actually attempts to
        select the high-index file — without the re-indexing fix, this causes
        an IndexError (priorities list has length 2 but index 2 is written).
        """
        with tempfile.TemporaryDirectory() as tmp:
            # index 0: small real file, index 1: pad, index 2: small real file
            # Both fit in the budget; without re-indexing, priorities[2] overflows.
            info = self._make_info([
                ('site/a.html', 100),
                ('site/.pad/0000', 512),
                ('site/b.html', 200),
            ])
            prios = _compute_new_version_priorities(info, tmp, max_site_mb=100)
            self.assertEqual(prios[0], 1)   # a.html: selected
            self.assertEqual(prios[1], 0)   # pad: always 0
            self.assertEqual(prios[2], 1)   # b.html: selected
            stored = _load_file_priorities(tmp)
            self.assertNotIn('site/.pad/0000', stored)
            self.assertEqual(stored['site/a.html'], AUTO_PICK)
            self.assertEqual(stored['site/b.html'], AUTO_PICK)


class TestDownloadComputesPriorities(unittest.IsolatedAsyncioTestCase):

    def _make_handle(self, files: list[tuple[str, int]]) -> MagicMock:
        handle = MagicMock()
        handle.has_metadata.return_value = True
        handle.is_seed.return_value = True
        handle.status.return_value = MagicMock(paused=True)
        fs = MagicMock()
        fs.num_files.return_value = len(files)
        fs.file_path.side_effect = [f[0] for f in files] * 20
        fs.file_size.side_effect = [f[1] for f in files] * 20
        info = MagicMock()
        info.files.return_value = fs
        info.info_section.return_value = b'fake'
        handle.torrent_file.return_value = info
        return handle

    async def test_uses_budget_algorithm_without_previous_state(self) -> None:
        """When no file_priorities.json exists, initial_priorities is used."""
        session = TorrentSession()
        # small.html is alone at root (file_group fits); media/huge.bin is in a
        # subdir (group exceeds budget) so it is excluded.
        handle = self._make_handle([('site/small.html', 10),
                                    ('site/media/huge.bin', 10 * 1024 * 1024)])
        handle.get_file_priorities.return_value = [1, 0]

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path,
                                           max_site_mb=1)

        # media/huge.bin (10 MB) exceeds 1 MB budget → excluded
        handle.prioritize_files.assert_called_once_with([1, 0])
        self.assertTrue(any('file selection' in line for line in log.output))

    async def test_manual_skip_persists_across_versions(self) -> None:
        """A SKIP file from a previous version is still excluded in the new version."""
        session = TorrentSession()
        handle = self._make_handle([('site/a.html', 10), ('site/b.html', 10)])
        handle.get_file_priorities.return_value = [1, 1]

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            site_dir = os.path.dirname(os.path.dirname(torrent_path))
            # Pre-populate with a SKIP state for b.html
            _save_file_priorities(site_dir, {'site/b.html': SKIP})

            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path,
                                           max_site_mb=100)

        # b.html should be excluded (SKIP state preserved)
        handle.prioritize_files.assert_called_once_with([1, 0])

    async def test_manual_pick_persists_across_versions(self) -> None:
        """A PICK file from a previous version is still included in the new version."""
        session = TorrentSession()
        # small.html alone at root → budget selects it; media/huge.bin would be
        # excluded by budget but is PICK → both end up at priority 1.
        handle = self._make_handle([('site/small.html', 100),
                                    ('site/media/huge.bin', 10 * 1024 * 1024)])
        handle.get_file_priorities.return_value = [1, 1]

        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            site_dir = os.path.dirname(os.path.dirname(torrent_path))
            # media/huge.bin is manually picked despite exceeding budget
            _save_file_priorities(site_dir, {'site/media/huge.bin': PICK})

            with patch.object(session._session, 'add_torrent', return_value=handle), \
                 patch('libtorrent.parse_magnet_uri', return_value=MagicMock()), \
                 patch('libtorrent.bdecode', return_value={}), \
                 patch('libtorrent.bencode', return_value=b'fake'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO'):
                    await session.download('magnet:?xt=urn:btih:abc', version_dir, torrent_path,
                                           max_site_mb=1)

        # small.html=1 (budget), media/huge.bin=1 (PICK) → all prios 1 → no call
        handle.prioritize_files.assert_not_called()


# ---------------------------------------------------------------------------
# _prepopulate
# ---------------------------------------------------------------------------

class TestPrepopulate(unittest.TestCase):

    def _make_site(self, tmp: str, name: str, files: dict[str, str]) -> Site:
        """Create a published site version (complete with site.torrent)."""
        sites_dir = os.path.join(tmp, 'sites')
        data_dir = os.path.join(tmp, 'data')
        src = os.path.join(sites_dir, name)
        os.makedirs(src, exist_ok=True)
        for fname, content in files.items():
            with open(os.path.join(src, fname), 'w') as f:
                f.write(content)
        s = Site(name, sites_dir=sites_dir, data_dir=data_dir, npub=FAKE_NPUB)
        s.create()
        return s

    def _site_dir_for(self, s: Site) -> str:
        return os.path.dirname(os.path.dirname(s.torrent_path))

    def test_hard_links_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s1 = self._make_site(tmp, 'mysite', {'file.txt': 'content'})
            site_dir = self._site_dir_for(s1)
            # Simulate downloading the same torrent into a new dir
            new_version_dir = os.path.join(site_dir, '100')
            os.makedirs(new_version_dir)
            new_info = lt.torrent_info(s1.torrent_path)
            count = _prepopulate(new_info, site_dir, new_version_dir)

            dst = os.path.join(new_version_dir, 'site', 'file.txt')
            src = os.path.join(os.path.dirname(s1.torrent_path), 'site', 'file.txt')
            self.assertEqual(count, 1)
            self.assertTrue(os.path.exists(dst))
            self.assertEqual(os.stat(dst).st_ino, os.stat(src).st_ino)

    def test_does_not_link_when_hash_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s1 = self._make_site(tmp, 'mysite', {'f.txt': 'original'})
            site_dir = self._site_dir_for(s1)
            new_version_dir = os.path.join(site_dir, '100')
            os.makedirs(new_version_dir)

            # Create a torrent with different content (different hash) elsewhere
            with tempfile.TemporaryDirectory() as tmp2:
                s_other = self._make_site(tmp2, 'other', {'f.txt': 'completely different'})
                new_info = lt.torrent_info(s_other.torrent_path)
                count = _prepopulate(new_info, site_dir, new_version_dir)

            self.assertEqual(count, 0)
            self.assertFalse(os.path.exists(os.path.join(new_version_dir, 'site', 'f.txt')))

    def test_replaces_existing_placeholder_with_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s1 = self._make_site(tmp, 'mysite', {'f.txt': 'content'})
            site_dir = self._site_dir_for(s1)
            new_version_dir = os.path.join(site_dir, '100')
            existing = os.path.join(new_version_dir, 'site', 'f.txt')
            os.makedirs(os.path.dirname(existing))
            with open(existing, 'w') as f:
                f.write('placeholder')

            new_info = lt.torrent_info(s1.torrent_path)
            count = _prepopulate(new_info, site_dir, new_version_dir)

            src = os.path.join(os.path.dirname(s1.torrent_path), 'site', 'f.txt')
            self.assertEqual(os.stat(existing).st_ino, os.stat(src).st_ino)
            self.assertEqual(count, 1)

    def test_only_indices_empty_set_restores_nothing(self) -> None:
        """An empty only_indices set skips all files and returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            s1 = self._make_site(tmp, 'mysite', {'file.txt': 'content'})
            site_dir = self._site_dir_for(s1)
            new_version_dir = os.path.join(site_dir, '100')
            os.makedirs(new_version_dir)
            new_info = lt.torrent_info(s1.torrent_path)
            count = _prepopulate(new_info, site_dir, new_version_dir,
                                 only_indices=set())
            self.assertEqual(count, 0)

    def test_only_indices_excludes_unspecified_files(self) -> None:
        """Files whose index is not in only_indices are not restored."""
        with tempfile.TemporaryDirectory() as tmp:
            s1 = self._make_site(tmp, 'mysite', {'a.txt': 'aaa', 'b.txt': 'bbb'})
            site_dir = self._site_dir_for(s1)
            new_version_dir = os.path.join(site_dir, '100')
            os.makedirs(new_version_dir)
            info = lt.torrent_info(s1.torrent_path)
            fs = info.files()
            real_indices = [
                i for i in range(fs.num_files())
                if '/.pad/' not in fs.file_path(i).replace('\\', '/')
            ]
            # Only restore the first real file; the second should be untouched.
            count = _prepopulate(info, site_dir, new_version_dir,
                                 only_indices={real_indices[0]})
            self.assertEqual(count, 1)

    def test_links_from_older_version_when_absent_from_recent(self) -> None:
        """Files absent from the most recent version are found in older ones."""
        with tempfile.TemporaryDirectory() as tmp:
            # v1 has both old.txt and common.txt
            s1 = self._make_site(tmp, 'mysite',
                                  {'old.txt': 'archive', 'common.txt': 'shared'})
            # v2: remove old.txt from source, keep common.txt unchanged
            src_dir = os.path.join(tmp, 'sites', 'mysite')
            os.remove(os.path.join(src_dir, 'old.txt'))
            s2 = self._make_site(tmp, 'mysite', {'common.txt': 'shared'})

            site_dir = self._site_dir_for(s1)
            v3_dir = os.path.join(site_dir, '100')  # simulated new download
            os.makedirs(v3_dir)

            # "v3" re-adds old.txt with same content as v1
            new_info = lt.torrent_info(s1.torrent_path)  # proxy for v3's torrent
            count = _prepopulate(new_info, site_dir, v3_dir)

            old_dst = os.path.join(v3_dir, 'site', 'old.txt')
            old_src_v1 = os.path.join(os.path.dirname(s1.torrent_path), 'site', 'old.txt')
            self.assertTrue(os.path.exists(old_dst))
            # old.txt must have been linked from v1 (the only version that has it)
            self.assertEqual(os.stat(old_dst).st_ino, os.stat(old_src_v1).st_ino)
            self.assertGreater(count, 0)


# ---------------------------------------------------------------------------
# TorrentSession.file_list
# ---------------------------------------------------------------------------

class TestFileList(unittest.TestCase):

    def test_returns_none_when_handle_not_found(self) -> None:
        session = TorrentSession()
        self.assertIsNone(session.file_list('/nonexistent.torrent'))

    def test_returns_none_when_handle_invalid(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_valid.return_value = False
        session._handles['/t.torrent'] = handle
        self.assertIsNone(session.file_list('/t.torrent'))

    def test_returns_none_when_no_torrent_file(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_valid.return_value = True
        handle.torrent_file.return_value = None
        session._handles['/t.torrent'] = handle
        self.assertIsNone(session.file_list('/t.torrent'))

    def test_returns_file_entries_with_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # torrent_path inside version dir inside site_dir
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            site_dir = os.path.join(tmp, 'npub', 'site')
            _save_file_priorities(site_dir, {
                'site/index.html': AUTO_PICK,
                'site/style.css': SKIP,
            })

            session = TorrentSession()
            handle = MagicMock()
            handle.is_valid.return_value = True
            fs = MagicMock()
            fs.num_files.return_value = 2
            fs.file_path.side_effect = ['site/index.html', 'site/style.css'] * 10
            fs.file_size.side_effect = [100, 200] * 10
            handle.get_file_priorities.return_value = [1, 0]
            info = MagicMock()
            info.files.return_value = fs
            handle.torrent_file.return_value = info
            session._handles[torrent_path] = handle

            result = session.file_list(torrent_path)
            self.assertIsNotNone(result)
            files, total = result
            self.assertEqual(total, 2)
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0]['path'], 'site/index.html')
            self.assertEqual(files[0]['priority'], 1)
            self.assertEqual(files[0]['state'], AUTO_PICK)
            self.assertEqual(files[1]['path'], 'site/style.css')
            self.assertEqual(files[1]['priority'], 0)
            self.assertEqual(files[1]['state'], SKIP)

    def test_pad_files_excluded_from_results(self) -> None:
        """Pad files must not appear in the file_list output."""
        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'n', 's', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')

            session = TorrentSession()
            handle = MagicMock()
            handle.is_valid.return_value = True
            fs = MagicMock()
            fs.num_files.return_value = 3
            fs.file_path.side_effect = [
                'site/a.html', 'site/.pad/0000', 'site/b.html',
            ] * 10
            fs.file_size.side_effect = [10, 512, 20] * 10
            handle.get_file_priorities.return_value = [1, 0, 1]
            info = MagicMock()
            info.files.return_value = fs
            handle.torrent_file.return_value = info
            session._handles[torrent_path] = handle

            files, total = session.file_list(torrent_path)
            self.assertEqual(total, 3)  # full lt file count including pad
            self.assertEqual(len(files), 2)  # only real files
            paths = [f['path'] for f in files]
            self.assertIn('site/a.html', paths)
            self.assertIn('site/b.html', paths)
            self.assertNotIn('site/.pad/0000', paths)

    def test_priority_defaults_to_1_when_prios_list_too_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'n', 's', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')

            session = TorrentSession()
            handle = MagicMock()
            handle.is_valid.return_value = True
            fs = MagicMock()
            fs.num_files.return_value = 2
            fs.file_path.side_effect = ['site/a.html', 'site/b.html'] * 10
            fs.file_size.side_effect = [10, 20] * 10
            handle.get_file_priorities.return_value = []
            info = MagicMock()
            info.files.return_value = fs
            handle.torrent_file.return_value = info
            session._handles[torrent_path] = handle

            files, total = session.file_list(torrent_path)
            self.assertEqual(total, 2)
            self.assertEqual(files[0]['priority'], 1)
            self.assertEqual(files[1]['priority'], 1)


# ---------------------------------------------------------------------------
# TorrentSession.set_file_priorities
# ---------------------------------------------------------------------------

class TestSetFilePriorities(unittest.TestCase):

    def _make_session_with_handle(self, tmp: str, file_names: list[str],
                                   file_sizes: list[int] | None = None
                                   ) -> tuple[TorrentSession, str]:
        """Return (session, torrent_path) for a handle inside a versioned dir tree."""
        # Use tmp/npub/site/1/site.torrent so _site_dir() returns tmp/npub/site
        version_dir = os.path.join(tmp, 'npub', 'site', '1')
        os.makedirs(version_dir, exist_ok=True)
        torrent_path = os.path.join(version_dir, 'site.torrent')

        session = TorrentSession()
        session._session = MagicMock()

        fs = MagicMock()
        fs.num_files.return_value = len(file_names)
        fs.file_path.side_effect = lambda i: file_names[i]
        if file_sizes:
            fs.file_size.side_effect = lambda i: file_sizes[i]

        info = MagicMock()
        info.files.return_value = fs

        status = MagicMock()
        status.save_path = tmp

        handle = MagicMock()
        handle.is_valid.return_value = True
        handle.torrent_file.return_value = info
        handle.status.return_value = status

        session._handles[torrent_path] = handle
        return session, torrent_path

    def test_returns_false_when_not_found(self) -> None:
        session = TorrentSession()
        self.assertFalse(session.set_file_priorities('/nonexistent.torrent', [1]))

    def test_returns_false_when_invalid(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_valid.return_value = False
        session._handles['/t.torrent'] = handle
        self.assertFalse(session.set_file_priorities('/t.torrent', [1]))

    def test_calls_prioritize_and_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['page.html'])
            result = session.set_file_priorities(torrent_path, [1])
            session._handles[torrent_path].prioritize_files.assert_called_once_with([1])
            self.assertTrue(result)

    def test_priority_zero_deletes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fname = 'page.html'
            fpath = os.path.join(tmp, fname)
            open(fpath, 'w').close()
            session, torrent_path = self._make_session_with_handle(tmp, [fname])
            with self.assertLogs('daemon.session', level='INFO') as log:
                result = session.set_file_priorities(torrent_path, [0])
            self.assertTrue(result)
            self.assertFalse(os.path.exists(fpath))
            self.assertTrue(any('deleted priority-0 file' in line for line in log.output))

    def test_priority_one_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fname = 'page.html'
            fpath = os.path.join(tmp, fname)
            open(fpath, 'w').close()
            session, torrent_path = self._make_session_with_handle(tmp, [fname])
            result = session.set_file_priorities(torrent_path, [1])
            self.assertTrue(result)
            self.assertTrue(os.path.exists(fpath))

    def test_priority_zero_missing_file_is_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['absent.html'])
            result = session.set_file_priorities(torrent_path, [0])
            self.assertTrue(result)

    def test_resume_data_requested_after_priority_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['page.html'])
            handle = session._handles[torrent_path]
            session.set_file_priorities(torrent_path, [1])
            handle.save_resume_data.assert_called_once()
            resume_path = torrent_path.replace('.torrent', '.resume')
            self.assertIn(handle, session._pending_resume)
            self.assertEqual(session._pending_resume[handle], resume_path)

    def test_auto_pick_to_zero_becomes_skip(self) -> None:
        """Turning off an AUTO_PICK file should record it as SKIP."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': AUTO_PICK})
            session.set_file_priorities(torrent_path, [0])
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], SKIP)

    def test_auto_skip_to_one_becomes_pick(self) -> None:
        """Turning on an AUTO_SKIP file should record it as PICK."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': AUTO_SKIP})
            session.set_file_priorities(torrent_path, [1])
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], PICK)

    def test_no_change_preserves_state(self) -> None:
        """Setting the same effective priority preserves the existing state."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': AUTO_PICK})
            session.set_file_priorities(torrent_path, [1])  # already 1 (AUTO_PICK)
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], AUTO_PICK)  # state unchanged

    def test_skip_to_one_becomes_pick(self) -> None:
        """User re-enables a manually-skipped file → PICK."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': SKIP})
            session.set_file_priorities(torrent_path, [1])
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], PICK)

    def test_pick_to_zero_becomes_skip(self) -> None:
        """User disables a manually-picked file → SKIP."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': PICK})
            session.set_file_priorities(torrent_path, [0])
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], SKIP)

    def test_missing_priority_one_file_triggers_prepopulate(self) -> None:
        """When a priority-1 file is missing from disk, _prepopulate is called."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            # site/a.html does not exist on disk → missing_selected = {0}
            with patch('daemon.session._prepopulate', return_value=1) as mock_prep, \
                 self.assertLogs('daemon.session', level='INFO'):
                session.set_file_priorities(torrent_path, [1])
            mock_prep.assert_called_once()
            self.assertEqual(mock_prep.call_args.kwargs['only_indices'], {0})
            session._handles[torrent_path].force_recheck.assert_called_once()

    def test_no_force_recheck_when_prepopulate_restores_nothing(self) -> None:
        """force_recheck is not called when _prepopulate returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['site/a.html'])
            with patch('daemon.session._prepopulate', return_value=0):
                session.set_file_priorities(torrent_path, [1])
            session._handles[torrent_path].force_recheck.assert_not_called()

    def test_no_prepopulate_when_file_already_on_disk(self) -> None:
        """_prepopulate is not called when the priority-1 file already exists."""
        with tempfile.TemporaryDirectory() as tmp:
            fname = 'site/a.html'
            fpath = os.path.join(tmp, fname)
            os.makedirs(os.path.dirname(fpath))
            open(fpath, 'w').close()
            session, torrent_path = self._make_session_with_handle(tmp, [fname])
            with patch('daemon.session._prepopulate') as mock_prep:
                session.set_file_priorities(torrent_path, [1])
            mock_prep.assert_not_called()

    def test_handle_alert_writes_resume_file_and_clears_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['page.html'])
            handle = session._handles[torrent_path]
            resume_path = torrent_path.replace('.torrent', '.resume')
            session._pending_resume[handle] = resume_path

            alert = MagicMock(spec=lt.save_resume_data_alert)
            alert.handle = handle
            alert.params = MagicMock()
            with patch('daemon.session.lt.write_resume_data_buf', return_value=b'RESUME'):
                with self.assertLogs('daemon.session', level='INFO'):
                    session._handle_alert(alert)

            self.assertTrue(os.path.isfile(resume_path))
            self.assertNotIn(handle, session._pending_resume)

    def test_handle_alert_failed_clears_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, torrent_path = self._make_session_with_handle(tmp, ['page.html'])
            handle = session._handles[torrent_path]
            resume_path = torrent_path.replace('.torrent', '.resume')
            session._pending_resume[handle] = resume_path

            alert = MagicMock(spec=lt.save_resume_data_failed_alert)
            alert.handle = handle
            alert.torrent_name = 'site'
            with self.assertLogs('daemon.session', level='WARNING'):
                session._handle_alert(alert)

            self.assertNotIn(handle, session._pending_resume)


# ---------------------------------------------------------------------------
# TorrentSession.reset_file_priorities
# ---------------------------------------------------------------------------

class TestResetFilePriorities(unittest.TestCase):

    def _make_session_with_handle(self, tmp: str, files: list[tuple[str, int]]
                                   ) -> tuple[TorrentSession, str]:
        """Build (session, torrent_path) with proper versioned dir structure."""
        version_dir = os.path.join(tmp, 'npub', 'site', '1')
        os.makedirs(version_dir, exist_ok=True)
        torrent_path = os.path.join(version_dir, 'site.torrent')

        session = TorrentSession()
        session._session = MagicMock()

        fs = MagicMock()
        fs.num_files.return_value = len(files)
        fs.file_path.side_effect = [f[0] for f in files] * 20
        fs.file_size.side_effect = [f[1] for f in files] * 20

        info = MagicMock()
        info.files.return_value = fs

        status = MagicMock()
        status.save_path = tmp

        handle = MagicMock()
        handle.is_valid.return_value = True
        handle.torrent_file.return_value = info
        handle.status.return_value = status

        session._handles[torrent_path] = handle
        return session, torrent_path

    def test_returns_false_when_not_found(self) -> None:
        session = TorrentSession()
        self.assertFalse(session.reset_file_priorities('/nonexistent.torrent'))

    def test_returns_false_when_no_metadata(self) -> None:
        session = TorrentSession()
        handle = MagicMock()
        handle.is_valid.return_value = True
        handle.torrent_file.return_value = None
        session._handles['/t.torrent'] = handle
        self.assertFalse(session.reset_file_priorities('/t.torrent'))

    def test_clears_manual_states_and_reruns_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # small.html alone at root → budget selects it; media/huge.bin is in
            # a subdir → budget excludes it.
            files = [('site/small.html', 100), ('site/media/huge.bin', 10 * 1024 * 1024)]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            site_dir = _site_dir(torrent_path)
            # Pre-populate with manual overrides
            _save_file_priorities(site_dir, {
                'site/small.html': SKIP,        # manually excluded
                'site/media/huge.bin': PICK,    # manually included
            })
            with self.assertLogs('daemon.session', level='INFO'):
                result = session.reset_file_priorities(torrent_path, max_site_mb=1)

            self.assertTrue(result)
            stored = _load_file_priorities(site_dir)
            # After reset: budget re-runs.  small.html fits (100 B), media/huge.bin doesn't.
            self.assertEqual(stored['site/small.html'], AUTO_PICK)
            self.assertEqual(stored['site/media/huge.bin'], AUTO_SKIP)
            session._handles[torrent_path].prioritize_files.assert_called_once_with([1, 0])

    def test_deletes_files_newly_excluded_by_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = [('site/small.html', 100), ('site/huge.bin', 10 * 1024 * 1024)]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            # Create the huge.bin file on disk (simulating it was previously PICK)
            huge_path = os.path.join(tmp, 'site', 'huge.bin')
            os.makedirs(os.path.dirname(huge_path))
            with open(huge_path, 'wb') as f:
                f.write(b'\x00' * 100)

            with self.assertLogs('daemon.session', level='INFO'):
                session.reset_file_priorities(torrent_path, max_site_mb=1)

            # huge.bin exceeds budget → should be deleted
            self.assertFalse(os.path.exists(huge_path))

    def test_no_budget_selects_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = [('site/a.html', 100), ('site/b.bin', 10 * 1024 * 1024)]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            site_dir = _site_dir(torrent_path)
            _save_file_priorities(site_dir, {'site/a.html': SKIP, 'site/b.bin': SKIP})
            with self.assertLogs('daemon.session', level='INFO'):
                session.reset_file_priorities(torrent_path, max_site_mb=0)

            session._handles[torrent_path].prioritize_files.assert_called_once_with([1, 1])

    def test_budget_with_pad_file_between_real_files(self) -> None:
        """Budget algorithm works correctly when pad files create non-contiguous indices.

        Both real files must fit in the budget so that initial_priorities
        actually attempts to select the high-index file — without the
        re-indexing fix, this causes an IndexError (priorities list has
        length 2 but index 2 is written).
        """
        with tempfile.TemporaryDirectory() as tmp:
            # index 0: small real file, index 1: pad, index 2: small real file
            # Both fit in the budget; without re-indexing, priorities[2] overflows.
            files = [
                ('site/a.html', 100),
                ('site/.pad/0000', 512),
                ('site/b.html', 200),
            ]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            with self.assertLogs('daemon.session', level='INFO'):
                result = session.reset_file_priorities(torrent_path, max_site_mb=100)

            self.assertTrue(result)
            site_dir = _site_dir(torrent_path)
            stored = _load_file_priorities(site_dir)
            self.assertEqual(stored['site/a.html'], AUTO_PICK)
            self.assertEqual(stored['site/b.html'], AUTO_PICK)
            self.assertNotIn('site/.pad/0000', stored)
            # libtorrent priorities: a.html=1, pad=0, b.html=1
            session._handles[torrent_path].prioritize_files.assert_called_once_with([1, 0, 1])

    def test_missing_included_file_triggers_prepopulate(self) -> None:
        """After reset, missing priority-1 files are restored via _prepopulate."""
        with tempfile.TemporaryDirectory() as tmp:
            files = [('site/a.html', 100)]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            # site/a.html does not exist → missing_selected = {0}
            with patch('daemon.session._prepopulate', return_value=1) as mock_prep, \
                 self.assertLogs('daemon.session', level='INFO'):
                session.reset_file_priorities(torrent_path, max_site_mb=0)
            mock_prep.assert_called_once()
            self.assertEqual(mock_prep.call_args.kwargs['only_indices'], {0})
            session._handles[torrent_path].force_recheck.assert_called_once()

    def test_no_force_recheck_after_reset_when_prepopulate_restores_nothing(self) -> None:
        """force_recheck is not called when _prepopulate returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            files = [('site/a.html', 100)]
            session, torrent_path = self._make_session_with_handle(tmp, files)
            with patch('daemon.session._prepopulate', return_value=0), \
                 self.assertLogs('daemon.session', level='INFO'):
                session.reset_file_priorities(torrent_path, max_site_mb=0)
            session._handles[torrent_path].force_recheck.assert_not_called()


# ---------------------------------------------------------------------------
# _site_info
# ---------------------------------------------------------------------------

class TestSiteInfo(unittest.TestCase):

    def _make_handle(self, state_str: str = 'seeding', upload_rate: int = 0,
                     total_wanted: int = 0, num_peers: int = 0) -> MagicMock:
        handle = MagicMock()
        status = MagicMock()
        mock_state = MagicMock()
        mock_state.__str__ = MagicMock(return_value=f'torrent_status.states.{state_str}')
        status.state = mock_state
        status.upload_rate = upload_rate
        status.total_wanted = total_wanted
        status.num_peers = num_peers
        handle.status.return_value = status
        handle.torrent_file.return_value = None
        return handle

    def test_returns_correct_fields(self) -> None:
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, 'npub1testfake', 'mysite', '3', 'site.torrent')
            os.makedirs(os.path.dirname(torrent_path))
            info = session._site_info(torrent_path, self._make_handle(
                upload_rate=500, total_wanted=1024, num_peers=1,
            ))
        self.assertEqual(info['identifier'], 'mysite.tfake')
        self.assertEqual(info['url_identifier'], 'mysite.npub1testfake')
        self.assertEqual(info['version'], 3)
        self.assertEqual(info['upload_rate'], 500)
        self.assertEqual(info['disk_bytes'], 0)   # empty version dir
        self.assertEqual(info['num_peers'], 1)
        self.assertEqual(info['state'], 'seeding')

    def test_site_total_bytes_excludes_pad_files(self) -> None:
        session = TorrentSession()
        handle = self._make_handle()
        mock_info = MagicMock()
        mock_fs = MagicMock()
        mock_fs.num_files.return_value = 3
        mock_fs.file_path.side_effect = lambda i: [
            'site/index.html', 'site/.pad/0000', 'site/style.css',
        ][i]
        mock_fs.file_size.side_effect = lambda i: [100, 1000, 200][i]
        mock_info.files.return_value = mock_fs
        handle.torrent_file.return_value = mock_info
        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, 'npub1testfake', 'mysite', '1', 'site.torrent')
            os.makedirs(os.path.dirname(torrent_path))
            info = session._site_info(torrent_path, handle)
        self.assertEqual(info['site_total_bytes'], 300)


class TestSitesInfo(unittest.TestCase):

    def _make_handle(self, name: str = 'mysite', state_str: str = 'torrent_status.states.seeding',
                     upload_rate: int = 0, download_rate: int = 0,
                     total_upload: int = 0, total_wanted: int = 0,
                     num_peers: int = 0) -> MagicMock:
        handle = MagicMock()
        handle.is_valid.return_value = True
        handle.torrent_file.return_value = None
        status = MagicMock()
        status.name = name
        mock_state = MagicMock()
        mock_state.__str__ = MagicMock(return_value=state_str)
        status.state = mock_state
        status.upload_rate = upload_rate
        status.download_rate = download_rate
        status.total_upload = total_upload
        status.total_wanted = total_wanted
        status.num_peers = num_peers
        handle.status.return_value = status
        return handle

    def test_returns_info_for_valid_handle(self) -> None:
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'site', '1')
            os.makedirs(version_dir)
            with open(os.path.join(version_dir, 'file.txt'), 'w') as f:
                f.write('hello')
            torrent_path = os.path.join(version_dir, '1.torrent')
            session._handles[torrent_path] = self._make_handle(
                upload_rate=1000, total_wanted=1048576, num_peers=2,
            )
            info = session.sites_info()
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]['version'], 1)
        self.assertEqual(info[0]['upload_rate'], 1000)
        self.assertEqual(info[0]['disk_bytes'], 5)   # one 5-byte file on disk
        self.assertEqual(info[0]['num_peers'], 2)
        self.assertEqual(info[0]['state'], 'seeding')
        self.assertEqual(info[0]['exclusive_bytes'], 5)

    def test_exclusive_bytes_zero_when_version_dir_missing(self) -> None:
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, 'npub', 'site', '1', '1.torrent')
            os.makedirs(os.path.dirname(torrent_path))
            session._handles[torrent_path] = self._make_handle()
            info = session.sites_info()
        self.assertEqual(info[0]['exclusive_bytes'], 0)

    def test_sorted_by_identifier_then_version(self) -> None:
        """sites_info returns entries sorted by (url_identifier, version)."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            # Insert in reverse order: site_b v2, site_b v1, site_a v1
            paths = [
                os.path.join(tmp, 'npub', 'site_b', '2', 'site.torrent'),
                os.path.join(tmp, 'npub', 'site_b', '1', 'site.torrent'),
                os.path.join(tmp, 'npub', 'site_a', '1', 'site.torrent'),
            ]
            for p in paths:
                os.makedirs(os.path.dirname(p))
                session._handles[p] = self._make_handle()

            result = session.sites_info()

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]['identifier'], 'site_a.npub')
        self.assertEqual(result[1]['identifier'], 'site_b.npub')
        self.assertEqual(result[1]['version'], 1)
        self.assertEqual(result[2]['identifier'], 'site_b.npub')
        self.assertEqual(result[2]['version'], 2)

    def test_skips_invalid_handles(self) -> None:
        """Invalid handles must be filtered out even when path structure is valid."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            version_dir = os.path.join(tmp, 'npub', 'mysite', '1')
            os.makedirs(version_dir)
            torrent_path = os.path.join(version_dir, 'site.torrent')
            handle = MagicMock()
            handle.is_valid.return_value = False
            session._handles[torrent_path] = handle
            self.assertEqual(session.sites_info(), [])


class TestDiskStats(unittest.TestCase):

    def test_total_includes_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'a.txt'), 'w') as f:
                f.write('aaa')
            with open(os.path.join(tmp, 'b.txt'), 'w') as f:
                f.write('bb')
            total, _exclusive = TorrentSession._disk_stats(tmp)
        self.assertEqual(total, 5)

    def test_exclusive_counts_only_unshared_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, 'a.txt')
            b = os.path.join(tmp, 'b.txt')
            b_link = os.path.join(tmp, 'b_link.txt')
            with open(a, 'w') as f:
                f.write('aaa')
            with open(b, 'w') as f:
                f.write('bb')
            os.link(b, b_link)
            _total, exclusive = TorrentSession._disk_stats(tmp)
        self.assertEqual(exclusive, 3)

    def test_returns_zeros_for_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(TorrentSession._disk_stats(tmp), (0, 0))


class TestStats(unittest.TestCase):

    def test_aggregates_rates_across_sites(self) -> None:
        session = TorrentSession()
        session.sites_info = lambda: [  # type: ignore[method-assign]
            {'upload_rate': 1000, 'download_rate': 500},
            {'upload_rate': 2000, 'download_rate': 100},
        ]
        stats = session.stats()
        self.assertEqual(stats['upload_rate'], 3000)
        self.assertEqual(stats['download_rate'], 600)
        self.assertEqual(stats['num_sites'], 2)

    def test_returns_zeros_when_no_sites(self) -> None:
        session = TorrentSession()
        session.sites_info = lambda: []  # type: ignore[method-assign]
        stats = session.stats()
        self.assertEqual(stats['num_sites'], 0)
        self.assertEqual(stats['upload_rate'], 0)
        self.assertEqual(stats['download_rate'], 0)


class TestDebugInfo(unittest.TestCase):

    def _torrent_path(self, tmp: str, npub: str = 'npub1abcde',
                      site: str = 'mysite', ver: int = 1) -> str:
        path = os.path.join(tmp, npub, site, str(ver), 'site.torrent')
        os.makedirs(os.path.dirname(path))
        return path

    def _make_handle(self, state_str: str = 'torrent_status.states.seeding',
                     errc_value: int = 0, errc_raises: bool = False,
                     trackers: list | None = None,
                     peers: list | None = None) -> MagicMock:
        handle = MagicMock()
        handle.is_valid.return_value = True
        s = MagicMock()
        mock_state = MagicMock()
        mock_state.__str__ = MagicMock(return_value=state_str)
        s.state = mock_state
        s.progress = 0.5
        s.num_peers = 1
        s.num_seeds = 0
        s.connect_candidates = 3
        s.download_rate = 100
        s.upload_rate = 200
        if errc_raises:
            s.errc.value.side_effect = AttributeError('no errc')
            s.error = 'some error'
        else:
            s.errc.value.return_value = errc_value
            s.errc.message.return_value = 'disk full' if errc_value else ''
        handle.status.return_value = s
        handle.trackers.return_value = trackers or []
        handle.get_peer_info.return_value = peers or []
        return handle

    def test_returns_error_when_session_not_running(self) -> None:
        """debug_info returns an error dict when the session has been shut down."""
        session = TorrentSession()
        session._session = None  # simulate post-shutdown state
        result = session.debug_info()
        self.assertEqual(result['error'], 'session not running')
        self.assertEqual(result['torrents'], [])

    def test_skips_invalid_handles(self) -> None:
        """Invalid handles are excluded from the torrent list."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp)
            handle = MagicMock()
            handle.is_valid.return_value = False
            session._handles[tp] = handle
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        self.assertEqual(result['torrents'], [])

    def test_returns_torrent_fields(self) -> None:
        """Torrent entry contains expected fields with correct values."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp, ver=3)
            session._handles[tp] = self._make_handle()
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        self.assertEqual(len(result['torrents']), 1)
        t = result['torrents'][0]
        self.assertEqual(t['version'], 3)
        self.assertEqual(t['state'], 'seeding')
        self.assertEqual(t['error'], '')
        self.assertEqual(t['num_peers'], 1)
        self.assertEqual(t['connect_candidates'], 3)

    def test_error_string_from_errc(self) -> None:
        """Non-zero errc.value() causes the error message to be included."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp)
            session._handles[tp] = self._make_handle(errc_value=28)
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        self.assertEqual(result['torrents'][0]['error'], 'disk full')

    def test_error_string_falls_back_when_errc_raises(self) -> None:
        """If errc raises, the fallback reads s.error instead."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp)
            session._handles[tp] = self._make_handle(errc_raises=True)
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        self.assertEqual(result['torrents'][0]['error'], 'some error')

    def test_tracker_fields_and_endpoints(self) -> None:
        """Tracker entries include url, tier, fails, and endpoint messages."""
        ep = MagicMock()
        ep.message = 'connection timed out'
        ep.last_announce = 'yesterday'
        ep.next_announce = 'tomorrow'
        tr = MagicMock()
        tr.url = 'udp://tracker.example.com:1234/announce'
        tr.tier = 0
        tr.fails = 2
        tr.verified = False
        tr.updating = False
        tr.endpoints = [ep]
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp)
            session._handles[tp] = self._make_handle(trackers=[tr])
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        tracker = result['torrents'][0]['trackers'][0]
        self.assertEqual(tracker['url'], 'udp://tracker.example.com:1234/announce')
        self.assertEqual(tracker['fails'], 2)
        self.assertEqual(tracker['endpoints'][0]['message'], 'connection timed out')

    def test_peer_source_flags_decoded(self) -> None:
        """Peer source bitmask is decoded to a list of source name strings."""
        peer = MagicMock()
        peer.ip = ('192.168.1.1', 6881)
        peer.source = 0x8 | 0x2   # lsd + dht
        peer.progress = 0.75
        peer.payload_down_speed = 512
        peer.payload_up_speed = 256
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._torrent_path(tmp)
            session._handles[tp] = self._make_handle(peers=[peer])
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        p = result['torrents'][0]['peers'][0]
        self.assertEqual(p['ip'], '192.168.1.1:6881')
        self.assertIn('lsd', p['source'])
        self.assertIn('dht', p['source'])
        self.assertNotIn('tracker', p['source'])
        self.assertEqual(p['down_speed'], 512)

    def test_session_settings_included(self) -> None:
        """Session settings block reflects what get_settings() returns."""
        session = TorrentSession()
        session._handles = {}
        session._session = MagicMock()
        session._session.get_settings.return_value = {
            'allow_multiple_connections_per_ip': True,
            'enable_dht': True,
            'enable_lsd': True,
            'local_service_announce_interval': 10,
        }
        result = session.debug_info()
        self.assertTrue(result['session']['allow_multiple_connections_per_ip'])
        self.assertEqual(result['session']['local_service_announce_interval'], 10)

    def test_sorted_by_identifier_then_version(self) -> None:
        """Torrents are returned sorted by (identifier, version)."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            paths = [
                self._torrent_path(tmp, site='z_site', ver=2),
                self._torrent_path(tmp, site='z_site', ver=1),
                self._torrent_path(tmp, site='a_site', ver=1),
            ]
            for p in paths:
                session._handles[p] = self._make_handle()
            session._session = MagicMock()
            session._session.get_settings.return_value = {}
            result = session.debug_info()
        identifiers = [(t['identifier'], t['version']) for t in result['torrents']]
        self.assertEqual(identifiers[0][0], 'a_site.abcde')
        self.assertEqual(identifiers[1], (identifiers[2][0], 1))
        self.assertEqual(identifiers[2][1], 2)


class TestHandleResumeAlert(unittest.TestCase):

    def _make_pending(self, tmp: str) -> tuple[dict, MagicMock]:
        torrent_path = os.path.join(tmp, '1.torrent')
        handle = MagicMock()
        return {torrent_path: handle}, handle

    def test_save_alert_only_writes_for_matching_handle(self) -> None:
        """Only the handle matching the alert is removed; others stay in pending.

        handle_b (second entry) matches the alert.  With the correct guard
        (alert.handle == handle), path_b is written and path_a stays.  Without
        the guard (always True) path_a would be written first and path_b would
        remain unprocessed.
        """
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            handle_a = MagicMock()
            handle_b = MagicMock()
            path_a = os.path.join(tmp, 'a.torrent')
            path_b = os.path.join(tmp, 'b.torrent')
            # Insert handle_a first so it appears first in iteration order.
            # The alert matches handle_b (second entry).
            pending = {path_a: handle_a, path_b: handle_b}
            alert = MagicMock(spec=lt.save_resume_data_alert)
            alert.handle = handle_b
            alert.params = MagicMock()
            with patch('libtorrent.write_resume_data_buf', return_value=b'data'), \
                 self.assertLogs('daemon.session', level='INFO'):
                session._handle_resume_alert(alert, pending)
            # path_b matched → removed; path_a unaffected
            self.assertIn(path_a, pending)
            self.assertNotIn(path_b, pending)

    def test_save_alert_writes_resume_file_and_removes_from_pending(self) -> None:
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            pending, handle = self._make_pending(tmp)
            torrent_path = list(pending)[0]
            alert = MagicMock(spec=lt.save_resume_data_alert)
            alert.handle = handle
            alert.params = MagicMock()
            with patch('libtorrent.write_resume_data_buf', return_value=b'data'), \
                 self.assertLogs('daemon.session', level='INFO'):
                session._handle_resume_alert(alert, pending)
            self.assertNotIn(torrent_path, pending)
            with open(torrent_path.replace('.torrent', '.resume'), 'rb') as f:
                self.assertEqual(f.read(), b'data')

    def test_failed_alert_only_removes_matching_handle(self) -> None:
        """Failed alert removes only the matching handle, leaving others."""
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            handle_a = MagicMock()
            handle_b = MagicMock()
            path_a = os.path.join(tmp, 'a.torrent')
            path_b = os.path.join(tmp, 'b.torrent')
            # handle_b (second entry) is the one that matches the alert
            pending = {path_a: handle_a, path_b: handle_b}
            alert = MagicMock(spec=lt.save_resume_data_failed_alert)
            alert.handle = handle_b
            alert.torrent_name = 'b'
            with self.assertLogs('daemon.session', level='WARNING'):
                session._handle_resume_alert(alert, pending)
            self.assertIn(path_a, pending)
            self.assertNotIn(path_b, pending)

    def test_failed_alert_removes_from_pending_with_warning(self) -> None:
        session = TorrentSession()
        with tempfile.TemporaryDirectory() as tmp:
            pending, handle = self._make_pending(tmp)
            torrent_path = list(pending)[0]
            alert = MagicMock(spec=lt.save_resume_data_failed_alert)
            alert.handle = handle
            alert.torrent_name = '1'
            with self.assertLogs('daemon.session', level='WARNING'):
                session._handle_resume_alert(alert, pending)
        self.assertNotIn(torrent_path, pending)

    def test_other_alert_is_dispatched(self) -> None:
        session = TorrentSession()
        dispatched = []
        session._handle_alert = dispatched.append
        alert = MagicMock(spec=lt.alert)
        alert.message.return_value = 'info'
        session._handle_resume_alert(alert, {})
        self.assertEqual(dispatched, [alert])


class TestShutdown(unittest.IsolatedAsyncioTestCase):

    async def test_shutdown_saves_resume_data(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        mock_session = session._session

        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, '1.torrent')
            handle = MagicMock()
            handle.is_valid.return_value = True
            session._handles[torrent_path] = handle

            alert = MagicMock(spec=lt.save_resume_data_alert)
            alert.handle = handle
            alert.params = MagicMock()
            session._session.pop_alerts.side_effect = [[alert], []]

            with patch('libtorrent.write_resume_data_buf', return_value=b'resume_data'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='INFO') as log:
                    await session.shutdown()

            resume_path = torrent_path.replace('.torrent', '.resume')
            with open(resume_path, 'rb') as f:
                self.assertEqual(f.read(), b'resume_data')

        handle.save_resume_data.assert_called_once()
        mock_session.pause.assert_called_once()
        self.assertTrue(any('shutdown complete' in line for line in log.output))

    async def test_shutdown_dispatches_other_alerts(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, '1.torrent')
            handle = MagicMock()
            handle.is_valid.return_value = True
            session._handles[torrent_path] = handle

            other = MagicMock(spec=lt.alert)
            other.message.return_value = 'info'
            save_alert = MagicMock(spec=lt.save_resume_data_alert)
            save_alert.handle = handle
            save_alert.params = MagicMock()
            session._session.pop_alerts.side_effect = [[other, save_alert]]

            dispatched = []
            session._handle_alert = dispatched.append
            with patch('libtorrent.write_resume_data_buf', return_value=b'x'), \
                 patch('asyncio.sleep', new_callable=AsyncMock):
                await session.shutdown()

        self.assertEqual(len(dispatched), 1)
        self.assertIs(dispatched[0], other)

    async def test_shutdown_handles_failed_alert(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        mock_session = session._session

        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, '1.torrent')
            handle = MagicMock()
            handle.is_valid.return_value = True
            session._handles[torrent_path] = handle

            alert = MagicMock(spec=lt.save_resume_data_failed_alert)
            alert.handle = handle
            alert.torrent_name = '1'
            session._session.pop_alerts.side_effect = [[alert], []]

            with patch('asyncio.sleep', new_callable=AsyncMock):
                with self.assertLogs('daemon.session', level='WARNING') as log:
                    await session.shutdown()

        self.assertTrue(any('could not save resume data' in line for line in log.output))
        mock_session.pause.assert_called_once()

    async def test_shutdown_skips_invalid_handles(self) -> None:
        """Invalid handles are excluded from save_resume_data requests."""
        session = TorrentSession()
        session._session = MagicMock()
        session._session.pop_alerts.return_value = []

        valid = MagicMock()
        valid.is_valid.return_value = True
        invalid = MagicMock()
        invalid.is_valid.return_value = False

        session._handles['/valid.torrent'] = valid
        session._handles['/invalid.torrent'] = invalid

        with patch('asyncio.sleep', new_callable=AsyncMock):
            with self.assertLogs('daemon.session', level='INFO'):
                await session.shutdown()

        valid.save_resume_data.assert_called_once()
        invalid.save_resume_data.assert_not_called()


class TestGroupBySite(unittest.TestCase):

    def test_groups_versions_by_site_dir(self) -> None:
        session = TorrentSession()
        session._handles = {
            f'data/sites/{FAKE_NPUB}/site_a/1/site.torrent': MagicMock(),
            f'data/sites/{FAKE_NPUB}/site_a/2/site.torrent': MagicMock(),
            f'data/sites/{FAKE_NPUB}/site_b/1/site.torrent': MagicMock(),
        }
        groups = session._group_by_site()
        self.assertIn(f'data/sites/{FAKE_NPUB}/site_a', groups)
        self.assertIn(f'data/sites/{FAKE_NPUB}/site_b', groups)
        self.assertEqual(sorted(v for v, _ in groups[f'data/sites/{FAKE_NPUB}/site_a']), [1, 2])
        self.assertEqual(len(groups[f'data/sites/{FAKE_NPUB}/site_b']), 1)

    def test_ignores_non_numeric_filenames(self) -> None:
        session = TorrentSession()
        session._handles = {f'data/sites/{FAKE_NPUB}/site_a/foo/site.torrent': MagicMock()}
        self.assertEqual(session._group_by_site(), {})


class TestVersionAge(unittest.TestCase):

    def _make_handle(self, last_upload: datetime.datetime) -> MagicMock:
        handle = MagicMock()
        status = MagicMock()
        status.last_upload = last_upload
        handle.status.return_value = status
        return handle

    def test_uses_last_upload_when_set(self) -> None:
        now = datetime.datetime.now()
        handle = self._make_handle(now - datetime.timedelta(days=3))
        age = TorrentSession._version_age('irrelevant.torrent', handle, now)
        self.assertAlmostEqual(age.total_seconds(),
                               datetime.timedelta(days=3).total_seconds(), delta=1)

    def test_falls_back_to_mtime_when_never_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            torrent_path = os.path.join(tmp, '1.torrent')
            open(torrent_path, 'w').close()
            old_ts = (datetime.datetime.now() - datetime.timedelta(days=10)).timestamp()
            os.utime(torrent_path, (old_ts, old_ts))
            now = datetime.datetime.now()
            age = TorrentSession._version_age(
                torrent_path, self._make_handle(datetime.datetime(1970, 1, 1)), now,
            )
        self.assertGreater(age, datetime.timedelta(days=9))

    def test_returns_max_when_torrent_file_missing(self) -> None:
        """When the torrent file does not exist, version_age returns timedelta.max."""
        now = datetime.datetime.now()
        handle = self._make_handle(datetime.datetime(1970, 1, 1))
        age = TorrentSession._version_age('/nonexistent/path.torrent', handle, now)
        self.assertEqual(age, datetime.timedelta.max)


class TestCleanupOldVersions(unittest.TestCase):

    def _make_handle(self, last_upload: datetime.datetime | None) -> MagicMock:
        handle = MagicMock()
        handle.is_valid.return_value = True
        status = MagicMock()
        status.last_upload = last_upload
        handle.status.return_value = status
        return handle

    def _old_upload(self) -> datetime.datetime:
        return datetime.datetime.now() - KEEP_DURATION - datetime.timedelta(days=1)

    def _recent_upload(self) -> datetime.datetime:
        return datetime.datetime.now() - KEEP_DURATION / 2

    def _make_versioned_torrent(self, site_data: str, ver: int) -> str:
        ver_dir = os.path.join(site_data, str(ver))
        os.makedirs(ver_dir, exist_ok=True)
        path = os.path.join(ver_dir, f'{ver}.torrent')
        open(path, 'w').close()
        return path

    def test_removes_old_version_keeps_latest(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            session._handles[old_torrent] = self._make_handle(self._old_upload())
            session._handles[new_torrent] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        self.assertNotIn(old_torrent, session._handles)
        self.assertIn(new_torrent, session._handles)
        session._session.remove_torrent.assert_called_once()

    def test_keeps_only_version_even_if_old(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            torrent = self._make_versioned_torrent(site_data, 1)
            session._handles[torrent] = self._make_handle(self._old_upload())
            session.cleanup_old_versions()
        self.assertIn(torrent, session._handles)
        session._session.remove_torrent.assert_not_called()

    def test_keeps_version_with_recent_upload(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            session._handles[old_torrent] = self._make_handle(self._recent_upload())
            session._handles[new_torrent] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        self.assertIn(old_torrent, session._handles)
        session._session.remove_torrent.assert_not_called()

    def test_removes_never_uploaded_version_when_file_is_old(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        old_epoch = datetime.datetime(1970, 1, 1)
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            old_ts = (datetime.datetime.now() - KEEP_DURATION
                      - datetime.timedelta(days=1)).timestamp()
            os.utime(old_torrent, (old_ts, old_ts))
            session._handles[old_torrent] = self._make_handle(old_epoch)
            session._handles[new_torrent] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        self.assertNotIn(old_torrent, session._handles)
        session._session.remove_torrent.assert_called_once()

    def test_keeps_never_uploaded_version_when_file_is_recent(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        old_epoch = datetime.datetime(1970, 1, 1)
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            session._handles[old_torrent] = self._make_handle(old_epoch)
            session._handles[new_torrent] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        self.assertIn(old_torrent, session._handles)
        session._session.remove_torrent.assert_not_called()

    def test_skips_invalid_handles(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            invalid = MagicMock()
            invalid.is_valid.return_value = False
            session._handles[old_torrent] = invalid
            session._handles[new_torrent] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        session._session.remove_torrent.assert_not_called()

    def test_skips_cleanup_when_latest_not_seeding(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            old_torrent = self._make_versioned_torrent(site_data, 1)
            new_torrent = self._make_versioned_torrent(site_data, 2)
            old_handle = self._make_handle(self._old_upload())
            new_handle = self._make_handle(self._recent_upload())
            new_handle.is_seed.return_value = False
            session._handles[old_torrent] = old_handle
            session._handles[new_torrent] = new_handle
            session.cleanup_old_versions()
        self.assertIn(old_torrent, session._handles)
        session._session.remove_torrent.assert_not_called()

    def test_cap_removes_oldest_when_over_max_versions(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            torrents = {
                v: self._make_versioned_torrent(site_data, v)
                for v in range(1, MAX_VERSIONS + 2)
            }
            for v, path in torrents.items():
                session._handles[path] = self._make_handle(self._recent_upload())
            session.cleanup_old_versions()
        self.assertNotIn(torrents[1], session._handles)
        for v in range(2, MAX_VERSIONS + 2):
            self.assertIn(torrents[v], session._handles)
        session._session.remove_torrent.assert_called_once()


class TestRemoveVersion(unittest.TestCase):

    def test_removes_handle_and_files(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            version_dir = os.path.join(site_data, '1')
            os.makedirs(version_dir)
            torrent = os.path.join(version_dir, '1.torrent')
            resume = os.path.join(version_dir, '1.resume')
            txt = os.path.join(version_dir, '1.txt')
            for path in (torrent, resume, txt):
                open(path, 'w').close()
            open(os.path.join(version_dir, 'file.html'), 'w').close()
            handle = MagicMock()
            session._handles[torrent] = handle
            with self.assertLogs('daemon.session', level='INFO') as log:
                session._remove_version(torrent, handle)
        session._session.remove_torrent.assert_called_once_with(handle)
        self.assertNotIn(torrent, session._handles)
        self.assertFalse(os.path.exists(torrent))
        self.assertFalse(os.path.exists(resume))
        self.assertFalse(os.path.exists(txt))
        self.assertFalse(os.path.exists(version_dir))
        self.assertTrue(any('removed old version' in line for line in log.output))

    def test_remove_version_tolerates_missing_version_dir(self) -> None:
        session = TorrentSession()
        session._session = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            site_data = os.path.join(tmp, 'site')
            torrent = os.path.join(site_data, '1', '1.torrent')
            handle = MagicMock()
            session._handles[torrent] = handle
            with self.assertLogs('daemon.session', level='INFO'):
                session._remove_version(torrent, handle)


if __name__ == '__main__':
    unittest.main()
