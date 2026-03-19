import hashlib
import os
import shutil
import time

import libtorrent as lt

from fileutil import atomic_open, CONTENT_DIR, last_complete_version, list_version_dirs
from snapshot import diff_manifests, write_changelog
from trackers import TrackerList


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
        storage = lt.file_storage()
        for root, dirs, files in os.walk(share_version_path):
            dirs.sort()
            for name in sorted(files):
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, share_base)
                storage.add_file(rel_path, os.path.getsize(full_path))
        return storage

    def _add_trackers(self) -> None:
        for tracker in TrackerList.select():
            self._torrent.add_tracker(tracker)

    def _write_file(self) -> None:
        with atomic_open(self.torrent_path, 'wb') as f:
            f.write(lt.bencode(self._torrent.generate()))

    def _create_changelog(
        self, source_manifest: dict[str, str], last_version: int | None, path: str,
        magnet_uri: str,
    ) -> None:
        previous_manifest = self.file_manifest(
            os.path.join(self.data_path, str(last_version), CONTENT_DIR)
        ) if last_version is not None else {}
        new_files, modified, deleted = diff_manifests(previous_manifest, source_manifest)
        write_changelog(path, new_files, modified, deleted, magnet_uri)

    def _build_torrent(self, content_dir: str, version_dir: str) -> None:
        storage = self._add_files(content_dir, version_dir)
        # set_piece_hashes can return ECANCELED (system:125) spuriously when
        # multiple libtorrent sessions are active concurrently (libtorrent 2.0.x
        # internal thread-pool issue).  Retry a few times before giving up.
        for attempt in range(7):
            self._torrent = lt.create_torrent(storage)
            self._add_trackers()
            try:
                lt.set_piece_hashes(self._torrent, version_dir)
                break
            except RuntimeError as e:
                msg = str(e).lower()
                retryable = 'canceled' in msg or 'no such file' in msg
                if attempt == 6 or not retryable:
                    raise
                time.sleep(0.5 * (attempt + 1))
        self._write_file()
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

