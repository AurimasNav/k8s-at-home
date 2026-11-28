"""Start FlareSolverr so that it solves one Cloudflare challenge at a time.

FlareSolverr starts a headless Chrome for every /v1 request and solves up to four at once; it
has no setting to limit that (FlareSolverr/FlareSolverr#1735 adds MAX_CONCURRENT_SOLVES, not
merged yet). Two Chromes don't fit in its 1 GiB limit together: when Prowlarr retries RuTracker
and Kinozal in the same second, both thrash at the limit, both time out after 60 s, Prowlarr
disables both trackers for another 24 h, and the node stalls for minutes.

This wraps flaresolverr_service._resolve_challenge (browser start, solve, browser quit) in a
semaphore and then runs /app/flaresolverr.py unchanged. A queued request waits out of its own
maxTimeout (Prowlarr's FlareSolverr "Request Timeout"; Prowlarr gives up 5 s after it), and its
solve gets what is left, capped at SOLVE_TIMEOUT_SECONDS, so it never outlives the caller.
Waitress gets 16 threads instead of 4, so /health still finds a free thread while requests queue.

Remove this file, and the command and mount in flaresolverr.yaml, once a FlareSolverr release
has its own concurrency limit (tracked in AurimasNav/k8s-at-home#1187).
"""

import logging
import os
import runpy
import sys
import threading
import time

APP = os.environ.get("FLARESOLVERR_APP", "/app")  # the image's source dir
sys.path.insert(0, APP)

import flaresolverr_service  # noqa: E402
import waitress  # noqa: E402

MAX_SOLVES = int(os.environ.get("MAX_CONCURRENT_SOLVES", "1"))
SOLVE_TIMEOUT_MS = int(os.environ.get("SOLVE_TIMEOUT_SECONDS", "60")) * 1000
WAITRESS_THREADS = int(os.environ.get("WAITRESS_THREADS", "16"))
# kept free after a wait: browser start and quit run outside FlareSolverr's own solve timeout
RESERVE_MS = 15_000
# don't start a browser with less than this left to solve in
MIN_SOLVE_MS = 20_000

_slots = threading.BoundedSemaphore(MAX_SOLVES)
_resolve_challenge = flaresolverr_service._resolve_challenge


def _resolve_one_at_a_time(req, method):
    budget_ms = int(req.maxTimeout)  # FlareSolverr sets 60000 when the caller sent none
    start = time.monotonic()
    waited_ms = 0
    if not _slots.acquire(blocking=False):
        max_wait_ms = budget_ms - RESERVE_MS - MIN_SOLVE_MS
        logging.info("Another challenge is being solved, queued (waits up to %.0f s)", max(max_wait_ms, 0) / 1000)
        if max_wait_ms <= 0 or not _slots.acquire(timeout=max_wait_ms / 1000):
            raise Exception(f"Another challenge was still being solved after {max(max_wait_ms, 0) / 1000:.0f} s in the queue.")
        waited_ms = int((time.monotonic() - start) * 1000)
        logging.info("Waited %.1f s in the queue", waited_ms / 1000)
    try:
        left_ms = budget_ms - waited_ms - (RESERVE_MS if waited_ms else 0)
        req.maxTimeout = min(SOLVE_TIMEOUT_MS, left_ms)
        return _resolve_challenge(req, method)
    finally:
        _slots.release()


flaresolverr_service._resolve_challenge = _resolve_one_at_a_time

_serve = waitress.serve


def _serve_more_threads(*args, **kwargs):
    kwargs.setdefault("threads", WAITRESS_THREADS)
    return _serve(*args, **kwargs)


waitress.serve = _serve_more_threads

if __name__ == "__main__":
    # flaresolverr.py configures logging itself; the messages above log through it
    sys.argv = [f"{APP}/flaresolverr.py"]
    runpy.run_path(f"{APP}/flaresolverr.py", run_name="__main__")
