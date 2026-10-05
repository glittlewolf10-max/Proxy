#!/usr/bin/env python3
"""Tiny personal web proxy.

Run:   python3 proxy.py            (then open http://127.0.0.1:8080)
       python3 proxy.py --port 9000 --no-browser
       python3 proxy.py --share    (run on a phone, use from another device)
       python3 proxy.py --cloud    (run on a cloud host; needs $PROXY_KEY)

By default it only listens on 127.0.0.1, so nothing else on your network can
use it. With --share it listens on your Wi-Fi and asks for a secret key.
With --cloud it listens on $PORT, asks for $PROXY_KEY, and won't open
addresses on the host's private network.
Pure standard library - no pip installs needed.
"""

import argparse
import ipaddress
import gzip
import hmac
import html
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/p/"

# Headers we never pass back to the browser (they would break framing,
# rewriting, or leak the upstream site's security policy onto localhost).
DROP_RESPONSE_HEADERS = {
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
    "strict-transport-security",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "referrer-policy",
    "permissions-policy",
    "location",
    "alt-svc",
}

FORWARD_REQUEST_HEADERS = {
    "user-agent",
    "accept",
    "accept-language",
    "content-type",
    "cache-control",
    "pragma",
    # Needed for video/audio seeking and for the browser's own cache.
    "range",
    "if-range",
    "if-none-match",
    "if-modified-since",
}

SKIP_SCHEMES = ("#", "javascript:", "data:", "mailto:", "tel:", "blob:", "about:")

