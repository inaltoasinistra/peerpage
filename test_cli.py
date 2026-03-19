import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

from nostr_sdk import Keys

import config as cfg_module
from config import Config, NostrConfig
from cli import (
    _delete_site, _download_from_file, _follow_npub, _publish_site,
    _queue_address_download, _read_changelog, _request_download,
    _resolve_site, _sites, _stop_daemon, main,
    DATA_DIR, SITES_DIR, HTTP_BASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(npub: str = '', relays: list[str] | None = None,
                 followed: list[str] | None = None) -> Config:
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    if not npub:
        npub = keys.public_key().to_bech32()
    return Config(nostr=NostrConfig(
        private_key=nsec,
        relays=relays or [],
        public_key=npub,
        followed=list(followed or []),
    ))


def _make_site_data(data_dir: str, npub: str, site_name: str) -> str:
    site_data_dir = os.path.join(data_dir, 'sites', npub, site_name)
    os.makedirs(site_data_dir, exist_ok=True)
    with open(os.path.join(site_data_dir, '1.torrent'), 'wb') as f:
        f.write(b'fake')
    return site_data_dir


# ---------------------------------------------------------------------------
# _resolve_site
# ---------------------------------------------------------------------------

class TestResolveSite(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, 'data')

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _patch_data_dir(self):
        return patch('cli.DATA_DIR', self.data_dir)

    def test_resolves_by_plain_name(self) -> None:
        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        with self._patch_data_dir():
            result_npub, result_name = _resolve_site('mysite')
        self.assertEqual(result_npub, npub)
        self.assertEqual(result_name, 'mysite')

    def test_resolves_by_npub5(self) -> None:
        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        arg = f'mysite.{npub[-5:]}'
        with self._patch_data_dir():
            result_npub, result_name = _resolve_site(arg)
        self.assertEqual(result_npub, npub)
        self.assertEqual(result_name, 'mysite')

    def test_resolves_by_peerpage_address(self) -> None:
        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        address = f'peerpage://mysite.{npub}'
        with self._patch_data_dir():
            result_npub, result_name = _resolve_site(address)
        self.assertEqual(result_npub, npub)
        self.assertEqual(result_name, 'mysite')

    def test_raises_when_not_found(self) -> None:
        os.makedirs(os.path.join(self.data_dir, 'sites'), exist_ok=True)
        with self._patch_data_dir():
            with self.assertRaises(ValueError) as ctx:
                _resolve_site('nosuchsite')
        self.assertIn('not found', str(ctx.exception))

    def test_raises_when_ambiguous(self) -> None:
        npub1 = Keys.generate().public_key().to_bech32()
        npub2 = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub1, 'mysite')
        _make_site_data(self.data_dir, npub2, 'mysite')
        with self._patch_data_dir():
            with self.assertRaises(ValueError) as ctx:
                _resolve_site('mysite')
        self.assertIn('ambiguous', str(ctx.exception))

    def test_npub5_disambiguates(self) -> None:
        npub1 = Keys.generate().public_key().to_bech32()
        npub2 = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub1, 'mysite')
        _make_site_data(self.data_dir, npub2, 'mysite')
        with self._patch_data_dir():
            result_npub, _ = _resolve_site(f'mysite.{npub1[-5:]}')
        self.assertEqual(result_npub, npub1)

    def test_raises_when_sites_base_missing(self) -> None:
        with patch('cli.DATA_DIR', '/nonexistent/path'):
            with self.assertRaises(ValueError) as ctx:
                _resolve_site('mysite')
        self.assertIn('no sites found', str(ctx.exception))


# ---------------------------------------------------------------------------
# _read_changelog
# ---------------------------------------------------------------------------

class TestReadChangelog(unittest.TestCase):

    def test_returns_raw_content(self) -> None:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('hello\nworld\n')
            path = f.name
        try:
            self.assertEqual(_read_changelog(path), 'hello\nworld\n')
        finally:
            os.unlink(path)

    def test_strips_magnet_header(self) -> None:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('magnet: magnet:?xt=...\n\nactual content\n')
            path = f.name
        try:
            self.assertEqual(_read_changelog(path), 'actual content\n')
        finally:
            os.unlink(path)

    def test_strips_magnet_header_with_no_body(self) -> None:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('magnet: magnet:?xt=...\n')
            path = f.name
        try:
            self.assertEqual(_read_changelog(path), '')
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _stop_daemon
# ---------------------------------------------------------------------------

