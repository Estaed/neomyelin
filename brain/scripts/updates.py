#!/usr/bin/env python3
"""Tell the user once when a newer NeoMyelin release is out.

Session start (memory_context.py) calls `session_notice`, which reads files in the vault and never
the network. It returns one `[Memory: Update]` line when all of these hold:

- the notice is on: `update_notice` in the config is not false, and config.UPDATE_NOTICE_ENV is
  not off;
- install.py recorded this vault's version in `.brain/VERSION` (from the release's VERSION file).
  A copy without one records none, and an unknown version is never "outdated";
- the cached latest release (`.brain/.state/update-check.json`) is newer, compared as numbers
  x.y.z, and fresh: the last check worked and was made within FRESH;
- this vault was not told about that version yet: `.brain/.state/update-notices/<version>` is
  created exclusively, so of two sessions starting together only one shows it.

When a check is due, session start starts `updates.py --check` detached (no console window,
NEOMYELIN_INVOKED_BY set) and returns at once. That process makes one request to GitHub's
latest-release API for config.RELEASE_REPO (conditional on the cached ETag), writes the cache
atomically and is ended after WORKER_LIMIT seconds whatever happens. Due: a day after a check that
worked; after a failure 1 hour, 6 hours, then a day (longer when GitHub sends Retry-After). An
exclusive claim file lets only one of several sessions starting together start it.

Only the version and the release page URL are kept from GitHub's answer: the release notes, names
and every other field are dropped before the cache is written, so none of them reaches a session.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

API_HOST = 'api.github.com'
API = f'https://{API_HOST}/repos/{config.RELEASE_REPO}/releases/latest'
PAGE = f'https://github.com/{config.RELEASE_REPO}/releases/tag/'
GUIDE = f'https://raw.githubusercontent.com/{config.RELEASE_REPO}/main/INSTALL.md'
VERSION_FILE = 'VERSION'           # in .brain/, written by install.py
CACHE = 'update-check.json'        # in .brain/.state/, written only by the check
CLAIM = 'update-check.claim'       # in .brain/.state/, while a check runs
NOTICES = 'update-notices'         # in .brain/.state/, one file per version announced
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'  # memory_context.INVOKED_ENV: the layer's own processes
OFF = {'off', '0', 'false', 'no'}

HOUR, DAY = 3600, 86400
FRESH = 7 * DAY                    # a cache older than this shows nothing
BACKOFF = (HOUR, 6 * HOUR, DAY)    # after the 1st, the 2nd, the 3rd and later failure in a row
LONGEST_WAIT = 7 * DAY             # a Retry-After is honoured up to this
CLAIM_WINDOW = 60                  # seconds a claim keeps other sessions from starting a check
WORKER_LIMIT = 15                  # seconds the check process lives at most
TIMEOUT = 5                        # seconds per network operation
MAX_ANSWER = 1024 * 1024
MAX_FILE = 16 * 1024
VERSION_RE = re.compile(r'(\d{1,9})\.(\d{1,9})\.(\d{1,9})', re.ASCII)
ETAG_RE = re.compile(r'[\x21-\x7e]{1,256}', re.ASCII)
ERROR_RE = re.compile(r'[a-z0-9_]{1,40}', re.ASCII)
CACHE_KEYS = {'schema', 'attempted_at', 'next_check_at', 'failures', 'checked_at', 'etag', 'latest', 'error'}


class CheckError(Exception):
    """A check that brought no release: `code` goes into the cache, `wait` is GitHub's ask (seconds)."""

    def __init__(self, code: str, wait: float = 0.0):
        super().__init__(code)
        self.code, self.wait = code, wait


def parse_version(value: object) -> tuple[int, int, int] | None:
    """(major, minor, patch) of an `x.y.z` string, else None."""
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def state_dir(vault: Path) -> Path:
    return vault / '.brain' / '.state'


def version_path(vault: Path) -> Path:
    return vault / '.brain' / VERSION_FILE


def installed_version(vault: Path) -> str | None:
    """The version install.py recorded, or None (none recorded, or not x.y.z)."""
    try:
        with version_path(vault).open('rb') as handle:
            text = handle.read(64).decode('utf-8-sig').strip()
    except (OSError, UnicodeError):
        return None
    return text if parse_version(text) else None


