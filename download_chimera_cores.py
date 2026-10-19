#!/usr/bin/env python3
"""
Download the latest nightly release assets for every repo in the
ToolAssisted-run GitHub org whose name starts with "chimera-core-".

Files are saved into a "cores" folder next to this script.
Uses only the Python standard library (Python 3.8+).

Usage:
    python download_chimera_cores.py            # asks what to do if rate-limited
    python download_chimera_cores.py --wait     # wait for the reset and continue
    python download_chimera_cores.py --no-wait  # stop at the rate limit, never ask

If GitHub's API rate limit runs out mid-run, the script can wait until the
limit resets and then carry on where it stopped. Run interactively without
either option, it asks you; run unattended (e.g. as a scheduled task), it only
waits when given --wait.

Optional: set a GITHUB_TOKEN environment variable (a fine-grained, read-only
token with no extra permissions is enough) to raise the API rate limit from
60 to 5000 requests/hour. The token is only sent to api.github.com and is
stripped from any redirect that leaves that host.

Safety measures:
  * Only non-fork, non-archived repos owned by the org are considered.
  * Asset filenames must be plain, portable names (no paths, no Windows
    reserved names or special characters).
  * Download URLs must have the exact shape
    https://github.com/<org>/<repo>/releases/download/<tag>/<file>,
    and redirects may only go to GitHub's own API and release-file hosts.
  * Every file must match the SHA-256 digest GitHub publishes for it; assets
    without a digest are refused (see REQUIRE_DIGEST).
  * Oversized assets are refused before downloading; downloads have an
    overall time limit and are written to a freshly created .part file
    (never through an existing file or symlink).
  * A local manifest records the hash of every file downloaded, so existing
    files are re-verified on later runs.
  * Network errors and malformed API data on one repo are reported and the
    run continues; hitting GitHub's API rate limit stops the run with the
    reset time. Text from GitHub is escaped before printing, and printing
    never fails because of the console or log file's encoding.
  * A lock file stops two copies of the script from running at once.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ORG = "ToolAssisted-run"
REPO_PREFIX = "chimera-core-"
TAG_PREFIX = "nightly"          # matches tags like nightly-2026-10-13
ASSET_SUFFIX = ".chimeraCore"   # set to "" to download every asset
DEST_DIR = Path(__file__).resolve().parent / "cores"
MAX_BYTES = 1 * 1024**3         # refuse assets larger than 1 GiB
REQUIRE_DIGEST = True           # refuse assets GitHub has no SHA-256 for
READ_TIMEOUT = 60               # seconds a single network read may stall
DOWNLOAD_DEADLINE = 30 * 60     # seconds a whole file download may take
MAX_RATE_LIMIT_WAIT = 65 * 60   # never wait longer than this for a reset
RESET_MARGIN = 5                # extra seconds to wait past the reset time

API = "https://api.github.com"
API_HOST = "api.github.com"
# Hosts a redirect may lead to: the API itself (renamed repos), github.com,
# and the two hosts GitHub serves release files from.
ALLOWED_REDIRECT_HOSTS = {
    "github.com",
    "api.github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}

# Errors that mean "this request failed" rather than "the script is broken":
# HTTPError, URLError, timeouts and socket errors are all OSError subclasses;
# bad JSON or unexpected response shapes are ValueError.
NET_ERRORS = (OSError, ValueError)


def manifest_path():
    return DEST_DIR / ".manifest.json"


# ---------- output ----------

def clean(text):
    """Make text safe to print anywhere: control characters (e.g. terminal
    escape codes) and non-ASCII characters are shown as escapes like \\x1b or
    \\u591c, so nothing can mess with the terminal or fail to encode."""
    return "".join(
        c if c.isascii() and c.isprintable()
        else c.encode("unicode_escape").decode("ascii")
        for c in str(text)
    )


def make_output_safe():
    """Never crash on printing, whatever encoding the console or log file
    uses (e.g. cp1252 when output is redirected on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def log(repo, message):
    print(f"[{repo}] {clean(message)}")


# ---------- networking ----------

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Only follow redirects to allowed https GitHub hosts, and never carry the
    Authorization header to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlparse(newurl)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in ALLOWED_REDIRECT_HOSTS:
            raise urllib.error.URLError(f"blocked redirect to {newurl}")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        old_host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
        if new_req is not None and host != old_host:
            new_req.remove_header("Authorization")
        return new_req


OPENER = urllib.request.build_opener(SafeRedirect)


class RateLimited(Exception):
    """GitHub's API rate limit is used up; no point making more API calls.
    Deliberately not an OSError/ValueError so per-repo handlers don't absorb it.
    reset_at is the Unix time the limit resets, if GitHub said."""

    def __init__(self, message, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at


def rate_limit_error(err):
    """Return a RateLimited for a rate-limit HTTPError, else None."""
    if err.code not in (403, 429) or err.headers is None:
        return None
    remaining = err.headers.get("X-RateLimit-Remaining")
    retry_after = err.headers.get("Retry-After")
    if err.code != 429 and remaining != "0" and not retry_after:
        return None  # an ordinary 403 (e.g. no access), not a rate limit
    when = None
    try:
        if retry_after:
            when = time.time() + int(retry_after)
        elif err.headers.get("X-RateLimit-Reset"):
            when = int(err.headers["X-RateLimit-Reset"])
    except ValueError:
        pass
    msg = "GitHub API rate limit reached"
    if when:
        msg += f"; it resets at {clock(when)} local time"
    return RateLimited(msg + ".", when)


def clock(unix_time):
    return time.strftime("%H:%M", time.localtime(unix_time))


class RateLimitPolicy:
    """Decides whether to wait out a rate limit: --wait always waits,
    --no-wait never does, otherwise ask once if someone is at the keyboard."""

    MAX_WAITS_IN_A_ROW = 3  # guards against looping if the limit never clears

    def __init__(self, wait_flag):
        self.decision = wait_flag          # True, False, or None = ask
        self.waits_in_a_row = 0

    def wait_for_reset(self, err):
        """Sleep until the limit resets and return True, or return False to stop."""
        if self.waits_in_a_row >= self.MAX_WAITS_IN_A_ROW:
            print("Still rate-limited after waiting; giving up.")
            return False
        reset_at = err.reset_at or (time.time() + 60)
        seconds = max(reset_at - time.time(), 0) + RESET_MARGIN
        if seconds > MAX_RATE_LIMIT_WAIT:
            print(f"The reset is more than {MAX_RATE_LIMIT_WAIT // 60} minutes away; not waiting.")
            return False

        until = clock(time.time() + seconds)
        minutes = max(1, round(seconds / 60))
        if self.decision is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()
            if not interactive:
                return False
            try:
                answer = input(f"\n{err}\nWait until {until} (about {minutes} min) "
                               "and continue? [y/N] ")
            except EOFError:
                answer = ""
            self.decision = answer.strip().lower() in ("y", "yes")
            if not self.decision:
                return False
        elif not self.decision:
            return False
        else:
            print(f"\n{err}")

        print(f"Waiting until {until} (about {minutes} min); press Ctrl+C to stop.")
        time.sleep(seconds)
        print("Resuming.")
        self.waits_in_a_row += 1
        return True

    def succeeded(self):
        self.waits_in_a_row = 0


def api_get(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "chimera-core-downloader",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == API_HOST:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers),
                         timeout=READ_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        limited = rate_limit_error(e)
        if limited:
            raise limited from e
        raise


def api_get_list(url):
    """api_get, but insist on a JSON list (GitHub sends an object on errors)."""
    data = api_get(url)
    if not isinstance(data, list):
        msg = data.get("message") if isinstance(data, dict) else None
        raise ValueError(f"unexpected API response: {msg or type(data).__name__}")
    return data


def describe(err):
    if isinstance(err, urllib.error.HTTPError):
        text = f"HTTP {err.code} {err.reason}"
    elif isinstance(err, urllib.error.URLError):
        text = f"network error: {err.reason}"
    else:
        text = f"{type(err).__name__}: {err}"
    return clean(text)


# ---------- validation helpers ----------

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
RESERVED_SUFFIXES = (".part", ".tmp")   # names this script uses for temp files
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def safe_filename(name):
    """Return name if it is a plain, portable filename, else None."""
    if not isinstance(name, str) or not SAFE_NAME.match(name) or name.endswith("."):
        return None
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return None
    if name.lower().endswith(RESERVED_SUFFIXES):
        return None
    return name


def url_is_trusted(url, repo, tag, name):
    """True only for https://github.com/<ORG>/<repo>/releases/download/<tag>/<name>
    with nothing extra (no port, credentials, query, fragment or dot segments)."""
    if not isinstance(url, str):
        return False
    p = urllib.parse.urlparse(url)
    if (p.scheme != "https" or p.netloc.lower() != "github.com"
            or p.params or p.query or p.fragment):
        return False
    segs = p.path.split("/")
    if len(segs) != 7 or segs[0] != "":
        return False
    _, org, rep, releases, download, url_tag, url_name = segs
    url_tag = urllib.parse.unquote(url_tag)
    url_name = urllib.parse.unquote(url_name)
    return (
        org.lower() == ORG.lower()
        and rep.lower() == repo.lower()
        and releases == "releases"
        and download == "download"
        and url_tag == tag and url_tag not in (".", "..") and "/" not in url_tag
        and url_name == name
    )


def expected_sha256(asset):
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if SHA256_HEX.match(value):
            return value
    return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def create_new(path, mode="wb", **kwargs):
    """Open path for writing only if it does not already exist (as a file,
    folder or symlink), so we never write through something planted there."""
    return open(path, mode.replace("w", "x"), **kwargs)


# ---------- single-instance lock ----------

def acquire_lock():
    """Return an open lock handle, or None if another copy is running.
    The OS releases the lock automatically if the process dies."""
    f = open(DEST_DIR / ".lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def release_lock(f):
    """Unlock explicitly before closing: on Windows, a lock left to be cleaned
    up by close() may linger briefly and block an immediate re-run."""
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


# ---------- manifest of downloaded files ----------

def load_manifest():
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-formed entries; anything else is treated as unknown.
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, dict)
        and isinstance(v.get("sha256"), str) and SHA256_HEX.match(v["sha256"])
    }


def save_manifest(manifest):
    target = manifest_path()
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with create_new(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    tmp.replace(target)


# ---------- GitHub queries ----------

def list_repos():
    repos, page = [], 1
    while True:
        batch = api_get_list(
            f"{API}/orgs/{ORG}/repos?type=public&per_page=100&page={page}")
        repos.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < 100:
            break
        page += 1
    names = []
    for r in repos:
        name, owner = r.get("name"), r.get("owner")
        if (isinstance(name, str) and name.lower().startswith(REPO_PREFIX)
                and isinstance(owner, dict)
                and str(owner.get("login", "")).lower() == ORG.lower()
                and not r.get("fork") and not r.get("archived")
                and re.fullmatch(r"[A-Za-z0-9._-]+", name)):
            names.append(name)
    return sorted(names)


def latest_nightly(repo):
    releases = api_get_list(f"{API}/repos/{ORG}/{repo}/releases?per_page=100")
    nightlies = [
        r for r in releases
        if isinstance(r, dict) and not r.get("draft")
        and isinstance(r.get("tag_name"), str)
        and r["tag_name"].lower().startswith(TAG_PREFIX)
        and isinstance(r.get("published_at") or r.get("created_at"), str)
    ]
    if not nightlies:
        return None
    nightlies.sort(key=lambda r: r.get("published_at") or r["created_at"], reverse=True)
    return nightlies[0]


# ---------- download ----------

def download(url, dest, expected_size, want_sha):
    """Download url to dest and return its SHA-256 hex digest.
    Raises on any failure; never leaves a partial file behind."""
    tmp = dest.with_name(dest.name + ".part")
    limit = min(MAX_BYTES, expected_size) if expected_size else MAX_BYTES
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    h = hashlib.sha256()
    total = 0
    f = create_new(tmp)  # fails if anything already sits at tmp; we don't touch it
    try:
        with f:
            req = urllib.request.Request(
                url, headers={"User-Agent": "chimera-core-downloader"})
            with OPENER.open(req, timeout=READ_TIMEOUT) as resp:
                # read1 returns as soon as any data arrives, so the deadline
                # is checked regularly even if the server sends very slowly.
                while chunk := resp.read1(1 << 20):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"download exceeded {limit} bytes")
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"download took longer than {DOWNLOAD_DEADLINE}s")
                    h.update(chunk)
                    f.write(chunk)
        if expected_size and total != expected_size:
            raise ValueError(f"size mismatch: got {total}, expected {expected_size}")
        digest = h.hexdigest()
        if want_sha and digest != want_sha:
            raise ValueError("SHA-256 mismatch")
        tmp.replace(dest)
        return digest
    except BaseException:
        tmp.unlink(missing_ok=True)  # safe: we created this file ourselves
        raise


