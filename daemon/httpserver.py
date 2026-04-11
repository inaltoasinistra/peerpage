"""Local HTTP server mapping peerpage site URLs to local files.

URL schema
----------
  http://0.0.0.0:8008/@/                               dashboard
  http://0.0.0.0:8008/@/config                         config UI
  http://0.0.0.0:8008/<name>.<npub>/                   site root (latest version)
  http://0.0.0.0:8008/<name>.<npub>/<path>             site file (latest version)
"""

import html
import json
import logging
import os
import shutil
import string
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

import config as cfg_module
from config import Config
from fileutil import CONTENT_DIR, last_complete_version
from nostr_client import NostrClient
from snapshot import torrent_manifest
from .session import TorrentSession

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / 'templates'
_STATIC_DIR = Path(__file__).parent / 'static'


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _render(name: str, **kwargs) -> str:
    """Render template *name* with the given keyword arguments."""
    text = (_TEMPLATES_DIR / name).read_text()
    return string.Template(text).safe_substitute(**kwargs)


def _page(title: str, body: str) -> str:
    return _render('base.html', title=title, body=body)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _parse_identifier(identifier: str) -> tuple[str, str] | None:
    """Split 'sitename.npub1xxx' into (site_name, npub), or None if malformed."""
    pos = identifier.rfind('.npub1')
    if pos == -1:
        return None
    return identifier[:pos], identifier[pos + 1:]


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{n / 1024:.1f} KB'
    return f'{n / (1024 * 1024):.1f} MB'


def _dir_listing(identifier: str, path: str, abs_dir: str, ls: bool = False) -> web.Response:
    """Return an HTML directory listing for *abs_dir*."""
    qs = '?ls' if ls else ''
    try:
        entries = sorted(os.scandir(abs_dir), key=lambda e: (not e.is_dir(), e.name))
    except OSError:
        raise web.HTTPNotFound()
    rows = []
    if path:
        rows.append(f'<tr><td colspan="2"><a href="../{qs}">⬆ Parent directory</a></td></tr>')
    for entry in entries:
        name = html.escape(entry.name)
        if entry.is_dir():
            rows.append(
                f'<tr>'
                f'<td><a href="{name}/{qs}">📁 {name}</a></td>'
                f'<td class="size dim">—</td>'
                f'</tr>'
            )
        else:
            try:
                size = _fmt_size(entry.stat().st_size)
            except OSError:
                size = '—'
            rows.append(
                f'<tr>'
                f'<td><a href="{name}">📄 {name}</a></td>'
                f'<td class="size dim">{size}</td>'
                f'</tr>'
            )
    body = (
        '<table class="dir-listing">'
        '<thead><tr><th>Name</th><th class="size">Size</th></tr></thead>'
        '<tbody>' + '\n'.join(rows) + '</tbody>'
        '</table>'
    )
    return web.Response(text=_page(f'{identifier}/{path}', body),
                        content_type='text/html')