class TestStopDaemon(unittest.TestCase):

    def test_prints_stopped_on_success(self) -> None:
        with patch('cli._http_post', return_value={'ok': True}), \
             patch('builtins.print') as mock_print:
            _stop_daemon()
        mock_print.assert_called_once_with('daemon stopped')

    def test_exits_when_daemon_not_running(self) -> None:
        with patch('cli._http_post', side_effect=urllib.error.URLError('refused')):
            with self.assertRaises(SystemExit):
                _stop_daemon()

    def test_posts_to_correct_endpoint(self) -> None:
        calls = []
        with patch('cli._http_post', side_effect=lambda p, d: calls.append(p) or {'ok': True}), \
             patch('builtins.print'):
            _stop_daemon()
        self.assertEqual(calls, ['/@/api/stop'])


# ---------------------------------------------------------------------------
# _sites
# ---------------------------------------------------------------------------

class TestSites(unittest.TestCase):

    def test_prints_sites_json(self) -> None:
        payload = [{'identifier': 'foo', 'version': 1}]
        with patch('cli._http_get', return_value=payload), \
             patch('builtins.print') as mock_print:
            _sites()
        output = mock_print.call_args[0][0]
        self.assertEqual(json.loads(output), payload)

    def test_exits_when_daemon_not_running(self) -> None:
        with patch('cli._http_get', side_effect=urllib.error.URLError('refused')):
            with self.assertRaises(SystemExit):
                _sites()

    def test_gets_correct_endpoint(self) -> None:
        calls = []
        with patch('cli._http_get', side_effect=lambda p: calls.append(p) or []), \
             patch('builtins.print'):
            _sites()
        self.assertEqual(calls, ['/@/api/sites'])


# ---------------------------------------------------------------------------
# _request_download / _queue_address_download
# ---------------------------------------------------------------------------

class TestRequestDownload(unittest.TestCase):

    def test_posts_address_to_api(self) -> None:
        posted = []
        def fake_post(path, data):
            posted.append((path, data))
            return {'ok': True}
        with patch('cli._http_post', side_effect=fake_post):
            _request_download('mysite', 'peerpage://mysite.npub1abc')
        self.assertEqual(posted, [('/@/api/add', {'address': 'peerpage://mysite.npub1abc'})])

    def test_exits_on_error_response(self) -> None:
        with patch('cli._http_post', return_value={'error': 'something went wrong'}):
            with self.assertRaises(SystemExit):
                _request_download('mysite', 'peerpage://mysite.npub1abc')

    def test_exits_when_daemon_not_running(self) -> None:
        with patch('cli._http_post', side_effect=urllib.error.URLError('refused')):
            with self.assertRaises(SystemExit):
                _request_download('mysite', 'peerpage://mysite.npub1abc')


class TestQueueAddressDownload(unittest.TestCase):

    def test_prints_queued_message(self) -> None:
        with patch('cli._request_download'), \
             patch('builtins.print') as mock_print:
            _queue_address_download('mysite', 'peerpage://mysite.npub1abc')
        mock_print.assert_called_once_with('mysite: download queued')

    def test_delegates_to_request_download(self) -> None:
        calls = []
        with patch('cli._request_download', side_effect=lambda s, a: calls.append((s, a))), \
             patch('builtins.print'):
            _queue_address_download('mysite', 'peerpage://mysite.npub1abc')
        self.assertEqual(calls, [('mysite', 'peerpage://mysite.npub1abc')])


# ---------------------------------------------------------------------------
# _follow_npub
# ---------------------------------------------------------------------------