def process_repo(repo, manifest):
    """Handle one repo. Returns (downloaded, skipped, failed) counts."""
    downloaded = skipped = failed = 0

    release = latest_nightly(repo)
    if release is None:
        log(repo, "no nightly release found, skipping")
        return 0, 0, 0
    tag = release["tag_name"]

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("release has no asset list")
    assets = [a for a in assets if isinstance(a, dict)
              and isinstance(a.get("name"), str) and a["name"].endswith(ASSET_SUFFIX)]
    if not assets:
        log(repo, f"{tag} has no '{ASSET_SUFFIX}' asset, skipping")
        return 0, 0, 0

    for asset in assets:
        name = safe_filename(asset["name"])
        if name is None:
            log(repo, f"unsafe filename {asset['name']!r}, skipping")
            failed += 1
            continue
        url = asset.get("browser_download_url")
        if not url_is_trusted(url, repo, tag, name):
            log(repo, f"untrusted download URL for {name}, skipping")
            failed += 1
            continue

        size = asset.get("size")
        size = size if isinstance(size, int) and size > 0 else 0
        if size > MAX_BYTES:
            log(repo, f"{name} is {size} bytes, over the {MAX_BYTES}-byte limit, skipping")
            failed += 1
            continue

        want_sha = expected_sha256(asset)
        if want_sha is None:
            if REQUIRE_DIGEST:
                log(repo, f"no SHA-256 published for {name}, skipping")
                failed += 1
                continue
            log(repo, f"warning: no SHA-256 published for {name}; "
                      "integrity cannot be verified against GitHub")

        dest = DEST_DIR / name
        if dest.is_file():
            # Compare against GitHub's digest if there is one, otherwise
            # against the hash we recorded when we downloaded it.
            known = want_sha or manifest.get(name, {}).get("sha256")
            try:
                have = sha256_of(dest) if known else None
            except OSError as e:
                log(repo, f"could not read existing {name} ({describe(e)}); "
                          "is it open in another program? skipping")
                failed += 1
                continue
            if known and have == known:
                log(repo, f"already have {name}")
                skipped += 1
                continue
            log(repo, f"existing {name} could not be verified, re-downloading")

        log(repo, f"downloading {name} ({tag})...")
        try:
            digest = download(url, dest, size, want_sha)
        except NET_ERRORS as e:
            log(repo, f"download failed: {describe(e)}")
            failed += 1
            continue

        downloaded += 1
        manifest[name] = {"sha256": digest, "repo": repo, "tag": tag}
        try:
            save_manifest(manifest)
        except OSError as e:
            print(f"warning: could not update manifest: {describe(e)}")

    return downloaded, skipped, failed


