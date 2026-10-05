#!/usr/bin/env python3
"""Tiny personal web proxy.

Run:   python3 proxy.py            (then open http://127.0.0.1:8080)
       python3 proxy.py --port 9000 --no-browser

It only listens on 127.0.0.1, so nothing else on your network can use it.
Pure standard library - no pip installs needed.
"""

import argparse
import gzip
import html
import http.cookiejar
import json
import os
import re
import shutil
import sys
import threading
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
}

SKIP_SCHEMES = ("#", "javascript:", "data:", "mailto:", "tel:", "blob:", "about:")

ATTR_RE = re.compile(
    r"""(\s(?:href|src|action|poster|data-src|formaction)\s*=\s*)(["'])(.*?)\2""",
    re.IGNORECASE | re.DOTALL,
)
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


opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cookie_jar), _NoRedirect()
)


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
        value = html.unescape(m.group(3))
        return m.group(1) + m.group(2) + html.escape(to_proxy(value, base), quote=True) + m.group(2)

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
    text = ATTR_RE.sub(attr, text)
    text = SRCSET_RE.sub(srcset, text)
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
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def route(self):
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
        self.send_error(404, "Not found - start from http://127.0.0.1:%d/" % self.server.server_port)

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
        self.wfile.write(body)

    # ---- the actual proxy ---------------------------------------------
    def proxy(self, target):
        if not target.lower().startswith(("http://", "https://")):
            target = "https://" + target
        # Some browsers collapse "https://" to "https:/" in paths - repair it.
        target = re.sub(r"^(https?):/(?!/)", r"\1://", target, flags=re.IGNORECASE)

        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        headers["Accept-Encoding"] = "gzip, deflate"
        upstream_ref = self.referer_target()
        if upstream_ref:
            headers["Referer"] = upstream_ref

        body = None
        if self.command == "POST":
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
            rewrite = "text/html" in ctype or "text/css" in ctype

            self.send_response(status)
            for k, v in resp.headers.items():
                if k.lower() not in DROP_RESPONSE_HEADERS:
                    self.send_header(k, v)

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
                if self.command != "HEAD":
                    self.wfile.write(out)
                return

            encoding = resp.headers.get("Content-Encoding")
            if encoding and encoding.lower() in ("gzip", "deflate"):
                out = decode_body(resp.read(), encoding)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)
                return

            # Everything else (images, JS, video...) is streamed straight through.
            length = resp.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    shutil.copyfileobj(resp, self.wfile, 64 * 1024)
                except (BrokenPipeError, ConnectionResetError):
                    pass


def main():
    parser = argparse.ArgumentParser(description="Tiny local web proxy")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on (Chromebook fallback: 0.0.0.0, then open penguin.linux.test)",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if args.host in ("127.0.0.1", "localhost"):
        url = "http://127.0.0.1:%d/" % args.port
        print("Proxy running at %s  (only reachable from this computer)" % url)
    else:
        url = "http://localhost:%d/" % args.port
        print("Proxy listening on %s:%d" % (args.host, args.port))
        print("On a Chromebook open http://penguin.linux.test:%d/" % args.port)
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