class TestFollowNpub(unittest.TestCase):

    def test_adds_npub_to_followed_list(self) -> None:
        cfg = _make_config()
        with patch('cli.cfg_module.load', return_value=cfg), \
             patch('cli.cfg_module.save') as mock_save, \
             patch('builtins.print'):
            _follow_npub('npub1newone')
        self.assertIn('npub1newone', cfg.nostr.followed)
        mock_save.assert_called_once_with(cfg)

    def test_prints_now_following(self) -> None:
        cfg = _make_config()
        with patch('cli.cfg_module.load', return_value=cfg), \
             patch('cli.cfg_module.save'), \
             patch('builtins.print') as mock_print:
            _follow_npub('npub1newone')
        mock_print.assert_called_once_with('npub1newone: now following')

    def test_already_followed_prints_message_and_skips_save(self) -> None:
        cfg = _make_config(followed=['npub1existing'])
        with patch('cli.cfg_module.load', return_value=cfg), \
             patch('cli.cfg_module.save') as mock_save, \
             patch('builtins.print') as mock_print:
            _follow_npub('npub1existing')
        mock_save.assert_not_called()
        mock_print.assert_called_once_with('npub1existing: already followed')


# ---------------------------------------------------------------------------
# _publish_site
# ---------------------------------------------------------------------------

class TestPublishSite(unittest.TestCase):

    def _mock_site(self, create_return=True, create_raises=None,
                   version=1, magnet='magnet:?xt=test', data_path='/tmp/data'):
        site = MagicMock()
        if create_raises:
            site.create.side_effect = create_raises
        else:
            site.create.return_value = create_return
        site.version = version
        site.magnet_uri = magnet
        site.data_path = data_path
        return site

    def test_prints_no_changes_when_unchanged(self) -> None:
        site = self._mock_site(create_return=False, version=3)
        cfg = _make_config()
        with patch('cli.Site', return_value=site), \
             patch('cli.cfg_module.load', return_value=cfg), \
             patch('builtins.print') as mock_print:
            _publish_site('mysite')
        output = mock_print.call_args[0][0]
        self.assertIn('no changes', output)
        self.assertIn('3', output)

    def test_exits_on_empty_source(self) -> None:
        site = self._mock_site(create_raises=ValueError('source folder is empty: /foo'))
        cfg = _make_config()
        with patch('cli.Site', return_value=site), \
             patch('cli.cfg_module.load', return_value=cfg):
            with self.assertRaises(SystemExit):
                _publish_site('mysite')

    def test_prints_version_created_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = os.path.join(tmp, 'data')
            ver_dir = os.path.join(data_path, '1')
            os.makedirs(ver_dir)
            changelog = os.path.join(ver_dir, 'changelog.txt')
            with open(changelog, 'w') as f:
                f.write('added index.html\n')

            site = self._mock_site(version=1, data_path=data_path)
            cfg = _make_config()
            nostr_client = MagicMock()
            nostr_client.publish = MagicMock(return_value=None)
            nostr_client.site_address.return_value = 'peerpage://mysite.npub1abc'
            nostr_client.naddr_address.return_value = 'naddr1abc'

            with patch('cli.Site', return_value=site), \
                 patch('cli.cfg_module.load', return_value=cfg), \
                 patch('cli.NostrClient', return_value=nostr_client), \
                 patch('builtins.print') as mock_print:
                import asyncio
                with patch('cli.asyncio.run', return_value=None):
                    _publish_site('mysite')

            printed = [c[0][0] for c in mock_print.call_args_list]
            self.assertTrue(any('version 1 created' in s for s in printed))
            self.assertTrue(any('peerpage://mysite.npub1abc' in s for s in printed))

    def test_writes_event_json_when_event_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_path = os.path.join(tmp, 'data')
            ver_dir = os.path.join(data_path, '1')
            os.makedirs(ver_dir)
            with open(os.path.join(ver_dir, 'changelog.txt'), 'w') as f:
                f.write('')

            site = self._mock_site(version=1, data_path=data_path)
            cfg = _make_config()
            event = {'id': 'abc123', 'kind': 30078}
            nostr_client = MagicMock()
            nostr_client.publish = MagicMock(return_value=event)
            nostr_client.site_address.return_value = 'peerpage://mysite.npub1abc'
            nostr_client.naddr_address.return_value = 'naddr1abc'

            with patch('cli.Site', return_value=site), \
                 patch('cli.cfg_module.load', return_value=cfg), \
                 patch('cli.NostrClient', return_value=nostr_client), \
                 patch('builtins.print'), \
                 patch('cli.asyncio.run', return_value=event):
                _publish_site('mysite')

            event_path = os.path.join(ver_dir, 'event.json')
            self.assertTrue(os.path.exists(event_path))
            with open(event_path) as f:
                self.assertEqual(json.load(f), event)


