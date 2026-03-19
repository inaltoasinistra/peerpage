#!/usr/bin/env python3
"""Subscribe to all peerpage Nostr events and print them as they arrive."""
import asyncio
import datetime
import sys

from nostr_sdk import (
    Client, Filter, HandleNotification, Kind, Keys, NostrSigner,
    RelayUrl, SecretKey, TagKind,
)

import config as cfg_module
from config import NOSTR_KIND


class _Handler(HandleNotification):

    async def handle(self, relay_url, subscription_id, event) -> None:
        ts = datetime.datetime.fromtimestamp(event.created_at().as_secs())
        npub = event.author().to_bech32()
        identifier = event.tags().identifier() or '(none)'
        magnet_tag = event.tags().find(TagKind.MAGdonNET())
        magnet = magnet_tag.content() if magnet_tag else '(none)'
        content = event.content()
        first_line = content.split('\n')[0] if content else ''

        print(f'[{ts}]  {npub}')
        print(f'  identifier : {identifier}')
        print(f'  magnet     : {magnet}')
        if first_line:
            print(f'  content    : {first_line[:120]}')
        print()

    async def handle_msg(self, relay_url, msg) -> None:
        pass


async def main() -> None:
    cfg = cfg_module.load()
    relays = cfg.nostr.relays
    print(f'relays : {", ".join(relays)}')
    print(f'filter : kind={NOSTR_KIND}')
    print('waiting for events (Ctrl+C to stop)...\n')

    keys = Keys(SecretKey.parse(cfg.nostr.private_key))
    client = Client(NostrSigner.keys(keys))
    for relay in relays:
        try:
            await client.add_relay(RelayUrl.parse(relay))
        except Exception as e:
            print(f'warning: failed to add relay {relay}: {e}', file=sys.stderr)

    await client.connect()
    await client.subscribe(Filter().kind(Kind(NOSTR_KIND)))
    try:
        await client.handle_notifications(_Handler())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
