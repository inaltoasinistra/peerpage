import json
import logging
import os
import random
import time
import urllib.request

from fileutil import atomic_open

logger = logging.getLogger(__name__)

TRACKERS_FILE = 'trackers.json'
TRACKERS_URL = 'https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt'
TRACKERS_MAX_AGE = 7 * 24 * 3600  # one week in seconds
TRACKERS_FALLBACK = [
    'udp://tracker.openbittorrent.com:80/announce',
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://tracker.coppersurfer.tk:6969/announce',
    'udp://tracker.leechers-paradise.org:6969/announce',
    'udp://tracker.internetwarriors.net:1337/announce',
    'udp://exodus.desync.com:6969/announce',
    'udp://tracker.torrent.eu.org:451/announce',
]


class TrackerList:

    @staticmethod
    def _fetch() -> list[str]:
        with urllib.request.urlopen(TRACKERS_URL, timeout=10) as resp:
            text = resp.read().decode()
        return [line.strip() for line in text.splitlines() if line.strip()]

    @staticmethod
    def _load() -> list[str]:
        if os.path.isfile(TRACKERS_FILE):
            age = time.time() - os.path.getmtime(TRACKERS_FILE)
            if age < TRACKERS_MAX_AGE:
                with open(TRACKERS_FILE) as f:
                    return json.load(f)
        try:
            trackers = TrackerList._fetch()
            with atomic_open(TRACKERS_FILE) as f:
                json.dump(trackers, f)
            logger.info('tracker list updated (%d trackers)', len(trackers))
            return trackers
        except Exception as e:
            logger.warning('failed to fetch trackers: %s', e)
            if os.path.isfile(TRACKERS_FILE):
                with open(TRACKERS_FILE) as f:
                    return json.load(f)
            return TRACKERS_FALLBACK

    @staticmethod
    def select(count: int = 3) -> list[str]:
        return random.sample(TrackerList._load(), count)
