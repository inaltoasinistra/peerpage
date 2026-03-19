import json
import logging
from datetime import timedelta
from urllib.parse import quote, unquote

from nostr_sdk import (
    Client, EventBuilder, Filter, Kind, Keys, Nip19Coordinate, NostrSigner,
    PublicKey, RelayUrl, SecretKey, Tag, TagKind, Timestamp, Coordinate,
)

from config import Config, NOSTR_KIND
from fileutil import get_tag
from version import get_user_agent

PROTOCOL_VERSION = '-1'


def _protocol_ok(event_json: dict) -> bool:
    """Return True if the event's protocol tag is compatible with ours."""
    return get_tag(event_json, 'protocol') == PROTOCOL_VERSION

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = timedelta(seconds=10)


class NostrClient:

    def __init__(self, config: Config) -> None:
        self._config = config
        self._keys = Keys(SecretKey.parse(config.nostr.private_key))

    async def _add_relays(self, client: Client) -> None:
        """Add all configured relays to *client*, logging and skipping failures."""
        for relay in self._config.nostr.relays:
            try:
                await client.add_relay(RelayUrl.parse(relay))
            except Exception as e:
                logger.warning('failed to add relay %s: %s', relay, e)

    async def publish(self, identifier: str, magnet_uri: str, changelog: str,
                      version: int) -> dict | None:
        tags = [Tag.identifier(identifier), Tag.parse(['magnet', magnet_uri])]
        tags += [Tag.parse(['r', relay]) for relay in self._config.nostr.relays]
        tags.append(Tag.parse(['protocol', PROTOCOL_VERSION]))
        tags.append(Tag.parse(['user_agent', get_user_agent()]))
        tags.append(Tag.parse(['version', str(version)]))
        event = EventBuilder(Kind(NOSTR_KIND), changelog).tags(tags).sign_with_keys(self._keys)
        logger.debug('publishing %s (event %s) to %s',
                     identifier, event.id().to_hex()[:8], self._config.nostr.relays)
        client = Client(NostrSigner.keys(self._keys))
        try:
            await self._add_relays(client)
            await client.connect()
            await client.send_event(event)
            logger.info('published %s event %s', identifier, event.id().to_hex()[:8])
            return json.loads(event.as_json())
        except Exception as e:
            logger.warning('failed to publish to Nostr: %s', e)
            return None
        finally:
            await client.disconnect()

    async def fetch_latest(self, pubkey_str: str, identifier: str,
                           since: int = 0,
                           extra_relays: list[str] | None = None) -> dict | None:
        logger.debug('fetch_latest %s/%s since=%s', pubkey_str[:8], identifier, since)
        pubkey = PublicKey.parse(pubkey_str)
        f = (Filter()
             .kind(Kind(NOSTR_KIND))
             .author(pubkey)
             .identifier(identifier)
             .since(Timestamp.from_secs(since)))
        client = Client(NostrSigner.keys(self._keys))
        try:
            await self._add_relays(client)
            if extra_relays:
                configured = set(self._config.nostr.relays)
                for relay in extra_relays:
                    if relay not in configured:
                        try:
                            await client.add_relay(RelayUrl.parse(relay))
                        except Exception as e:
                            logger.warning('failed to add hint relay %s: %s', relay, e)
            await client.connect()
            events = await client.fetch_events(f, timeout=_FETCH_TIMEOUT)
        except Exception as e:
            logger.warning('failed to fetch from Nostr: %s', e)
            return None
        finally:
            await client.disconnect()
        vec = [e for e in events.to_vec() if _protocol_ok(json.loads(e.as_json()))]
        logger.debug('fetch_latest %s/%s: %d event(s)', pubkey_str[:8], identifier, len(vec))
        if not vec:
            return None
        best = max(vec, key=lambda e: e.created_at().as_secs())
        magnet_tag = best.tags().find(TagKind.MAGNET())
        if magnet_tag is None:
            return None
        logger.debug('fetch_latest %s/%s: selected event %s created_at=%s',
                     pubkey_str[:8], identifier,
                     best.id().to_hex()[:8], best.created_at().as_secs())
        return {
            'magnet': magnet_tag.content(),
            'created_at': best.created_at().as_secs(),
            'event': json.loads(best.as_json()),
        }

    async def fetch_all_sites(self, pubkey_str: str) -> list[dict]:
        logger.debug('fetch_all_sites %s', pubkey_str[:8])
        pubkey = PublicKey.parse(pubkey_str)
        f = Filter().kind(Kind(NOSTR_KIND)).author(pubkey)
        client = Client(NostrSigner.keys(self._keys))
        try:
            await self._add_relays(client)
            await client.connect()
            events = await client.fetch_events(f, timeout=_FETCH_TIMEOUT)
        except Exception as e:
            logger.warning('failed to fetch all sites from Nostr: %s', e)
            return []
        finally:
            await client.disconnect()
        best: dict[str, tuple[int, str]] = {}  # identifier -> (created_at, magnet)
        for event in events.to_vec():
            if not _protocol_ok(json.loads(event.as_json())):
                continue
            magnet_tag = event.tags().find(TagKind.MAGNET())
            if magnet_tag is None:
                continue
            identifier = event.tags().identifier()
            if not identifier:
                continue
            ts = event.created_at().as_secs()
            if identifier not in best or ts > best[identifier][0]:
                best[identifier] = (ts, magnet_tag.content())
        sites = [
            {'identifier': ident, 'magnet': magnet, 'created_at': ts}
            for ident, (ts, magnet) in best.items()
        ]
        logger.debug('fetch_all_sites %s: %d site(s) found', pubkey_str[:8], len(sites))
        return sites

    async def fetch_magnet(self, address: str) -> str | None:
        pubkey_str, identifier, relays = self.parse_address(address)
        result = await self.fetch_latest(pubkey_str, identifier, extra_relays=relays)
        if result is None:
            return None
        return result['magnet']

    def site_address(self, identifier: str) -> str:
        npub = self._keys.public_key().to_bech32()
        base = f'peerpage://{identifier}.{npub}'
        if self._config.nostr.relays:
            def _relay_host(r: str) -> str:
                return r[len('wss://'):] if r.startswith('wss://') else quote(r, safe='')
            qs = '&'.join(f'r={_relay_host(r)}' for r in self._config.nostr.relays)
            return f'{base}?{qs}'
        return base

    def naddr_address(self, identifier: str) -> str:
        coord = Coordinate(Kind(NOSTR_KIND), self._keys.public_key(), identifier)
        relay_urls = [RelayUrl.parse(r) for r in self._config.nostr.relays]
        return Nip19Coordinate(coord, relay_urls).to_bech32()

    def pubkey_bech32(self) -> str:
        return self._keys.public_key().to_bech32()

    @staticmethod
    def parse_address(address: str) -> tuple[str, str, list[str]]:
        """Return (pubkey_str, identifier, relay_hints)."""
        if address.startswith('peerpage://'):
            rest = address[len('peerpage://'):]
            relays: list[str] = []
            if '?' in rest:
                rest, qs = rest.split('?', 1)
                for part in qs.split('&'):
                    if part.startswith('r='):
                        val = unquote(part[len('r='):])
                        relays.append(val if '://' in val else f'wss://{val}')
                    elif part.startswith('relay='):
                        relays.append(unquote(part[len('relay='):]))
            split_pos = rest.rfind('.npub1')
            if split_pos == -1:
                raise ValueError(f'invalid peerpage:// address: {address!r}')
            identifier = rest[:split_pos]
            pubkey_str = rest[split_pos + 1:]  # strip leading dot
            return pubkey_str, identifier, relays
        if address.startswith('naddr1'):
            nip19 = Nip19Coordinate.from_bech32(address)
            coord = nip19.coordinate()
            try:
                relays = [str(r) for r in nip19.relays()]
            except Exception:
                relays = []
            return coord.public_key().to_bech32(), coord.identifier(), relays
        raise ValueError(f'unrecognized address format: {address!r}')
