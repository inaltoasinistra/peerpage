# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`peerpage` publishes local folders ("sites") as BitTorrent torrents and seeds them. A site can also be downloaded from a magnet URI and re-seeded. The daemon serves site content and an API over HTTP on port 8008.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt  # installs libtorrent==2.0.11, nostr-sdk, tomli
```

## Running

```bash
./cli.py <site>                        # publish local site, emit Nostr event
./cli.py <naddr1|peerpage://>         # download site (name from address)
./cli.py <site> <naddr1|peerpage://>  # queue download, subscribe to updates
./cli.py follow <npub>               # follow all sites published by a user (persistent)
python -m daemon                      # seed all versions, process download requests
./tui.py                              # ncurses dashboard (daemon must be running)
./test.sh                             # run all tests
./coverage.sh                         # run tests with coverage report
```

Environment variables:

| Variable                | Default                          | Description                                                      |
|-------------------------|----------------------------------|------------------------------------------------------------------|
| `SITES_DIR`             | `~/peerpage`                     | User-authored site content; created if missing                   |
| `DATA_DIR`              | `~/.local/share/peerpage`        | Software-managed data (snapshots, torrents, resume files)        |
| `PEERPAGE_CONFIG_DIR`   | `~/.config/peerpage`             | Config directory; contains `config.toml` with Nostr identity     |
| `HTTP_PORT`             | `8008`                           | Port the daemon's HTTP server listens on                         |
| `HTTP_BASE`             | `http://localhost:8008`          | HTTP base URL used by `cli.py` to reach the daemon              |
| `PEERPAGE_URL`          | `http://localhost:8008`          | HTTP base URL used by `tui.py` to reach the daemon              |
| `PEERPAGE_LOCK`         | `/tmp/peerpage.lock`             | Lock file path; prevents two daemons from running simultaneously |
| `PEERPAGE_KEEP_SECONDS` | `86400` (1 day)                  | How long old versions are kept after their last upload           |

## File layout

```
~/peerpage/              ← SITES_DIR (user-authored content)
  <identifier>/          # edit files here

~/.local/share/peerpage/ ← DATA_DIR (software-managed)
  sites/
    <npub>/              # publisher's Nostr public key (bech32)
      <identifier>/
        <version>/         # immutable snapshot (hard-links to previous where unchanged)
          site.torrent
          changelog.txt    # changelog: magnet URI + new / modified / deleted files
          site.resume      # libtorrent resume data (crash recovery)
          event.json       # Nostr event that triggered this version (downloaded sites only)
          site/            # actual site files

~/.config/peerpage/      ← PEERPAGE_CONFIG_DIR
  config.toml            # Nostr identity, relay list, followed npubs (auto-generated)
```

## Architecture

### Tracker list (`trackers.py`)

**`TrackerList`** manages the tracker list used when creating torrents. Trackers are cached in `trackers.json` (not committed). If the file is missing or older than one week, it is refreshed from `https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt`. On network failure, the stale file is used if available, otherwise a hardcoded fallback list is used.

### Publishing (`publisher.py`, `cli.py`)

**`Site`** manages a site's versioned snapshots. Two entry points:

- `Site.create()` — compares `SITES_DIR/<identifier>/` against the last snapshot and, if changed:
  1. Snapshots source into `DATA_DIR/sites/<npub>/<identifier>/<version>/site/`, hard-linking unchanged files
  2. Builds a libtorrent `file_storage` and writes `…/<version>/site.torrent`
  3. Writes `…/<version>/changelog.txt` (magnet URI + new / modified / deleted files)

- `Site.finalize_download()` — called by the daemon after a download completes; writes the changelog by diffing against the previous version.

**`cli.py`** is the CLI entry point. See the Running section for the full command reference. Connects to the daemon HTTP API (`HTTP_BASE`) for download and delete operations; daemon must be running.

### Config (`config.py`)

Auto-generated on first run at `~/.config/peerpage/config.toml`. Stores the Nostr private key (`nsec1…`), the derived public key (`npub1…`), relay list, and the list of followed publishers. Uses `tomllib` (Python 3.11+) or `tomli`.

```toml
[nostr]
private_key = "nsec1…"
public_key = "npub1…"   # derived from private_key; added or corrected automatically on load
relays = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.band",
    "wss://nostr.wine",
]
followed = [
    "npub1…",           # npubs added via `./cli.py follow <npub>`
]
```

`load()` always derives `npub` from `npriv` and rewrites the file if `public_key` is missing or does not match. `save()` writes the config back to disk. Constants: `CONFIG_DIR`, `CONFIG_PATH`, `NOSTR_KIND = 34838`, `DEFAULT_RELAYS`.