def enabled(cfg: dict) -> bool:
    return (cfg.get(config.UPDATE_NOTICE_KEY, True) is not False
            and os.environ.get(config.UPDATE_NOTICE_ENV, '').strip().casefold() not in OFF)


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def read_cache(vault: Path) -> dict:
    """The cache as the check wrote it, or {} when it is absent or anything in it is odd (a hand
    edit, a foreign file): an odd cache shows nothing and makes a check due, which rewrites it."""
    try:
        with (state_dir(vault) / CACHE).open('rb') as handle:
            data = handle.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            return {}
        value = json.loads(data.decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(value, dict) or set(value) - CACHE_KEYS or value.get('schema') != 1:
        return {}
    attempted, next_check, failures = value.get('attempted_at'), value.get('next_check_at'), value.get('failures')
    if not (_number(attempted) and _number(next_check) and 0 <= next_check - attempted <= LONGEST_WAIT
            and type(failures) is int and 0 <= failures <= len(BACKOFF)):
        return {}
    if 'checked_at' in value and not _number(value['checked_at']):
        return {}
    if 'etag' in value and not (isinstance(value['etag'], str) and ETAG_RE.fullmatch(value['etag'])):
        return {}
    if 'error' in value and not (isinstance(value['error'], str) and ERROR_RE.fullmatch(value['error'])):
        return {}
    if 'latest' in value:
        latest = value['latest']
        # The URL is only ever the release page of that version: nothing else rides along.
        if not (isinstance(latest, dict) and set(latest) == {'version', 'url'}
                and parse_version(latest['version'])
                and latest['url'] in (PAGE + 'v' + latest['version'], PAGE + latest['version'])):
            return {}
    return value


def due(cache: dict, now: float) -> bool:
    """A check is due with no usable cache, once next_check_at has come, and when the clock is
    behind the last attempt (a clock set back never stops the checks)."""
    return not cache or not cache['attempted_at'] <= now < cache['next_check_at']


def newer(cache: dict, installed: str, now: float) -> dict | None:
    """The cached latest release when it is newer than `installed` and fresh, else None."""
    latest, checked = cache.get('latest'), cache.get('checked_at')
    if (not latest or 'error' in cache or checked is None or not 0 <= now - checked <= FRESH
            or parse_version(latest['version']) <= parse_version(installed)):
        return None
    return latest


def first_time(vault: Path, version: str) -> bool:
    """True once per version per vault. The marker is created exclusively, so of two sessions
    starting together one gets True; a marker that cannot be written means no line, never a
    notice on every session."""
    folder = state_dir(vault) / NOTICES
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / version).open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(dt.datetime.now().astimezone().isoformat(timespec='seconds') + '\n')
    except OSError:
        return False
    return True


def notice_line(vault: Path, installed: str, latest: dict) -> str:
    launcher = 'py -3' if os.name == 'nt' else 'python3'
    return (f"[Memory: Update] NeoMyelin {latest['version']} is out; this vault runs {installed}. "
            'In your first reply, tell the user so in one or two plain sentences: the release notes '
            f"are at {latest['url']}, and to update they can ask you. You then follow the Updating "
            f'section of {GUIDE} (download the release, run `{launcher} install.py --vault "{vault}"` '
            'from its folder). This notice is shown once.')


def claim(vault: Path) -> bool:
    """The right to start the check. An exclusive create, so of two sessions starting together
    one wins. A claim older than CLAIM_WINDOW (its check was ended before it cleaned up) is moved
    aside by one session, which then claims."""
    path = state_dir(vault) / CLAIM
    for _ in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                if 0 <= time.time() - path.stat().st_mtime < CLAIM_WINDOW:
                    return False
                aside = path.with_name(f'{CLAIM}.{os.getpid()}')
                os.replace(path, aside)
                if 0 <= time.time() - aside.stat().st_mtime < CLAIM_WINDOW:
                    os.replace(aside, path)  # another session claimed in between: its claim goes back
                    return False
                aside.unlink()
            except FileNotFoundError:
                continue  # released or moved meanwhile: try the exclusive create once more
            except OSError:
                return False
            continue
        except OSError:
            return False
        with os.fdopen(handle, 'w', encoding='utf-8') as out:
            out.write(f'{os.getpid()}\n')
        return True
    return False


