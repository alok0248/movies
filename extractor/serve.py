"""Minimal server: static files + /api/cookies endpoint."""
import os, json, re, http.cookiejar, urllib.request, http.server, socketserver
from urllib.parse import urlparse, quote
import socketserver
from socketserver import ThreadingMixIn

DIR = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/cookies":
            self._handle_cookies()
        elif p.path == "/api/health":
            self._json({"status": "ok"})
        elif p.path.startswith("/proxy/"):
            self._handle_proxy(p.path[7:])
        else:
            super().do_GET()

    def _handle_cookies(self):
        try:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")]
            resp = opener.open("https://player.videasy.to/", timeout=10)
            resp.read()
            cookies = {}
            for c in cj:
                cookies[c.name] = c.value
            for hdr in resp.headers.get_all("Set-Cookie") or []:
                m = re.match(r"([^=]+)=([^;]+)", hdr)
                if m:
                    cookies[m.group(1).strip()] = m.group(2).strip()
            print(f"[cookies] {len(cookies)} cookies fetched")
            self._json({"cookies": cookies, "count": len(cookies)})
        except Exception as e:
            print(f"[cookies] Error: {e}")
            self._json({"cookies": {}, "error": str(e)})

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_proxy(self, target_path):
        """Proxy requests to CDN/streams. Tries with videasy.to Origin first, falls back to no Origin."""
        # Reconstruct full URL — auto-detect http vs https
        if target_path.startswith("http/"):
            target_url = "http://" + target_path[5:]
        else:
            target_url = "https://" + target_path
        print(f"[proxy] -> {target_url[:120]}")
        try:
            req = urllib.request.Request(target_url)
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            req.add_header("Origin", "https://player.videasy.to")
            req.add_header("Referer", "https://player.videasy.to/")
            resp = urllib.request.urlopen(req, timeout=30)
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            data = resp.read()
            print(f"[proxy] <- OK {resp.status} {content_type} {len(data)} bytes")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            # If 403 with Origin header, retry without it
            if e.code == 403:
                print(f"[proxy] 403 with Origin, retrying without...")
                try:
                    req2 = urllib.request.Request(target_url)
                    req2.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                    resp2 = urllib.request.urlopen(req2, timeout=30)
                    content_type = resp2.headers.get("Content-Type", "application/octet-stream")
                    data = resp2.read()
                    print(f"[proxy] <- OK (retry) {resp2.status} {content_type} {len(data)} bytes")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception as e2:
                    print(f"[proxy] retry also failed: {e2}")
            print(f"[proxy] <- ERROR {e.code}")
            self.send_response(e.code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Proxy error: {e.code}".encode())
        except Exception as e:
            print(f"[proxy] <- EXCEPTION {e}")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())

    def log_message(self, fmt, *a):
        print(f"  {a[0]}" if a else "")

class ThreadedHTTPServer(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

PORT = 8765
ThreadedHTTPServer.allow_reuse_address = True
with ThreadedHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"Server: http://localhost:{PORT}/player.html")
    print(f"Cookies: http://localhost:{PORT}/api/cookies")
    print(f"Proxy:   http://localhost:{PORT}/proxy/<host>/<path>")
    httpd.serve_forever()
