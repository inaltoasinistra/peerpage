import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import trackers as trackers_module
from trackers import TrackerList, TRACKERS_FALLBACK


class TestLoadTrackers(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.trackers_file = os.path.join(self.tmp.name, 'trackers.json')
        self.patcher = patch.object(trackers_module, 'TRACKERS_FILE', self.trackers_file)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tmp.cleanup()

    def _write_file(self, trackers: list[str], age: float = 0) -> None:
        with open(self.trackers_file, 'w') as f:
            json.dump(trackers, f)
        os.utime(self.trackers_file, (time.time() - age, time.time() - age))

    def test_fresh_file_is_used_without_fetch(self) -> None:
        self._write_file(['udp://a.com:80/announce'], age=60)
        with patch.object(TrackerList, '_fetch') as mock_fetch:
            result = TrackerList._load()
        mock_fetch.assert_not_called()
        self.assertEqual(result, ['udp://a.com:80/announce'])

    def test_stale_file_triggers_fetch(self) -> None:
        self._write_file(['udp://old.com:80/announce'], age=8 * 24 * 3600)
        new = ['udp://new.com:80/announce']
        with patch.object(TrackerList, '_fetch', return_value=new):
            result = TrackerList._load()
        self.assertEqual(result, new)
        with open(self.trackers_file) as f:
            self.assertEqual(json.load(f), new)

    def test_missing_file_triggers_fetch(self) -> None:
        new = ['udp://new.com:80/announce']
        with patch.object(TrackerList, '_fetch', return_value=new):
            result = TrackerList._load()
        self.assertEqual(result, new)

    def test_fetch_failure_falls_back_to_stale_file(self) -> None:
        self._write_file(['udp://stale.com:80/announce'], age=8 * 24 * 3600)
        with patch.object(TrackerList, '_fetch', side_effect=OSError('timeout')):
            result = TrackerList._load()
        self.assertEqual(result, ['udp://stale.com:80/announce'])

    def test_fetch_failure_no_file_uses_fallback(self) -> None:
        with patch.object(TrackerList, '_fetch', side_effect=OSError('timeout')):
            result = TrackerList._load()
        self.assertEqual(result, TRACKERS_FALLBACK)


class TestFetch(unittest.TestCase):

    def test_parses_tracker_list(self) -> None:
        body = b'udp://a.com:80/announce\n\nudp://b.com:80/announce\n'
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = TrackerList._fetch()
        self.assertEqual(result, ['udp://a.com:80/announce', 'udp://b.com:80/announce'])


if __name__ == '__main__':
    unittest.main()
