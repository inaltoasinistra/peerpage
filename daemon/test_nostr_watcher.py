import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from nostr_sdk import Keys

from .nostr_watcher import NostrWatcher, NOSTR_POLL_INTERVAL


def _make_npub() -> str:
    return Keys.generate().public_key().to_bech32()


def _write_event_json(data_dir: str, npub: str, site_name: str,
                      version: int, event: dict) -> None:
    ver_path = os.path.join(data_dir, 'sites', npub, site_name, str(version))
    os.makedirs(ver_path, exist_ok=True)
    with open(os.path.join(ver_path, 'event.json'), 'w') as f:
        json.dump(event, f)


class TestSubscribe(unittest.IsolatedAsyncioTestCase):

    async def test_subscribe_schedules_fetch_and_write(self) -> None:
        npub = _make_npub()
        address = f'peerpage://site.{npub}'
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher('/nonexistent', nostr_client)
        watcher.subscribe('site', address)
        await asyncio.sleep(0)  # let the task run
        nostr_client.fetch_latest.assert_called_once()

    async def test_subscribe_writes_event_json_when_event_found(self) -> None:
        npub = _make_npub()
        address = f'peerpage://site.{npub}'
        with tempfile.TemporaryDirectory() as tmp:
            fake_event = {'id': 'ev1', 'created_at': 1000,
                          'tags': [['d', 'site'], ['magnet', 'magnet:?x']]}
            nostr_client = MagicMock()
            nostr_client.fetch_latest = AsyncMock(
                return_value={'magnet': 'magnet:?x', 'created_at': 1000,
                              'event': fake_event},
            )
            watcher = NostrWatcher(tmp, nostr_client)
            watcher.subscribe('site', address)
            await asyncio.sleep(0)
        nostr_client.fetch_latest.assert_called_once()



