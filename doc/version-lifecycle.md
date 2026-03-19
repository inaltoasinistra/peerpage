# Site Version Lifecycle

A *site version* is a numbered snapshot of a site's content.  Each version
lives in its own directory:

```
~/.local/share/peerpage/sites/<npub>/<site_name>/<version>/
```

---

## On-disk states

A version directory is in exactly one state, determined by which files are
present:

| State          | Files present            | Meaning                                         |
|----------------|--------------------------|-------------------------------------------------|
| **Incomplete** | `event.json`             | Discovered from Nostr, not yet downloaded       |
| **Complete**   | `site.torrent` + `site/` | Downloaded (or published) and being seeded      |
| **Rejected**   | `rejected`               | Downloaded but failed validation; never retried |

Other files that may appear alongside these:

| File            | Purpose                                           |
|-----------------|---------------------------------------------------|
| `.resume`       | libtorrent resume data, written on clean shutdown |
| `changelog.txt` | Human-readable diff from the previous version     |

---

## Stage 1 — Discovery

`NostrWatcher` polls Nostr relays every 30 s looking for addressable events of
kind `34838`.  When a new event appears for a subscribed site it:

1. Picks the next local version number (`max(existing) + 1`, or `1`).
2. Creates the version directory.
3. Writes `event.json` with the full Nostr event payload (which includes the
   `magnet` tag).

The version is now **incomplete**.

---

## Stage 2 — Download

`Watcher` scans the data directory every 2 s.  When it finds an incomplete
version it starts a download task:

1. **Read magnet URI** from `event.json`.
2. **Add to libtorrent** and wait for metadata.
3. **Pre-populate** from the previous version: files whose SHA-256 Merkle root
   matches are hard-linked instead of downloaded, saving both bandwidth and
   disk space.  libtorrent then verifies the hard-linked pieces and skips
   downloading them.
4. **Download** the remaining pieces from peers.
5. **Write `site.torrent`** atomically once the torrent is complete.

Only the *highest* incomplete version is downloaded at any time.  If a newer
version arrives while a download is in progress, the old task is cancelled and
the new one starts.  Stale lower incomplete versions are deleted immediately.

### Validation

After the download completes, the torrent manifest is checked.  Every file
path inside the torrent must start with `site/` (the required top-level
directory).  If this check fails:

- `site.torrent` and the `site/` directory are deleted.
- A `rejected` marker is written so the version is never attempted again.

If validation passes, `changelog.txt` is written comparing the file manifest
with the previous version (new / modified / deleted files).  The version is
now **complete**.

---

## Stage 3 — Seeding

As soon as a version becomes complete, `Watcher` calls `session.seed()`.  The
behaviour depends on what is on disk:

- **`site/` exists, `.resume` file exists** — resume data is loaded and
  libtorrent trusts its stored state (fastest restart).
- **`site/` exists, no `.resume` file** — `seed_mode` is used; libtorrent
  trusts the files without re-hashing.
- **`site/` is missing** — the torrent is added without any optimistic flags
  so libtorrent re-downloads the content from scratch, as if it were a fresh
  incomplete version.

On a clean daemon shutdown, libtorrent writes a `.resume` file for every
active torrent so the next startup avoids a full re-check.

---

## Stage 4 — Cleanup

`Watcher` runs a cleanup pass roughly every hour.  For each site, cleanup only
proceeds if the **latest** version is fully seeding (to avoid deleting the
previous version while the new one is still downloading).

An old version is removed if either condition is met:

1. **Over the cap** — the site has more than `MAX_VERSIONS` (5) complete
   versions; the oldest ones are removed first.
2. **Expired** — the version has not been uploaded to any peer for longer than
   `KEEP_DURATION` (default 24 h, overridable via `PEERPAGE_KEEP_SECONDS`).

Age is measured from the last upload timestamp if the torrent has ever been
uploaded, otherwise from the `site.torrent` file's modification time.

Removal deletes the version directory from disk and removes the libtorrent
handle from the session.

---

## Lifecycle summary

```
Nostr event
    │
    ▼
[Incomplete]  ──────────────────────────────►  deleted
    │          stale (newer incomplete arrived)
    │
    │  download + validate
    ├──────────────────────────► [Rejected]  (permanent, never retried)
    │
    ▼
[Complete / Seeding]
    │
    │  KEEP_DURATION elapsed (no uploads)
    │  OR site exceeds MAX_VERSIONS cap
    ▼
  deleted
```

---

## Key constants

| Constant              | Default | Env override            |
|-----------------------|---------|-------------------------|
| `MAX_VERSIONS`        | 5       | —                       |
| `KEEP_DURATION`       | 24 h    | `PEERPAGE_KEEP_SECONDS` |
| `POLL_INTERVAL`       | 2 s     | —                       |
| `CLEANUP_INTERVAL`    | 1 h     | —                       |
| `NOSTR_POLL_INTERVAL` | 30 s    | —                       |
