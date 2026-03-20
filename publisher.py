import hashlib
import os
import shutil

import libtorrent as lt

from fileutil import atomic_open, CONTENT_DIR, last_complete_version, list_version_dirs
from snapshot import diff_manifests, write_changelog
from trackers import TrackerList

_BLOCK_SIZE = 16384  # BEP 52 block size (16 KiB)


class Site:

    def __init__(self, name: str, sites_dir: str, data_dir: str, npub: str):
        self.name = name
        self.source_path = os.path.join(sites_dir, name)
        self.data_path = os.path.join(data_dir, 'sites', npub, name)
        self.version: int | None = None
        self.torrent_path: str | None = None
        self._torrent = None
        self.magnet_uri: str | None = None

    @staticmethod
    def _hash_file(path: str) -> str:
        hasher = hashlib.sha1()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def file_manifest(folder: str) -> dict[str, str]:
        manifest = {}
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for name in sorted(files):
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, folder)
                manifest[rel_path] = Site._hash_file(full_path)
        return manifest

    def last_version(self) -> int | None:
        """Return the highest version number that has a completed .torrent file."""
        return last_complete_version(self.data_path)

    def _has_changes(self, source_manifest: dict[str, str], last_version: int | None) -> bool:
        if last_version is None:
            return True
        last_version_path = os.path.join(self.data_path, str(last_version), CONTENT_DIR)
        previous_manifest = self.file_manifest(last_version_path)
        return source_manifest != previous_manifest

    def _snapshot_file(self, rel_path: str, file_hash: str, destination_dir: str,
                       previous_dir: str | None, previous_manifest: dict[str, str]) -> None:
        destination = os.path.join(destination_dir, rel_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if previous_dir and previous_manifest.get(rel_path) == file_hash:
            os.link(os.path.join(previous_dir, rel_path), destination)
        else:
            shutil.copyfile(os.path.join(self.source_path, rel_path), destination)

    def _snapshot(self, source_manifest: dict[str, str], last_version: int | None, new_version: int) -> None:
        destination_dir = os.path.join(self.data_path, str(new_version), CONTENT_DIR)
        os.makedirs(destination_dir, exist_ok=True)
        previous_dir = (os.path.join(self.data_path, str(last_version), CONTENT_DIR)
                        if last_version is not None else None)
        previous_manifest = self.file_manifest(previous_dir) if previous_dir else {}
        for rel_path, file_hash in source_manifest.items():
            self._snapshot_file(rel_path, file_hash, destination_dir, previous_dir, previous_manifest)

    @staticmethod
    def _add_files(share_version_path: str, share_base: str) -> lt.file_storage:
        """Add files to a file_storage in BEP 52 DFS lexicographic order.

        BEP 52 requires the v1 file list to match the DFS traversal of the v2
        file tree, where entries at each directory level are sorted by name and
        directories are not segregated from files.  os.walk would place all
        files at a level before descending into subdirectories, breaking
        alphabetical interleaving of files and directory names.
        """
        storage = lt.file_storage()

        def _recurse(dir_path: str) -> None:
            for name in sorted(os.listdir(dir_path)):
                full_path = os.path.join(dir_path, name)
                if os.path.isdir(full_path):
                    _recurse(full_path)
                else:
                    rel_path = os.path.relpath(full_path, share_base)
                    storage.add_file(rel_path, os.path.getsize(full_path))

        _recurse(share_version_path)
        return storage

    def _add_trackers(self) -> None:
        for tracker in TrackerList.select():
            self._torrent.add_tracker(tracker)

    def _create_changelog(
        self, source_manifest: dict[str, str], last_version: int | None, path: str,
        magnet_uri: str,
    ) -> None:
        previous_manifest = self.file_manifest(
            os.path.join(self.data_path, str(last_version), CONTENT_DIR)
        ) if last_version is not None else {}
        new_files, modified, deleted = diff_manifests(previous_manifest, source_manifest)
        write_changelog(path, new_files, modified, deleted, magnet_uri)

    @staticmethod
    def _pad_storage(storage: lt.file_storage, piece_length: int) -> lt.file_storage:
        """Return a new file_storage with v1 pad files for piece alignment.

        BEP 52 hybrid torrents require that each real file starts on a piece
        boundary in the v1 file list.  Pad files fill the tail of each file's
        last piece so the next file begins at a piece boundary.
        """
        padded = lt.file_storage()
        root_dir: str | None = None
        offset = 0
        for i in range(storage.num_files()):
            rel_path = storage.file_path(i).replace('\\', '/')
            size = storage.file_size(i)
            if root_dir is None and '/' in rel_path:
                root_dir = rel_path.split('/')[0]
            padded.add_file(rel_path, size)
            offset += size
            remainder = offset % piece_length
            if remainder:
                pad_size = piece_length - remainder
                prefix = f'{root_dir}/' if root_dir else ''
                padded.add_file(f'{prefix}.pad/{pad_size}', pad_size,
                                lt.file_storage.flag_pad_file)
                offset += pad_size
        return padded

    @staticmethod
    def _hash_pieces(torrent: lt.create_torrent, storage: lt.file_storage,
                     base_dir: str) -> None:
        """Compute SHA1 piece hashes in Python, avoiding libtorrent's I/O thread pool.

        lt.set_piece_hashes() submits jobs to libtorrent's global hash thread
        pool, which can be canceled (ECANCELED) when another lt.session is
        active concurrently (e.g. the daemon).  Computing the hashes here avoids
        that dependency entirely.

        Pad files (whose paths contain /.pad/) are virtual: they contribute
        zero bytes to the piece hash without requiring a file on disk.
        """
        piece_length = torrent.piece_length()
        piece_index = 0
        hasher = hashlib.sha1()  # running hasher spanning piece boundaries
        bytes_in_piece = 0
        for i in range(storage.num_files()):
            rel_path = storage.file_path(i).replace('\\', '/')
            remaining = storage.file_size(i)
            if remaining == 0:
                continue
            is_pad = '/.pad/' in rel_path or rel_path.startswith('.pad/')
            if is_pad:
                while remaining > 0:
                    want = min(65536, remaining, piece_length - bytes_in_piece)
                    hasher.update(b'\x00' * want)
                    bytes_in_piece += want
                    remaining -= want
                    if bytes_in_piece == piece_length:
                        torrent.set_hash(piece_index, hasher.digest())
                        piece_index += 1
                        hasher = hashlib.sha1()
                        bytes_in_piece = 0
            else:
                path = os.path.join(base_dir, rel_path)
                with open(path, 'rb') as f:
                    while remaining > 0:
                        want = min(65536, remaining, piece_length - bytes_in_piece)
                        chunk = f.read(want)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        bytes_in_piece += len(chunk)
                        remaining -= len(chunk)
                        if bytes_in_piece == piece_length:
                            torrent.set_hash(piece_index, hasher.digest())
                            piece_index += 1
                            hasher = hashlib.sha1()
                            bytes_in_piece = 0
        if bytes_in_piece > 0:
            torrent.set_hash(piece_index, hasher.digest())

    @staticmethod
    def _merkle_root(hashes: list[bytes]) -> bytes:
        """SHA-256 Merkle root of hashes, zero-padding to the next power of 2."""
        n = 1
        while n < len(hashes):
            n <<= 1
        nodes = list(hashes) + [b'\x00' * 32] * (n - len(hashes))
        while len(nodes) > 1:
            nodes = [hashlib.sha256(nodes[i] + nodes[i + 1]).digest()
                     for i in range(0, len(nodes), 2)]
        return nodes[0]

    @staticmethod
    def _compute_v2(storage: lt.file_storage, base_dir: str,
                    piece_length: int) -> tuple[dict, dict]:
        """Compute BEP 52 v2 fields: file tree and piece layers.

        Returns (file_tree, piece_layers) where:
        - file_tree is the dict to inject at info[b'file tree']
        - piece_layers maps pieces_root → concatenated per-piece SHA256 hashes
          (only for files spanning more than one piece, per BEP 52)

        The file tree is keyed relative to the torrent name (first path component
        stripped), so 'site/a.txt' appears as {b'a.txt': {b'': {...}}}.
        Pad files (/.pad/) are skipped — they have no v2 representation.
        """
        file_tree: dict = {}
        piece_layers: dict = {}
        blocks_per_piece = piece_length // _BLOCK_SIZE
        for i in range(storage.num_files()):
            rel_path = storage.file_path(i).replace('\\', '/')
            if '/.pad/' in rel_path or rel_path.startswith('.pad/'):
                continue
            parts = rel_path.split('/')
            tree_parts = parts[1:] if len(parts) > 1 else parts  # strip torrent name
            file_size = storage.file_size(i)

            if file_size == 0:
                node = file_tree
                for part in tree_parts[:-1]:
                    node = node.setdefault(part.encode(), {})
                node[tree_parts[-1].encode()] = {b'': {b'length': 0}}
                continue

            abs_path = os.path.join(base_dir, rel_path)
            block_hashes: list[bytes] = []
            with open(abs_path, 'rb') as f:
                while True:
                    block = f.read(_BLOCK_SIZE)
                    if not block:
                        break
                    block_hashes.append(hashlib.sha256(block).digest())  # raw data, no padding

            piece_hashes: list[bytes] = []
            for j in range(0, len(block_hashes), blocks_per_piece):
                chunk = block_hashes[j:j + blocks_per_piece]
                chunk += [b'\x00' * 32] * (blocks_per_piece - len(chunk))
                piece_hashes.append(Site._merkle_root(chunk))

            pieces_root = Site._merkle_root(piece_hashes)

            node = file_tree
            for part in tree_parts[:-1]:
                node = node.setdefault(part.encode(), {})
            node[tree_parts[-1].encode()] = {
                b'': {b'length': file_size, b'pieces root': pieces_root}
            }

            if len(piece_hashes) > 1:
                piece_layers[pieces_root] = b''.join(piece_hashes)

        return file_tree, piece_layers

    def _build_torrent(self, content_dir: str, version_dir: str) -> None:
        real_storage = self._add_files(content_dir, version_dir)
        # Determine piece length from real files, then build padded storage.
        # BEP 52 requires pad files in the v1 file list so each real file starts
        # at a piece boundary; libtorrent rejects hybrid torrents without them.
        piece_length = lt.create_torrent(real_storage, 0, lt.create_torrent.v1_only).piece_length()
        padded_storage = self._pad_storage(real_storage, piece_length)
        self._torrent = lt.create_torrent(padded_storage, piece_length, lt.create_torrent.v1_only)
        self._add_trackers()
        self._hash_pieces(self._torrent, padded_storage, version_dir)
        file_tree, piece_layers = self._compute_v2(padded_storage, version_dir, piece_length)
        data = self._torrent.generate()
        data[b'info'][b'file tree'] = file_tree
        data[b'info'][b'meta version'] = 2
        data[b'piece layers'] = piece_layers
        with atomic_open(self.torrent_path, 'wb') as f:
            f.write(lt.bencode(data))
        info = lt.torrent_info(self.torrent_path)
        self.magnet_uri = lt.make_magnet_uri(info)

    def finalize_download(self, version: int, previous_version: int | None, magnet_uri: str) -> None:
        content_dir = os.path.join(self.data_path, str(version), CONTENT_DIR)
        version_manifest = self.file_manifest(content_dir)
        self._create_changelog(version_manifest, previous_version,
                               os.path.join(self.data_path, str(version), 'changelog.txt'), magnet_uri)

    def _reuse_existing_version(self, last_version: int) -> None:
        self.version = last_version
        self.torrent_path = os.path.join(self.data_path, str(self.version), 'site.torrent')
        self.magnet_uri = lt.make_magnet_uri(lt.torrent_info(self.torrent_path))

    def _publish_new_version(self, source_manifest: dict[str, str], last_version: int | None) -> None:
        self.version = (last_version or 0) + 1
        version_dir = os.path.join(self.data_path, str(self.version))
        content_dir = os.path.join(version_dir, CONTENT_DIR)
        self.torrent_path = os.path.join(version_dir, 'site.torrent')
        os.makedirs(self.data_path, exist_ok=True)
        self._snapshot(source_manifest, last_version, self.version)
        self._build_torrent(content_dir, version_dir)
        self._create_changelog(source_manifest, last_version,
                               os.path.join(version_dir, 'changelog.txt'), self.magnet_uri)

    def create(self) -> bool:
        os.makedirs(self.source_path, exist_ok=True)
        source_manifest = self.file_manifest(self.source_path)
        if not source_manifest:
            raise ValueError(f'no files to publish in {self.source_path} — add content there first')
        last_version = self.last_version()
        if not self._has_changes(source_manifest, last_version):
            self._reuse_existing_version(last_version)
            return False
        try:
            self._publish_new_version(source_manifest, last_version)
        except OSError:
            # A concurrent delete can remove snapshot files mid-publish.
            # Clean up the incomplete version dir and report no change.
            failed_ver = self.version
            if failed_ver is not None:
                partial = os.path.join(self.data_path, str(failed_ver))
                if os.path.isdir(partial) and not os.path.isfile(
                        os.path.join(partial, 'site.torrent')):
                    shutil.rmtree(partial, ignore_errors=True)
            self.version = last_version
            self.torrent_path = None
            self.magnet_uri = None
            return False
        return True
