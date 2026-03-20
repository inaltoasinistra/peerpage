#!/usr/bin/env python3
import asyncio
import fcntl
import logging
import os
import sys

import config as cfg_module
from nostr_client import NostrClient
from .httpserver import HttpServer
from .nostr_watcher import NostrWatcher
from .session import TorrentSession
from .watcher import Watcher

SITES_DIR = os.environ.get('SITES_DIR', os.path.expanduser('~/peerpage'))
DATA_DIR = os.environ.get('DATA_DIR', os.path.expanduser('~/.local/share/peerpage'))
LOCK_FILE = os.environ.get('PEERPAGE_LOCK', '/tmp/peerpage.lock')

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)

logger = logging.getLogger(__name__)


def _acquire_lock():
    fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit('error: daemon is already running')
    fd.write(str(os.getpid()) + '\n')
    fd.flush()
    return fd


async def run() -> None:
    os.makedirs(SITES_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = cfg_module.load()
    nostr_client = NostrClient(cfg)
    session = TorrentSession()
    watcher = Watcher(SITES_DIR, DATA_DIR, session, config=cfg)
    nostr_watcher = NostrWatcher(DATA_DIR, nostr_client,
                                 followed_npubs=cfg.nostr.followed)

    def on_download(site_name: str, address: str) -> None:
        nostr_watcher.subscribe(site_name, address)

    def on_delete(npub: str, site_name: str) -> None:
        site_data_dir = os.path.join(DATA_DIR, 'sites', npub, site_name)
        session.stop_site(site_data_dir)

    main_task = asyncio.current_task()

    def on_stop() -> None:
        if main_task is not None:
            main_task.cancel()

    http_host = cfg.http_host
    http_port = int(os.environ.get('HTTP_PORT', cfg.http_port))
    http = HttpServer(DATA_DIR, session, on_download=on_download, on_stop=on_stop,
                      config=cfg, config_path=cfg_module.CONFIG_PATH)
    await http.start(host=http_host, port=http_port)
    logger.info('daemon started, watching %s', SITES_DIR)
    try:
        await asyncio.gather(session.run(), watcher.run(), nostr_watcher.run())
    except asyncio.CancelledError:
        pass
    finally:
        await http.stop()
        await session.shutdown()


def main() -> None:
    lock_fd = _acquire_lock()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info('daemon stopped')
    finally:
        lock_fd.close()


if __name__ == '__main__':
    main()
