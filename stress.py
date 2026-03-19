#!/usr/bin/env python3
"""
peerpage stress test.

Starts N independent daemons (named after the NATO phonetic alphabet),
drives random site creation, versioning, and cross-daemon subscriptions
against the real Nostr network, then checks consistency.

Consistency checks (run periodically):
  (a) Liveness     — subscribed daemon eventually reaches 'seeding' state
  (b) Integrity    — file hashes match the publisher's copy byte-for-byte
  (c) Version      — subscriber eventually catches up to the publisher's
                     latest version (via Nostr polling)

Usage:
    python stress.py [N]              # start N daemons (default: 3)
    python stress.py [N] --dir DIR    # override state directory
    python stress.py [N] --keep       # keep state directory on exit
    python stress.py --help
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import libtorrent as lt

import config as cfg_module
from fileutil import atomic_open, list_version_dirs
from nostr_client import NostrClient
from publisher import Site

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NATO = [
    'alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf',
    'hotel', 'india', 'juliet', 'kilo', 'lima', 'mike', 'november',
    'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango',
]

_ADJECTIVES = [
    'amber', 'arctic', 'azure', 'bold', 'bright', 'calm', 'coastal',
    'crisp', 'dark', 'deep', 'emerald', 'frosty', 'golden', 'grand',
    'hollow', 'indigo', 'ivory', 'jade', 'lofty', 'lunar',
    'misty', 'noble', 'open', 'pale', 'rapid', 'silver', 'silent',
    'swift', 'tall', 'vast',
]
_NOUNS = [
    'basin', 'bluff', 'canyon', 'cliff', 'coast', 'cove', 'creek',
    'delta', 'dune', 'fjord', 'forest', 'glade', 'grove', 'harbor',
    'haven', 'heath', 'isle', 'lagoon', 'marsh', 'meadow',
    'moor', 'peak', 'plain', 'ridge', 'shore', 'slope', 'spire',
    'stream', 'tide', 'vale',
]

BASE_HTTP_PORT   = 9100
STRESS_INTERVAL  = 20.0   # seconds between stress actions
REPORT_INTERVAL  = 90.0   # seconds between consistency-check rounds
LIVENESS_TIMEOUT = 240.0  # seconds to wait for a download to appear as seeding
VERSION_TIMEOUT  = 120.0  # extra seconds for version catch-up after liveness


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DaemonInfo:
    name: str
    root_dir: str
    http_port: int
    npub: str
    config: cfg_module.Config
    proc: subprocess.Popen | None = None
    # site_name -> {version, magnet, address}
    sites: dict = field(default_factory=dict)


@dataclass
class Subscription:
    publisher: DaemonInfo
    subscriber: DaemonInfo
    site_name: str
    pub_version: int          # publisher's version number (for logging)
    pub_torrent_path: str     # publisher's site.torrent for that version
    subscribed_at: float      # monotonic timestamp
    checked: bool = False     # True once liveness has been confirmed


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _random_site_name() -> str:
    return f'{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}'


# Each profile is (name, {rel_path: size_kb}).
# Designed for max_site_mb=1 (1024 KB budget); the expected outcome with the
# greedy recursive priorities algorithm is shown in the comments.
_SITE_PROFILES: list[tuple[str, dict[str, int]]] = [
    # tiny: ~50 KB — fits entirely, all files downloaded
    ('tiny', {
        'data.txt': 49,
    }),
    # small: ~600 KB — fits entirely, all files downloaded
    ('small', {
        'style.css': 5,
        'assets/hero.jpg': 590,
    }),
    # medium: ~1.5 MB — assets/ recursed; logo fits, hero skipped
    ('medium', {
        'style.css': 5,
        'assets/logo.png': 200,
        'assets/hero.jpg': 1300,
    }),
    # large: ~5 MB — assets/ recursed; logo+banner fit, video skipped
    ('large', {
        'style.css': 5,
        'assets/logo.png': 100,
        'assets/banner.jpg': 500,
        'assets/video.mp4': 4400,
    }),
    # huge: ~7 MB — two dirs both recursed; only small files from each fit
    ('huge', {
        'style.css': 5,
        'images/banner.jpg': 3000,
        'images/thumb.jpg': 100,
        'docs/report.pdf': 3000,
        'docs/summary.txt': 50,
    }),
]


def _write_blob(path: str, size_kb: int) -> None:
    """Write size_kb kilobytes of pseudorandom bytes to path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    chunk = os.urandom(min(size_kb * 1024, 65536))
    remaining = size_kb * 1024
    with open(path, 'wb') as f:
        while remaining > 0:
            n = min(len(chunk), remaining)
            f.write(chunk[:n])
            remaining -= n


