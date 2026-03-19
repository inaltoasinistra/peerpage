#!/usr/bin/env python3
"""ncurses dashboard for the peerpage daemon."""
import curses
import json
import os
import time
import urllib.request

BASE_URL = os.environ.get('PEERPAGE_URL', 'http://localhost:8008')
REFRESH = 1.0  # seconds between polls


def _format_rate(bps: int) -> str:
    if bps >= 1_000_000:
        return f'{bps / 1_000_000:.1f} MB/s'
    if bps >= 1_000:
        return f'{bps / 1_000:.1f} KB/s'
    return f'{bps} B/s'


def _format_bytes(n: int) -> str:
    if n >= 1_073_741_824:
        return f'{n / 1_073_741_824:.1f} GB'
    if n >= 1_048_576:
        return f'{n / 1_048_576:.1f} MB'
    if n >= 1_024:
        return f'{n / 1_024:.1f} KB'
    return f'{n} B'


def _fetch() -> list | None:
    try:
        with urllib.request.urlopen(f'{BASE_URL}/@/api/sites', timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, json.JSONDecodeError):
        return None


def _safe_addstr(win: curses.window, row: int, col: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if row < 0 or row >= h:
        return
    max_len = w - col - 1
    if max_len <= 0:
        return
    try:
        win.addstr(row, col, text[:max_len], attr)
    except curses.error:
        pass


def _draw(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # header
    curses.init_pair(2, curses.COLOR_GREEN, -1)   # seeding
    curses.init_pair(3, curses.COLOR_YELLOW, -1)  # other states
    curses.init_pair(4, curses.COLOR_RED, -1)     # error / no connection

    last_fetch = 0.0
    data: list | None = None

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q')):
            break

        now = time.monotonic()
        if now - last_fetch >= REFRESH:
            data = _fetch()
            last_fetch = now

        stdscr.erase()
        h, _ = stdscr.getmaxyx()

        if data is None:
            _safe_addstr(stdscr, 0, 0,
                         f' connecting to daemon ({BASE_URL}) ...',
                         curses.color_pair(4) | curses.A_BOLD)
            _safe_addstr(stdscr, h - 1, 0, ' q: quit')
            stdscr.refresh()
            time.sleep(0.1)
            continue

        sites = sorted(data,
                       key=lambda s: (s.get('identifier', ''), s.get('version', 0)))
        total_up = sum(s['upload_rate'] for s in sites)
        total_dn = sum(s['download_rate'] for s in sites)
        n = len(sites)
        label = 'site' if n == 1 else 'sites'

        _safe_addstr(stdscr, 0, 0,
                     f' peerpage   {n} {label}   ↑ {_format_rate(total_up)}   ↓ {_format_rate(total_dn)}',
                     curses.color_pair(1) | curses.A_BOLD)

        cols = (f'  {"ID  ":<20} {"VER":>4}  {"STATE":<12}'
                f' {"UP":>10} {"DN":>10} {"DISK":>10} {"EXCL":>10} {"PEERS":>6}')
        _safe_addstr(stdscr, 2, 0, cols, curses.A_UNDERLINE)

        for i, site in enumerate(sites):
            row = 3 + i
            if row >= h - 1:
                break
            state = site.get('state', '?')
            color = curses.color_pair(2) if state == 'seeding' else curses.color_pair(3)
            line = (
                f'  {site.get("identifier", "?"):<20}'
                f' {site.get("version", "?"):>4} '
                f' {state:<12}'
                f' {_format_rate(site["upload_rate"]):>10}'
                f' {_format_rate(site["download_rate"]):>10}'
                f' {_format_bytes(site["disk_bytes"]):>10}'
                f' {_format_bytes(site["exclusive_bytes"]):>10}'
                f' {site["num_peers"]:>6}'
            )
            _safe_addstr(stdscr, row, 0, line, color)

        _safe_addstr(stdscr, h - 1, 0, ' q: quit')
        stdscr.refresh()
        time.sleep(0.05)


def main() -> None:
    curses.wrapper(_draw)


if __name__ == '__main__':
    main()
