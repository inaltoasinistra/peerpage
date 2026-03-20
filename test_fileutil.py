import os
import tempfile
import unittest
from unittest.mock import patch

from fileutil import atomic_open, get_tag, initial_priorities, iter_sites, list_version_dirs


class TestAtomicOpen(unittest.TestCase):

    def test_text_write_produces_correct_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with atomic_open(path) as f:
                f.write('hello')
            with open(path) as f:
                self.assertEqual(f.read(), 'hello')

    def test_binary_write_produces_correct_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.bin')
            with atomic_open(path, 'wb') as f:
                f.write(b'\x00\x01\x02')
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b'\x00\x01\x02')

    def test_exception_leaves_target_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with open(path, 'w') as f:
                f.write('original')
            with self.assertRaises(RuntimeError):
                with atomic_open(path) as f:
                    f.write('partial')
                    raise RuntimeError('oops')
            with open(path) as f:
                self.assertEqual(f.read(), 'original')

    def test_exception_cleans_up_tmp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with self.assertRaises(ValueError):
                with atomic_open(path) as f:
                    raise ValueError('boom')
            # Only the target path may exist, no .tmp leftover
            files = os.listdir(tmp)
            self.assertNotIn('out.txt', files)  # target was never written
            self.assertEqual(len(files), 0)

    def test_unlink_failure_during_cleanup_does_not_suppress_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with patch('fileutil.os.unlink', side_effect=OSError('already gone')):
                with self.assertRaises(RuntimeError):
                    with atomic_open(path) as f:
                        raise RuntimeError('original')

    def test_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with open(path, 'w') as f:
                f.write('old')
            with atomic_open(path) as f:
                f.write('new')
            with open(path) as f:
                self.assertEqual(f.read(), 'new')

    def test_kwargs_forwarded_to_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.txt')
            with atomic_open(path, encoding='ascii') as f:
                f.write('ascii text')
            with open(path) as f:
                self.assertEqual(f.read(), 'ascii text')


