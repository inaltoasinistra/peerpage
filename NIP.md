NIP-XX
======

Peerpage: Static Site Publishing via BitTorrent and Nostr
----------------------------------------------------------

`draft` `optional`

## Abstract

This NIP defines a protocol for publishing, discovering, and sharing static
websites using BitTorrent for content distribution and Nostr for authenticated,
versioned announcements.

Publishers create a torrent of their website files and publish a Nostr event
containing the magnet URI. Subscribers discover the event, download the torrent,
and serve the content locally. The network is fully decentralised: Nostr provides
discovery and authentication; BitTorrent provides peer-to-peer content
distribution.

## Motivation

Static websites hosted on centralised servers depend on a single point of
failure. Peerpage lets any Nostr user publish a website by sharing a folder as
a BitTorrent torrent and announcing it on Nostr. Subscribers download and
re-seed the site automatically, making it resilient to publisher downtime. No
central registry, CDN, or hosting provider is required.

## Event Format

Peerpage uses **addressable events** (kind `34838`) as defined in NIP-01.

```json
{
  "kind": 34838,
  "pubkey": "<publisher-pubkey-hex>",
  "content": "<changelog text>",
  "tags": [
    ["d",        "<site-identifier>"],
    ["magnet",   "<magnet-uri>"],
    ["r",        "<relay-url>"],
    ["protocol", "<protocol-version>"]
  ]
}
```

### Tag Definitions

| Tag          | Mandatory | Description                                                                                                                           |
|--------------|-----------|---------------------------------------------------------------------------------------------------------------------------------------|
| `d`          | yes       | Site identifier — an arbitrary UTF-8 string naming the site within the publisher's key-space.                                         |
| `magnet`     | yes       | Magnet URI of the current site torrent. MUST include both `xt=urn:btih:` (v1 info-hash) and `xt=urn:btmh:` (v2 info-hash) parameters. |
| `protocol`   | yes       | Protocol version string (see [Protocol Versioning](#protocol-versioning)). Events without this tag MUST be rejected.                  |
| `r`          | no        | Relay hint. One tag per relay URL. Publishers SHOULD include the relays they publish to.                                              |
| `version`    | no        | Informational site revision counter. Not used for event ordering; clients use `created_at` instead.                                   |
| `user_agent` | no        | Client identifier, e.g. `Peerpage/0.1.0 (a3f9c12)`. Informational only.                                                               |

### Content

The event content is a human-readable changelog describing what changed in this
version. It MAY be empty.

### Kind Derivation

The event kind 34838 was chosen deterministically:

```
34838 = int.from_bytes(sha1(b'peerpage').digest()) % 10000 + 30000
```

It falls in the NIP-01 addressable range `[30000, 40000)`.

## Torrent Format

### BitTorrent Version

Torrents MUST carry v2 file metadata (BEP-52). Pure v1 torrents MUST be
rejected because SHA-256 Merkle roots are required for per-file integrity
verification and cross-version deduplication.

Both **hybrid v1+v2** and **pure v2** torrents are accepted. Clients SHOULD
publish hybrid v1+v2 torrents to maximise propagation across trackers and the
DHT network, which has broad v1 support but limited v2 support.

### Directory Structure

The torrent MUST contain exactly one top-level directory named `site`. All
website files MUST be placed inside this directory. Clients MUST reject torrents
where any file path does not begin with `site/`.

```
site/
├── index.html
├── about/
│   └── index.html
└── assets/
    ├── style.css
    └── logo.png
```

### Save Path

When downloading, clients SHOULD use the following layout so that the site
content is accessible at a predictable location:

```
<data_dir>/sites/<npub>/<identifier>/<local_version>/site/<website-files>
```

The torrent is saved with `<data_dir>/sites/<npub>/<identifier>/<local_version>/` as
the save path, so the `site/` directory lands at the correct location.

`<local_version>` is a client-assigned sequential integer (1, 2, 3, …)
incremented each time a newer event is received. It is unrelated to the
optional `version` tag in the Nostr event, which is publisher-assigned and
informational only.

## Site Address

A peerpage site is identified by its publisher's public key and site identifier:

```
peerpage://<identifier>.<npub>
```

Where `<npub>` is the NIP-19 bech32-encoded public key (beginning with `npub1`)
and `<identifier>` is the value of the `d` tag.

A peerpage address always resolves to the **latest** version of the site.
Specific version numbers are a client-local implementation detail and are not
part of the address format.

When parsing, implementations MUST split on the **last** occurrence of `.npub1`
to correctly handle identifiers that themselves contain dots.

Example: `peerpage://myblog.npub1abc123…`

## Protocol Versioning

The `protocol` tag carries an integer string representing the protocol version.

The special value `"-1"` signals a pre-release or development build. Clients
SHOULD accept events with `protocol` equal to `"-1"` for testing purposes, but
MAY reject them in production deployments.

Non-negative integer values (`"0"`, `"1"`, …) are stable protocol versions.
Clients MUST process events with a version they support and MUST reject events
with an unsupported or missing `protocol` tag.

Each revision of this NIP that changes the event format or the torrent
structure in a backward-incompatible way MUST increment the protocol version.
The first stable protocol version is `"0"`; it will be assigned when the NIP is
finalised.

## Client Behaviour

### Publishing

1. Assemble website files into a `site/` directory.
2. Create a hybrid v1+v2 BitTorrent torrent with all files under `site/`.
3. Seed the torrent.
4. Sign and publish a kind `34838` addressable event containing:
   - `d`: the site identifier.
   - `magnet`: the magnet URI of the new torrent.
   - `protocol`: the current protocol version string.
   - Optionally `version`, `r` (relay hints), `user_agent`.

### Subscribing

1. Fetch kind `34838` events from the target publisher pubkey, filtered by `d`.
2. Discard events with an unsupported or missing `protocol` tag.
3. Among remaining events, select the one with the **latest `created_at`**.
4. Download the torrent identified by the `magnet` tag.
5. Validate the downloaded torrent:
   - MUST have v2 file metadata (SHA-256 Merkle roots). Reject pure v1 torrents.
   - Every file path MUST begin with `site/`. Reject torrents that violate this.
   Rejected torrents MUST be permanently discarded; do not retry the same event.
6. Re-seed the torrent to contribute bandwidth to the network.

### Version Updates

When a new event with a higher `version` (or later `created_at`) is received:

1. Begin downloading the new torrent.
2. Continue seeding the current version until the new one is fully downloaded
   and verified.
3. After the new version is fully seeded, continue seeding older versions for
   a while to serve peers that have not yet discovered the update.

## Example Event

```json
{
  "kind": 34838,
  "pubkey": "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
  "content": "initial release",
  "tags": [
    ["d",        "myblog"],
    ["magnet",   "magnet:?xt=urn:btih:aabbccdd…&xt=urn:btmh:1220eeff…&tr=wss://tracker.example.com"],
    ["r",        "wss://relay.damus.io"],
    ["r",        "wss://nos.lol"],
    ["protocol", "<version>"]
  ]
}
```

## References

- [NIP-19](https://github.com/nostr-protocol/nips/blob/master/19.md) — bech32-encoded entities
- [NIP-01](https://github.com/nostr-protocol/nips/blob/master/01.md) — base protocol, including addressable events
- [BEP-52](https://www.bittorrent.org/beps/bep_0052.html) — the BitTorrent Protocol Specification v2
