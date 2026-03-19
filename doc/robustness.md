# Robustness Analysis

## 1. Is it clear when a site is ready to be used?

**Mostly yes, but with a gap.**

`last_complete_version()` in `fileutil.py` checks only whether `site.torrent`
exists on disk.  Because `site.torrent` is always written atomically
(`atomic_open` → `os.replace`), and only after libtorrent confirms
`handle.is_seed()` (all pieces verified), the file's presence is a reliable
signal that the content in `site/` is complete and verified.

The gap: `last_complete_version()` does not check whether the `site/`
directory actually exists.  If `site/` was deleted after the torrent was
marked complete, the HTTP server will classify the version as ready and then
return 404s for every file.  `session.seed()` already handles this case —
it detects the missing content directory and queues a re-download — but until
that re-download completes the version looks ready when it is not.

**Fix:** `last_complete_version()` should also require `site/` to be present.

---

## 2. Can the daemon run on a dirty data directory?

### Handled correctly

| Scenario                                                          | Behaviour                                                                                                                         |
|-------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| Non-numeric dir names (`tmp/`, `backup/`)                         | Silently ignored by `list_version_dirs()`                                                                                         |
| Both `event.json` and `site.torrent` present (crash during write) | `site.torrent` wins; version is seeded. Safe because `site.torrent` is written atomically, so it is always complete if it exists. |
| Corrupt `.resume` file                                            | `lt.read_resume_data()` raises; `seed()` falls back to `seed_mode`. libtorrent re-verifies on first upload attempt.               |
| Daemon crash mid-download (before `site.torrent` is written)      | On restart the version is still incomplete; a fresh download starts.                                                              |
| Unexpected exception inside a download task                       | The `finally` block in `_download()` always removes the task from `_tasks`.                                                       |

### Gaps

**Orphaned numeric directories** — directories containing neither `event.json`
nor `site.torrent` (e.g. left by a partial `rmtree` after a crash) are
ignored by `_classify_versions()` and never cleaned up.  They accumulate
silently.

**Corrupt or truncated `site.torrent`** — `lt.torrent_info()` raises an
exception, which is caught in `_seed_if_new()` and logged as a warning.  The
path is added to `_seen` so it is never retried.  The version directory stays
on disk forever, appearing complete to `last_complete_version()` even though
it cannot be seeded or served.

**`event.json` with invalid/empty content** — `_classify_versions()` sees
`event.json` and marks the version incomplete.  `_read_magnet()` then fails to
parse it (empty file → `json.JSONDecodeError`, caught silently) and returns
`None`.  `_start_download()` skips versions without a magnet URI, so the
version is never downloaded and never deleted.  It stays incomplete
indefinitely.

---

## 3. Overall robustness

The core write path is solid: both `event.json` and `site.torrent` are written
atomically, and a download only transitions to complete after libtorrent has
verified every piece.  A crash at any point in the download leaves the
directory in the incomplete state, which is safe to retry.

The main weaknesses are:

### Corrupt `site.torrent` is unrecoverable

Once `_seen` records the path, the daemon will never retry seeding or
re-downloading it.  The directory stays on disk looking complete but broken.

**Suggested fix:** delete the corrupt `site.torrent` (and `site/` if present)
in `_seed_if_new()` on failure and remove the path from `_seen`, so the next
watcher cycle reclassifies it as incomplete and retriggers the download.

### Bad `event.json` blocks a version forever

`_read_magnet()` returns `None` silently.  The incomplete version is never
started and never cleaned up.

**Suggested fix:** log a warning and delete the directory (or write a
`rejected` marker) so cleanup can reclaim the space.

### Orphaned directories are never collected

Numeric directories with no recognised marker file are invisible to all
existing code paths.

**Suggested fix:** in `_classify_versions()`, collect directories that are
neither complete, incomplete, nor rejected, and delete them after a grace
period (or immediately on startup).

### `last_complete_version()` does not verify `site/` exists

As described above — leads to a window where the HTTP server announces
readiness but serves only 404s.

**Suggested fix:**

```python
def last_complete_version(site_data: str) -> int | None:
    versions = [
        v for v in list_version_dirs(site_data)
        if os.path.isfile(os.path.join(site_data, str(v), 'site.torrent'))
        and os.path.isdir(os.path.join(site_data, str(v), CONTENT_DIR))
    ]
    return max(versions) if versions else None
```

### Shutdown resume-data deadline

`session.shutdown()` waits at most `max(5 s, N × 1 s)` for libtorrent to write
`.resume` files, where N is the number of active torrents.  This ensures each
torrent has at least 1 s to flush its state before the session is torn down.