class TestLoadSubscriptions(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loads_from_event_json_in_last_version(self) -> None:
        npub = _make_npub()
        event = {'id': 'abc', 'created_at': 7777,
                 'tags': [['d', 'my-site'], ['magnet', 'magnet:?x']],
                 'pubkey': 'hex'}
        _write_event_json(self.data_dir, npub, 'my-site', 1, event)

        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()

        self.assertIn('my-site', watcher._subscriptions)
        sub = watcher._subscriptions['my-site']
        self.assertEqual(sub['pubkey'], npub)
        self.assertEqual(sub['identifier'], 'my-site')
        self.assertEqual(sub['last_seen_at'], 7778)
        self.assertEqual(sub['event_id'], 'abc')

    def test_last_seen_at_derived_from_event_created_at_plus_one(self) -> None:
        npub = _make_npub()
        event = {'id': 'xyz', 'created_at': 9999,
                 'tags': [['d', 's']], 'pubkey': 'hex'}
        _write_event_json(self.data_dir, npub, 's', 1, event)

        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()

        self.assertEqual(watcher._subscriptions['s']['last_seen_at'], 10000)

    def test_uses_latest_version_event(self) -> None:
        npub = _make_npub()
        old_event = {'id': 'old', 'created_at': 1000, 'tags': [['d', 's']], 'pubkey': 'hex'}
        new_event = {'id': 'new', 'created_at': 2000, 'tags': [['d', 's']], 'pubkey': 'hex'}
        _write_event_json(self.data_dir, npub, 's', 1, old_event)
        _write_event_json(self.data_dir, npub, 's', 2, new_event)

        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()

        self.assertEqual(watcher._subscriptions['s']['event_id'], 'new')
        self.assertEqual(watcher._subscriptions['s']['last_seen_at'], 2001)

    def test_falls_back_to_previous_version_when_latest_has_no_event_json(self) -> None:
        """An incomplete download dir (no event.json) should not block loading."""
        npub = _make_npub()
        event = {'id': 'abc', 'created_at': 5000, 'tags': [['d', 'site']], 'pubkey': 'hex'}
        _write_event_json(self.data_dir, npub, 'site', 3, event)
        # v4 exists (incomplete) but has no event.json
        os.makedirs(os.path.join(self.data_dir, 'sites', npub, 'site', '4'), exist_ok=True)

        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()

        self.assertIn('site', watcher._subscriptions)
        self.assertEqual(watcher._subscriptions['site']['event_id'], 'abc')

    def test_skips_pseudo_events_written_by_write_event(self) -> None:
        """event.json with id=None (from Watcher.write_event) must not create a subscription."""
        npub = _make_npub()
        pseudo_event = {'id': None, 'created_at': 5000,
                        'pubkey': 'remote', 'tags': [['magnet', 'magnet:?x']]}
        _write_event_json(self.data_dir, npub, 'site', 1, pseudo_event)

        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()

        self.assertEqual(watcher._subscriptions, {})

    def test_skips_sites_without_state_file(self) -> None:
        npub = _make_npub()
        os.makedirs(os.path.join(self.data_dir, 'sites', npub, 'plain-site'))
        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()
        self.assertEqual(watcher._subscriptions, {})

    def test_handles_missing_sites_dir(self) -> None:
        watcher = NostrWatcher('/nonexistent', MagicMock())
        watcher._load_subscriptions()  # should not raise
        self.assertEqual(watcher._subscriptions, {})

    def test_loads_multiple_subscriptions(self) -> None:
        npub = _make_npub()
        for i, name in enumerate(('alpha', 'beta')):
            event = {'id': f'e{i}', 'created_at': 1000, 'tags': [['d', name]], 'pubkey': 'hex'}
            _write_event_json(self.data_dir, npub, name, 1, event)
        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()
        self.assertIn('alpha', watcher._subscriptions)
        self.assertIn('beta', watcher._subscriptions)

    def test_non_dir_entry_in_sites_base_is_skipped(self) -> None:
        sites_base = os.path.join(self.data_dir, 'sites')
        os.makedirs(sites_base)
        open(os.path.join(sites_base, 'not_a_dir'), 'w').close()
        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()
        self.assertEqual(watcher._subscriptions, {})

    def test_non_dir_entry_in_npub_dir_is_skipped(self) -> None:
        npub = _make_npub()
        npub_dir = os.path.join(self.data_dir, 'sites', npub)
        os.makedirs(npub_dir)
        open(os.path.join(npub_dir, 'not_a_dir'), 'w').close()
        watcher = NostrWatcher(self.data_dir, MagicMock())
        watcher._load_subscriptions()
        self.assertEqual(watcher._subscriptions, {})


class TestFetchAndWrite(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_writes_event_json_on_new_event(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'ev1', 'created_at': 2000,
                      'tags': [['d', 'site'], ['magnet', 'magnet:?x']]}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 2000, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'site')

        site_data = os.path.join(self.data_dir, 'sites', npub, 'site')
        ver_dirs = [e for e in os.listdir(site_data) if e.isdigit()]
        self.assertEqual(len(ver_dirs), 1)
        event_path = os.path.join(site_data, ver_dirs[0], 'event.json')
        self.assertTrue(os.path.isfile(event_path))
        with open(event_path) as f:
            self.assertEqual(json.load(f)['id'], 'ev1')

    async def test_does_nothing_when_fetch_returns_none(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'site')

        sites_base = os.path.join(self.data_dir, 'sites')
        self.assertFalse(os.path.isdir(sites_base))

    async def test_skips_already_seen_event_id(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'same-id', 'created_at': 5000}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 5000, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'site',
            'last_seen_at': 5001, 'event_id': 'same-id',
        }

        await watcher._fetch_and_write('site', npub, 'site')

        sites_base = os.path.join(self.data_dir, 'sites')
        self.assertFalse(os.path.isdir(sites_base))

    async def test_updates_subscriptions_after_writing(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'new-ev', 'created_at': 9999}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 9999, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'remote-site')

        self.assertEqual(watcher._subscriptions['site']['event_id'], 'new-ev')
        self.assertEqual(watcher._subscriptions['site']['last_seen_at'], 10000)
        self.assertEqual(watcher._subscriptions['site']['identifier'], 'remote-site')

    async def test_passes_since_to_fetch_latest(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'remote', since=5555)

        nostr_client.fetch_latest.assert_called_once_with(npub, 'remote', since=5555, extra_relays=None)

    async def test_increments_version_dir_on_second_event(self) -> None:
        """Without a version tag, falls back to _next_version()."""
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(side_effect=[
            {'magnet': 'magnet:?x', 'created_at': 1000,
             'event': {'id': 'e1', 'created_at': 1000}},
            {'magnet': 'magnet:?y', 'created_at': 2000,
             'event': {'id': 'e2', 'created_at': 2000}},
        ])
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'site')
        await watcher._fetch_and_write('site', npub, 'site')

        site_data = os.path.join(self.data_dir, 'sites', npub, 'site')
        ver_dirs = sorted(int(e) for e in os.listdir(site_data) if e.isdigit())
        self.assertEqual(ver_dirs, [1, 2])

    async def test_ignores_version_tag_always_uses_next_version(self) -> None:
        """Event with ['version', '868'] must land in dir 869 (_next_version), not 868."""
        npub = _make_npub()
        # Pre-create dir 868 (simulating a publisher run that already built the torrent)
        existing_dir = os.path.join(self.data_dir, 'sites', npub, 'site', '868')
        os.makedirs(existing_dir, exist_ok=True)
        fake_event = {'id': 'ev868', 'created_at': 5000,
                      'tags': [['d', 'site'], ['magnet', 'magnet:?x'], ['version', '868']]}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 5000, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._fetch_and_write('site', npub, 'site')

        site_data = os.path.join(self.data_dir, 'sites', npub, 'site')
        ver_dirs = sorted(int(e) for e in os.listdir(site_data) if e.isdigit())
        self.assertEqual(ver_dirs, [868, 869])  # event.json goes into 869, not 868
        with open(os.path.join(site_data, '869', 'event.json')) as f:
            self.assertEqual(json.load(f)['id'], 'ev868')