# Matches quoted values (href="x", href='x') and unquoted ones (href=x).
ATTR_RE = re.compile(
    r"""(\s(?:href|src|action|poster|data-src|formaction)\s*=\s*)"""
    r"""(?:(["'])(.*?)\2|([^\s"'<>`=]+))""",
    re.IGNORECASE | re.DOTALL,
)
# One HTML start tag, allowing ">" inside quoted attribute values.
TAG_RE = re.compile(r"""<[a-zA-Z][^\s>/]*(?:[^>"']|"[^"]*"|'[^']*')*>""")
SRCSET_RE = re.compile(r"""(\ssrcset\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
CSS_URL_RE = re.compile(r"""url\(\s*(["']?)([^"')]*?)\1\s*\)""", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"""(@import\s+)(["'])(.*?)\2""", re.IGNORECASE)
BASE_RE = re.compile(r"""<base\s[^>]*href\s*=\s*(["'])(.*?)\1[^>]*>""", re.IGNORECASE)
INTEGRITY_RE = re.compile(r"""\s(?:integrity|nonce)\s*=\s*(["']).*?\1""", re.IGNORECASE)
META_BLOCK_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?(?:content-security-policy|x-frame-options)[^>]*>"""
    r"""|<meta[^>]+name\s*=\s*["']?referrer[^>]*>""",
    re.IGNORECASE,
)
HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)

cookie_jar = http.cookiejar.CookieJar()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Let redirects through to us so we can rewrite them for the browser."""

    def redirect_request(self, *args, **kwargs):
        return None


def is_public_address(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


class _GuardedConnection:
    """In --cloud mode, refuse to talk to private addresses.

    Otherwise a page loaded through the proxy could make it fetch things only
    the server can reach (the host's internal network, cloud metadata). The
    check runs on the address actually connected to, so DNS tricks can't
    get around it.
    """

    block_private = False

    def connect(self):
        super().connect()
        if self.block_private and not is_public_address(self.sock.getpeername()[0]):
            self.sock.close()
            raise OSError("%s is a private network address; the proxy won't open it" % self.host)


class _GuardedHTTP(_GuardedConnection, http.client.HTTPConnection):
    pass


class _GuardedHTTPS(_GuardedConnection, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPS, req, context=self._context)


def make_opener(direct=False):
    handlers = [urllib.request.HTTPCookieProcessor(cookie_jar), _NoRedirect(), _HTTPHandler(), _HTTPSHandler()]
    if direct:
        # Ignore HTTP(S)_PROXY settings, so the private-address check sees
        # the real site rather than an outbound proxy.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


opener = make_opener()


class KeyAttempts:
    """Slows down anyone guessing the key: 10 wrong tries a minute, then wait."""

    LIMIT, WINDOW = 10, 60

    def __init__(self):
        self.lock = threading.Lock()
        self.failures = []

    def blocked(self):
        with self.lock:
            now = time.monotonic()
            self.failures = [t for t in self.failures if now - t < self.WINDOW]
            return len(self.failures) >= self.LIMIT

    def failed(self):
        with self.lock:
            self.failures.append(time.monotonic())


key_attempts = KeyAttempts()


def to_proxy(url, base):
    """Turn any link found in a page into a /p/<absolute-url> link."""
    url = url.strip()
    if not url or url.lower().startswith(SKIP_SCHEMES) or url.startswith(PREFIX):
        return url
    absolute = urllib.parse.urljoin(base, url)
    if not absolute.lower().startswith(("http://", "https://")):
        return url
    return PREFIX + absolute


def rewrite_css(text, base):
    text = CSS_URL_RE.sub(
        lambda m: "url(%s%s%s)" % (m.group(1), to_proxy(m.group(2), base), m.group(1)),
        text,
    )
    return CSS_IMPORT_RE.sub(
        lambda m: m.group(1) + m.group(2) + to_proxy(m.group(3), base) + m.group(2), text
    )


def js(value):
    return json.dumps(value).replace("</", "<\\/")


def inject_script(base, target):
    # Runs inside the proxied page: keeps JS-made requests and clicks inside the proxy.
    return """<script>(function(){
var P=%s,B=%s,T=%s,O=location.origin;
function rw(u){try{if(u==null)return u;var s=String(u);
 if(s.indexOf(P)===0||s.indexOf(O+P)===0)return s;
 var a=new URL(s,B);if(a.protocol!=='http:'&&a.protocol!=='https:')return s;
 if(a.origin===O)return s;return P+a.href;}catch(e){return u;}}
try{history.replaceState(history.state,'',P+T+location.hash);}catch(e){}
var f=window.fetch;if(f)window.fetch=function(i,o){if(!(i instanceof Request))i=rw(i);return f.call(this,i,o);};
var x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=rw(u);return x.apply(this,arguments);};
['pushState','replaceState'].forEach(function(k){var h=history[k];history[k]=function(s,t,u){if(u!=null)u=rw(u);return h.call(this,s,t,u);};});
var w=window.open;window.open=function(u){arguments[0]=rw(u);return w.apply(this,arguments);};
if(navigator.serviceWorker){try{navigator.serviceWorker.register=function(){return Promise.reject(new Error('disabled by proxy'));};}catch(e){}}
document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[href]');
 if(!a)return;var r=rw(a.getAttribute('href'));if(r!==a.getAttribute('href'))a.setAttribute('href',r);
 if(a.target==='_blank'||a.target==='_top'||a.target==='_parent')a.target='_self';},true);
document.addEventListener('submit',function(e){var t=e.target,ac=t.getAttribute('action');
 if(ac)t.setAttribute('action',rw(ac));},true);
})();</script>""" % (js(PREFIX), js(base), js(target))


def rewrite_html(text, target):
    base = target
    m = BASE_RE.search(text)
    if m:
        base = urllib.parse.urljoin(target, html.unescape(m.group(2)))
        text = BASE_RE.sub("", text)

    def attr(m):
        quote = m.group(2) or '"'  # unquoted values come back quoted
        value = html.unescape(m.group(3) if m.group(2) else m.group(4))
        return m.group(1) + quote + html.escape(to_proxy(value, base), quote=True) + quote

    def srcset(m):
        parts = []
        for item in html.unescape(m.group(3)).split(","):
            bits = item.strip().split(None, 1)
            if bits:
                bits[0] = to_proxy(bits[0], base)
                parts.append(" ".join(bits))
        return m.group(1) + m.group(2) + html.escape(", ".join(parts), quote=True) + m.group(2)

    text = META_BLOCK_RE.sub("", text)
    text = INTEGRITY_RE.sub("", text)
    # Only touch attributes inside tags, so JavaScript like "var src=x" is left alone.
    text = TAG_RE.sub(lambda t: SRCSET_RE.sub(srcset, ATTR_RE.sub(attr, t.group(0))), text)
    text = rewrite_css(text, base)

    script = inject_script(base, target)
    if HEAD_RE.search(text):
        return HEAD_RE.sub(lambda h: h.group(0) + script, text, count=1)
    return script + text


def decode_body(data, encoding):
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        return gzip.decompress(data)
    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    return data


def charset_of(content_type):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    return m.group(1) if m else "utf-8"


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalProxy/1.0"

    def log_message(self, fmt, *args):
        line = re.sub(r"key=[^&\s\"]*", "key=***", fmt % args)  # keep the key out of logs
        sys.stderr.write("  %s\n" % line)

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_PATCH(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_OPTIONS(self):
        self.route()

    def route(self):
        if self.server.allowed_hosts is not None:
            # Without a key, only answer to our own address. This stops other
            # websites using DNS tricks to reach the proxy from your browser.
            if self.headers.get("Host", "").lower() not in self.server.allowed_hosts:
                return self.send_error(403, "Unexpected Host header")
        if self.server.key and not self.authorized():
            return
        if self.path in ("/", "/index.html"):
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if self.path.startswith(PREFIX):
            return self.proxy(self.path[len(PREFIX):])
        # A page asked for "/something" (root-relative URL we didn't rewrite,
        # usually built by JavaScript). Work out which site it belongs to from
        # the Referer and send it through the proxy.
        site = self.referer_target()
        if site:
            fixed = urllib.parse.urljoin(site, self.path)
            return self.redirect(PREFIX + fixed, code=307)
        self.send_error(404, "Not found - start from the proxy's home page")

    def authorized(self):
        """In --share mode every request needs the key (kept in a cookie).

        Returns True to carry on; otherwise a login page or redirect was sent.
        """
        key = self.server.key.encode()
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "proxykey" and hmac.compare_digest(value.encode(), key):
                return True
        parts = urllib.parse.urlsplit(self.path)
        query = parts.query
        if self.command == "POST" and parts.path == "/":
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            query = self.rfile.read(length).decode("utf-8", "replace")
        given = urllib.parse.parse_qs(query).get("key", [""])[0].strip()
        if given and parts.path == "/":
            if key_attempts.blocked():
                return self.send_error(429, "Too many wrong keys - wait a minute and try again")
            if hmac.compare_digest(given.encode(), key):
                secure = self.headers.get("X-Forwarded-Proto", "") == "https" and self.server.cloud
                self.send_response(303)
                self.send_header(
                    "Set-Cookie",
                    "proxykey=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000%s"
                    % (self.server.key, "; Secure" if secure else ""),
                )
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            key_attempts.failed()
        body = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Local Proxy</title>"
            "<body style='font:16px system-ui;padding:2rem;max-width:30rem;margin:auto'>"
            "<h2>Enter the proxy key</h2>"
            "<p>%s</p>%s"
            "<form method=post action='/'><input name=key type=password autofocus autocomplete=off "
            "style='font:inherit;padding:8px;width:100%%;box-sizing:border-box'>"
            "<p><button style='font:inherit;padding:8px 16px'>Unlock</button></p></form>"
            % (
                "It's the PROXY_KEY you set on your cloud host." if self.server.cloud
                else "It's shown in the terminal where the proxy is running.",
                "<p style='color:#b91c1c'>That key isn't right.</p>" if given else "",
            )
        ).encode()
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def referer_target(self):
        ref = self.headers.get("Referer", "")
        path = urllib.parse.urlsplit(ref).path
        if ref and path.startswith(PREFIX):
            # Keep the full upstream URL (including its own query string).
            return ref[ref.index(PREFIX) + len(PREFIX):]
        return None

    # ---- helpers -------------------------------------------------------
    def serve_file(self, name, ctype):
        with open(os.path.join(HERE, name), "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location, code=302):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def error_page(self, code, message, target):
        body = (
            "<!doctype html><meta charset=utf-8><title>Proxy error</title>"
            "<body style='font:16px system-ui;padding:2rem;max-width:40rem'>"
            "<h2>Couldn't load that page</h2><p><code>%s</code></p><p>%s</p>"
            % (html.escape(target), html.escape(message))
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- the actual proxy ---------------------------------------------
    def proxy(self, target):
        if not target.lower().startswith(("http://", "https://")):
            target = "https://" + target
        # Some browsers collapse "https://" to "https:/" in paths - repair it.
        target = re.sub(r"^(https?):/(?!/)", r"\1://", target, flags=re.IGNORECASE)

        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        # A compressed partial response can't be decoded, so ask for plain bytes
        # when the browser wants a byte range (video seeking).
        headers["Accept-Encoding"] = "identity" if "range" in (k.lower() for k in headers) else "gzip, deflate"
        upstream_ref = self.referer_target()
        if upstream_ref:
            headers["Referer"] = upstream_ref

        body = None
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

        req = urllib.request.Request(target, data=body, headers=headers, method=self.command)
        try:
            resp = opener.open(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e  # 3xx/4xx/5xx still carry a usable response
        except Exception as e:  # DNS failure, TLS error, timeout...
            return self.error_page(502, str(getattr(e, "reason", e)), target)

        with resp:
            status = resp.status if hasattr(resp, "status") else resp.code
            location = resp.headers.get("Location")
            if 300 <= status < 400 and location:
                return self.redirect(PREFIX + urllib.parse.urljoin(target, location), status)

            ctype = resp.headers.get("Content-Type", "")
            # A 206 is only part of the file, so it can't be rewritten safely.
            rewrite = status != 206 and ("text/html" in ctype or "text/css" in ctype)

            self.send_response(status)
            for k, v in resp.headers.items():
                if k.lower() not in DROP_RESPONSE_HEADERS:
                    self.send_header(k, v)

            if self.command == "HEAD" or status in (204, 304) or status < 200:
                # These responses never carry a body.
                self.end_headers()
                return

            if rewrite:
                raw = decode_body(resp.read(), resp.headers.get("Content-Encoding"))
                charset = charset_of(ctype)
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    charset = "utf-8"
                    text = raw.decode(charset, errors="replace")
                text = rewrite_html(text, target) if "text/html" in ctype else rewrite_css(text, target)
                out = text.encode(charset, errors="replace")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            encoding = resp.headers.get("Content-Encoding")
            if encoding and encoding.lower() in ("gzip", "deflate"):
                out = decode_body(resp.read(), encoding)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            # Everything else (images, JS, video...) is streamed straight through.
            length = resp.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            self.end_headers()
            try:
                shutil.copyfileobj(resp, self.wfile, 64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass


def lan_addresses():
    """Best guesses at this device's Wi-Fi / hotspot address."""
    found = []
    for cmd in (["ip", "-4", "-o", "addr"], ["ifconfig"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
            found += re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out)
        except Exception:
            pass
    try:
        # No packet is sent; this just asks which interface would be used.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except Exception:
        pass
    result = []
    for addr in found:
        ip = ipaddress.ip_address(addr)
        if ip.is_private and not ip.is_loopback and addr not in result:
            result.append(addr)
    return result


def main():
    parser = argparse.ArgumentParser(description="Tiny local web proxy")
    parser.add_argument("--port", type=int, default=None, help="default 8080 (or $PORT with --cloud)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument(
        "--share",
        action="store_true",
        help="listen on Wi-Fi so another device (e.g. a Chromebook) can use it; requires a key",
    )
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="run on a cloud host: listen on $PORT, key from $PROXY_KEY, block private addresses",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="address to listen on (default 127.0.0.1, or 0.0.0.0 with --share/--cloud)",
    )
    parser.add_argument("--key", help="use this key instead of a random one (--share mode)")
    args = parser.parse_args()

    port = args.port or int((os.environ.get("PORT") if args.cloud else None) or 8080)
    host = args.host or ("0.0.0.0" if args.share or args.cloud else "127.0.0.1")
    local_only = host in ("127.0.0.1", "localhost") and not args.cloud
    key = args.key or (os.environ.get("PROXY_KEY", "").strip() if args.cloud else None)

    if key and not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        parser.error("the key may only use letters, digits, - and _")
    if args.cloud and (not key or len(key) < 16):
        example = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(20))
        parser.error(
            "--cloud needs a key of at least 16 characters, because the proxy is on the "
            "public internet. Set the PROXY_KEY environment variable on your host, "
            "for example: PROXY_KEY=%s" % example
        )

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.cloud = args.cloud
    if args.cloud:
        global opener
        opener = make_opener(direct=True)
        _GuardedConnection.block_private = True
    readable = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes such as l/1, o/0
    server.key = None if local_only else (key or "".join(secrets.choice(readable) for _ in range(8)))
    # With no key, only answer requests addressed to this machine (see route()).
    server.allowed_hosts = (
        {h + p for h in ("127.0.0.1", "localhost") for p in ("", ":%d" % port)} if local_only else None
    )

    if args.cloud:
        url = None
        print("Proxy running in cloud mode on port %d." % port)
        print("Open your host's web address and enter your PROXY_KEY.")
    elif local_only:
        url = "http://127.0.0.1:%d/" % port
        print("Proxy running at %s  (only reachable from this computer)" % url)
    else:
        url = "http://localhost:%d/?key=%s" % (port, server.key)
        print("Proxy is shared on your network. Key: %s" % server.key)
        print("On the other device, open one of these in Chrome:")
        for addr in lan_addresses():
            print("    http://%s:%d/?key=%s" % (addr, port, server.key))
        print("    http://penguin.linux.test:%d/?key=%s   (Chromebook Linux only)" % (port, server.key))
        print("Anyone without the key just gets a password page.")
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()
    if url and not args.no_browser and not args.share:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
