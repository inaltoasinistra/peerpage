import os
import tempfile
import unittest

import config as cfg_module
from config import Config, NostrConfig, DEFAULT_RELAYS, _write, load, save


class TestWrite(unittest.TestCase):

    def test_write_creates_valid_toml(self) -> None:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        cfg = Config(nostr=NostrConfig(private_key='nsec1abc', relays=['wss://r1', 'wss://r2']))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sub', 'config.toml')
            _write(path, cfg)
            self.assertTrue(os.path.isfile(path))
            with open(path, 'rb') as f:
                data = tomllib.load(f)
        self.assertEqual(data['nostr']['private_key'], 'nsec1abc')
        self.assertEqual(data['nostr']['relays'], ['wss://r1', 'wss://r2'])

    def test_write_creates_parent_dirs(self) -> None:
        cfg = Config(nostr=NostrConfig(private_key='nsec1x', relays=[]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'a', 'b', 'c.toml')
            _write(path, cfg)
            self.assertTrue(os.path.isfile(path))


class TestLoad(unittest.TestCase):

    def test_load_existing_config(self) -> None:
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        cfg_in = Config(nostr=NostrConfig(private_key=nsec, relays=['wss://r1']))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            _write(path, cfg_in)
            cfg_out = load(path)
        self.assertEqual(cfg_out.nostr.private_key, nsec)
        self.assertEqual(cfg_out.nostr.relays, ['wss://r1'])

    def test_load_generates_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sub', 'config.toml')
            with self.assertLogs('config', level='INFO') as log:
                cfg = load(path)
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(cfg.nostr.private_key.startswith('nsec1'))
            self.assertTrue(any('generated new Nostr identity' in line for line in log.output))

    def test_generated_config_uses_default_relays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            cfg = load(path)
        self.assertEqual(cfg.nostr.relays, DEFAULT_RELAYS)

    def test_invalid_toml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            with open(path, 'wb') as f:
                f.write(b'not valid toml ][')
            with self.assertRaises(Exception):
                load(path)

    def test_generated_key_is_valid_nostr_key(self) -> None:
        from nostr_sdk import Keys, SecretKey
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            cfg = load(path)
        # Should parse without error
        sk = SecretKey.parse(cfg.nostr.private_key)
        keys = Keys(sk)
        self.assertTrue(keys.public_key().to_bech32().startswith('npub1'))

    def test_load_sets_public_key(self) -> None:
        from nostr_sdk import Keys, SecretKey
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            cfg = load(path)
        derived = Keys(SecretKey.parse(cfg.nostr.private_key)).public_key().to_bech32()
        self.assertEqual(cfg.nostr.public_key, derived)

    def test_load_writes_missing_public_key(self) -> None:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            # write config without public_key
            with open(path, 'w') as f:
                f.write(f'[nostr]\nprivate_key = "{nsec}"\nrelays = []\n')
            with self.assertLogs('config', level='INFO'):
                cfg = load(path)
            with open(path, 'rb') as f:
                data = tomllib.load(f)
        self.assertIn('public_key', data['nostr'])
        self.assertEqual(data['nostr']['public_key'], cfg.nostr.public_key)

    def test_load_fixes_mismatched_public_key(self) -> None:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            # write config with a wrong public_key
            with open(path, 'w') as f:
                f.write(f'[nostr]\nprivate_key = "{nsec}"\npublic_key = "npub1wrong"\nrelays = []\n')
            with self.assertLogs('config', level='INFO'):
                cfg = load(path)
            with open(path, 'rb') as f:
                data = tomllib.load(f)
        self.assertEqual(data['nostr']['public_key'], cfg.nostr.public_key)
        self.assertNotEqual(data['nostr']['public_key'], 'npub1wrong')


class TestFollowed(unittest.TestCase):

    def test_followed_defaults_to_empty(self) -> None:
        cfg = Config(nostr=NostrConfig(private_key='nsec1x', relays=[]))
        self.assertEqual(cfg.nostr.followed, [])

    def test_write_and_load_preserves_followed(self) -> None:
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        npub = keys.public_key().to_bech32()
        cfg = Config(nostr=NostrConfig(private_key=nsec, relays=[], followed=[npub]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            _write(path, cfg)
            loaded = load(path)
        self.assertEqual(loaded.nostr.followed, [npub])

    def test_load_defaults_followed_to_empty_when_absent(self) -> None:
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            with open(path, 'w') as f:
                f.write(f'[nostr]\nprivate_key = "{nsec}"\nrelays = []\n')
            with self.assertLogs('config', level='INFO'):
                cfg = load(path)
        self.assertEqual(cfg.nostr.followed, [])

    def test_save_adds_npub_to_followed(self) -> None:
        from nostr_sdk import Keys
        keys = Keys.generate()
        nsec = keys.secret_key().to_bech32()
        npub = keys.public_key().to_bech32()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.toml')
            cfg = Config(nostr=NostrConfig(private_key=nsec, relays=[], followed=[npub]))
            save(cfg, path)
            loaded = load(path)
        self.assertIn(npub, loaded.nostr.followed)


if __name__ == '__main__':
    unittest.main()
