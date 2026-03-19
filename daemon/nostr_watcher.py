import asyncio
import json
import logging
import os
import time

from fileutil import atomic_open, get_tag, iter_sites, list_version_dirs
from nostr_client import NostrClient
from .watcher import _next_version

logger = logging.getLogger(__name__)

NOSTR_POLL_INTERVAL = 30.0


class NostrWatcher:
    """Polls Nostr for site events and writes event.json files to version dirs.

    This component has no callbacks.  When a new or updated Nostr event is
    found it creates a new version directory under
    DATA_DIR/sites/<npub>/<site>/<ver>/ and writes event.json into it.
    The Watcher component picks those up and drives the actual download.
    """

    def __init__(self, data_dir: str, nostr_client: NostrClient,
                 followed_npubs: list[str] | None = None) -> None:
        self._data_dir = data_dir
        self._nostr_client = nostr_client
        self._followed_npubs: list[str] = followed_npubs or []
        self._subscriptions: dict[str, dict] = {}  # site_name -> sub dict

    def subscribe(self, site_name: str, address: str) -> None:
        """Fetch the latest Nostr event for *address* and write event.json.

        Schedules an async task; returns immediately.
        """
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        asyncio.create_task(
            self._fetch_and_write(site_name, pubkey_str, identifier, extra_relays=relays)
        )

    async def _fetch_and_write(self, site_name: str, pubkey_str: str,
                               identifier: str, since: int = 0,
                               extra_relays: list[str] | None = None) -> None:
        """Fetch the latest event and, if new, write event.json to a version dir."""
        result = await self._nostr_client.fetch_latest(
            pubkey_str, identifier, since=since, extra_relays=extra_relays,
        )
        if result is None:
            return
        event = result['event']
        if event['id'] == self._subscriptions.get(site_name, {}).get('event_id'):
            logger.debug('no new event for %s (event %s already processed)',
                         site_name, event['id'][:8])
            return
        self._write_event_dir(site_name, pubkey_str, event)
        self._subscriptions[site_name] = {
            'pubkey': pubkey_str,
            'identifier': identifier,
            'last_seen_at': result['created_at'] + 1,
            'event_id': event['id'],
            'relays': extra_relays or [],
        }
        logger.info('new event for %s: %s', site_name, event['id'][:8])

    def _write_event_dir(self, site_name: str, pubkey_str: str, event: dict) -> None:
        """Create a version directory and write event.json into it."""
        site_data = os.path.join(self._data_dir, 'sites', pubkey_str, site_name)
        ver = _next_version(site_data)
        ver_dir = os.path.join(site_data, str(ver))
        os.makedirs(ver_dir, exist_ok=True)
        with atomic_open(os.path.join(ver_dir, 'event.json')) as f:
            json.dump(event, f, indent=2)
        logger.debug('created %s v%d', site_name, ver)

    def _load_subscriptions(self) -> None:
        """Read existing event.json files on disk to populate _subscriptions."""
        sites_base = os.path.join(self._data_dir, 'sites')
        for npub, site_name, site_dir in iter_sites(sites_base):
            sub = self._load_subscription(npub, site_name, site_dir)
            if sub is not None:
                self._subscriptions[site_name] = sub

    @staticmethod
    def _load_subscription(npub: str, site_name: str, site_dir: str) -> dict | None:
        """Return subscription dict from the newest version dir that has event.json."""
        for ver in sorted(list_version_dirs(site_dir), reverse=True):
            event_json = os.path.join(site_dir, str(ver), 'event.json')
            if not os.path.isfile(event_json):
                continue
            with open(event_json) as f:
                event = json.load(f)
            if not event.get('id'):
                continue  # skip pseudo-events written by write_event()
            relays = [t[1] for t in event.get('tags', [])
                      if isinstance(t, list) and len(t) >= 2 and t[0] == 'r']
            return {
                'pubkey': npub,
                'identifier': get_tag(event, 'd', site_name),
                'last_seen_at': event['created_at'] + 1,
                'event_id': event['id'],
                'relays': relays,
            }
        return None

    async def _discover_followed(self) -> None:
        """Discover new sites from followed npubs and write event.json for new ones."""
        for npub in self._followed_npubs:
            sites = await self._nostr_client.fetch_all_sites(npub)
            for site in sites:
                identifier = site['identifier']
                if identifier not in self._subscriptions:
                    await self._fetch_and_write(identifier, npub, identifier, since=0)

    async def _check_updates(self) -> None:
        """Poll Nostr for updates to tracked sites and write event.json when new."""
        for identifier, sub in list(self._subscriptions.items()):
            await self._fetch_and_write(
                identifier, sub['pubkey'], sub['identifier'],
                since=sub['last_seen_at'],
                extra_relays=sub.get('relays', []),
            )

    async def run(self) -> None:
        self._load_subscriptions()
        while True:
            await self._discover_followed()
            await self._check_updates()
            await asyncio.sleep(NOSTR_POLL_INTERVAL)