def _generate_content(source_path: str, version: int) -> None:
    """Write a site with a random size profile to test the priority-selection algorithm.

    Clears any previous content so switching profiles between versions leaves
    no stale files.  index.html always carries a nonce to guarantee a new
    torrent version is created.
    """
    if os.path.isdir(source_path):
        shutil.rmtree(source_path)
    os.makedirs(source_path)

    with open(os.path.join(source_path, 'index.html'), 'w') as f:
        f.write(
            f'<!doctype html><html><body>\n'
            f'<h1>Version {version}</h1>\n'
            f'<p>nonce: {random.randint(0, 2**32)}</p>\n'
            f'<p>ts: {time.time():.3f}</p>\n'
            f'</body></html>\n'
        )

    profile_name, blobs = random.choice(_SITE_PROFILES)
    for rel_path, size_kb in blobs.items():
        _write_blob(os.path.join(source_path, rel_path), size_kb)
    return profile_name  # caller logs this


def _setup_daemon(name: str, index: int, stress_dir: str) -> DaemonInfo:
    root = os.path.join(stress_dir, name)
    os.makedirs(os.path.join(root, 'sites'), exist_ok=True)
    os.makedirs(os.path.join(root, 'data'),  exist_ok=True)
    os.makedirs(os.path.join(root, 'config'), exist_ok=True)

    config_path = os.path.join(root, 'config', 'config.toml')
    cfg = cfg_module.load(config_path)
    cfg.max_site_mb = 1  # 1 MB budget to exercise the file-priority algorithm
    cfg_module.save(cfg, config_path)
    return DaemonInfo(
        name=name,
        root_dir=root,
        http_port=BASE_HTTP_PORT + index,
        npub=cfg.nostr.public_key,
        config=cfg,
    )


def _start_daemon(daemon: DaemonInfo) -> subprocess.Popen:
    root = daemon.root_dir
    env = os.environ.copy()
    env['SITES_DIR']           = os.path.join(root, 'sites')
    env['DATA_DIR']            = os.path.join(root, 'data')
    env['PEERPAGE_CONFIG_DIR'] = os.path.join(root, 'config')
    env['PEERPAGE_LOCK']       = os.path.join(root, 'peerpage.lock')
    env['PEERPAGE_SOCK']       = os.path.join(root, 'peerpage.sock')
    env['HTTP_PORT']           = str(daemon.http_port)
    env['LOG_LEVEL']           = 'DEBUG'

    log_path = os.path.join(root, 'daemon.log')
    log_file = open(log_path, 'w')
    return subprocess.Popen(
        [sys.executable, '-m', 'daemon'],
        env=env,
        stdout=log_file,
        stderr=log_file,
        cwd=str(_HERE),
    )


