import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from .httpserver import HttpServer, _fmt_size, _parse_identifier, _safe_join


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestFmtSize(unittest.TestCase):

    def test_bytes(self) -> None:
        self.assertEqual(_fmt_size(0), '0 B')
        self.assertEqual(_fmt_size(1023), '1023 B')

    def test_kilobytes(self) -> None:
        result = _fmt_size(1024)
        self.assertIn('KB', result)
        result2 = _fmt_size(1024 * 1024 - 1)
        self.assertIn('KB', result2)

    def test_megabytes(self) -> None:
        result = _fmt_size(1024 * 1024)
        self.assertIn('MB', result)
        result2 = _fmt_size(5 * 1024 * 1024)
        self.assertIn('MB', result2)


class TestParseIdentifier(unittest.TestCase):

    def test_valid(self) -> None:
        self.assertEqual(_parse_identifier('cubbies.npub1abc'), ('cubbies', 'npub1abc'))

    def test_dot_in_site_name(self) -> None:
        self.assertEqual(_parse_identifier('my.site.npub1abc'), ('my.site', 'npub1abc'))

    def test_npub1_in_site_name_splits_at_last_occurrence(self) -> None:
        # Site name itself contains '.npub1' — rfind picks the correct separator
        self.assertEqual(_parse_identifier('my.npub1thing.npub1abc'),
                         ('my.npub1thing', 'npub1abc'))

    def test_no_npub1(self) -> None:
        self.assertIsNone(_parse_identifier('cubbies.notnpub'))

    def test_empty_string(self) -> None:
        self.assertIsNone(_parse_identifier(''))


class TestSafeJoin(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normal_path(self) -> None:
        result = _safe_join(self.root, 'subdir/file.txt')
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(self.root))

    def test_traversal_returns_none(self) -> None:
        self.assertIsNone(_safe_join(self.root, '../../etc/passwd'))

    def test_root_itself(self) -> None:
        self.assertIsNotNone(_safe_join(self.root, ''))

    def test_absolute_component_returns_none(self) -> None:
        # An absolute path as rel collapses to /etc/passwd regardless of root
        self.assertIsNone(_safe_join(self.root, '/etc/passwd'))


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------

