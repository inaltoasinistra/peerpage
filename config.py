import logging
import os
import sys

from fileutil import atomic_open

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from nostr_sdk import Keys, SecretKey

logger = logging.getLogger(__name__)

CONFIG_DIR = os.environ.get('PEERPAGE_CONFIG_DIR', os.path.expanduser('~/.config/peerpage'))
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.toml')
# Addressable events: 30000 <= n < 40000
# int.from_bytes(hashlib.sha1(b'peerpage').digest()) % 10000 + 30000
NOSTR_KIND = 34838
DEFAULT_RELAYS = [
    'wss://relay.damus.io',
    'wss://nos.lol',
    'wss://relay.nostr.band',
    'wss://nostr.wine',
]


class NostrConfig:

    def __init__(self, private_key: str, relays: list[str], public_key: str = '',
                 followed: list[str] | None = None) -> None:
        self.private_key = private_key
        self.relays = relays
        self.public_key = public_key
        self.followed: list[str] = followed if followed is not None else []


DEFAULT_MAX_SITE_MB = 100
DEFAULT_HTTP_HOST = '127.0.0.1'
DEFAULT_HTTP_PORT = 8008


class Config:

    def __init__(self, nostr: NostrConfig, max_site_mb: int = DEFAULT_MAX_SITE_MB,
                 http_host: str = DEFAULT_HTTP_HOST,
                 http_port: int = DEFAULT_HTTP_PORT) -> None:
        self.nostr = nostr
        self.max_site_mb = max_site_mb
        self.http_host = http_host
        self.http_port = http_port


def _write(path: str, config: Config) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    relay_items = '\n'.join(f'    "{r}",' for r in config.nostr.relays)
    followed_items = '\n'.join(f'    "{f}",' for f in config.nostr.followed)
    content = (
        f'max_site_mb = {config.max_site_mb}\n'
        f'http_host = "{config.http_host}"\n'
        f'http_port = {config.http_port}\n'
        f'\n'
        f'[nostr]\n'
        f'private_key = "{config.nostr.private_key}"\n'
        f'public_key = "{config.nostr.public_key}"\n'
        f'relays = [\n{relay_items}\n]\n'
        f'followed = [\n{followed_items}\n]\n'
    )
    with atomic_open(path) as f:
        f.write(content)


def save(config: Config, path: str = CONFIG_PATH) -> None:
    _write(path, config)


def load(path: str = CONFIG_PATH) -> Config:
    if not os.path.isfile(path):
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        npub = keys.public_key().to_bech32()
        cfg = Config(nostr=NostrConfig(private_key=nsec, relays=list(DEFAULT_RELAYS),
                                       public_key=npub))
        _write(path, cfg)
        logger.info('generated new Nostr identity: %s', npub)
        return cfg
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    nostr_data = data['nostr']
    derived_npub = Keys(SecretKey.parse(nostr_data['private_key'])).public_key().to_bech32()
    cfg = Config(
        nostr=NostrConfig(
            private_key=nostr_data['private_key'],
            relays=nostr_data['relays'],
            public_key=derived_npub,
            followed=nostr_data.get('followed', []),
        ),
        max_site_mb=int(data.get('max_site_mb', DEFAULT_MAX_SITE_MB)),
        http_host=str(data.get('http_host', DEFAULT_HTTP_HOST)),
        http_port=int(data.get('http_port', DEFAULT_HTTP_PORT)),
    )
    if nostr_data.get('public_key', '') != derived_npub:
        _write(path, cfg)
        logger.info('updated public_key in config: %s', derived_npub)
    return cfg