class TestListVersionDirs(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_empty_for_missing_path(self) -> None:
        self.assertEqual(list_version_dirs('/nonexistent'), [])

    def test_returns_numeric_subdirs(self) -> None:
        for name in ('1', '2', '10'):
            os.makedirs(os.path.join(self.tmp.name, name))
        result = sorted(list_version_dirs(self.tmp.name))
        self.assertEqual(result, [1, 2, 10])

    def test_skips_non_numeric_entries(self) -> None:
        os.makedirs(os.path.join(self.tmp.name, 'site'))
        os.makedirs(os.path.join(self.tmp.name, '3'))
        result = list_version_dirs(self.tmp.name)
        self.assertEqual(result, [3])

    def test_skips_numeric_files(self) -> None:
        open(os.path.join(self.tmp.name, '5'), 'w').close()
        self.assertEqual(list_version_dirs(self.tmp.name), [])


class TestIterSites(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_site(self, npub: str, site_name: str) -> str:
        path = os.path.join(self.tmp.name, npub, site_name)
        os.makedirs(path)
        return path

    def test_yields_nothing_for_missing_base(self) -> None:
        self.assertEqual(list(iter_sites('/nonexistent')), [])

    def test_yields_correct_tuples(self) -> None:
        site_dir = self._make_site('npub1abc', 'mysite')
        results = list(iter_sites(self.tmp.name))
        self.assertEqual(results, [('npub1abc', 'mysite', site_dir)])

    def test_yields_multiple_sites(self) -> None:
        self._make_site('npub1a', 'site_a')
        self._make_site('npub1b', 'site_b')
        results = {(n, s) for n, s, _ in iter_sites(self.tmp.name)}
        self.assertEqual(results, {('npub1a', 'site_a'), ('npub1b', 'site_b')})

    def test_skips_non_dir_in_base(self) -> None:
        open(os.path.join(self.tmp.name, 'file.txt'), 'w').close()
        self.assertEqual(list(iter_sites(self.tmp.name)), [])

    def test_skips_non_dir_in_npub_dir(self) -> None:
        npub_dir = os.path.join(self.tmp.name, 'npub1abc')
        os.makedirs(npub_dir)
        open(os.path.join(npub_dir, 'not_a_dir'), 'w').close()
        self.assertEqual(list(iter_sites(self.tmp.name)), [])


class TestGetTag(unittest.TestCase):

    def test_returns_value_when_tag_found(self) -> None:
        event = {'tags': [['magnet', 'magnet:?x']]}
        self.assertEqual(get_tag(event, 'magnet'), 'magnet:?x')

    def test_returns_none_when_tag_missing(self) -> None:
        event = {'tags': [['d', 'site']]}
        self.assertIsNone(get_tag(event, 'magnet'))

    def test_returns_custom_default(self) -> None:
        event = {'tags': []}
        self.assertEqual(get_tag(event, 'x', 'fallback'), 'fallback')

    def test_returns_none_when_no_tags_key(self) -> None:
        self.assertIsNone(get_tag({}, 'magnet'))

    def test_skips_malformed_tags(self) -> None:
        event = {'tags': ['not_a_list', ['short'], ['magnet', 'magnet:?x']]}
        self.assertEqual(get_tag(event, 'magnet'), 'magnet:?x')

    def test_returns_first_match(self) -> None:
        event = {'tags': [['d', 'first'], ['d', 'second']]}
        self.assertEqual(get_tag(event, 'd'), 'first')


KB = 1024
MB = 1024 * 1024


def _f(index: int, path: str, size: int) -> dict:
    return {'index': index, 'path': path, 'size': size}


class TestInitialPriorities(unittest.TestCase):

    # ---------------------------------------------------------------- edge cases

    def test_empty_files(self) -> None:
        self.assertEqual(initial_priorities([], 100 * MB), [])

    def test_budget_zero(self) -> None:
        files = [_f(0, 'site/a.html', 10 * KB)]
        self.assertEqual(initial_priorities(files, 0), [0])

    def test_budget_negative(self) -> None:
        files = [_f(0, 'site/a.html', 10 * KB)]
        self.assertEqual(initial_priorities(files, -1), [0])

    def test_single_file_fits(self) -> None:
        files = [_f(0, 'site/index.html', 10 * KB)]
        self.assertEqual(initial_priorities(files, 100 * MB), [1])

    def test_single_file_too_large(self) -> None:
        files = [_f(0, 'site/video.mp4', 200 * MB)]
        self.assertEqual(initial_priorities(files, 100 * MB), [0])

    def test_budget_exactly_matches_total(self) -> None:
        files = [_f(0, 'site/a.html', 50 * MB), _f(1, 'site/b.html', 50 * MB)]
        self.assertEqual(initial_priorities(files, 100 * MB), [1, 1])

    # ---------------------------------------------------------------- flat files

    def test_all_flat_files_fit(self) -> None:
        files = [
            _f(0, 'site/index.html', 10 * KB),
            _f(1, 'site/style.css',  20 * KB),
            _f(2, 'site/app.js',     30 * KB),
        ]
        self.assertEqual(initial_priorities(files, 100 * MB), [1, 1, 1])

    def test_flat_file_group_selects_small_when_combined_size_exceeds_budget(self) -> None:
        # All three files share site/ — they form one group.
        # Combined size (≈80 MB) exceeds the 50 MB budget, so the algorithm
        # falls back to individual selection: small files are picked, large skipped.
        files = [
            _f(0, 'site/large.mp4', 80 * MB),
            _f(1, 'site/style.css', 10 * KB),
            _f(2, 'site/app.js',    20 * KB),
        ]
        result = initial_priorities(files, 50 * MB)
        self.assertEqual(result, [0, 1, 1])

    def test_flat_siblings_small_selected_when_large_exceeds_budget(self) -> None:
        # index.html (10 KB) fits in the 100 MB budget; database.sql (500 MB) does not.
        # When the group exceeds budget, files are tried individually → index.html selected.
        files = [
            _f(0, 'site/index.html',   10 * KB),
            _f(1, 'site/database.sql', 500 * MB),
        ]
        result = initial_priorities(files, 100 * MB)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[1], 0)

    # ---------------------------------------------------------- whole dir fits

    def test_whole_dir_fits(self) -> None:
        files = [
            _f(0, 'site/index.html',   10 * KB),
            _f(1, 'site/css/main.css', 30 * KB),
            _f(2, 'site/css/reset.css', 10 * KB),
        ]
        self.assertEqual(initial_priorities(files, 100 * MB), [1, 1, 1])

    def test_dir_selected_atomically_when_it_fits(self) -> None:
        # css/ (200 KB total) fits within the budget; both its files must be
        # selected together.  videos/ is too large even to recurse into usefully.
        files = [
            _f(0, 'site/css/a.css',       100 * KB),
            _f(1, 'site/css/b.css',       100 * KB),
            _f(2, 'site/videos/huge.mp4', 300 * MB),
        ]
        result = initial_priorities(files, 250 * KB)
        self.assertEqual(result[0], 1)  # css/a.css
        self.assertEqual(result[1], 1)  # css/b.css  (both or neither)
        self.assertEqual(result[2], 0)  # huge.mp4

    def test_small_dirs_selected_before_large(self) -> None:
        # tiny (1 MB) and medium (50 MB) fit in 55 MB; large (200 MB) is
        # recursed into but its single file also doesn't fit the 4 MB left.
        files = [
            _f(0, 'site/tiny/a.html',   1 * MB),
            _f(1, 'site/medium/b.html', 50 * MB),
            _f(2, 'site/large/c.html',  200 * MB),
        ]
        result = initial_priorities(files, 55 * MB)
        self.assertEqual(result[0], 1)  # tiny/
        self.assertEqual(result[1], 1)  # medium/
        self.assertEqual(result[2], 0)  # large/c.html: 200 MB > 4 MB remaining

    # ---------------------------------------------------------- dir recursion

    def test_typical_static_site_recurses_into_assets(self) -> None:
        # assets/ (149 MB) exceeds the budget; the algorithm descends into it.
        # Inside assets/, fonts/ (2 MB) is selected as a subdir.  The direct-file
        # group (≈147 MB) exceeds the budget, so files are tried individually:
        # style.css, app.js, and hero.jpg each fit; only bg-video.mp4 is skipped.
        #
        # Budget: 100 MB
        #   index.html (10 KB)         file_group → selected  (≈99.99 MB left)
        #   assets/ (149 MB) too big   recurse:
        #     fonts/ (2 MB)            subdir     → selected  (≈97.99 MB left)
        #     file_group (≈147 MB) > budget → individual fallback:
        #       style.css (50 KB)  → selected  (≈97.94 MB left)
        #       app.js    (200 KB) → selected  (≈97.74 MB left)
        #       hero.jpg  (30 MB)  → selected  (≈67.74 MB left)
        #       bg-video  (117 MB) → skipped
        files = [
            _f(0, 'site/index.html',              10 * KB),
            _f(1, 'site/assets/style.css',        50 * KB),
            _f(2, 'site/assets/app.js',          200 * KB),
            _f(3, 'site/assets/fonts/arial.woff',  2 * MB),
            _f(4, 'site/assets/hero.jpg',         30 * MB),
            _f(5, 'site/assets/bg-video.mp4',    117 * MB),
        ]
        result = initial_priorities(files, 100 * MB)
        self.assertEqual(result[0], 1)  # index.html
        self.assertEqual(result[1], 1)  # assets/style.css — individually selected
        self.assertEqual(result[2], 1)  # assets/app.js    — individually selected
        self.assertEqual(result[3], 1)  # assets/fonts/arial.woff — subdir fits
        self.assertEqual(result[4], 1)  # assets/hero.jpg  — individually selected
        self.assertEqual(result[5], 0)  # assets/bg-video.mp4 — exceeds remaining budget

    def test_supplementary_video_dir_small_selected_when_group_too_large(self) -> None:
        # After core site content fills most of the budget, videos/ (300 MB)
        # is too large; recursing into it finds intro.mp4 (2 MB) and full.mp4
        # (298 MB).  Their combined group exceeds the ≈2.49 MB left, so files
        # are tried individually: intro.mp4 fits, full.mp4 is skipped.
        #
        # Budget: 100 MB
        #   index.html    10 KB  file_group → selected
        #   css/         500 KB  subdir     → selected
        #   js/            2 MB  subdir     → selected
        #   images/       95 MB  subdir     → selected   (≈2.49 MB left)
        #   videos/ too big      recurse:
        #     file_group (300 MB) > 2.49 MB → individual fallback:
        #       intro.mp4 (2 MB)  → selected
        #       full.mp4 (298 MB) → skipped
        files = [
            _f(0, 'site/index.html',        10 * KB),
            _f(1, 'site/css/style.css',    500 * KB),
            _f(2, 'site/js/app.js',          2 * MB),
            _f(3, 'site/images/hero.jpg',   95 * MB),
            _f(4, 'site/videos/intro.mp4',   2 * MB),
            _f(5, 'site/videos/full.mp4',  298 * MB),
        ]
        result = initial_priorities(files, 100 * MB)
        self.assertEqual(result[0], 1)  # index.html
        self.assertEqual(result[1], 1)  # css/style.css
        self.assertEqual(result[2], 1)  # js/app.js
        self.assertEqual(result[3], 1)  # images/hero.jpg
        self.assertEqual(result[4], 1)  # videos/intro.mp4 — individually fits
        self.assertEqual(result[5], 0)  # videos/full.mp4 — exceeds remaining budget

    def test_deep_recursion_multi_level(self) -> None:
        # docs/ → advanced/ → chapter1/ and chapter2/ each require recursion.
        # chapter2/ has two sibling files whose combined size (45 MB) fits, so
        # both are selected together.  chapter1/ has a single large file (90 MB)
        # that exceeds the remaining budget and is skipped.
        #
        # Budget: 100 MB
        #   index.html  (1 KB)                    file_group → selected  (≈99.999 MB left)
        #   docs/ (185 MB) too big                recurse:
        #     intro/ (5 MB)                       subdir     → selected  (≈94.999 MB left)
        #     advanced/ (180 MB) too big          recurse:
        #       chapter2/ (45 MB)                 subdir     → both files selected (≈49.999 MB left)
        #       chapter1/ (90 MB) too big         recurse:
        #         file_group (90 MB) > 49.999 MB  → skipped
        files = [
            _f(0, 'site/index.html',                        1 * KB),
            _f(1, 'site/docs/intro/page1.html',             2 * MB),
            _f(2, 'site/docs/intro/page2.html',             3 * MB),
            _f(3, 'site/docs/advanced/chapter1/main.md',   90 * MB),
            _f(4, 'site/docs/advanced/chapter2/part1.md',  20 * MB),
            _f(5, 'site/docs/advanced/chapter2/part2.md',  25 * MB),
        ]
        result = initial_priorities(files, 100 * MB)
        self.assertEqual(result[0], 1)  # index.html
        self.assertEqual(result[1], 1)  # docs/intro/page1.html
        self.assertEqual(result[2], 1)  # docs/intro/page2.html
        self.assertEqual(result[3], 0)  # docs/advanced/chapter1/main.md — file_group too large
        self.assertEqual(result[4], 1)  # docs/advanced/chapter2/part1.md — both siblings fit
        self.assertEqual(result[5], 1)  # docs/advanced/chapter2/part2.md — selected together

    def test_two_large_dirs_both_recursed(self) -> None:
        # Both media/ (120 MB) and archive/ (200 MB) exceed the budget.
        # They are recursed into in ascending size order: media/ first.
        # Inside each, a small subdir is selected while the large direct file
        # (a single-file group that exceeds the remaining budget) is skipped.
        #
        # Budget: 100 MB
        #   index.html              10 KB  file_group → selected  (≈99.99 MB left)
        #   media/ (120 MB) too big        recurse:
        #     thumbs/ (5 MB)               subdir     → selected  (≈94.99 MB left)
        #     file_group [movie] (115 MB)  > budget   → skipped
        #   archive/ (200 MB) too big      recurse:
        #     summary/ (20 MB)             subdir     → selected  (≈74.99 MB left)
        #     file_group [backup] (180 MB) > budget   → skipped
        files = [
            _f(0, 'site/index.html',                       10 * KB),
            _f(1, 'site/media/thumbs/thumb1.jpg',           3 * MB),
            _f(2, 'site/media/thumbs/thumb2.jpg',           2 * MB),
            _f(3, 'site/media/movie.mkv',                 115 * MB),
            _f(4, 'site/archive/summary/overview.pdf',     15 * MB),
            _f(5, 'site/archive/summary/notes.txt',         5 * MB),
            _f(6, 'site/archive/full-backup.tar',         180 * MB),
        ]
        result = initial_priorities(files, 100 * MB)
        self.assertEqual(result[0], 1)  # index.html
        self.assertEqual(result[1], 1)  # media/thumbs/thumb1.jpg
        self.assertEqual(result[2], 1)  # media/thumbs/thumb2.jpg
        self.assertEqual(result[3], 0)  # media/movie.mkv — file_group exceeds budget
        self.assertEqual(result[4], 1)  # archive/summary/overview.pdf
        self.assertEqual(result[5], 1)  # archive/summary/notes.txt
        self.assertEqual(result[6], 0)  # archive/full-backup.tar — file_group exceeds budget

    # ---------------------------------------------------- no 'site/' prefix

    def test_no_site_prefix_flat(self) -> None:
        files = [
            _f(0, 'index.html', 10 * KB),
            _f(1, 'style.css',  20 * KB),
        ]
        self.assertEqual(initial_priorities(files, 100 * MB), [1, 1])

    def test_no_site_prefix_with_dir_recurse(self) -> None:
        # Works correctly even without the 'site/' prefix.
        # index.html and video.mp4 are direct siblings at the root level; their
        # combined group (≈200 MB) exceeds the 50 MB budget, so files are tried
        # individually: index.html (10 KB) fits, video.mp4 (200 MB) does not.
        # css/ (30 KB) is a subdir and is selected independently.
        files = [
            _f(0, 'index.html',    10 * KB),
            _f(1, 'css/main.css',  10 * KB),
            _f(2, 'css/theme.css', 20 * KB),
            _f(3, 'video.mp4',    200 * MB),
        ]
        result = initial_priorities(files, 50 * MB)
        self.assertEqual(result[0], 1)  # index.html — individually fits
        self.assertEqual(result[1], 1)  # css/main.css
        self.assertEqual(result[2], 1)  # css/theme.css
        self.assertEqual(result[3], 0)  # video.mp4 — exceeds budget


if __name__ == '__main__':
    unittest.main()
