#!/usr/bin/env python3
import asyncio
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

import config as cfg_module
from fileutil import atomic_open, iter_sites
from nostr_client import NostrClient
from publisher import Site

SITES_DIR = os.environ.get('SITES_DIR', os.path.expanduser('~/peerpage'))
DATA_DIR = os.environ.get('DATA_DIR', os.path.expanduser('~/.local/share/peerpage'))
HTTP_BASE = os.environ.get('HTTP_BASE', 'http://localhost:8008')


def _http_get(path: str) -> object:
    with urllib.request.urlopen(f'{HTTP_BASE}{path}', timeout=5) as resp:
        return json.loads(resp.read())


def _http_post(path: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f'{HTTP_BASE}{path}', data=body)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _request_download(site: str, address: str) -> None:
    try:
        resp = _http_post('/@/api/add', {'address': address})
        if 'error' in resp:
            print(f'error: {resp["error"]}', file=sys.stderr)
            sys.exit(1)
    except urllib.error.URLError as e:
        print(f'error: cannot connect to daemon ({e})', file=sys.stderr)
        sys.exit(1)


def _stop_daemon() -> None:
    try:
        _http_post('/@/api/stop', {})
        print('daemon stopped')
    except urllib.error.URLError as e:
        print(f'error: cannot connect to daemon ({e})', file=sys.stderr)
        sys.exit(1)


def _sites() -> None:
    try:
        data = _http_get('/@/api/sites')
        print(json.dumps(data, indent=2))
    except urllib.error.URLError as e:
        print(f'error: cannot connect to daemon ({e})', file=sys.stderr)
        sys.exit(1)


def _read_changelog(txt_path: str) -> str:
    with open(txt_path) as f:
        content = f.read()
    if content.startswith('magnet: '):
        content = content.split('\n', 2)[2] if content.count('\n') >= 2 else ''
    return content


def _is_address(arg: str) -> bool:
    return arg.startswith('naddr1') or arg.startswith('peerpage://')


def _queue_address_download(site: str, address: str) -> None:
    _request_download(site, address)
    print(f'{site}: download queued')


def _follow_npub(npub: str) -> None:
    cfg = cfg_module.load()
    if npub in cfg.nostr.followed:
        print(f'{npub}: already followed')
        return
    cfg.nostr.followed.append(npub)
    cfg_module.save(cfg)
    print(f'{npub}: now following')


def _publish_site(site: str) -> None:
    cfg = cfg_module.load()
    t = Site(site, sites_dir=SITES_DIR, data_dir=DATA_DIR, npub=cfg.nostr.public_key)
    try:
        created = t.create()
    except ValueError as e:
        print(f'{site}: error: {e}', file=sys.stderr)
        sys.exit(1)
    if not created:
        print(f'{site}: no changes, still at version {t.version}')
        return
    print(f'{site}: version {t.version} created')
    nostr_client = NostrClient(cfg)
    changelog = _read_changelog(os.path.join(t.data_path, str(t.version), 'changelog.txt'))
    print('publishing to Nostr...')
    event = asyncio.run(nostr_client.publish(site, t.magnet_uri, changelog, t.version))
    if event is not None:
        event_path = os.path.join(t.data_path, str(t.version), 'event.json')
        with atomic_open(event_path) as f:
            json.dump(event, f, indent=2)
    print(f'published to Nostr: {nostr_client.site_address(site)}')
    print(f'naddr: {nostr_client.naddr_address(site)}')


def _resolve_site(arg: str) -> tuple[str, str]:
    """Return (npub, site_name) from a name, name.npub5, or peerpage:// address."""
    if arg.startswith('peerpage://'):
        pubkey_str, identifier, _relays = NostrClient.parse_address(arg)
        return pubkey_str, identifier

    sites_base = os.path.join(DATA_DIR, 'sites')
    if not os.path.isdir(sites_base):
        raise ValueError('no sites found')

    if '.' in arg:
        search_name, npub5 = arg.rsplit('.', 1)
    else:
        search_name, npub5 = arg, None

    matches: list[tuple[str, str]] = []
    for npub, site_name, _ in iter_sites(sites_base):
        if site_name == search_name and (npub5 is None or npub.endswith(npub5)):
            matches.append((npub, site_name))

    if not matches:
        raise ValueError(f'site not found: {arg!r}')
    if len(matches) > 1:
        ids = [f'{site}.{npub[-5:]}' for npub, site in matches]
        raise ValueError(f'ambiguous site {arg!r}; use one of: {", ".join(ids)}')
    return matches[0]


def _delete_site(arg: str) -> None:
    try:
        npub, site_name = _resolve_site(arg)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)

    identifier = f'{site_name}.{npub}'
    try:
        # Daemon stops seeding and deletes the data dir
        _http_post(f'/@/api/delete/{identifier}', {})
    except urllib.error.URLError:
        # Daemon not running — delete data dir manually
        site_data_dir = os.path.join(DATA_DIR, 'sites', npub, site_name)
        if os.path.isdir(site_data_dir):
            shutil.rmtree(site_data_dir)

    # Delete source directory only for own sites
    cfg = cfg_module.load()
    if npub == cfg.nostr.public_key:
        source_dir = os.path.join(SITES_DIR, site_name)
        if os.path.isdir(source_dir):
            shutil.rmtree(source_dir)

    print(f'{site_name}: deleted')


def _download_from_file(path: str) -> None:
    """Queue downloads for every address in *path* (one per line, # comments ok)."""
    try:
        with open(os.path.expanduser(path)) as f:
            lines = f.readlines()
    except OSError as e:
        print(f'error: cannot read {path}: {e}', file=sys.stderr)
        sys.exit(1)

    queued = skipped = 0
    for raw in lines:
        address = raw.strip()
        if not address or address.startswith('#'):
            continue
        try:
            _, site_name, _relays = NostrClient.parse_address(address)
        except ValueError as e:
            print(f'warning: skipping {address!r}: {e}', file=sys.stderr)
            skipped += 1
            continue
        _queue_address_download(site_name, address)
        queued += 1

    print(f'{queued} address(es) queued' + (f', {skipped} skipped' if skipped else ''))


def _usage() -> None:
    p = sys.argv[0]
    print(
        f'usage:\n'
        f'  {p} sites                           show daemon status as JSON\n'
        f'  {p} <site>                          publish local site to Nostr\n'
        f'  {p} <naddr1|peerpage://>            download site (name derived from address)\n'
        f'  {p} <site> <naddr1|peerpage://>     queue download and subscribe to updates\n'
        f'  {p} @<file>                         queue all addresses listed in file\n'
        f'  {p} follow <npub>                   follow all sites published by a user\n'
        f'  {p} delete <site|name.npub5|peerpage://>   delete a site\n'
        f'  {p} stop                              stop the daemon',
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == 'stop':
        _stop_daemon()
    elif len(sys.argv) == 2 and sys.argv[1] == 'sites':
        _sites()
    elif len(sys.argv) == 3 and sys.argv[1] == 'follow':
        _follow_npub(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'delete':
        _delete_site(sys.argv[2])
    elif len(sys.argv) == 3:
        site, arg = sys.argv[1], sys.argv[2]
        _queue_address_download(site, arg)
    elif len(sys.argv) == 2:
        arg = sys.argv[1]
        if arg.startswith('@'):
            _download_from_file(arg[1:])
        elif _is_address(arg):
            _, identifier, _relays = NostrClient.parse_address(arg)
            _queue_address_download(identifier, arg)
        else:
            _publish_site(arg)
    else:
        _usage()


if __name__ == '__main__':
    main()