def _safe_join(root: str, rel: str) -> str | None:
    """Return absolute path of root/rel only if it stays within root.

    Returns None on path traversal attempts.
    """
    abs_root = Path(root).resolve()
    abs_path = (abs_root / rel).resolve()
    try:
        abs_path.relative_to(abs_root)
        return str(abs_path)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class HttpServer:

    def __init__(self, data_dir: str, session: TorrentSession,
                 on_download: Callable[[str, str], None] | None = None,
                 on_stop: Callable[[], None] | None = None,
                 config: Config | None = None,
                 config_path: str = '') -> None:
        self._data_dir = data_dir
        self._session = session
        self._on_download = on_download
        self._on_stop = on_stop
        self._config = config
        self._config_path = config_path
        self._runner: web.AppRunner | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get('/', lambda r: web.HTTPFound('/@/'))
        app.router.add_get('/@/', self._handle_dashboard)
        app.router.add_post('/@/add', self._handle_add)
        app.router.add_post('/@/api/add', self._handle_api_add)
        app.router.add_get('/@/api/sites', self._handle_api_sites)
        app.router.add_get('/@/files/{identifier}', self._handle_files)
        app.router.add_get('/@/api/files/{identifier}', self._handle_api_files)
        app.router.add_post('/@/api/priority/{identifier}', self._handle_api_priority)
        app.router.add_post('/@/api/reset/{identifier}', self._handle_api_reset)
        app.router.add_post('/@/api/delete/{identifier}', self._handle_api_delete)
        app.router.add_post('/@/api/stop', self._handle_api_stop)
        app.router.add_get('/@/api/debug', self._handle_api_debug)
        app.router.add_get('/@/config', self._handle_config)
        app.router.add_post('/@/config', self._handle_config_post)
        app.router.add_static('/@/static', path=_STATIC_DIR)
        # /<identifier>  (no trailing slash) → 301 to add slash
        app.router.add_get('/{identifier}', self._handle_no_slash)
        # /<identifier>/<path>  (path may be empty for the root /)
        app.router.add_get('/{identifier}/{path:.*}', self._handle_site)
        return app

    async def start(self, host: str = '0.0.0.0', port: int = 8008) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        logger.info('HTTP server listening on http://%s:%d/', host, port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ UI

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        sites = self._session.sites_info()
        if sites:
            latest = {}
            for s in sites:
                key = s['url_identifier']
                if s['version'] > latest.get(key, -1):
                    latest[key] = s['version']
            rows = ''.join(
                _render('dashboard_row.html',
                        site_cell=(
                            f'<a href="/{s["url_identifier"]}/">{s["identifier"]}</a>'
                            + (
                                f' <a href="/@/files/{s["url_identifier"]}"'
                                f' class="files-link dim">files</a>'
                                if s['state'] != 'downloading_metadata'
                                else ''
                            )
                            + f' <a href="#" class="del-link dim"'
                              f' onclick="deleteSite(\'{s["url_identifier"]}\');return false;"'
                              f'>delete</a>'
                            if s['version'] == latest[s['url_identifier']]
                            else s['identifier']
                        ),
                        version=s['version'],
                        state=s['state'],
                        num_peers=s['num_peers'],
                        peers_class='dim' if s['num_peers'] == 0 else '',
                        upload_kb=s['upload_rate'] // 1024,
                        upload_class='dim' if s['upload_rate'] == 0 else '',
                        download_kb=s['download_rate'] // 1024,
                        download_class='dim' if s['download_rate'] == 0 else '',
                        disk_mb=f"{s['disk_bytes'] / (1024 * 1024):.1f}",
                        disk_class='dim' if s['disk_bytes'] < 0.1 * 1024 * 1024 else '',
                        exclusive_mb=f"{s['exclusive_bytes'] / (1024 * 1024):.1f}",
                        excl_class='dim' if s['exclusive_bytes'] < 0.1 * 1024 * 1024 else '',
                        site_total_mb=f"{s['site_total_bytes'] / (1024 * 1024):.1f}",
                        total_class='dim' if s['site_total_bytes'] < 0.1 * 1024 * 1024 else '')
                for s in sites
            )
        else:
            rows = '<tr><td colspan="9" class="dim">No active sites.</td></tr>'
        body = _render('dashboard.html', rows=rows)
        return web.Response(text=_page('Dashboard', body),
                            content_type='text/html')

    async def _handle_api_sites(self, request: web.Request) -> web.Response:
        return web.json_response(self._session.sites_info())

    async def _handle_config(self, request: web.Request) -> web.Response:
        max_site_mb = (self._config.max_site_mb if self._config is not None
                       else cfg_module.DEFAULT_MAX_SITE_MB)
        saved_msg = 'Saved.' if 'saved' in request.rel_url.query else ''
        body = _render('config.html', max_site_mb=max_site_mb, saved_msg=saved_msg)
        return web.Response(text=_page('Config', body), content_type='text/html')

    async def _handle_config_post(self, request: web.Request) -> web.Response:
        data = await request.post()
        try:
            max_site_mb = int(data['max_site_mb'])
            if max_site_mb < 1:
                raise ValueError
        except (KeyError, ValueError):
            raise web.HTTPBadRequest()
        if self._config is not None:
            self._config.max_site_mb = max_site_mb
            if self._config_path:
                cfg_module.save(self._config, self._config_path)
        raise web.HTTPSeeOther('/@/config?saved=1')

    async def _handle_add(self, request: web.Request) -> web.Response:
        """Accept a peerpage address and queue it for download (web UI, redirects)."""
        if self._on_download is not None:
            data = await request.post()
            address = data.get('address', '').strip()
            try:
                _, site_name, _relays = NostrClient.parse_address(address)
                self._on_download(site_name, address)
            except ValueError:
                pass
        raise web.HTTPSeeOther('/@/')

    async def _handle_api_add(self, request: web.Request) -> web.Response:
        """Accept a peerpage address and queue it for download (API, returns JSON)."""
        if self._on_download is None:
            raise web.HTTPServiceUnavailable()
        data = await request.post()
        address = data.get('address', '').strip()
        try:
            _, site_name, _relays = NostrClient.parse_address(address)
        except ValueError as e:
            raise web.HTTPBadRequest(reason=str(e))
        self._on_download(site_name, address)
        return web.json_response({'ok': True})

    # ---------------------------------------------------------- file selector

    def _torrent_path_for(self, site_name: str, npub: str) -> str | None:
        site_data = os.path.join(self._data_dir, 'sites', npub, site_name)
        version = last_complete_version(site_data)
        if version is None:
            return None
        return os.path.join(site_data, str(version), 'site.torrent')

    def _resolve_torrent_path(self, request: web.Request) -> tuple[str, str]:
        """Return (identifier, torrent_path) or raise HTTPNotFound."""
        identifier = request.match_info['identifier']
        parsed = _parse_identifier(identifier)
        if parsed is None:
            raise web.HTTPNotFound()
        site_name, npub = parsed
        torrent_path = self._torrent_path_for(site_name, npub)
        if torrent_path is None:
            raise web.HTTPNotFound()
        return identifier, torrent_path

    async def _handle_files(self, request: web.Request) -> web.Response:
        identifier, torrent_path = self._resolve_torrent_path(request)
        result = self._session.file_list(torrent_path)
        if result is None:
            raise web.HTTPServiceUnavailable()
        files, total_files = result
        body = _render('files.html',
                       identifier=html.escape(identifier),
                       files_json=json.dumps(files),
                       total_files=total_files)
        return web.Response(text=_page(f'{identifier} — files', body),
                            content_type='text/html')

    async def _handle_api_files(self, request: web.Request) -> web.Response:
        identifier, torrent_path = self._resolve_torrent_path(request)
        result = self._session.file_list(torrent_path)
        if result is None:
            raise web.HTTPNotFound()
        files, total_files = result
        return web.json_response({'identifier': identifier, 'files': files,
                                  'total_files': total_files})

    async def _handle_api_priority(self, request: web.Request) -> web.Response:
        identifier, torrent_path = self._resolve_torrent_path(request)
        try:
            data = await request.json()
            priorities = [int(p) for p in data['priorities']]
        except Exception:
            raise web.HTTPBadRequest()
        if not self._session.set_file_priorities(torrent_path, priorities):
            raise web.HTTPNotFound()
        return web.json_response({'ok': True})

    async def _handle_api_reset(self, request: web.Request) -> web.Response:
        """Reset file priorities to automatic (re-run budget algorithm)."""
        identifier, torrent_path = self._resolve_torrent_path(request)
        max_site_mb = (self._config.max_site_mb if self._config is not None
                       else cfg_module.DEFAULT_MAX_SITE_MB)
        if not self._session.reset_file_priorities(torrent_path, max_site_mb):
            raise web.HTTPNotFound()
        return web.json_response({'ok': True})

    async def _handle_api_delete(self, request: web.Request) -> web.Response:
        identifier = request.match_info['identifier']
        parsed = _parse_identifier(identifier)
        if parsed is None:
            raise web.HTTPNotFound()
        site_name, npub = parsed
        site_data_dir = os.path.join(self._data_dir, 'sites', npub, site_name)
        if not os.path.isdir(site_data_dir):
            raise web.HTTPNotFound()
        self._session.stop_site(site_data_dir)
        shutil.rmtree(site_data_dir)
        logger.info('deleted site: %s', identifier)
        return web.json_response({'ok': True})

    async def _handle_api_stop(self, request: web.Request) -> web.Response:
        if self._on_stop is not None:
            self._on_stop()
        return web.json_response({'ok': True})

    async def _handle_api_debug(self, request: web.Request) -> web.Response:
        return web.json_response(self._session.debug_info())

    # ---------------------------------------------------------- site serving

    @staticmethod
    async def _handle_no_slash(request: web.Request) -> web.Response:
        """Redirect /<id> → /<id>/ so relative links work."""
        ident = request.match_info['identifier']
        raise web.HTTPMovedPermanently(location=f'/{ident}/')

    async def _handle_site(self, request: web.Request) -> web.Response:
        identifier = request.match_info['identifier']
        path = unquote(request.match_info.get('path', ''))

        parsed = _parse_identifier(identifier)
        if parsed is None:
            raise web.HTTPNotFound()
        site_name, npub = parsed

        site_data = os.path.join(self._data_dir, 'sites', npub, site_name)
        version = last_complete_version(site_data)
        if version is None:
            body = _render('not_available.html', site_name=site_name)
            return web.Response(text=_page('Not yet available', body),
                                content_type='text/html', status=503)

        content_root = os.path.join(site_data, str(version), CONTENT_DIR)

        # Directory path (empty or ends with /): try index.html, then listing
        if not path or path.endswith('/'):
            index = _safe_join(content_root, path + 'index.html')
            if index and os.path.isfile(index) and 'ls' not in request.rel_url.query:
                return web.FileResponse(index)
            abs_dir = _safe_join(content_root, path)
            if abs_dir and os.path.isdir(abs_dir):
                return _dir_listing(identifier, path, abs_dir,
                                    ls='ls' in request.rel_url.query)
            raise web.HTTPNotFound()

        abs_path = _safe_join(content_root, path)
        if abs_path is None:
            raise web.HTTPBadRequest(reason='invalid path')

        if os.path.isdir(abs_path):
            # Add trailing slash so relative links resolve correctly
            raise web.HTTPMovedPermanently(location=f'/{identifier}/{path}/')

        if os.path.isfile(abs_path):
            return web.FileResponse(abs_path)

        return self._not_downloaded_page(npub, site_name, version, path)

    def _not_downloaded_page(self, npub: str, site_name: str,
                              version: int, path: str) -> web.Response:
        torrent_path = os.path.join(
            self._data_dir, 'sites', npub, site_name, str(version), 'site.torrent',
        )
        if not os.path.isfile(torrent_path):
            raise web.HTTPNotFound()
        try:
            manifest = torrent_manifest(torrent_path)
        except Exception:
            raise web.HTTPNotFound()
        if f'{CONTENT_DIR}/{path}' not in manifest:
            raise web.HTTPNotFound()
        body = _render('not_downloaded.html', path=path, site_name=site_name)
        return web.Response(text=_page('File not downloaded', body),
                            content_type='text/html')
