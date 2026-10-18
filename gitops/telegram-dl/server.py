#!/usr/bin/env python3
"""Telegram Desktop portable (Windows) download relay.

Its only job: fetch the latest Telegram Desktop portable zip from telegram.org
and stream it to the browser. It is NOT an open proxy: the upstream URLs are
fixed below and the client cannot choose another destination.

  GET /telegram          small page with download buttons
  GET /telegram/x64      Windows 64-bit portable zip (most PCs)
  GET /telegram/x86      Windows 32-bit portable zip
  GET /telegram/arm      Windows ARM portable zip
  GET /telegram/healthz  liveness/readiness probe
"""
import html
import logging
import os
import posixpath
import shutil
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/telegram"
TARGETS = {
    "x64": ("Windows 64-bit", "https://telegram.org/dl/desktop/win64_portable"),
    "x86": ("Windows 32-bit", "https://telegram.org/dl/desktop/win_portable"),
    "arm": ("Windows ARM", "https://telegram.org/dl/desktop/winarm_portable"),
}
ALLOWED_FINAL_HOSTS = {"td.telegram.org", "telegram.org", "updates.tdesktop.com"}
CHUNK = 256 * 1024
UPSTREAM_TIMEOUT = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-dl")

PAGE = """<!doctype html>
<html lang="lt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Portable</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222}}
a.btn{{display:block;margin:.6rem 0;padding:.9rem 1rem;border-radius:.6rem;background:#2aabee;color:#fff;
text-decoration:none;font-weight:600;text-align:center}}
a.alt{{background:#e8f4fb;color:#1a6fa3}}
small{{color:#666}}
</style></head><body>
<h1>Telegram Portable</h1>
<p>Naujausia Telegram Desktop portable versija Windows kompiuteriams.<br>
<small>Atsisiunčiama tiesiai iš telegram.org per šį serverį. Išarchyvuokite .zip ir paleiskite Telegram.exe.</small></p>
{buttons}
</body></html>
"""


def page():
    buttons = []
    for key, (label, _) in TARGETS.items():
        cls = "btn" if key == "x64" else "btn alt"
        extra = " (daugumai kompiuterių)" if key == "x64" else ""
        buttons.append(f'<a class="{cls}" href="{PREFIX}/{key}">Atsisiųsti: {html.escape(label)}{extra}</a>')
    return PAGE.format(buttons="\n".join(buttons)).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "telegram-dl/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - base class signature
        log.info("%s %s", self.headers.get("X-Forwarded-For", self.client_address[0]), format % args)

    def _send_simple(self, code, body, ctype="text/plain; charset=utf-8", head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request(head=False)

    def handle_request(self, head):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path == f"{PREFIX}/healthz":
            return self._send_simple(200, b"ok\n", head=head)
        if path in (PREFIX, ""):
            return self._send_simple(200, page(), "text/html; charset=utf-8", head=head)
        key = path[len(PREFIX) + 1:] if path.startswith(PREFIX + "/") else None
        if key not in TARGETS:
            return self._send_simple(404, b"not found\n", head=head)
        self.relay(key, head)

    def relay(self, key, head):
        label, url = TARGETS[key]
        req = urllib.request.Request(url, headers={"User-Agent": "telegram-dl-relay/1.0"})
        try:
            upstream = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)  # noqa: S310 - fixed https URL
        except Exception as e:  # noqa: BLE001
            log.warning("upstream error for %s: %s", key, e)
            return self._send_simple(502, f"telegram.org nepasiekiamas: {e}\n".encode(), head=head)
        with upstream:
            final = urllib.parse.urlsplit(upstream.geturl())
            name = posixpath.basename(final.path)
            if final.scheme != "https" or final.hostname not in ALLOWED_FINAL_HOSTS or not name.endswith(".zip"):
                log.warning("unexpected upstream target for %s: %s", key, upstream.geturl())
                return self._send_simple(502, b"unexpected upstream file\n", head=head)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                self.close_connection = True
            self.end_headers()
            if head:
                return
            log.info("streaming %s (%s, %s bytes)", name, label, length or "?")
            try:
                shutil.copyfileobj(upstream, self.wfile, CHUNK)
            except (BrokenPipeError, ConnectionResetError):
                log.info("client closed connection during %s", name)


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - container listens on all interfaces
    srv.daemon_threads = True
    log.info("listening on :%d", port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
