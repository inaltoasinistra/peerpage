import subprocess

_VERSION: str | None = None  # Set to a release string, e.g. "0.1.0"

_commit: str | None = None
_commit_fetched: bool = False


def _get_commit() -> str | None:
    global _commit, _commit_fetched
    if not _commit_fetched:
        try:
            _commit = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            _commit = None
        _commit_fetched = True
    return _commit


def get_user_agent() -> str:
    commit = _get_commit()
    if _VERSION and commit:
        return f'Peerpage/{_VERSION} ({commit})'
    if commit:
        return f'Peerpage/{commit}'
    if _VERSION:
        return f'Peerpage/{_VERSION}'
    return 'Peerpage/unknown'