def release(vault: Path) -> None:
    try:
        (state_dir(vault) / CLAIM).unlink()
    except OSError:
        pass


def start_check(vault: Path) -> None:
    """`updates.py --check` detached: the session start does not wait for it."""
    env = {**os.environ, INVOKED_ENV: 'updates', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--check', '--vault', str(vault)],
                     cwd=SCRIPT_DIR, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                     start_new_session=os.name != 'nt')


def session_notice(cfg: dict, now: float | None = None, start: bool = True) -> str:
    """The `[Memory: Update]` line, or ''. Starts the detached check when one is due, unless
    start is False or this process was started by the layer itself. Files only, never the network."""
    if not enabled(cfg):
        return ''
    vault = config.vault_path(cfg)
    installed = installed_version(vault)
    if installed is None:
        return ''
    now = time.time() if now is None else now
    cache = read_cache(vault)
    latest = newer(cache, installed, now)
    text = notice_line(vault, installed, latest) if latest and first_time(vault, latest['version']) else ''
    if start and not os.environ.get(INVOKED_ENV) and due(cache, now) and claim(vault):
        # Another session's check may have finished between the read above and the claim.
        if due(read_cache(vault), now):
            try:
                start_check(vault)
                return text
            except OSError:
                pass
        release(vault)
    return text


# ---------------------------------------------------------------------------------------------
# The check: the detached process session start launches.

class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to GitHub's API host over HTTPS (a renamed repository answers with
    one); a redirect anywhere else ends the check."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urllib.parse.urlsplit(newurl)
            port = parts.port
        except ValueError:
            raise CheckError('redirect_off_github') from None
        if (parts.scheme != 'https' or parts.hostname != API_HOST or parts.username or parts.password
                or port not in (None, 443)):
            raise CheckError('redirect_off_github')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _etag(value: object) -> str | None:
    return value if isinstance(value, str) and ETAG_RE.fullmatch(value) else None


def _wait(headers) -> float:
    """Seconds GitHub asks to wait: Retry-After (seconds or a date), or until the rate limit
    resets when no request is left. 0 when it asks nothing or says it oddly."""
    def seconds(read) -> float:
        try:
            value = read()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    def retry_after() -> float:
        retry = (headers.get('Retry-After') or '').strip()
        if retry.isdigit():
            return float(retry)
        return email.utils.parsedate_to_datetime(retry).timestamp() - time.time() if retry else 0.0

    def reset() -> float:
        if headers.get('X-RateLimit-Remaining') != '0':
            return 0.0
        return float(headers.get('X-RateLimit-Reset') or 0) - time.time()

    return max(seconds(retry_after), seconds(reset))


def parse_release(data: bytes) -> dict:
    """{'version', 'url'} from the latest-release answer; CheckError when it is not a release."""
    try:
        answer = json.loads(data.decode('utf-8'))
    except (ValueError, RecursionError):
        raise CheckError('not_json') from None
    if not isinstance(answer, dict) or answer.get('draft') is not False or answer.get('prerelease') is not False:
        raise CheckError('not_a_release')
    tag = answer.get('tag_name')
    version = tag[1:] if isinstance(tag, str) and tag.startswith('v') else tag
    if not parse_version(version):
        raise CheckError('bad_version')
    url = PAGE + tag
    page = answer.get('html_url')
    if not isinstance(page, str) or page.casefold() != url.casefold():
        raise CheckError('bad_release_url')
    return {'version': version, 'url': url}