def remove_leftover_parts():
    """Delete .part files left by an interrupted run (only call while locked)."""
    for stale in DEST_DIR.glob("*.part"):
        try:
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
        except OSError as e:
            print(f"warning: could not remove leftover {stale.name}: {describe(e)}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download the latest nightly Chimera cores from GitHub.")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--wait", dest="wait", action="store_true", default=None,
                        help="if GitHub's rate limit is hit, wait for the reset "
                             "and continue")
    choice.add_argument("--no-wait", dest="wait", action="store_false",
                        help="if GitHub's rate limit is hit, stop without asking")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    make_output_safe()
    DEST_DIR.mkdir(exist_ok=True)
    lock = acquire_lock()
    if lock is None:
        sys.exit("Another copy of this script is already running; exiting.")

    try:
        remove_leftover_parts()
        manifest = load_manifest()
        policy = RateLimitPolicy(args.wait)

        while True:
            try:
                repos = list_repos()
                break
            except RateLimited as e:
                if not policy.wait_for_reset(e):
                    sys.exit(f"{e} Try again then, run with --wait, "
                             "or set GITHUB_TOKEN to raise the limit.")
            except NET_ERRORS as e:
                sys.exit(f"Could not list repos ({describe(e)}).")
        policy.succeeded()

        print(f"Found {len(repos)} repos starting with '{REPO_PREFIX}'.")
        downloaded = skipped = failed = not_checked = 0

        i = 0
        while i < len(repos):
            repo = repos[i]
            try:
                d, s, f = process_repo(repo, manifest)
            except RateLimited as e:
                if policy.wait_for_reset(e):
                    continue  # retry the same repo after the reset
                not_checked = len(repos) - i
                print(f"\n{e} Stopping; {not_checked} repo(s) not checked. "
                      "Run again after the reset, run with --wait, "
                      "or set GITHUB_TOKEN to raise the limit.")
                break
            except NET_ERRORS as e:
                log(repo, f"release lookup failed: {describe(e)}")
                d, s, f = 0, 0, 1
            except (KeyError, TypeError, AttributeError) as e:
                log(repo, f"unexpected data from GitHub, skipping: {describe(e)}")
                d, s, f = 0, 0, 1
            policy.succeeded()
            downloaded += d
            skipped += s
            failed += f
            i += 1

        summary = f"Downloaded: {downloaded}, already present: {skipped}, failed: {failed}"
        if not_checked:
            summary += f", not checked: {not_checked}"
        print(f"\nDone. {summary}")
        print(f"Files are in: {DEST_DIR}")
    finally:
        release_lock(lock)


if __name__ == "__main__":
    main()
