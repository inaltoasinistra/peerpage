import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import libtorrent as lt

from publisher import Site


FAKE_NPUB = 'npub1testfake'


def content_hash(magnet_uri: str) -> str:
    match = re.search(r'urn:bt(?:ih|mh):([a-fA-F0-9]+)', magnet_uri)
    return match.group(1).lower() if match else ''


class TestSite(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.source = os.path.join(self.sites_dir, 'testsite')
        os.makedirs(self.source)
        self._write('a.txt', 'content a')
        self._write('b.txt', 'content b')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _site(self) -> Site:
        return Site('testsite', sites_dir=self.sites_dir, data_dir=self.data_dir,
                    npub=FAKE_NPUB)

    def _write(self, name: str, content: str) -> None:
        with open(os.path.join(self.source, name), 'w') as f:
            f.write(content)

    def _snapshot(self, version: int, filename: str) -> str:
        return os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'testsite', str(version), 'site', filename)

    def _torrent_file(self, version: int) -> str:
        return os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'testsite', str(version), 'site.torrent')

    def test_first_create_produces_version_1(self) -> None:
        t = self._site()
        t.create()
        self.assertEqual(t.version, 1)
        self.assertIsNotNone(t.magnet_uri)
        self.assertTrue(os.path.exists(self._torrent_file(1)))

    def test_no_changes_reuses_magnet_uri(self) -> None:
        t1 = self._site()
        t1.create()

        t2 = self._site()
        t2.create()
        self.assertEqual(t2.version, 1)
        self.assertFalse(os.path.exists(self._torrent_file(2)))
        self.assertEqual(content_hash(t2.magnet_uri), content_hash(t1.magnet_uri))

    def test_changed_source_creates_new_version(self) -> None:
        t1 = self._site()
        t1.create()

        self._write('a.txt', 'modified')
        t2 = self._site()
        t2.create()

        self.assertEqual(t2.version, 2)
        self.assertNotEqual(content_hash(t2.magnet_uri), content_hash(t1.magnet_uri))
        self.assertTrue(os.path.exists(self._torrent_file(2)))

    def test_unchanged_files_are_hard_linked(self) -> None:
        self._site().create()

        self._write('a.txt', 'modified')
        self._site().create()

        inode = lambda ver, name: os.stat(self._snapshot(ver, name)).st_ino

        # b.txt unchanged — must share inode across versions
        self.assertEqual(inode(1, 'b.txt'), inode(2, 'b.txt'))

        # a.txt changed — must have a new inode
        self.assertNotEqual(inode(1, 'a.txt'), inode(2, 'a.txt'))

    def test_changelog_is_written(self) -> None:
        self._site().create()

        self._write('b.txt', 'modified')
        self._write('c.txt', 'new file')
        os.remove(os.path.join(self.source, 'a.txt'))
        t2 = self._site()
        t2.create()

        with open(self._torrent_file(2).replace('site.torrent', 'changelog.txt')) as f:
            changelog = f.read()
        self.assertIn('magnet:', changelog)
        self.assertIn(t2.magnet_uri, changelog)
        self.assertIn('new files:', changelog)
        self.assertIn('c.txt', changelog)
        self.assertIn('modified files:', changelog)
        self.assertIn('b.txt', changelog)
        self.assertIn('deleted files:', changelog)
        self.assertIn('a.txt', changelog)

    def test_finalize_download_writes_changelog(self) -> None:
        self._write('a.txt', 'content')
        self._site().create()

        snapshot_v2 = os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'testsite', '2', 'site')
        os.makedirs(snapshot_v2)
        with open(os.path.join(snapshot_v2, 'b.txt'), 'w') as f:
            f.write('new content')

        magnet = 'magnet:?xt=urn:btih:xyz'
        self._site().finalize_download(2, 1, magnet)

        with open(os.path.join(self.data_dir, 'sites', FAKE_NPUB, 'testsite', '2', 'changelog.txt')) as f:
            changelog = f.read()
        self.assertIn(magnet, changelog)
        self.assertIn('b.txt', changelog)  # new file
        self.assertIn('a.txt', changelog)  # deleted file


