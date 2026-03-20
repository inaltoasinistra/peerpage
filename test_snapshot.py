import os
import tempfile
import unittest
from unittest.mock import patch

import libtorrent as lt

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

    def test_returns_path_to_hash_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = _make_site(tmp, 'mysite', {'a.txt': 'hello', 'b.txt': 'world'})
            manifest = torrent_manifest(s.torrent_path)
        self.assertIn('site/a.txt', manifest)
        self.assertIn('site/b.txt', manifest)
        self.assertIsInstance(manifest['site/a.txt'], bytes)
        self.assertIsInstance(manifest['site/b.txt'], bytes)
        # hybrid v2 torrents store per-file SHA-256 Merkle roots (32 bytes)
        self.assertEqual(len(manifest['site/a.txt']), 32)

    def test_same_content_gives_same_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, \
             tempfile.TemporaryDirectory() as tmp2:
            s1 = _make_site(tmp1, 'mysite', {'f.txt': 'same', 'g.txt': 'other'})
            s2 = _make_site(tmp2, 'mysite', {'f.txt': 'same', 'g.txt': 'other'})
            m1 = torrent_manifest(s1.torrent_path)
            m2 = torrent_manifest(s2.torrent_path)
        self.assertEqual(m1['site/f.txt'], m2['site/f.txt'])

    def test_different_content_gives_different_hash(self) -> None:
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


class TestHashPieces(unittest.TestCase):

    @staticmethod
    def _make_torrent(tmp: str, files: dict[str, bytes]) -> tuple[lt.create_torrent, lt.file_storage]:
        for rel, content in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(content)
        fs = lt.file_storage()
        for rel in sorted(files):
            fs.add_file(rel, len(files[rel]))
        torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
        return torrent, fs

    @staticmethod
    def _pieces_bytes(torrent: lt.create_torrent) -> bytes:
        """Extract the raw 'pieces' field (concatenated SHA1 hashes) from the torrent."""
        return lt.bdecode(lt.bencode(torrent.generate()))[b'info'][b'pieces']

    @staticmethod
    def _ref_pieces(tmp: str, files: dict[str, bytes]) -> bytes:
        """Compute reference pieces using lt.set_piece_hashes."""
        fs = lt.file_storage()
        for rel in sorted(files):
            fs.add_file(rel, len(files[rel]))
        t = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
        lt.set_piece_hashes(t, tmp)
        return lt.bdecode(lt.bencode(t.generate()))[b'info'][b'pieces']

    def test_single_piece_matches_libtorrent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/a.txt': b'hello', 'site/b.txt': b'world'}
            torrent, fs = self._make_torrent(tmp, files)
            Site._hash_pieces(torrent, fs, tmp)
            self.assertEqual(self._pieces_bytes(torrent), self._ref_pieces(tmp, files))

    def test_multiple_pieces_matches_libtorrent(self) -> None:
        # Content larger than one piece to exercise the piece-boundary flush (lines 129-132)
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/big.bin': os.urandom(40000)}
            torrent, fs = self._make_torrent(tmp, files)
            Site._hash_pieces(torrent, fs, tmp)
            self.assertEqual(self._pieces_bytes(torrent), self._ref_pieces(tmp, files))

    def test_empty_file_skipped(self) -> None:
        # Empty file must be skipped without affecting hashes (line 118: continue)
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/empty.bin': b'', 'site/data.txt': b'content'}
            torrent, fs = self._make_torrent(tmp, files)
            Site._hash_pieces(torrent, fs, tmp)
            self.assertEqual(self._pieces_bytes(torrent), self._ref_pieces(tmp, files))

    def test_short_read_does_not_loop(self) -> None:
        # If the file yields fewer bytes than storage.file_size() claims,
        # the inner loop must break rather than spin forever (line 124: break).
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'site', 'a.txt')
            os.makedirs(os.path.dirname(path))
            with open(path, 'wb') as f:
                f.write(b'short')       # 5 bytes on disk
            fs = lt.file_storage()
            fs.add_file('site/a.txt', 100)  # storage claims 100 bytes
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            # Must return without hanging
            Site._hash_pieces(torrent, fs, tmp)