class TestHandlers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.session = MagicMock()
        self.session.sites_info.return_value = []
        self.http = HttpServer(self.data_dir, self.session)
        self.client = TestClient(TestServer(self.http._build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    def _write_site_file(self, npub: str, site: str, ver: int,
                         rel_path: str, content: str = 'hello') -> str:
        path = os.path.join(
            self.data_dir, 'sites', npub, site, str(ver), 'site', rel_path,
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def _make_torrent_stub(self, npub: str, site: str, ver: int) -> None:
        """Create an empty site.torrent marker so version is considered complete."""
        ver_dir = os.path.join(self.data_dir, 'sites', npub, site, str(ver))
        os.makedirs(ver_dir, exist_ok=True)
        open(os.path.join(ver_dir, 'site.torrent'), 'w').close()

    # --- dashboard / config UI ----------------------------------------

    async def test_dashboard_returns_200(self) -> None:
        resp = await self.client.get('/@/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('Dashboard', text)

    async def test_dashboard_lists_sites(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.abc12',
            'url_identifier': 'mysite.npub1abc12',
            'version': 1, 'state': 'seeding',
            'num_peers': 3, 'upload_rate': 2048, 'download_rate': 0,
            'disk_bytes': 1048576, 'exclusive_bytes': 524288, 'site_total_bytes': 2097152,
        }]
        resp = await self.client.get('/@/')
        text = await resp.text()
        self.assertIn('mysite.abc12', text)
        self.assertIn('seeding', text)

    async def test_config_returns_200(self) -> None:
        resp = await self.client.get('/@/config')
        self.assertEqual(resp.status, 200)

    async def test_config_shows_max_site_mb(self) -> None:
        mock_config = MagicMock()
        mock_config.max_site_mb = 42
        self.http._config = mock_config
        resp = await self.client.get('/@/config')
        text = await resp.text()
        self.assertIn('42', text)

    async def test_config_post_updates_value(self) -> None:
        mock_config = MagicMock()
        mock_config.max_site_mb = 50
        self.http._config = mock_config
        self.http._config_path = ''
        resp = await self.client.post('/@/config', data={'max_site_mb': '200'},
                                      allow_redirects=False)
        self.assertEqual(resp.status, 303)
        self.assertIn('saved=1', resp.headers['Location'])
        self.assertEqual(mock_config.max_site_mb, 200)

    async def test_config_post_invalid_value_returns_400(self) -> None:
        resp = await self.client.post('/@/config', data={'max_site_mb': 'abc'})
        self.assertEqual(resp.status, 400)

    async def test_config_post_zero_returns_400(self) -> None:
        resp = await self.client.post('/@/config', data={'max_site_mb': '0'})
        self.assertEqual(resp.status, 400)

    async def test_dashboard_site_link(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.abc12',
            'url_identifier': 'mysite.npub1abc12',
            'version': 1, 'state': 'seeding',
            'num_peers': 0, 'upload_rate': 0, 'download_rate': 0,
            'disk_bytes': 0, 'exclusive_bytes': 0, 'site_total_bytes': 0,
        }]
        resp = await self.client.get('/@/')
        text = await resp.text()
        self.assertIn('/mysite.npub1abc12/', text)

    async def test_dashboard_hides_files_link_when_downloading_metadata(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.abc12',
            'url_identifier': 'mysite.npub1abc12',
            'version': 1, 'state': 'downloading_metadata',
            'num_peers': 0, 'upload_rate': 0, 'download_rate': 0,
            'disk_bytes': 0, 'exclusive_bytes': 0, 'site_total_bytes': 0,
        }]
        resp = await self.client.get('/@/')
        text = await resp.text()
        # Extract only the server-rendered table body (before the <script> block)
        tbody = text.split('<script>')[0]
        self.assertNotIn('/@/files/', tbody)

    async def test_dashboard_shows_files_link_when_metadata_available(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.abc12',
            'url_identifier': 'mysite.npub1abc12',
            'version': 1, 'state': 'downloading',
            'num_peers': 0, 'upload_rate': 0, 'download_rate': 0,
            'disk_bytes': 0, 'exclusive_bytes': 0, 'site_total_bytes': 0,
        }]
        resp = await self.client.get('/@/')
        text = await resp.text()
        tbody = text.split('<script>')[0]
        self.assertIn('/@/files/', tbody)

    async def test_dashboard_only_latest_version_is_linked(self) -> None:
        base = dict(identifier='mysite.abc12', url_identifier='mysite.npub1abc12',
                    state='seeding', num_peers=0, upload_rate=0, download_rate=0,
                    disk_bytes=0, exclusive_bytes=0, site_total_bytes=0)
        self.session.sites_info.return_value = [
            {**base, 'version': 1},
            {**base, 'version': 2},
        ]
        resp = await self.client.get('/@/')
        text = await resp.text()
        # Link appears exactly once (for version 2 only)
        self.assertEqual(text.count('/mysite.npub1abc12/'), 1)

    # --- JSON API -----------------------------------------------------

    async def test_api_sites_returns_json(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.abc12',
            'url_identifier': 'mysite.npub1abc12',
            'version': 1, 'state': 'seeding',
            'num_peers': 2, 'upload_rate': 1024, 'download_rate': 0,
            'disk_bytes': 2097152, 'exclusive_bytes': 1048576, 'site_total_bytes': 3145728,
        }]
        resp = await self.client.get('/@/api/sites')
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, 'application/json')
        data = await resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['identifier'], 'mysite.abc12')

    # --- add site form ------------------------------------------------

    async def test_add_valid_address_calls_on_download(self) -> None:
        on_download = MagicMock()
        self.http._on_download = on_download
        from nostr_sdk import Keys, Kind, Coordinate, Nip19Coordinate, RelayUrl
        from config import NOSTR_KIND
        keys = Keys.generate()
        coord = Coordinate(Kind(NOSTR_KIND), keys.public_key(), 'mysite')
        naddr = Nip19Coordinate(coord, [RelayUrl.parse('wss://relay.damus.io')]).to_bech32()
        resp = await self.client.post('/@/add', data={'address': naddr},
                                      allow_redirects=False)
        self.assertEqual(resp.status, 303)
        on_download.assert_called_once_with('mysite', naddr)

    async def test_add_invalid_address_redirects_silently(self) -> None:
        on_download = MagicMock()
        self.http._on_download = on_download
        resp = await self.client.post('/@/add', data={'address': 'not-an-address'},
                                      allow_redirects=False)
        self.assertEqual(resp.status, 303)
        on_download.assert_not_called()

    async def test_add_without_callback_redirects(self) -> None:
        resp = await self.client.post('/@/add', data={'address': 'peerpage://x.npub1abc'},
                                      allow_redirects=False)
        self.assertEqual(resp.status, 303)

    # --- redirect: no trailing slash ----------------------------------

    async def test_no_trailing_slash_redirects(self) -> None:
        resp = await self.client.get('/mysite.npub1abc',
                                     allow_redirects=False)
        self.assertEqual(resp.status, 301)
        self.assertIn('/mysite.npub1abc/', resp.headers['Location'])

    # --- site not available -------------------------------------------

    async def test_site_with_no_version_returns_503(self) -> None:
        resp = await self.client.get('/nosite.npub1abc/')
        self.assertEqual(resp.status, 503)

    async def test_unknown_identifier_returns_404(self) -> None:
        resp = await self.client.get('/bad-identifier/')
        self.assertEqual(resp.status, 404)

    # --- file serving -------------------------------------------------

    async def test_serves_existing_file(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', '<h1>hi</h1>')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/index.html')
        self.assertEqual(resp.status, 200)
        self.assertIn('html', resp.content_type)
        text = await resp.text()
        self.assertIn('<h1>hi</h1>', text)

    async def test_serves_index_html_for_root_dir(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'root index')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('root index', text)

    async def test_ls_query_forces_dir_listing_over_index(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'root index')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/?ls')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertNotIn('root index', text)
        self.assertIn('index.html', text)

    async def test_ls_propagates_to_subdir_links(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/page.html', 'x')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/?ls')
        text = await resp.text()
        self.assertIn('sub/?ls', text)

    async def test_ls_propagates_to_parent_link(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/page.html', 'x')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/sub/?ls')
        text = await resp.text()
        self.assertIn('../?ls', text)

    async def test_ls_absent_no_qs_in_links(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/page.html', 'x')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/')
        text = await resp.text()
        self.assertNotIn('?ls', text)

    async def test_serves_index_html_for_subdir(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/index.html', 'sub index')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/sub/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('sub index', text)

    async def test_directory_without_trailing_slash_redirects(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/index.html', 'x')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/sub',
                                     allow_redirects=False)
        self.assertEqual(resp.status, 301)
        self.assertTrue(resp.headers['Location'].endswith('/sub/'))

    async def test_serves_latest_when_multiple_versions(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'v1')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self._write_site_file('npub1abc', 'mysite', 2, 'index.html', 'v2')
        self._make_torrent_stub('npub1abc', 'mysite', 2)
        resp = await self.client.get('/mysite.npub1abc/')
        text = await resp.text()
        self.assertIn('v2', text)

    async def test_directory_listing_when_no_index(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'docs/readme.txt', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/docs/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('readme.txt', text)

    async def test_directory_listing_has_parent_link(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'docs/readme.txt', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/docs/')
        text = await resp.text()
        self.assertIn('../', text)

    async def test_root_listing_no_parent_link(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'file.txt', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertNotIn('../', text)
        self.assertIn('file.txt', text)

    async def test_directory_listing_shows_subdir(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'sub/file.txt', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.get('/mysite.npub1abc/')
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('sub/', text)

    # --- file selector UI -------------------------------------------

    async def test_files_returns_200(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.file_list.return_value = (
            [{'index': 0, 'path': 'site/index.html', 'size': 2, 'priority': 1}],
            1,
        )
        resp = await self.client.get('/@/files/mysite.npub1abc')
        self.assertEqual(resp.status, 200)

    async def test_files_returns_404_when_no_version(self) -> None:
        resp = await self.client.get('/@/files/mysite.npub1abc')
        self.assertEqual(resp.status, 404)

    async def test_files_returns_503_when_file_list_none(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.file_list.return_value = None
        resp = await self.client.get('/@/files/mysite.npub1abc')
        self.assertEqual(resp.status, 503)

    async def test_files_returns_404_for_bad_identifier(self) -> None:
        resp = await self.client.get('/@/files/no-npub-here')
        self.assertEqual(resp.status, 404)

    # --- file selector API ------------------------------------------

    async def test_api_files_returns_json(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.file_list.return_value = (
            [{'index': 0, 'path': 'site/index.html', 'size': 2, 'priority': 1}],
            1,
        )
        resp = await self.client.get('/@/api/files/mysite.npub1abc')
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data['identifier'], 'mysite.npub1abc')
        self.assertEqual(len(data['files']), 1)
        self.assertEqual(data['total_files'], 1)

    async def test_api_files_returns_404_when_no_version(self) -> None:
        resp = await self.client.get('/@/api/files/mysite.npub1abc')
        self.assertEqual(resp.status, 404)

    async def test_api_files_returns_404_when_file_list_none(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.file_list.return_value = None
        resp = await self.client.get('/@/api/files/mysite.npub1abc')
        self.assertEqual(resp.status, 404)

    async def test_api_priority_returns_ok(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.set_file_priorities.return_value = True
        resp = await self.client.post(
            '/@/api/priority/mysite.npub1abc',
            json={'priorities': [1, 0]},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data['ok'])

    async def test_api_priority_returns_400_for_missing_key(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        resp = await self.client.post(
            '/@/api/priority/mysite.npub1abc',
            json={'wrong_key': [1]},
        )
        self.assertEqual(resp.status, 400)

    async def test_api_priority_returns_404_when_set_fails(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        self.session.set_file_priorities.return_value = False
        resp = await self.client.post(
            '/@/api/priority/mysite.npub1abc',
            json={'priorities': [1]},
        )
        self.assertEqual(resp.status, 404)

    async def test_api_priority_returns_404_for_unknown_site(self) -> None:
        resp = await self.client.post(
            '/@/api/priority/mysite.npub1abc',
            json={'priorities': [1]},
        )
        self.assertEqual(resp.status, 404)

    async def test_config_post_saves_to_disk_when_config_path_set(self) -> None:
        import tempfile as _tempfile
        mock_config = MagicMock()
        mock_config.max_site_mb = 50
        self.http._config = mock_config
        with _tempfile.NamedTemporaryFile(suffix='.yml', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self.http._config_path = tmp_path
            with patch('daemon.httpserver.cfg_module.save') as mock_save:
                resp = await self.client.post('/@/config', data={'max_site_mb': '100'},
                                              allow_redirects=False)
            self.assertEqual(resp.status, 303)
            mock_save.assert_called_once_with(mock_config, tmp_path)
        finally:
            os.unlink(tmp_path)

    # --- delete API ---------------------------------------------------

    async def test_api_delete_removes_site_dir(self) -> None:
        self._write_site_file('npub1abc', 'mysite', 1, 'index.html', 'hi')
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        site_dir = os.path.join(self.data_dir, 'sites', 'npub1abc', 'mysite')
        self.assertTrue(os.path.isdir(site_dir))
        resp = await self.client.post('/@/api/delete/mysite.npub1abc')
        self.assertEqual(resp.status, 200)
        self.assertFalse(os.path.isdir(site_dir))
        self.session.stop_site.assert_called_once_with(site_dir)

    async def test_api_delete_returns_404_for_unknown_site(self) -> None:
        resp = await self.client.post('/@/api/delete/mysite.npub1abc')
        self.assertEqual(resp.status, 404)

    async def test_api_delete_returns_404_for_bad_identifier(self) -> None:
        resp = await self.client.post('/@/api/delete/no-npub-here')
        self.assertEqual(resp.status, 404)

    async def test_dashboard_row_has_delete_link_for_latest(self) -> None:
        self.session.sites_info.return_value = [{
            'identifier': 'mysite.np1ab',
            'url_identifier': 'mysite.npub1abc',
            'version': 1,
            'state': 'seeding',
            'upload_rate': 0,
            'download_rate': 0,
            'total_upload': 0,
            'disk_bytes': 0,
            'exclusive_bytes': 0,
            'site_total_bytes': 0,
            'num_peers': 0,
        }]
        resp = await self.client.get('/@/')
        text = await resp.text()
        tbody = text.split('<script>')[0]
        self.assertIn('deleteSite', tbody)

    # ------------------------------------------------------------------

    async def test_path_traversal_returns_400(self) -> None:
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        # Create the version/site dir so resolve_version succeeds
        site_dir = os.path.join(self.data_dir, 'sites', 'npub1abc', 'mysite', '1', 'site')
        os.makedirs(site_dir, exist_ok=True)
        resp = await self.client.get('/mysite.npub1abc/../../secret')
        # aiohttp normalises the URL before routing, so traversal is blocked early
        self.assertIn(resp.status, (400, 404))

    async def test_missing_file_not_in_torrent_returns_404(self) -> None:
        self._make_torrent_stub('npub1abc', 'mysite', 1)
        site_dir = os.path.join(self.data_dir, 'sites', 'npub1abc', 'mysite', '1', 'site')
        os.makedirs(site_dir, exist_ok=True)
        # No real torrent, so torrent_manifest will fail → 404
        resp = await self.client.get('/mysite.npub1abc/ghost.html')
        self.assertEqual(resp.status, 404)


if __name__ == '__main__':
    unittest.main()
