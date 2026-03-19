#!/usr/bin/env python3
"""Manually test publish by mutating the source directory and running publish."""
import os
import subprocess
import sys
import time
import uuid

import config as cfg_module
from publisher import Site

SITES_DIR = os.environ.get('SITES_DIR', os.path.expanduser('~/peerpage'))
DATA_DIR = os.environ.get('DATA_DIR', os.path.expanduser('~/.local/share/peerpage'))


def main() -> None:
    site = sys.argv[1] if len(sys.argv) > 1 else 'cubbies'
    source = os.path.join(SITES_DIR, site)

    files = sorted(os.listdir(source))
    if len(files) < 2:
        print(f'need at least 2 files in {source}', file=sys.stderr)
        sys.exit(1)

    cfg = cfg_module.load()
    t = Site(site, sites_dir=SITES_DIR, data_dir=DATA_DIR, npub=cfg.nostr.public_key)
    last_version = t.last_version()
    next_version = (last_version or 0) + 1

    to_remove = files[0]
    to_modify = files[1]
    new_file  = f'new-{uuid.uuid4().hex}.txt'

    print(f'remove:  {to_remove}')
    os.remove(os.path.join(source, to_remove))

    print(f'modify:  {to_modify}')
    with open(os.path.join(source, to_modify), 'a') as f:
        f.write('\nmodified by test_publish.py\n')

    print(f'create:  {new_file}')
    with open(os.path.join(source, new_file), 'w') as f:
        f.write('created by test_publish.py\n')
        f.write(f'{time.ctime()}\n')


    with open(os.path.join(source, 'version.txt'), 'w') as f:
        f.write(f'{next_version}\n')
        f.write(f'{time.ctime()}\n')

    print()
    sys.stdout.flush()
    subprocess.run(['./cli.py', site])


if __name__ == '__main__':
    main()