class TestSnapshotFile(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sites_dir = os.path.join(self.tmp.name, 'sites')
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.source = os.path.join(self.sites_dir, 'site')
        os.makedirs(self.source)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _site(self) -> Site:
        return Site('site', sites_dir=self.sites_dir, data_dir=self.data_dir, npub=FAKE_NPUB)

    def test_copies_new_file(self) -> None:
        with open(os.path.join(self.source, 'f.txt'), 'w') as f:
            f.write('hello')
        dest_dir = os.path.join(self.tmp.name, 'dest')
        os.makedirs(dest_dir)
        self._site()._snapshot_file('f.txt', 'any_hash', dest_dir, None, {})
        self.assertTrue(os.path.exists(os.path.join(dest_dir, 'f.txt')))

    def test_hard_links_unchanged_file(self) -> None:
        with open(os.path.join(self.source, 'f.txt'), 'w') as f:
            f.write('hello')
        prev_dir = os.path.join(self.tmp.name, 'prev')
        os.makedirs(prev_dir)
        with open(os.path.join(prev_dir, 'f.txt'), 'w') as f:
            f.write('hello')
        dest_dir = os.path.join(self.tmp.name, 'dest')
        os.makedirs(dest_dir)
        file_hash = Site._hash_file(os.path.join(self.source, 'f.txt'))
        self._site()._snapshot_file('f.txt', file_hash, dest_dir, prev_dir, {'f.txt': file_hash})
        src_inode = os.stat(os.path.join(prev_dir, 'f.txt')).st_ino
        dst_inode = os.stat(os.path.join(dest_dir, 'f.txt')).st_ino
        self.assertEqual(src_inode, dst_inode)

    def test_copies_changed_file_instead_of_linking(self) -> None:
        with open(os.path.join(self.source, 'f.txt'), 'w') as f:
            f.write('new content')
        prev_dir = os.path.join(self.tmp.name, 'prev')
        os.makedirs(prev_dir)
        with open(os.path.join(prev_dir, 'f.txt'), 'w') as f:
            f.write('old content')
        dest_dir = os.path.join(self.tmp.name, 'dest')
        os.makedirs(dest_dir)
        new_hash = Site._hash_file(os.path.join(self.source, 'f.txt'))
        old_hash = Site._hash_file(os.path.join(prev_dir, 'f.txt'))
        self._site()._snapshot_file('f.txt', new_hash, dest_dir, prev_dir, {'f.txt': old_hash})
        src_inode = os.stat(os.path.join(prev_dir, 'f.txt')).st_ino
        dst_inode = os.stat(os.path.join(dest_dir, 'f.txt')).st_ino
        self.assertNotEqual(src_inode, dst_inode)


class TestReuseExistingVersion(unittest.TestCase):

    def test_sets_version_and_magnet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sites_dir = os.path.join(tmp, 'sites')
            data_dir = os.path.join(tmp, 'data')
            os.makedirs(os.path.join(sites_dir, 'site'))
            with open(os.path.join(sites_dir, 'site', 'f.txt'), 'w') as f:
                f.write('hello')
            t = Site('site', sites_dir=sites_dir, data_dir=data_dir, npub=FAKE_NPUB)
            t.create()
            torrent_path = t.torrent_path
            magnet = t.magnet_uri

            t2 = Site('site', sites_dir=sites_dir, data_dir=data_dir, npub=FAKE_NPUB)
            t2._reuse_existing_version(1)
            self.assertEqual(t2.version, 1)
            self.assertEqual(t2.torrent_path, torrent_path)
            # compare content hash only — tracker order in URI is non-deterministic
            self.assertEqual(content_hash(t2.magnet_uri), content_hash(magnet))


class TestPublishNewVersion(unittest.TestCase):

    def test_delegates_to_snapshot_build_and_changelog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sites_dir = os.path.join(tmp, 'sites')
            data_dir = os.path.join(tmp, 'data')
            os.makedirs(os.path.join(sites_dir, 'site'))
            t = Site('site', sites_dir=sites_dir, data_dir=data_dir, npub=FAKE_NPUB)
            with patch.object(t, '_snapshot') as mock_snap, \
                 patch.object(t, '_build_torrent') as mock_build, \
                 patch.object(t, '_create_changelog') as mock_log:
                t._publish_new_version({'f.txt': 'abc'}, None)
            mock_snap.assert_called_once()
            mock_build.assert_called_once()
            mock_log.assert_called_once()
            self.assertEqual(t.version, 1)


if __name__ == '__main__':
    unittest.main()