# ---------------------------------------------------------------------------
# _delete_site
# ---------------------------------------------------------------------------

class TestDeleteSite(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, 'data')
        self.sites_dir = os.path.join(self.tmp, 'sites_src')
        os.makedirs(self.sites_dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_deletes_data_dir_when_daemon_not_running(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        site_data_dir = _make_site_data(self.data_dir, npub, 'mysite')
        cfg = _make_config(npub='npub1other')

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', side_effect=urllib.error.URLError('refused')), \
             patch('cli.cfg_module.load', return_value=cfg):
            _delete_site('mysite')

        self.assertFalse(os.path.isdir(site_data_dir))

    def test_deletes_source_dir_for_own_site(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        source_dir = os.path.join(self.sites_dir, 'mysite')
        os.makedirs(source_dir)
        cfg = _make_config(npub=npub)

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', side_effect=urllib.error.URLError('refused')), \
             patch('cli.cfg_module.load', return_value=cfg):
            _delete_site('mysite')

        self.assertFalse(os.path.isdir(source_dir))

    def test_does_not_delete_source_dir_for_foreign_site(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        source_dir = os.path.join(self.sites_dir, 'mysite')
        os.makedirs(source_dir)
        cfg = _make_config(npub='npub1other')

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', side_effect=urllib.error.URLError('refused')), \
             patch('cli.cfg_module.load', return_value=cfg):
            _delete_site('mysite')

        self.assertTrue(os.path.isdir(source_dir))

    def test_graceful_when_daemon_not_running(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        cfg = _make_config(npub='npub1other')

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', side_effect=urllib.error.URLError('refused')), \
             patch('cli.cfg_module.load', return_value=cfg):
            _delete_site('mysite')  # must not raise

    def test_exits_when_site_not_found(self) -> None:
        with patch('cli.DATA_DIR', self.data_dir):
            with self.assertRaises(SystemExit):
                _delete_site('nosuchsite')

    def test_notifies_daemon_when_running(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        cfg = _make_config(npub='npub1other')
        called_paths = []

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', side_effect=lambda p, d: called_paths.append(p) or {'ok': True}), \
             patch('cli.cfg_module.load', return_value=cfg):
            _delete_site('mysite')

        self.assertTrue(any(f'mysite.{npub}' in p for p in called_paths))

    def test_prints_deleted(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        _make_site_data(self.data_dir, npub, 'mysite')
        cfg = _make_config(npub='npub1other')

        with patch('cli.DATA_DIR', self.data_dir), \
             patch('cli.SITES_DIR', self.sites_dir), \
             patch('cli._http_post', return_value={'ok': True}), \
             patch('cli.cfg_module.load', return_value=cfg), \
             patch('builtins.print') as mock_print:
            _delete_site('mysite')

        mock_print.assert_called_once_with('mysite: deleted')


# ---------------------------------------------------------------------------
# _download_from_file
# ---------------------------------------------------------------------------

class TestDownloadFromFile(unittest.TestCase):

    def _write_file(self, lines: list[str]) -> str:
        f = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
        f.write('\n'.join(lines) + '\n')
        f.close()
        return f.name

    def _valid_address(self) -> str:
        npub = Keys.generate().public_key().to_bech32()
        return f'peerpage://mysite.{npub}'

    def test_queues_valid_addresses(self) -> None:
        addr1 = self._valid_address()
        addr2 = self._valid_address()
        path = self._write_file([addr1, addr2])
        queued = []
        try:
            with patch('cli._queue_address_download',
                       side_effect=lambda s, a: queued.append(a)), \
                 patch('builtins.print'):
                _download_from_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(sorted(queued), sorted([addr1, addr2]))

    def test_skips_comments_and_blank_lines(self) -> None:
        addr = self._valid_address()
        path = self._write_file(['# comment', '', addr, '  '])
        queued = []
        try:
            with patch('cli._queue_address_download',
                       side_effect=lambda s, a: queued.append(a)), \
                 patch('builtins.print'):
                _download_from_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(queued, [addr])

    def test_skips_invalid_address_with_warning(self) -> None:
        path = self._write_file(['not-an-address'])
        try:
            with patch('cli._queue_address_download') as mock_q, \
                 patch('builtins.print'):
                _download_from_file(path)
        finally:
            os.unlink(path)
        mock_q.assert_not_called()

    def test_prints_queued_count(self) -> None:
        addr = self._valid_address()
        path = self._write_file([addr])
        try:
            with patch('cli._queue_address_download'), \
                 patch('builtins.print') as mock_print:
                _download_from_file(path)
        finally:
            os.unlink(path)
        output = mock_print.call_args[0][0]
        self.assertIn('1 address', output)

    def test_prints_skipped_count_when_some_invalid(self) -> None:
        addr = self._valid_address()
        path = self._write_file([addr, 'bad-address'])
        try:
            with patch('cli._queue_address_download'), \
                 patch('builtins.print') as mock_print:
                _download_from_file(path)
        finally:
            os.unlink(path)
        output = mock_print.call_args[0][0]
        self.assertIn('1 skipped', output)

    def test_exits_when_file_not_found(self) -> None:
        with self.assertRaises(SystemExit):
            _download_from_file('/nonexistent/path.txt')


# ---------------------------------------------------------------------------
# main() routing
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def _run_main(self, argv: list[str]) -> None:
        with patch('sys.argv', argv):
            main()

    def test_stop_command(self) -> None:
        with patch('cli._stop_daemon') as mock_fn:
            self._run_main(['cli.py', 'stop'])
        mock_fn.assert_called_once_with()

    def test_sites_command(self) -> None:
        with patch('cli._sites') as mock_fn:
            self._run_main(['cli.py', 'sites'])
        mock_fn.assert_called_once_with()

    def test_follow_command(self) -> None:
        with patch('cli._follow_npub') as mock_fn:
            self._run_main(['cli.py', 'follow', 'npub1abc'])
        mock_fn.assert_called_once_with('npub1abc')

    def test_delete_command(self) -> None:
        with patch('cli._delete_site') as mock_fn:
            self._run_main(['cli.py', 'delete', 'mysite'])
        mock_fn.assert_called_once_with('mysite')

    def test_publish_site_for_plain_name(self) -> None:
        with patch('cli._publish_site') as mock_fn:
            self._run_main(['cli.py', 'mysite'])
        mock_fn.assert_called_once_with('mysite')

    def test_address_arg_queues_download(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        address = f'peerpage://mysite.{npub}'
        with patch('cli._queue_address_download') as mock_fn:
            self._run_main(['cli.py', address])
        mock_fn.assert_called_once_with('mysite', address)

    def test_site_plus_address_queues_download(self) -> None:
        npub = Keys.generate().public_key().to_bech32()
        address = f'peerpage://mysite.{npub}'
        with patch('cli._queue_address_download') as mock_fn:
            self._run_main(['cli.py', 'mysite', address])
        mock_fn.assert_called_once_with('mysite', address)

    def test_at_file_arg_downloads_from_file(self) -> None:
        with patch('cli._download_from_file') as mock_fn:
            self._run_main(['cli.py', '@/some/file.txt'])
        mock_fn.assert_called_once_with('/some/file.txt')

    def test_no_args_exits_with_usage(self) -> None:
        with self.assertRaises(SystemExit):
            self._run_main(['cli.py'])

    def test_unknown_command_exits_with_usage(self) -> None:
        with self.assertRaises(SystemExit):
            self._run_main(['cli.py', 'foo', 'bar', 'baz'])


if __name__ == '__main__':
    unittest.main()
