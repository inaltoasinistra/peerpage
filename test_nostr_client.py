import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from nostr_sdk import (
    Coordinate, EventBuilder, Keys, Kind, Nip19Coordinate, RelayUrl, Tag, TagKind,
)

from config import Config, NostrConfig, NOSTR_KIND
from nostr_client import NostrClient, PROTOCOL_VERSION


def _make_config(relays: list[str] | None = None) -> Config:
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    return Config(nostr=NostrConfig(
        private_key=nsec,
        relays=relays if relays is not None else ['wss://relay.damus.io'],
    ))


_DEFAULT_PROTOCOL = object()  # sentinel: include PROTOCOL_VERSION tag by default


def _make_event(keys: Keys, identifier: str, magnet: str,
                created_at_secs: int | None = None,
                protocol: object = _DEFAULT_PROTOCOL) -> object:
    tags = [Tag.identifier(identifier), Tag.parse(['magnet', magnet])]
    if protocol is _DEFAULT_PROTOCOL:
        tags.append(Tag.parse(['protocol', PROTOCOL_VERSION]))
    elif protocol is not None:
        tags.append(Tag.parse(['protocol', str(protocol)]))
    builder = EventBuilder(Kind(NOSTR_KIND), 'changelog').tags(tags)
    if created_at_secs is not None:
        from nostr_sdk import Timestamp
        builder = builder.custom_created_at(Timestamp.from_secs(created_at_secs))
    return builder.sign_with_keys(keys)


class TestParseAddress(unittest.TestCase):

    def setUp(self) -> None:
        self.keys = Keys.generate()
        self.npub = self.keys.public_key().to_bech32()

    def test_peerpage_format(self) -> None:
        address = f'peerpage://my-site.{self.npub}'
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        self.assertEqual(pubkey_str, self.npub)
        self.assertEqual(identifier, 'my-site')
        self.assertEqual(relays, [])

    def test_peerpage_format_with_relay_hints(self) -> None:
        address = f'peerpage://my-site.{self.npub}?relay=wss%3A%2F%2Frelay.damus.io&relay=wss%3A%2F%2Fnos.lol'
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        self.assertEqual(pubkey_str, self.npub)
        self.assertEqual(identifier, 'my-site')
        self.assertEqual(relays, ['wss://relay.damus.io', 'wss://nos.lol'])

    def test_naddr1_format(self) -> None:
        coord = Coordinate(Kind(NOSTR_KIND), self.keys.public_key(), 'their-site')
        nip19 = Nip19Coordinate(coord, [RelayUrl.parse('wss://relay.damus.io')])
        naddr = nip19.to_bech32()
        pubkey_str, identifier, relays = NostrClient.parse_address(naddr)
        self.assertEqual(pubkey_str, self.npub)
        self.assertEqual(identifier, 'their-site')
        self.assertIn('wss://relay.damus.io', relays)

    def test_dot_in_site_name(self) -> None:
        address = f'peerpage://my.site.{self.npub}'
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        self.assertEqual(pubkey_str, self.npub)
        self.assertEqual(identifier, 'my.site')

    def test_npub1_in_site_name_splits_at_last_occurrence(self) -> None:
        # rfind ensures the last '.npub1' is the separator
        address = f'peerpage://my.npub1thing.{self.npub}'
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        self.assertEqual(pubkey_str, self.npub)
        self.assertEqual(identifier, 'my.npub1thing')

    def test_invalid_peerpage_address_raises(self) -> None:
        with self.assertRaises(ValueError):
            NostrClient.parse_address('peerpage://no-npub-here')

    def test_unrecognized_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            NostrClient.parse_address('http://example.com')


