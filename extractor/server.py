"""
Videasy Extractor — Backend Server
Uses Playwright (headless Chromium) to load the Videasy player page,
let the real JavaScript execute (including DDoS Guard challenges),
intercept decrypted source data, and return playable video URLs as JSON.

Usage:
    python server.py          # starts on http://localhost:8765
    python server.py --port 9000
    python server.py --headed # visible browser window (for debugging)
"""

import sys
import os
import json
import time
import re
import http.cookiejar
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[!] playwright not installed. Run: pip install playwright && python -m playwright install chromium")
    sys.exit(1)

HEADED = "--headed" in sys.argv


def extract_sources(videasy_url: str, timeout_ms: int = 60000) -> dict:
    """
    Opens the Videasy player URL in a headless browser.
    Handles DDoS Guard challenges, intercepts network requests,
    and extracts decrypted source data from the page.
    """
    result = {"sources": [], "subtitles": [], "title": "", "error": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not HEADED,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        # Mask automation detection
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        captured = {"m3u8_urls": [], "sources_data": None}

        def on_response(response):
            url = response.url
            if ".m3u8" in url:
                captured["m3u8_urls"].append(url)
            # Try to capture JSON responses that contain sources
            try:
                if "sources-with-title" in url or "sources" in url:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct or "text" in ct:
                        try:
                            body = response.text()
                            if body.startswith("{") and "sources" in body:
                                data = json.loads(body)
                                if data.get("sources"):
                                    captured["sources_data"] = data
                                    print(f"    [intercept] Captured sources from {url[:80]}")
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # Step 1: Navigate — follow redirects, handle DDoS Guard
            print(f"  [1/6] Navigating to {videasy_url}")

            # If the URL uses videasy.net, the browser will follow the 301 to videasy.to
            page.goto(videasy_url, wait_until="commit", timeout=timeout_ms)

            # Wait for DDoS Guard to set cookies
            print("  [2/6] Handling DDoS Guard challenge...")
            page.wait_for_timeout(5000)

            # Check if we're still on a challenge page
            current_url = page.url
            print(f"    Current URL: {current_url}")

            # If the page redirected to a different URL, navigate again
            if "videasy" not in current_url.lower():
                print("    Redirected away, navigating back...")
                page.goto(videasy_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3000)

            # Step 3: Wait for the real page to load
            print("  [3/6] Waiting for player page to fully load...")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            # Step 4: Inject source capture hooks
            print("  [4/6] Injecting capture hooks...")
            page.evaluate("""
            () => {
                window.__SRC__ = null;

                // Hook XMLHttpRequest
                const origXHROpen = XMLHttpRequest.prototype.open;
                const origXHRSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__url = url;
                    return origXHROpen.call(this, method, url, ...rest);
                };
                XMLHttpRequest.prototype.send = function(...args) {
                    this.addEventListener('load', function() {
                        try {
                            const url = this.__url || '';
                            if (url.includes('sources-with-title') || url.includes('sources')) {
                                const data = JSON.parse(this.responseText);
                                if (data.sources && data.sources.length > 0) {
                                    window.__SRC__ = data;
                                    console.log('[Extractor] Captured sources via XHR:', data.sources.length, 'sources');
                                }
                            }
                        } catch(e) {}
                    });
                    return origXHRSend.apply(this, args);
                };

                // Hook fetch
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const resp = await origFetch.apply(this, args);
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    if (url.includes('sources-with-title') || url.includes('sources')) {
                        try {
                            const clone = resp.clone();
                            const data = await clone.json();
                            if (data.sources && data.sources.length > 0) {
                                window.__SRC__ = data;
                                console.log('[Extractor] Captured sources via fetch:', data.sources.length, 'sources');
                            }
                        } catch(e) {}
                    }
                    return resp;
                };

                console.log('[Extractor] Hooks installed');
            }
            """)

            # Step 5: Try to click play and wait for sources
            print("  [5/6] Triggering player and waiting for sources...")

            # Try clicking various play buttons
            for selector in [
                'button[aria-label*="play" i]',
                'button[aria-label*="Watch" i]',
                '.plyr__control--play',
                '[data-plyr="play"]',
                'button.plyr__control',
                'video',
            ]:
                try:
                    btn = page.query_selector(selector)
                    if btn:
                        btn.click()
                        print(f"    Clicked: {selector}")
                        break
                except Exception:
                    pass

            # Wait for sources to appear — poll for up to 30 seconds
            sources_data = None
            for attempt in range(30):
                # Check XHR/fetch hook
                sources_data = page.evaluate("window.__SRC__")
                if sources_data and sources_data.get("sources"):
                    print(f"    Sources found via hook (attempt {attempt+1})")
                    break

                # Check intercepted responses
                if captured["sources_data"]:
                    sources_data = captured["sources_data"]
                    print(f"    Sources found via response interception (attempt {attempt+1})")
                    break

                # Try to find video element source
                vid_src = page.evaluate("""
                () => {
                    const v = document.querySelector('video');
                    if (v && v.src && v.src.startsWith('http')) return v.src;
                    if (v && v.currentSrc && v.currentSrc.startsWith('http')) return v.currentSrc;
                    return null;
                }
                """)
                if vid_src:
                    print(f"    Found video src: {vid_src[:80]}...")
                    sources_data = {"sources": [{"url": vid_src, "quality": "auto", "type": "stream"}], "subtitles": []}
                    break

                # Check for HLS.js instance
                hls_src = page.evaluate("""
                () => {
                    // Try to find hls.js instances
                    if (window.hls) return window.hls.url;
                    if (window.videoPlayer && window.videoPlayer.src) return window.videoPlayer.src;
                    return null;
                }
                """)
                if hls_src:
                    sources_data = {"sources": [{"url": hls_src, "quality": "auto", "type": "hls"}], "subtitles": []}
                    break

                page.wait_for_timeout(1000)

            # Step 6: Collect all m3u8 URLs from network
            print("  [6/6] Collecting results...")

            # Get title
            title = ""
            try:
                title = page.evaluate("document.title") or ""
            except Exception:
                pass

            all_m3u8 = list(set(captured["m3u8_urls"]))

            # Build final result
            if sources_data and sources_data.get("sources"):
                result["sources"] = sources_data["sources"]
                result["subtitles"] = sources_data.get("subtitles", [])
                result["title"] = sources_data.get("title", "") or title
            elif all_m3u8:
                for url in all_m3u8:
                    quality = "1080p"
                    if "720" in url: quality = "720p"
                    elif "480" in url: quality = "480p"
                    elif "360" in url: quality = "360p"
                    result["sources"].append({"url": url, "quality": quality, "type": "hls"})
                result["title"] = title
            else:
                # Take a screenshot for debugging
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshot.png")
                try:
                    page.screenshot(path=debug_path)
                    print(f"    Debug screenshot saved: {debug_path}")
                except Exception:
                    pass

                # Get page content for debugging
                try:
                    page_text = page.evaluate("document.body.innerText") or ""
                    result["error"] = f"No video sources found. Page content preview: {page_text[:300]}"
                except Exception:
                    result["error"] = "No video sources found. Check debug_screenshot.png"

            result["title"] = result["title"] or title

        except Exception as e:
            result["error"] = f"Browser error: {str(e)}"
            print(f"  [!] Error: {e}")

            # Debug screenshot on error
            try:
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_error.png")
                page.screenshot(path=debug_path)
                print(f"    Error screenshot saved: {debug_path}")
            except Exception:
                pass

        finally:
            browser.close()

    return result


# === HTTP Server ===

class ExtractHandler(SimpleHTTPRequestHandler):
    """Handles /api/extract requests and serves static files."""

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/extract":
            params = parse_qs(parsed.query)
            url = params.get("url", [None])[0]

            if not url:
                self.send_json(400, {"error": "Missing 'url' parameter"})
                return

            # Normalize URL: videasy.net redirects to videasy.to
            if "videasy.net" in url:
                url = url.replace("videasy.net", "videasy.to")
                print(f"  [normalized] {url}")

            print(f"\n[Extract] Request for: {url}")
            start = time.time()
            try:
                result = extract_sources(url)
                elapsed = time.time() - start
                status = 200 if not result.get("error") else 404
                n_sources = len(result.get("sources", []))
                print(f"[Extract] Done in {elapsed:.1f}s: {n_sources} sources, error={result.get('error', 'none')[:80] if result.get('error') else 'none'}")
                self.send_json(status, result)
            except Exception as e:
                print(f"[Extract] Exception: {e}")
                self.send_json(500, {"error": str(e)})

        elif parsed.path == "/api/cookies":
            self.send_json(200, self.get_ddos_cookies())

        elif parsed.path == "/api/health":
            self.send_json(200, {"status": "ok", "headed": HEADED})

        else:
            # Serve static files from extractor/
            file_path = parsed.path.lstrip("/")
            if file_path == "" or file_path == "/":
                file_path = "player.html"
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_path)
            if os.path.isfile(file_path):
                self.send_file(file_path)
            else:
                self.send_error(404, f"File not found: {parsed.path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    @staticmethod
    def get_ddos_cookies():
        """Fetch fresh DDoS Guard cookies from videasy.to"""
        try:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj),
                urllib.request.HTTPSHandler()
            )
            opener.addheaders = [(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            )]
            resp = opener.open("https://player.videasy.to/", timeout=10)
            resp.read()
            cookies = {}
            for c in cj:
                cookies[c.name] = c.value
            # Also check response headers for any Set-Cookie we might have missed
            if hasattr(resp, "headers"):
                for header in resp.headers.get_all("Set-Cookie") or []:
                    m = re.match(r"([^=]+)=([^;]+)", header)
                    if m:
                        cookies[m.group(1).strip()] = m.group(2).strip()
            print(f"[Cookies] Fetched {len(cookies)} DDoS Guard cookies")
            return {"cookies": cookies, "count": len(cookies)}
        except Exception as e:
            print(f"[Cookies] Error: {e}")
            return {"cookies": {}, "error": str(e)}

    def send_json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        ext_map = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript",
            ".css": "text/css",
            ".py": "text/plain",
            ".png": "image/png",
            ".jpg": "image/jpeg",
        }
        ext = os.path.splitext(path)[1]
        ct = ext_map.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # quiet


def main():
    port = 8765
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(f"{'='*50}")
    print(f"  Videasy Extractor — Browser Backend")
    print(f"{'='*50}")
    print(f"  Player:  http://localhost:{port}/player.html")
    print(f"  API:     http://localhost:{port}/api/extract?url=<videasy_url>")
    print(f"  Health:  http://localhost:{port}/api/health")
    print(f"  Mode:    {'headed' if HEADED else 'headless'}")
    print(f"{'='*50}")
    print(f"")
    print(f"  The server launches a real browser in the background to")
    print(f"  load the Videasy player, let its JavaScript execute, and")
    print(f"  extract the video source URLs for you.")
    print(f"")
    print(f"  Open the player URL above in your browser to use it.")
    print(f"  Press Ctrl+C to stop.\n")

    server = HTTPServer(("127.0.0.1", port), ExtractHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
