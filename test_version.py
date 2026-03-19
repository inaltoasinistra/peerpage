import unittest
from unittest.mock import patch

import version


class TestGetCommit(unittest.TestCase):

    def setUp(self) -> None:
        self._saved_commit = version._commit
        self._saved_fetched = version._commit_fetched
        version._commit = None
        version._commit_fetched = False

    def tearDown(self) -> None:
        version._commit = self._saved_commit
        version._commit_fetched = self._saved_fetched

    def test_returns_nonempty_string_in_git_repo(self) -> None:
        result = version._get_commit()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_returns_none_when_git_unavailable(self) -> None:
        with patch('subprocess.check_output', side_effect=FileNotFoundError):
            result = version._get_commit()
        self.assertIsNone(result)

    def test_returns_none_when_not_in_git_repo(self) -> None:
        with patch('subprocess.check_output', side_effect=Exception('not a repo')):
            result = version._get_commit()
        self.assertIsNone(result)

    def test_caches_result_across_calls(self) -> None:
        call_count = 0

        def fake_check_output(cmd: list, **kwargs: object) -> bytes:
            nonlocal call_count
            call_count += 1
            return b'abc1234'

        with patch('subprocess.check_output', side_effect=fake_check_output):
            v1 = version._get_commit()
            v2 = version._get_commit()

        self.assertEqual(v1, 'abc1234')
        self.assertEqual(v1, v2)
        self.assertEqual(call_count, 1)  # subprocess called only once

    def test_strips_trailing_newline(self) -> None:
        with patch('subprocess.check_output', return_value=b'deadbeef\n'):
            result = version._get_commit()
        self.assertEqual(result, 'deadbeef')


class TestGetUserAgent(unittest.TestCase):

    def setUp(self) -> None:
        self._saved_version = version._VERSION
        self._saved_commit = version._commit
        self._saved_fetched = version._commit_fetched

    def tearDown(self) -> None:
        version._VERSION = self._saved_version
        version._commit = self._saved_commit
        version._commit_fetched = self._saved_fetched

    def _set(self, ver: str | None, commit: str | None) -> None:
        version._VERSION = ver
        version._commit = commit
        version._commit_fetched = True

    def test_version_and_commit(self) -> None:
        self._set('1.2.3', 'abc1234')
        self.assertEqual(version.get_user_agent(), 'Peerpage/1.2.3 (abc1234)')

    def test_commit_only(self) -> None:
        self._set(None, 'abc1234')
        self.assertEqual(version.get_user_agent(), 'Peerpage/abc1234')

    def test_version_only(self) -> None:
        self._set('1.2.3', None)
        self.assertEqual(version.get_user_agent(), 'Peerpage/1.2.3')

    def test_neither(self) -> None:
        self._set(None, None)
        self.assertEqual(version.get_user_agent(), 'Peerpage/unknown')

    def test_unknown_when_git_unavailable(self) -> None:
        version._VERSION = None
        version._commit_fetched = False
        with patch('subprocess.check_output', side_effect=FileNotFoundError):
            result = version.get_user_agent()
        self.assertEqual(result, 'Peerpage/unknown')


if __name__ == '__main__':
    unittest.main()