class TestDiscoverFollowed(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_discovers_new_site_via_fetch_latest(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_all_sites = AsyncMock(return_value=[
            {'identifier': 'cool-site', 'magnet': 'magnet:?xt=urn:btih:abc', 'created_at': 1000},
        ])
        fake_event = {'id': 'ev1', 'created_at': 1000,
                      'tags': [['d', 'cool-site'], ['magnet', 'magnet:?xt=urn:btih:abc']]}
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?xt=urn:btih:abc', 'created_at': 1000,
                          'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client, followed_npubs=[npub])

        await watcher._discover_followed()

        nostr_client.fetch_latest.assert_called_once()
        self.assertIn('cool-site', watcher._subscriptions)

    async def test_writes_event_json_for_discovered_site(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'ev1', 'created_at': 1000,
                      'tags': [['d', 'site'], ['magnet', 'magnet:?x']]}
        nostr_client = MagicMock()
        nostr_client.fetch_all_sites = AsyncMock(return_value=[
            {'identifier': 'site', 'magnet': 'magnet:?x', 'created_at': 1000},
        ])
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 1000, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client, followed_npubs=[npub])

        await watcher._discover_followed()

        site_data = os.path.join(self.data_dir, 'sites', npub, 'site')
        self.assertTrue(os.path.isdir(site_data))
        ver_dirs = [e for e in os.listdir(site_data) if e.isdigit()]
        self.assertEqual(len(ver_dirs), 1)
        event_path = os.path.join(site_data, ver_dirs[0], 'event.json')
        self.assertTrue(os.path.isfile(event_path))

    async def test_skips_already_subscribed_site(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_all_sites = AsyncMock(return_value=[
            {'identifier': 'cool-site', 'magnet': 'magnet:?xt=urn:btih:abc', 'created_at': 1000},
        ])
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher(self.data_dir, nostr_client, followed_npubs=[npub])
        watcher._subscriptions['cool-site'] = {'pubkey': npub, 'identifier': 'cool-site',
                                               'last_seen_at': 500, 'event_id': 'x'}

        await watcher._discover_followed()

        nostr_client.fetch_latest.assert_not_called()

    async def test_discovers_from_multiple_followed_npubs(self) -> None:
        npub_a = _make_npub()
        npub_b = _make_npub()

        async def fetch_all_sites(npub: str) -> list[dict]:
            if npub == npub_a:
                return [{'identifier': 'site-a', 'magnet': 'magnet:?a', 'created_at': 100}]
            return [{'identifier': 'site-b', 'magnet': 'magnet:?b', 'created_at': 200}]

        async def fetch_latest(pub: str, ident: str, since: int = 0, extra_relays=None) -> dict | None:
            return {'magnet': 'magnet:?x', 'created_at': 100,
                    'event': {'id': f'ev-{ident}', 'created_at': 100,
                              'tags': [['d', ident], ['magnet', 'magnet:?x']]}}

        nostr_client = MagicMock()
        nostr_client.fetch_all_sites = fetch_all_sites
        nostr_client.fetch_latest = fetch_latest
        watcher = NostrWatcher(self.data_dir, nostr_client, followed_npubs=[npub_a, npub_b])

        await watcher._discover_followed()

        self.assertIn('site-a', watcher._subscriptions)
        self.assertIn('site-b', watcher._subscriptions)

    async def test_no_followed_npubs_does_nothing(self) -> None:
        nostr_client = MagicMock()
        nostr_client.fetch_all_sites = AsyncMock()
        watcher = NostrWatcher(self.data_dir, nostr_client)

        await watcher._discover_followed()

        nostr_client.fetch_all_sites.assert_not_called()


class TestCheckUpdates(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_writes_event_json_when_new_event_found(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'ev-new', 'created_at': 2000,
                      'tags': [['d', 'site'], ['magnet', 'magnet:?x']]}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 2000, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'site', 'last_seen_at': 1000,
        }

        await watcher._check_updates()

        site_data = os.path.join(self.data_dir, 'sites', npub, 'site')
        self.assertTrue(os.path.isdir(site_data))

    async def test_does_not_write_when_no_new_events(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'site', 'last_seen_at': 1000,
        }

        await watcher._check_updates()

        sites_base = os.path.join(self.data_dir, 'sites')
        self.assertFalse(os.path.isdir(sites_base))

    async def test_does_not_re_trigger_same_event_by_id(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 5000,
                          'event': {'id': 'same-id', 'created_at': 5000}},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'remote',
            'last_seen_at': 5001, 'event_id': 'same-id',
        }

        await watcher._check_updates()

        sites_base = os.path.join(self.data_dir, 'sites')
        self.assertFalse(os.path.isdir(sites_base))

    async def test_passes_since_to_fetch_latest(self) -> None:
        npub = _make_npub()
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(return_value=None)
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'remote', 'last_seen_at': 5555,
        }

        await watcher._check_updates()

        nostr_client.fetch_latest.assert_called_once_with(npub, 'remote', since=5555, extra_relays=[])

    async def test_updates_last_seen_at_and_event_id(self) -> None:
        npub = _make_npub()
        fake_event = {'id': 'abc', 'created_at': 9999}
        nostr_client = MagicMock()
        nostr_client.fetch_latest = AsyncMock(
            return_value={'magnet': 'magnet:?x', 'created_at': 9999, 'event': fake_event},
        )
        watcher = NostrWatcher(self.data_dir, nostr_client)
        watcher._subscriptions['site'] = {
            'pubkey': npub, 'identifier': 'remote-site', 'last_seen_at': 1000,
        }

        await watcher._check_updates()

        self.assertEqual(watcher._subscriptions['site']['last_seen_at'], 10000)
        self.assertEqual(watcher._subscriptions['site']['event_id'], 'abc')


class TestRun(unittest.IsolatedAsyncioTestCase):

    async def test_run_loads_subscriptions_then_loops(self) -> None:
        watcher = NostrWatcher('/nonexistent', MagicMock())
        load_calls = []
        check_calls = []

        async def fake_check() -> None:
            check_calls.append(1)
            if len(check_calls) >= 2:
                raise asyncio.CancelledError

        watcher._load_subscriptions = lambda: load_calls.append(1)
        watcher._check_updates = fake_check

        with patch('asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await watcher.run()

        self.assertEqual(len(load_calls), 1)
        self.assertGreaterEqual(len(check_calls), 2)


if __name__ == '__main__':
    unittest.main()