### Nostr client (`nostr_client.py`)

**`NostrClient`** wraps `nostr_sdk` for publishing and fetching events (kind 34838):

- `publish(identifier, magnet_uri, changelog, version)` — emits an addressable Nostr event with `["version", str(version)]` and `["user_agent", "Peerpage/<git-hash>"]` tags
- `fetch_latest(pubkey_str, identifier, since=0)` — returns `{magnet, created_at, event}` or `None`
- `fetch_all_sites(pubkey_str)` — returns all sites published by an npub as `[{identifier, magnet, created_at}]`, deduplicated by identifier (latest event wins)
- `fetch_magnet(address)` — resolves a `peerpage://` or `naddr1` address to a magnet URI
- `site_address(identifier)` — returns `peerpage://identifier.npub1…`
- `naddr_address(identifier)` — returns the NIP-19 `naddr1…` encoding for the site
- `parse_address(address)` — `@staticmethod`; returns `(pubkey_bech32, identifier)`

Event format (kind 34838):
```json
{"kind": 34838, "tags": [["d","site"],["magnet","magnet:?…"],["r","wss://…"],["protocol","-1"],["version","…"],["user_agent","…"]], "content": "changelog"}
```

Address formats: `peerpage://identifier.npub1…` (primary) or `naddr1…` (NIP-19).

### Daemon (`daemon/`)

The daemon is built on **asyncio**. libtorrent manages its own internal threads; the daemon drives it from the event loop by polling alerts periodically.

The two key components follow a clean separation of concerns: **NostrWatcher** is a pure Nostr→disk writer; **Watcher** is a pure disk→download driver.  Communication between them happens entirely through the filesystem.

```
Daemon (asyncio event loop)
├── TorrentSession   — wraps lt.session; seeds DATA_DIR/sites/<npub>/<id>/<ver>/<ver>.torrent,
│                      downloads magnet URIs and writes the resulting .torrent file;
│                      saves resume data (.resume files) on shutdown for crash recovery;
│                      exposes sites_info() and stats() for the API;
│                      shutdown() sets self._session = None before logging "shutdown
│                      complete" — this is intentional: it triggers the lt.session
│                      C++ destructor synchronously (joining internal threads) so the
│                      message is only printed after libtorrent is truly done
├── NostrWatcher     — pure Nostr→disk writer; no callbacks; on startup
│                      _load_subscriptions() reads event.json files to rebuild poll state;
│                      each poll cycle: _discover_followed() calls fetch_all_sites() then
│                      fetch_latest(since=0) for newly discovered sites; _check_updates()
│                      calls fetch_latest(since=last_seen_at) for each tracked site;
│                      when a new event is found, _write_event_dir() creates a new version
│                      dir and writes event.json into it; subscribe() schedules an async
│                      fetch+write for a given peerpage:// or naddr1 address
├── Watcher          — pure disk→download driver; each poll cycle _sync() scans
│                      DATA_DIR/sites/<npub>/<id>/<ver>/ and enforces three rules:
│                      (1) seed every version dir that has a .torrent file;
│                      (2) among incomplete dirs (event.json, no .torrent) keep only the
│                      highest version and delete all lower stale ones;
│                      (3) download the highest incomplete version — if a newer incomplete
│                      dir appears while a download is in-progress, cancel it first;
│                      runs cleanup_old_versions() at most every
│                      min(CLEANUP_INTERVAL, KEEP_DURATION)
└── HttpServer       — aiohttp HTTP server on HTTP_PORT (default 8008);
                       serves site content at /<name>.<npub>/<path> and
                       exposes a JSON API under /@/api/
```

Threads are avoided: libtorrent's I/O is internal and `HttpServer` uses aiohttp's asyncio-native runner.

Module-level helpers in `daemon/watcher.py` shared across the daemon: `_next_version(site_data)` (returns `max_existing_dir + 1`), `_classify_versions(site_dir)`, `_read_magnet(ver_dir)`. `NostrWatcher` imports `_next_version` from `watcher`.

### API (`daemon/httpserver.py`)

`HttpServer` exposes a JSON API under `/@/api/` and serves site content at `/<name>.<npub>/<path>`:

| Endpoint                             | Method | Response                                                                   |
|--------------------------------------|--------|----------------------------------------------------------------------------|
| `/@/api/sites`                       | GET    | JSON array of `{identifier, url_identifier, version, state, upload_rate, download_rate, disk_bytes, exclusive_bytes, site_total_bytes, num_peers}` |
| `/@/api/add`                         | POST   | `{"ok": true}` or `{"error": "..."}` — queue a download by peerpage address |
| `/@/api/files/{identifier}`          | GET    | `{identifier, files, total_files}` — per-file list with priorities         |
| `/@/api/priority/{identifier}`       | POST   | `{"ok": true}` — set file priorities (body: `{"priorities": [...]}`)       |
| `/@/api/reset/{identifier}`          | POST   | `{"ok": true}` — reset priorities to automatic (re-run budget algorithm)   |
| `/@/api/delete/{identifier}`         | POST   | `{"ok": true}` — stop and delete a site                                    |
| `/@/api/stop`                        | POST   | `{"ok": true}` — stop the daemon                                           |
| `/@/api/debug`                       | GET    | `{torrents, session}` — internal libtorrent state: per-torrent tracker status, connected peers with discovery source (tracker/dht/lsd/pex), error strings, and active session settings |

Old versions are automatically removed once their `last_upload` age exceeds `KEEP_DURATION` (default 1 day, overridden by `PEERPAGE_KEEP_SECONDS`). The latest version of each site is always kept. For versions that were never uploaded, the torrent file mtime is used as the age reference. Cleanup runs at most every `min(CLEANUP_INTERVAL, KEEP_DURATION)` to respect short keep durations.

Only one daemon instance is allowed at a time, enforced via `fcntl.flock` on `PEERPAGE_LOCK`.

### TUI (`tui.py`)

`tui.py` is a standalone ncurses client. It polls `PEERPAGE_URL/@/api/sites` every second and redraws a table showing per-site rates, total disk usage (`DISK`), exclusive disk usage (`EXCL` — files not hard-linked to other versions), and peer count. Press `q` to quit.

## Format and compatibility

Do not make changes that alter wire-level formats (torrent file structure, bencode schema, on-disk layout) or break backward compatibility with existing stored data without first asking the user for explicit permission. When in doubt, ask before implementing.

## Testing and coverage

After every feature or change, run `./coverage.sh` and check the report. Coverage must stay at 100%. Entry-point scripts are excluded via `.coveragerc`.

### Mutation testing (on demand)

After adding regression tests, verify each test actually catches its bug by applying a targeted mutation to the production code, running the suite, and confirming the test fails.  Then revert the mutation.

Procedure for each new test:

1. Identify the single code path the test is supposed to guard (one condition, one call, one branch).
2. Apply a minimal mutation that disables exactly that behaviour — e.g. `if False:`, swap a constant, remove a condition, always-true a guard.
3. Run `./test.sh` and confirm only the expected test fails (or errors).  If other tests also fail, the mutation is too broad — narrow it.
4. If the test **does not** fail: the test is not testing what it claims.  Fix the test before reverting.
5. Revert the mutation and confirm all tests pass again.

Common mutations:

| What to break                          | How                                                        |
|----------------------------------------|------------------------------------------------------------|
| An `if condition:` branch | Replace condition with `False` |
| A function call | Replace call with a no-op or skip the block |
| A guard `if x > 0:` | Remove the guard (always execute the body) |
| An index/re-mapping | Use the original (unmapped) index instead of the re-indexed one |
| A filter `not isfile(...)` | Replace with `True` (never filters) |

## Debug tools (`claude/`)

Helper scripts in `./claude/` for live debugging without needing to type long API calls. All scripts talk to the HTTP server at `http://localhost:8008`. The daemon must be running (`./claude/daemon-start`).

Site names are resolved loosely: `cubbies` matches `cubbies.npub1…`.

| Script              | Usage                        | Example                          | Description                                                                            |
|---------------------|------------------------------|----------------------------------|----------------------------------------------------------------------------------------|
| `daemon-start`      | `./claude/daemon-start`      | `./claude/daemon-start`          | Start the daemon in the foreground                      |
| `daemon-stop`       | `./claude/daemon-stop`       | `./claude/daemon-stop`           | Stop the running daemon                                 |
| `sites`             | `./claude/sites`             | `./claude/sites`                 | List all active sites                                   |
| `files`             | `./claude/files <name>`      | `./claude/files cubbies`         | Show file list with priorities and states               |
| `all`               | `./claude/all <name>`        | `./claude/all cubbies`           | Set all files to priority 1 (PICK)                      |
| `none`              | `./claude/none <name>`       | `./claude/none cubbies`          | Set all files to priority 0 (SKIP)                      |
| `reset`             | `./claude/reset <name>`      | `./claude/reset cubbies`         | Reset priorities to automatic (re-run budget algorithm) |
| `?mutation-test`    | `?mutation-test`             | `?mutation-test`                 | Apply mutation testing to all recently added tests (see Testing and coverage section) |

@CODING_STYLE.md