def fetch_latest(etag: str | None = None) -> tuple[dict | None, str | None]:
    """One request to the latest-release API: (release, ETag), or (None, ETag) when the cached
    ETag still holds (304). Raises CheckError for everything else."""
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'neomyelin-update-check'}
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(API, headers=headers)
    try:
        with urllib.request.build_opener(_GitHubOnly()).open(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_ANSWER + 1)
            new_etag = response.headers.get('ETag')
    except CheckError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 304:
                return None, _etag(exc.headers.get('ETag')) or etag
            raise CheckError(f'http_{exc.code}', _wait(exc.headers)) from None
        finally:
            exc.close()
    except (OSError, ValueError, http.client.HTTPException):
        raise CheckError('network') from None
    if len(data) > MAX_ANSWER:
        raise CheckError('answer_too_large')
    return parse_release(data), _etag(new_etag)


def write_cache(vault: Path, value: dict) -> None:
    """Atomically: a session reading at the same moment sees the old file or the new one, whole."""
    state = state_dir(vault)
    state.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=state, prefix='.update-check-', suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, state / CACHE)
                break
            except PermissionError:  # Windows: a session start is reading it this moment
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        try:
            os.unlink(temporary)  # still there only when the replace failed
        except OSError:
            pass


def failed(previous: dict, now: float, exc: BaseException) -> dict:
    """The cache after a failed check: the last good result kept (shown again only after a check
    works), the next check 1 hour, 6 hours, then a day away, or later when GitHub asked for that."""
    code, wait = (exc.code, exc.wait) if isinstance(exc, CheckError) else ('unexpected', 0.0)
    failures = min(previous.get('failures', 0) + 1, len(BACKOFF))
    wait = min(max(BACKOFF[failures - 1], wait), LONGEST_WAIT)
    value = {key: previous[key] for key in ('checked_at', 'etag', 'latest') if key in previous}
    value.update(schema=1, attempted_at=now, next_check_at=now + wait, failures=failures, error=code)
    return value


def refresh(vault: Path, now: float | None = None, fetch=None) -> dict:
    """One check; its result goes into the cache atomically, a failure with its backoff."""
    now = time.time() if now is None else now
    previous = read_cache(vault)
    try:
        cached = previous.get('latest')
        latest, etag = (fetch or fetch_latest)(previous.get('etag') if cached else None)
        if latest is None:
            if not cached:
                raise CheckError('not_modified_without_cache')
            latest = cached
        value = {'schema': 1, 'attempted_at': now, 'next_check_at': now + DAY, 'failures': 0,
                 'checked_at': now, 'latest': latest}
        if etag:
            value['etag'] = etag
    except Exception as exc:  # noqa: BLE001 - every failure backs off, a bug included
        value = failed(previous, now, exc)
    write_cache(vault, value)
    return value


def give_up(vault: Path) -> None:
    """The watchdog's last act: a check that took too long (a stalled DNS lookup or peer) is a
    failure, so the backoff holds; then the claim goes."""
    try:
        write_cache(vault, failed(read_cache(vault), time.time(), CheckError('timeout')))
    except OSError:
        pass
    release(vault)


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true',
                        help='Ask GitHub for the latest release now and write the cache '
                             '(session start runs this in the background when a check is due).')
    parser.add_argument('--vault', type=Path, help='The vault; default: the one in .brain/config.json.')
    args = parser.parse_args(argv)
    if not args.check:
        parser.print_usage(sys.stderr)
        return 2
    try:
        vault = args.vault.expanduser().resolve() if args.vault else config.vault_path(config.load())
    except config.ConfigError as exc:
        print(f'updates.py: {exc}', file=sys.stderr)
        return 1
    if not (vault / '.brain').is_dir():
        print(f'updates.py: {vault} has no .brain folder; nothing was checked', file=sys.stderr)
        return 1

    def stop() -> None:
        give_up(vault)
        os._exit(1)

    watchdog = threading.Timer(WORKER_LIMIT, stop)
    watchdog.daemon = True
    watchdog.start()
    try:
        value = refresh(vault)
    finally:
        watchdog.cancel()
        release(vault)
    after = dt.datetime.fromtimestamp(value['next_check_at']).astimezone().strftime('%Y-%m-%d %H:%M')
    if 'error' in value:
        print(f"update check failed: {value['error']}; next check after {after}")
        return 1
    print(f"latest release: {value['latest']['version']} ({value['latest']['url']}); next check after {after}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
