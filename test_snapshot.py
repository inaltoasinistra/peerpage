import os
import tempfile
import unittest

from publisher import Site
from snapshot import diff_manifests, torrent_manifest, write_changelog

FAKE_NPUB = 'npub1testfake'


def _make_site(tmp: str, name: str, files: dict[str, str]) -> Site:
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


class TestTorrentManifest(unittest.TestCase):

    def test_returns_path_to_root_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = _make_site(tmp, 'mysite', {'a.txt': 'hello', 'b.txt': 'world'})
            manifest = torrent_manifest(s.torrent_path)
        self.assertIn('site/a.txt', manifest)
        self.assertIn('site/b.txt', manifest)
        self.assertIsInstance(manifest['site/a.txt'], bytes)
        self.assertIsInstance(manifest['site/b.txt'], bytes)

    def test_same_content_gives_same_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, \
             tempfile.TemporaryDirectory() as tmp2:
            s1 = _make_site(tmp1, 'mysite', {'f.txt': 'same', 'g.txt': 'other'})
            s2 = _make_site(tmp2, 'mysite', {'f.txt': 'same', 'g.txt': 'other'})
            m1 = torrent_manifest(s1.torrent_path)
            m2 = torrent_manifest(s2.torrent_path)
        self.assertEqual(m1['site/f.txt'], m2['site/f.txt'])

    def test_different_content_gives_different_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, \
             tempfile.TemporaryDirectory() as tmp2:
            s1 = _make_site(tmp1, 'mysite', {'f.txt': 'version one', 'g.txt': 'other'})
            s2 = _make_site(tmp2, 'mysite', {'f.txt': 'version two', 'g.txt': 'other'})
            m1 = torrent_manifest(s1.torrent_path)
            m2 = torrent_manifest(s2.torrent_path)
        self.assertNotEqual(m1['site/f.txt'], m2['site/f.txt'])


class TestDiffManifests(unittest.TestCase):

    def test_detects_new_file(self) -> None:
        new_files, modified, deleted = diff_manifests({}, {'a.txt': 'hash1'})
        self.assertEqual(new_files, ['a.txt'])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, [])

    def test_detects_deleted_file(self) -> None:
        new_files, modified, deleted = diff_manifests({'a.txt': 'hash1'}, {})
        self.assertEqual(new_files, [])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, ['a.txt'])

    def test_detects_modified_file(self) -> None:
        new_files, modified, deleted = diff_manifests(
            {'a.txt': 'old'}, {'a.txt': 'new'}
        )
        self.assertEqual(new_files, [])
        self.assertEqual(modified, ['a.txt'])
        self.assertEqual(deleted, [])

    def test_unchanged_file_not_reported(self) -> None:
        new_files, modified, deleted = diff_manifests(
            {'a.txt': 'same'}, {'a.txt': 'same'}
        )
        self.assertEqual(new_files, [])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, [])

    def test_results_are_sorted(self) -> None:
        new_files, _, _ = diff_manifests({}, {'z.txt': 'h', 'a.txt': 'h', 'm.txt': 'h'})
        self.assertEqual(new_files, ['a.txt', 'm.txt', 'z.txt'])


class TestWriteChangelog(unittest.TestCase):

    def test_writes_magnet_and_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'changelog.txt')
            write_changelog(path, ['c.txt'], ['b.txt'], ['a.txt'], 'magnet:?xt=test')
            with open(path) as f:
                content = f.read()
        self.assertIn('magnet: magnet:?xt=test', content)
        self.assertIn('new files:', content)
        self.assertIn('c.txt', content)
        self.assertIn('modified files:', content)
        self.assertIn('b.txt', content)
        self.assertIn('deleted files:', content)
        self.assertIn('a.txt', content)

    def test_omits_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'changelog.txt')
            write_changelog(path, ['new.txt'], [], [], 'magnet:?xt=test')
            with open(path) as f:
                content = f.read()
        self.assertNotIn('modified files:', content)
        self.assertNotIn('deleted files:', content)


if __name__ == '__main__':
    unittest.main()
