# Peerpage

Publish and browse decentralized websites over BitTorrent and Nostr.

A site is a folder on your disk. Peerpage snapshots it into a BitTorrent v2 torrent,
announces it on Nostr (kind 34838), and seeds it. Anyone with your Nostr address can
download and re-seed your site.

## Install

### Prerequisites

```
pip install pipx   # skip if pipx is already installed
```

### Install Peerpage

```
pipx install peerpage
```

This installs three commands:

| Command           | Purpose                                    |
|-------------------|--------------------------------------------|
| `peerpage`        | CLI — publish, download, follow, list      |
| `peerpage-daemon` | Daemon — seed torrents, process downloads  |
| `peerpage-tui`    | Live dashboard (ncurses)                   |

### Run the daemon

**Foreground (for testing):**

```
peerpage-daemon
```

**As a persistent background service (recommended):**

```
mkdir -p ~/.config/systemd/user
cp "$(pipx environment --value PIPX_LOCAL_VENVS)/peerpage/lib/python*/site-packages/../../../share/peerpage/peerpage.service" \
   ~/.config/systemd/user/
# or just download it directly:
curl -o ~/.config/systemd/user/peerpage.service \
     https://raw.githubusercontent.com/martinosalvetti/peerpage/main/peerpage.service

systemctl --user daemon-reload
systemctl --user enable --now peerpage
```

Check status:

```
systemctl --user status peerpage
journalctl --user -u peerpage -f
```

## Usage

```
peerpage <site>                        # publish a local site folder
peerpage <naddr1|peerpage://>         # download a site by Nostr address
peerpage <site> <naddr1|peerpage://>  # download and subscribe to updates
peerpage follow <npub>                # follow all sites published by a user
peerpage sites                        # list active sites (daemon must be running)
peerpage stop                         # stop the daemon
peerpage-tui                          # open the live dashboard
```

## Configuration

Auto-generated on first run at `~/.config/peerpage/config.toml`. Contains your Nostr
identity (nsec/npub), relay list, and followed publishers.

## Environment variables

| Variable                | Default                     | Description                            |
|-------------------------|-----------------------------|----------------------------------------|
| `SITES_DIR`             | `~/peerpage`                | Your site content folders              |
| `DATA_DIR`              | `~/.local/share/peerpage`   | Snapshots, torrents, resume files      |
| `PEERPAGE_CONFIG_DIR`   | `~/.config/peerpage`        | Config directory                       |
| `HTTP_PORT`             | `8008`                      | Daemon HTTP port                       |
| `PEERPAGE_KEEP_SECONDS` | `86400`                     | How long old versions are kept (seconds) |

## Development

```
git clone https://github.com/martinosalvetti/peerpage
cd peerpage
python3 -m venv venv
source venv/bin/activate
pip install -e .[dev]
./test.sh
./coverage.sh
```
