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