class TestSiteAddress(unittest.TestCase):

    def test_returns_correct_format(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        address = client.site_address('my-site')
        self.assertTrue(address.startswith('peerpage://my-site.npub1'))
        # Round-trip parse
        pubkey_str, identifier, relays = NostrClient.parse_address(address)
        self.assertEqual(pubkey_str, client.pubkey_bech32())
        self.assertEqual(identifier, 'my-site')
        self.assertIn('wss://relay.damus.io', relays)

    def test_includes_relay_hints(self) -> None:
        cfg = _make_config(relays=['wss://r1', 'wss://r2'])
        client = NostrClient(cfg)
        address = client.site_address('my-site')
        _, _, relays = NostrClient.parse_address(address)
        self.assertEqual(set(relays), {'wss://r1', 'wss://r2'})


class TestNaddrAddress(unittest.TestCase):

    def test_returns_naddr1_format(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        naddr = client.naddr_address('my-site')
        self.assertTrue(naddr.startswith('naddr1'))
        # Round-trip parse
        pubkey_str, identifier, relays = NostrClient.parse_address(naddr)
        self.assertEqual(pubkey_str, client.pubkey_bech32())
        self.assertEqual(identifier, 'my-site')
        self.assertIn('wss://relay.damus.io', relays)

    def test_includes_relay_hints(self) -> None:
        cfg = _make_config(relays=['wss://r1', 'wss://r2'])
        client = NostrClient(cfg)
        naddr = client.naddr_address('my-site')
        _, _, relays = NostrClient.parse_address(naddr)
        self.assertEqual(set(relays), {'wss://r1', 'wss://r2'})


class TestPubkeyBech32(unittest.TestCase):

    def test_returns_bech32_npub(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        npub = client.pubkey_bech32()
        self.assertTrue(npub.startswith('npub1'))


class TestPublish(unittest.IsolatedAsyncioTestCase):

    async def test_publish_sends_event_with_correct_tags(self) -> None:
        keys = Keys.generate()
        cfg = Config(nostr=NostrConfig(
            private_key=keys.secret_key().to_bech32(),
            relays=['wss://r1', 'wss://r2'],
        ))
        client = NostrClient(cfg)
        captured = []

        mock_instance = AsyncMock()
        mock_instance.send_event.side_effect = lambda e: captured.append(e)

        with patch('nostr_client.Client', return_value=mock_instance):
            await client.publish('my-site', 'magnet:?xt=urn:btih:abc', 'changelog text', version=3)

        mock_instance.send_event.assert_called_once()
        event = captured[0]
        self.assertEqual(event.content(), 'changelog text')
        self.assertEqual(event.tags().identifier(), 'my-site')
        magnet_tag = event.tags().find(TagKind.MAGNET())
        self.assertIsNotNone(magnet_tag)
        self.assertEqual(magnet_tag.content(), 'magnet:?xt=urn:btih:abc')
        # r tags for each relay
        r_tags = [t for t in event.tags().to_vec() if t.as_vec()[0] == 'r']
        self.assertEqual(len(r_tags), 2)
        # user_agent tag
        ua_tags = [t for t in event.tags().to_vec() if t.as_vec()[0] == 'user_agent']
        self.assertEqual(len(ua_tags), 1)
        self.assertTrue(ua_tags[0].as_vec()[1].startswith('Peerpage/'))
        # protocol tag
        proto_tags = [t for t in event.tags().to_vec() if t.as_vec()[0] == 'protocol']
        self.assertEqual(len(proto_tags), 1)
        self.assertEqual(proto_tags[0].as_vec()[1], PROTOCOL_VERSION)
        # version tag
        ver_tags = [t for t in event.tags().to_vec() if t.as_vec()[0] == 'version']
        self.assertEqual(len(ver_tags), 1)
        self.assertEqual(ver_tags[0].as_vec()[1], '3')

    async def test_publish_returns_event_dict(self) -> None:
        keys = Keys.generate()
        cfg = Config(nostr=NostrConfig(
            private_key=keys.secret_key().to_bech32(),
            relays=['wss://r1'],
        ))
        client = NostrClient(cfg)
        mock_instance = AsyncMock()

        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.publish('site', 'magnet:?x', 'log', version=1)

        self.assertIsNotNone(result)
        self.assertIn('id', result)
        self.assertIn('pubkey', result)
        self.assertEqual(result['content'], 'log')

    async def test_publish_tolerates_relay_failure(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.add_relay.side_effect = Exception('connection refused')

        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING') as log:
                await client.publish('site', 'magnet:?x', 'log', version=1)

        self.assertTrue(any('failed to add relay' in line for line in log.output))

    async def test_publish_tolerates_send_failure(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.send_event.side_effect = Exception('relay error')

        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING') as log:
                result = await client.publish('site', 'magnet:?x', 'log', version=1)

        self.assertTrue(any('failed to publish' in line for line in log.output))
        self.assertIsNone(result)

    async def test_publish_always_disconnects(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.connect.side_effect = Exception('fail')

        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING'):
                await client.publish('site', 'magnet:?x', 'log', version=1)

        mock_instance.disconnect.assert_called_once()


class TestFetchLatest(unittest.IsolatedAsyncioTestCase):

    async def test_returns_magnet_and_created_at(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'my-site', 'magnet:?xt=urn:btih:abc', 1740000000)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'my-site', since=0)

        self.assertIsNotNone(result)
        self.assertEqual(result['magnet'], 'magnet:?xt=urn:btih:abc')
        self.assertEqual(result['created_at'], 1740000000)
        self.assertIn('event', result)
        self.assertEqual(result['event']['created_at'], 1740000000)
        self.assertIn('id', result['event'])
        self.assertIn('sig', result['event'])

    async def test_returns_none_when_no_events(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = []
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'my-site')

        self.assertIsNone(result)

    async def test_returns_most_recent_event(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        old_event = _make_event(keys, 'my-site', 'magnet:?xt=urn:btih:old', 1700000000)
        new_event = _make_event(keys, 'my-site', 'magnet:?xt=urn:btih:new', 1800000000)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [old_event, new_event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'my-site')

        self.assertEqual(result['magnet'], 'magnet:?xt=urn:btih:new')

    async def test_returns_none_on_relay_error(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.fetch_events.side_effect = Exception('no relays')

        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING'):
                result = await client.fetch_latest(npub, 'my-site')

        self.assertIsNone(result)

    async def test_tolerates_relay_add_failure(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.add_relay.side_effect = Exception('refused')
        mock_instance.fetch_events.side_effect = Exception('no relays')

        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING') as log:
                result = await client.fetch_latest(npub, 'my-site')

        self.assertIsNone(result)
        self.assertTrue(any('failed to add relay' in line for line in log.output))

    async def test_extra_relays_are_added(self) -> None:
        cfg = _make_config(relays=['wss://configured'])
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site', 'magnet:?xt=urn:btih:abc', 1000)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            await client.fetch_latest(npub, 'site', extra_relays=['wss://hint'])
        added = [str(c.args[0]) for c in mock_instance.add_relay.call_args_list]
        self.assertIn('wss://configured', added)
        self.assertIn('wss://hint', added)

    async def test_extra_relays_skips_already_configured(self) -> None:
        cfg = _make_config(relays=['wss://r1'])
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site', 'magnet:?xt=urn:btih:abc', 1000)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            await client.fetch_latest(npub, 'site', extra_relays=['wss://r1'])
        # wss://r1 should be added only once
        added = [str(c.args[0]) for c in mock_instance.add_relay.call_args_list]
        self.assertEqual(added.count('wss://r1'), 1)

    async def test_returns_none_when_no_magnet_tag(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        # Event without magnet tag
        event = EventBuilder(Kind(NOSTR_KIND), 'content').tags(
            [Tag.identifier('my-site')]
        ).sign_with_keys(keys)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'my-site')

        self.assertIsNone(result)


class TestFetchAllSites(unittest.IsolatedAsyncioTestCase):

    async def test_returns_sites_from_npub(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:aaa', 1740000000)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['identifier'], 'site-a')
        self.assertEqual(result[0]['magnet'], 'magnet:?xt=urn:btih:aaa')
        self.assertEqual(result[0]['created_at'], 1740000000)

    async def test_deduplicates_by_identifier_keeps_latest(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        old_event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:old', 1700000000)
        new_event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:new', 1800000000)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [old_event, new_event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['magnet'], 'magnet:?xt=urn:btih:new')

    async def test_returns_multiple_sites(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event_a = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:aaa', 1000)
        event_b = _make_event(keys, 'site-b', 'magnet:?xt=urn:btih:bbb', 2000)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event_a, event_b]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)

        identifiers = {r['identifier'] for r in result}
        self.assertEqual(identifiers, {'site-a', 'site-b'})

    async def test_returns_empty_on_relay_error(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        mock_instance = AsyncMock()
        mock_instance.fetch_events.side_effect = Exception('no relays')

        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            with self.assertLogs('nostr_client', level='WARNING'):
                result = await client.fetch_all_sites(npub)

        self.assertEqual(result, [])

    async def test_skips_events_without_magnet_tag(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = EventBuilder(Kind(NOSTR_KIND), 'content').tags(
            [Tag.identifier('site-a')]
        ).sign_with_keys(keys)

        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)

        self.assertEqual(result, [])


class TestProtocolFiltering(unittest.IsolatedAsyncioTestCase):

    async def test_fetch_latest_accepts_matching_protocol(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site', 'magnet:?xt=urn:btih:abc', 1000,
                            protocol=PROTOCOL_VERSION)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'site')
        self.assertIsNotNone(result)

    async def test_fetch_latest_rejects_no_protocol_tag(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site', 'magnet:?xt=urn:btih:abc', 1000, protocol=None)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'site')
        self.assertIsNone(result)

    async def test_fetch_all_sites_rejects_no_protocol_tag(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:abc', 1000, protocol=None)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)
        self.assertEqual(result, [])

    async def test_fetch_latest_rejects_incompatible_protocol(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site', 'magnet:?xt=urn:btih:abc', 1000, protocol='99')
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_latest(npub, 'site')
        self.assertIsNone(result)

    async def test_fetch_all_sites_rejects_incompatible_protocol(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:abc', 1000, protocol='99')
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)
        self.assertEqual(result, [])

    async def test_fetch_all_sites_accepts_matching_protocol(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        event = _make_event(keys, 'site-a', 'magnet:?xt=urn:btih:abc', 1000,
                            protocol=PROTOCOL_VERSION)
        mock_events = MagicMock()
        mock_events.to_vec.return_value = [event]
        mock_instance = AsyncMock()
        mock_instance.fetch_events.return_value = mock_events

        npub = keys.public_key().to_bech32()
        with patch('nostr_client.Client', return_value=mock_instance):
            result = await client.fetch_all_sites(npub)
        self.assertEqual(len(result), 1)


class TestFetchMagnet(unittest.IsolatedAsyncioTestCase):

    async def test_returns_magnet_string(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        address = f'peerpage://my-site.{npub}'

        with patch.object(client, 'fetch_latest', new=AsyncMock(
            return_value={'magnet': 'magnet:?xt=urn:btih:abc', 'created_at': 1000},
        )):
            result = await client.fetch_magnet(address)

        self.assertEqual(result, 'magnet:?xt=urn:btih:abc')

    async def test_returns_none_when_fetch_latest_returns_none(self) -> None:
        cfg = _make_config()
        client = NostrClient(cfg)
        keys = Keys.generate()
        npub = keys.public_key().to_bech32()
        address = f'peerpage://my-site.{npub}'

        with patch.object(client, 'fetch_latest', new=AsyncMock(return_value=None)):
            result = await client.fetch_magnet(address)

        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