async def _wait_ready(http_port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(
                    f'http://127.0.0.1:{http_port}/@/api/sites',
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------

async def _api_sites(http_port: int) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f'http://127.0.0.1:{http_port}/@/api/sites',
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return await resp.json()
        except Exception:
            return []


async def _http_add(http_port: int, address: str) -> bool:
    """POST address to /@/add; returns True on success (303 redirect is success)."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f'http://127.0.0.1:{http_port}/@/add',
                data={'address': address},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status in (200, 302, 303)
        except Exception:
            return False


def _read_changelog(path: str) -> str:
    """Return changelog text, stripping the leading 'magnet: …\n\n' header."""
    try:
        with open(path) as f:
            content = f.read()
        if content.startswith('magnet: '):
            parts = content.split('\n\n', 1)
            return parts[1] if len(parts) > 1 else ''
        return content
    except OSError:
        return ''


async def _publish(daemon: DaemonInfo, site_name: str) -> tuple[int, str] | None:
    """Create/update a site and publish it to Nostr.

    Returns (version, peerpage_address) on success, None on failure.
    Runs the CPU-intensive torrent-creation in a thread executor.
    """
    sites_dir = os.path.join(daemon.root_dir, 'sites')
    data_dir  = os.path.join(daemon.root_dir, 'data')

    next_ver = (daemon.sites.get(site_name, {}).get('version') or 0) + 1
    source_path = os.path.join(sites_dir, site_name)
    profile = _generate_content(source_path, next_ver)

    site = Site(site_name, sites_dir=sites_dir, data_dir=data_dir, npub=daemon.npub)
    changed = site.create()
    if not changed:
        return None  # content identical — should not happen due to _generate_content

    changelog = _read_changelog(
        os.path.join(site.data_path, str(site.version), 'changelog.txt')
    )
    nostr = NostrClient(daemon.config)
    event = await nostr.publish(site_name, site.magnet_uri, changelog, site.version)
    if event is None:
        return None

    # Write event.json so the local daemon recognises this version as its own
    with atomic_open(os.path.join(site.data_path, str(site.version), 'event.json')) as f:
        json.dump(event, f, indent=2)

    address = nostr.site_address(site_name)
    daemon.sites[site_name] = {
        'version': site.version,
        'magnet': site.magnet_uri,
        'address': address,
    }
    return site.version, address, profile


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------

def _file_manifest(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for root, _, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel  = os.path.relpath(full, path)
            h = hashlib.sha1()
            with open(full, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            result[rel] = h.hexdigest()
    return result


def _torrent_info_hash(torrent_path: str) -> str | None:
    """Return the hex info-hash of a torrent file, or None on error."""
    try:
        return str(lt.torrent_info(torrent_path).info_hash())
    except Exception:
        return None


def _find_sub_version_dir(sub_data_dir: str, pub_torrent_path: str) -> str | None:
    """Return the subscriber version dir whose torrent matches the publisher's, or None.

    The subscriber uses its own 1-based directory numbering independently of
    the publisher, so version numbers cannot be compared directly; we match
    by torrent info-hash instead.
    """
    expected = _torrent_info_hash(pub_torrent_path)
    if expected is None:
        return None
    for ver in sorted(list_version_dirs(sub_data_dir), reverse=True):
        sub_torrent = os.path.join(sub_data_dir, str(ver), 'site.torrent')
        if not os.path.isfile(sub_torrent):
            continue
        if _torrent_info_hash(sub_torrent) == expected:
            return os.path.join(sub_data_dir, str(ver))
    return None


async def _wait_for_version(
    sub_data_dir: str, pub_torrent_path: str, timeout: float,
) -> str | None:
    """Poll until the subscriber has a complete version matching pub_torrent_path.

    Returns the subscriber's version directory on success, None on timeout.
    'Complete' means site.torrent present (libtorrent verified all pieces) and
    the site/ content directory exists.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ver_dir = _find_sub_version_dir(sub_data_dir, pub_torrent_path)
        if ver_dir and os.path.isdir(os.path.join(ver_dir, 'site')):
            return ver_dir
        await asyncio.sleep(5)
    return None


async def check_subscription(sub: Subscription, log: logging.Logger) -> list[str]:
    issues: list[str] = []
    pub = sub.publisher
    sub_data_dir = os.path.join(
        sub.subscriber.root_dir, 'data', 'sites', pub.npub, sub.site_name,
    )

    # (a) Liveness — subscriber must eventually have a version with matching content
    ver_dir = await _wait_for_version(
        sub_data_dir, sub.pub_torrent_path, LIVENESS_TIMEOUT,
    )
    if ver_dir is None:
        issues.append(
            f'LIVENESS: {sub.subscriber.name} did not receive '
            f'{sub.site_name} v{sub.pub_version} (from {pub.name}) '
            f'within {LIVENESS_TIMEOUT:.0f}s'
        )
        return issues

    # (b) Content integrity — file-by-file SHA-1 comparison
    pub_content = os.path.join(os.path.dirname(sub.pub_torrent_path), 'site')
    sub_content = os.path.join(ver_dir, 'site')
    if os.path.isdir(pub_content) and os.path.isdir(sub_content):
        pm = _file_manifest(pub_content)
        sm = _file_manifest(sub_content)
        if pm != sm:
            extra   = sorted(set(sm) - set(pm))
            missing = sorted(set(pm) - set(sm))
            changed = sorted(k for k in pm if k in sm and pm[k] != sm[k])
            issues.append(
                f'INTEGRITY: {sub.subscriber.name}/{sub.site_name} v{sub.pub_version}: '
                f'extra={extra} missing={missing} changed={changed}'
            )
    else:
        issues.append(
            f'INTEGRITY: content dir absent — '
            f'pub={os.path.isdir(pub_content)} sub={os.path.isdir(sub_content)}'
        )

    # (c) Version agreement — subscriber must eventually get the publisher's latest version
    latest_ver = pub.sites.get(sub.site_name, {}).get('version')
    latest_torrent = os.path.join(
        pub.root_dir, 'data', 'sites', pub.npub,
        sub.site_name, str(latest_ver), 'site.torrent',
    ) if latest_ver else None
    if latest_torrent and latest_torrent != sub.pub_torrent_path:
        latest_ver_dir = await _wait_for_version(
            sub_data_dir, latest_torrent, VERSION_TIMEOUT,
        )
        if latest_ver_dir is None:
            issues.append(
                f'VERSION: {sub.subscriber.name} has {sub.site_name} v{sub.pub_version} '
                f'but {pub.name} is now at v{latest_ver}'
            )

    return issues


# ---------------------------------------------------------------------------
# Stress loop
# ---------------------------------------------------------------------------

class StressTool:

    def __init__(self, n: int, stress_dir: str) -> None:
        self.n = n
        self.stress_dir = stress_dir
        self.daemons: list[DaemonInfo] = []
        self.subscriptions: list[Subscription] = []
        self.issues: list[str] = []
        self._log = logging.getLogger('stress')
        self._addresses_path = os.path.join(stress_dir, 'addresses.txt')
        self._saved_addresses: set[str] = set()
        # Load addresses already written in previous runs
        try:
            with open(self._addresses_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._saved_addresses.add(line)
        except OSError:
            pass

    # ---------------------------------------------------------------- setup

    async def setup(self) -> None:
        for i, name in enumerate(NATO[:self.n]):
            d = _setup_daemon(name, i, self.stress_dir)
            self.daemons.append(d)
            self._log.info('configured %s  port=%d  npub=...%s',
                           name, d.http_port, d.npub[-8:])

        for d in self.daemons:
            d.proc = _start_daemon(d)
            self._log.info('started %s  pid=%d  log=%s/daemon.log',
                           d.name, d.proc.pid, d.root_dir)

        self._log.info('addresses file: %s', self._addresses_path)
        self._log.info('waiting for daemons...')
        for d in self.daemons:
            if not await _wait_ready(d.http_port):
                self._log.error('%s did not become ready in time', d.name)
                sys.exit(1)
            self._log.info('  %s ready', d.name)

    def teardown(self) -> None:
        self._log.info('shutting down...')
        for d in self.daemons:
            if d.proc and d.proc.poll() is None:
                d.proc.terminate()
        for d in self.daemons:
            if d.proc:
                try:
                    d.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    d.proc.kill()

        total = len(self.subscriptions)
        checked = sum(1 for s in self.subscriptions if s.checked)
        self._log.info('subscriptions: %d total, %d checked', total, checked)
        if self.issues:
            self._log.warning('%d issue(s):', len(self.issues))
            for issue in self.issues:
                self._log.warning('  %s', issue)
        else:
            self._log.info('no consistency issues found')

    # --------------------------------------------------------- stress actions

    def _record_address(self, addr: str) -> None:
        """Append *addr* to addresses.txt if it has not been written before."""
        if addr in self._saved_addresses:
            return
        self._saved_addresses.add(addr)
        with open(self._addresses_path, 'a') as f:
            f.write(addr + '\n')

    async def _action_create(self) -> None:
        daemon = random.choice(self.daemons)
        site_name = _random_site_name()
        self._log.info('[%s] create %s', daemon.name, site_name)
        result = await _publish(daemon, site_name)
        if result:
            ver, addr, profile = result
            self._record_address(addr)
            self._log.info('[%s] published %s v%d  profile=%s  %s',
                           daemon.name, site_name, ver, profile, addr)
        else:
            self._log.warning('[%s] publish failed for %s', daemon.name, site_name)

    async def _action_update(self) -> None:
        with_sites = [d for d in self.daemons if d.sites]
        if not with_sites:
            await self._action_create()
            return
        daemon = random.choice(with_sites)
        site_name = random.choice(list(daemon.sites))
        self._log.info('[%s] update %s', daemon.name, site_name)
        result = await _publish(daemon, site_name)
        if result:
            ver, addr, profile = result
            self._record_address(addr)
            self._log.info('[%s] updated %s → v%d  profile=%s',
                           daemon.name, site_name, ver, profile)
        else:
            self._log.warning('[%s] update failed for %s', daemon.name, site_name)

    async def _action_subscribe(self) -> None:
        with_sites = [d for d in self.daemons if d.sites]
        if not with_sites:
            await self._action_create()
            return
        publisher = random.choice(with_sites)
        site_name = random.choice(list(publisher.sites))
        others = [d for d in self.daemons if d is not publisher]
        if not others:
            return
        subscriber = random.choice(others)
        info = publisher.sites[site_name]
        ver = info['version']
        ok = await _http_add(subscriber.http_port, info['address'])
        self._log.info(
            '[%s] subscribe %s/%s v%d → %s',
            subscriber.name, publisher.name, site_name, ver,
            'queued' if ok else 'FAILED',
        )
        if ok:
            pub_torrent_path = os.path.join(
                publisher.root_dir, 'data', 'sites', publisher.npub,
                site_name, str(ver), 'site.torrent',
            )
            self.subscriptions.append(Subscription(
                publisher=publisher,
                subscriber=subscriber,
                site_name=site_name,
                pub_version=ver,
                pub_torrent_path=pub_torrent_path,
                subscribed_at=time.monotonic(),
            ))

    async def _random_action(self) -> None:
        action = random.choices(
            ['create', 'update', 'subscribe'],
            weights=[30, 30, 40],
        )[0]
        if action == 'create':
            await self._action_create()
        elif action == 'update':
            await self._action_update()
        else:
            await self._action_subscribe()

    # --------------------------------------------------- consistency checks

    async def _run_checks(self) -> None:
        # Subscriptions that are at least 30s old and not yet checked
        pending = [
            s for s in self.subscriptions
            if not s.checked and time.monotonic() - s.subscribed_at >= 30
        ]
        if not pending:
            return
        self._log.info('=== consistency check (%d pending) ===', len(pending))
        for sub in pending:
            sub.checked = True
            self._log.info(
                'checking %s → %s/%s v%d',
                sub.subscriber.name, sub.publisher.name, sub.site_name, sub.pub_version,
            )
            issues = await check_subscription(sub, self._log)
            if issues:
                for issue in issues:
                    self._log.error('  ISSUE: %s', issue)
                    self.issues.append(issue)
            else:
                self._log.info(
                    '  OK: %s has %s v%d from %s',
                    sub.subscriber.name, sub.site_name, sub.pub_version, sub.publisher.name,
                )
        self._log.info('=== end check ===')

    # -------------------------------------------------------------- main loop

    async def run(self) -> None:
        last_stress = 0.0
        last_report = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_stress >= STRESS_INTERVAL:
                    await self._random_action()
                    last_stress = now
                if now - last_report >= REPORT_INTERVAL:
                    await self._run_checks()
                    last_report = now
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def main(self) -> None:
        await self.setup()
        try:
            await self.run()
        finally:
            self.teardown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='peerpage stress test',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Ctrl-C to stop; summary is printed on exit.',
    )
    parser.add_argument(
        'n', type=int, nargs='?', default=10,
        help=f'number of daemons to start (default: 10, max: {len(NATO)})',
    )
    parser.add_argument(
        '--dir', metavar='DIR',
        default=os.path.expanduser('~/peerpage-stress'),
        help='state directory (default: ~/peerpage-stress)',
    )
    parser.add_argument(
        '--clean', action='store_true',
        help='remove state directory on exit (default: keep for persistence)',
    )
    args = parser.parse_args()

    if args.n < 2:
        parser.error('need at least 2 daemons (one publisher, one subscriber)')
    if args.n > len(NATO):
        parser.error(f'max {len(NATO)} daemons')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s  %(message)s',
        datefmt='%H:%M:%S',
    )
    # Silence noisy third-party loggers
    for noisy in ('aiohttp', 'nostr_sdk', 'urllib3'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stress_dir = args.dir
    os.makedirs(stress_dir, exist_ok=True)
    logging.getLogger('stress').info(
        'dir=%s  daemons=%d  ports=%d-%d',
        stress_dir, args.n, BASE_HTTP_PORT, BASE_HTTP_PORT + args.n - 1,
    )

    tool = StressTool(args.n, stress_dir)
    try:
        asyncio.run(tool.main())
    finally:
        if args.clean:
            shutil.rmtree(stress_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