class TestComputeV2(unittest.TestCase):

    @staticmethod
    def _make_storage(files: dict[str, bytes]) -> lt.file_storage:
        fs = lt.file_storage()
        for rel in sorted(files):
            fs.add_file(rel, len(files[rel]))
        return fs

    @staticmethod
    def _write_files(tmp: str, files: dict[str, bytes]) -> None:
        for rel, content in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(content)

    @staticmethod
    def _lt_pieces_root(tmp: str, files: dict[str, bytes], rel: str) -> bytes:
        """Compute the pieces root for one file using libtorrent (hybrid torrent)."""
        fs = lt.file_storage()
        for r in sorted(files):
            fs.add_file(r, len(files[r]))
        t = lt.create_torrent(fs, 0)  # hybrid v1+v2
        lt.set_piece_hashes(t, tmp)
        info = lt.torrent_info(lt.bencode(t.generate()))
        lt_files = info.files()
        for i in range(lt_files.num_files()):
            if lt_files.file_path(i).replace('\\', '/') == rel:
                return bytes.fromhex(str(lt_files.root(i)))
        raise KeyError(rel)

    def test_single_piece_file_no_piece_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/a.txt': b'hello world'}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, piece_layers = Site._compute_v2(fs, tmp, piece_length)
        # Single-piece file: piece_layers must be empty
        self.assertEqual(piece_layers, {})
        entry = file_tree[b'a.txt'][b'']
        self.assertEqual(entry[b'length'], len(b'hello world'))
        self.assertIn(b'pieces root', entry)
        self.assertEqual(len(entry[b'pieces root']), 32)

    def test_multi_piece_file_populates_piece_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            content = os.urandom(40000)
            files = {'site/big.bin': content}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, piece_layers = Site._compute_v2(fs, tmp, piece_length)
        pieces_root = file_tree[b'big.bin'][b''][b'pieces root']
        self.assertIn(pieces_root, piece_layers)
        # Each entry is a 32-byte SHA-256 hash
        self.assertEqual(len(piece_layers[pieces_root]) % 32, 0)
        self.assertGreater(len(piece_layers[pieces_root]), 32)

    def test_empty_file_has_no_pieces_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/empty.txt': b'', 'site/data.txt': b'content'}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, _ = Site._compute_v2(fs, tmp, piece_length)
        empty_entry = file_tree[b'empty.txt'][b'']
        self.assertEqual(empty_entry[b'length'], 0)
        self.assertNotIn(b'pieces root', empty_entry)

    def test_pieces_root_matches_libtorrent(self) -> None:
        """Manual SHA-256 Merkle root must agree with libtorrent's computation."""
        with tempfile.TemporaryDirectory() as tmp:
            files = {'site/f.txt': b'test content for verification'}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, _ = Site._compute_v2(fs, tmp, piece_length)
            lt_root = self._lt_pieces_root(tmp, files, 'site/f.txt')
        our_root = file_tree[b'f.txt'][b''][b'pieces root']
        self.assertEqual(our_root, lt_root)

    def test_multi_piece_pieces_root_matches_libtorrent(self) -> None:
        """Multi-piece SHA-256 Merkle root must agree with libtorrent's computation."""
        with tempfile.TemporaryDirectory() as tmp:
            content = os.urandom(40000)
            files = {'site/big.bin': content}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, _ = Site._compute_v2(fs, tmp, piece_length)
            lt_root = self._lt_pieces_root(tmp, files, 'site/big.bin')
        our_root = file_tree[b'big.bin'][b''][b'pieces root']
        self.assertEqual(our_root, lt_root)

    def test_nested_subdirectory_file(self) -> None:
        """Files in subdirectories (site/sub/file.txt) must produce nested file tree entries."""
        with tempfile.TemporaryDirectory() as tmp:
            # Both a non-empty and an empty file in a subdirectory to cover both branches
            files = {'site/sub/page.html': b'content', 'site/sub/empty.txt': b''}
            self._write_files(tmp, files)
            fs = self._make_storage(files)
            torrent = lt.create_torrent(fs, 0, lt.create_torrent.v1_only)
            piece_length = torrent.piece_length()
            file_tree, _ = Site._compute_v2(fs, tmp, piece_length)
        # tree_parts = ['sub', 'page.html'] → nested dict
        self.assertIn(b'sub', file_tree)
        self.assertIn(b'page.html', file_tree[b'sub'])
        entry = file_tree[b'sub'][b'page.html'][b'']
        self.assertEqual(entry[b'length'], len(b'content'))
        # empty file in subdir: length=0, no pieces root
        self.assertIn(b'empty.txt', file_tree[b'sub'])
        self.assertEqual(file_tree[b'sub'][b'empty.txt'][b''], {b'length': 0})


class TestCreateEdgeCases(unittest.TestCase):

    def test_create_raises_on_empty_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sites_dir = os.path.join(tmp, 'sites')
            data_dir = os.path.join(tmp, 'data')
            os.makedirs(os.path.join(sites_dir, 'mysite'))
            s = Site('mysite', sites_dir=sites_dir, data_dir=data_dir, npub='npub1test')
            with self.assertRaises(ValueError):
                s.create()

    def test_create_handles_oserror_cleans_partial_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sites_dir = os.path.join(tmp, 'sites')
            data_dir = os.path.join(tmp, 'data')
            src = os.path.join(sites_dir, 'mysite')
            os.makedirs(src)
            with open(os.path.join(src, 'a.txt'), 'w') as f:
                f.write('hello')
            s = Site('mysite', sites_dir=sites_dir, data_dir=data_dir, npub='npub1test')
            with patch.object(Site, '_build_torrent', side_effect=OSError('disk error')):
                result = s.create()
            self.assertFalse(result)
            # Partial version dir must be removed
            partial = os.path.join(data_dir, 'sites', 'npub1test', 'mysite', '1')
            self.assertFalse(os.path.isdir(partial))

    def test_create_returns_false_and_preserves_last_version_on_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sites_dir = os.path.join(tmp, 'sites')
            data_dir = os.path.join(tmp, 'data')
            src = os.path.join(sites_dir, 'mysite')
            os.makedirs(src)
            with open(os.path.join(src, 'a.txt'), 'w') as f:
                f.write('hello')
            s = Site('mysite', sites_dir=sites_dir, data_dir=data_dir, npub='npub1test')
            with patch.object(Site, '_build_torrent', side_effect=OSError('disk error')):
                result = s.create()
            self.assertFalse(result)
            self.assertIsNone(s.torrent_path)
            self.assertIsNone(s.magnet_uri)


if __name__ == '__main__':
    unittest.main()
