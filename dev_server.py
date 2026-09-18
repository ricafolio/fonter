#!/usr/bin/env python3
"""
dev_server.py

Live-preview template.html while you edit it — without re-running the
(slow) zip/font extraction every time.

Normal workflow:
    1. Run fonter.py at least once so an output folder exists with
       fonts/, manifest.json and meta.json:
           python3 fonter.py
    2. Start the dev server, pointed at that same output folder:
           python3 dev_server.py
           python3 dev_server.py --output my-preview --port 8000
    3. It opens a browser tab. Edit template.html and save — the tab
       auto-reloads a moment later. Your real fonts render exactly as
       they will in the final build, since it reuses the same fonts/
       and manifest.json.
    4. When you're done, run fonter.py again normally (e.g. after adding
       new zips) to do a full "real" build.

No fonts extracted yet / just want to preview the page chrome and CSS?
    python3 dev_server.py --demo
This fabricates a few placeholder manifest entries (no real font files,
so custom typefaces won't actually render — you'll see the fallback
system font — but every UI element, layout, and interaction is real).

Requires only the Python standard library.
"""

import argparse
import http.server
import importlib.util
import json
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPT_DIR / "template.html"
FONTER_PATH = SCRIPT_DIR / "fonter.py"

# Load fonter.py as a module so we reuse its exact build_html() — no
# duplicated template-substitution logic to keep in sync.
if not FONTER_PATH.exists():
    sys.exit(
        f"Error: fonter.py not found next to dev_server.py (expected at {FONTER_PATH})."
    )
_spec = importlib.util.spec_from_file_location("fonter_module", FONTER_PATH)
fonter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fonter)

# Injected into every dev build, right before </body>. Polls a tiny
# endpoint served by our HTTP handler and reloads the page the moment
# template.html changes on disk.
RELOAD_SNIPPET = """
<script>
(function () {
  let known = null;
  async function poll() {
    try {
      const res = await fetch('/__devreload', { cache: 'no-store' });
      const v = await res.text();
      if (known === null) {
        known = v;
      } else if (v !== known) {
        location.reload();
        return;
      }
    } catch (e) {
      /* server restarting, etc. — just keep trying */
    }
    setTimeout(poll, 600);
  }
  poll();
})();
</script>
</body>
"""

DEMO_MANIFEST = [
    {
        "id": "demo_sans_400_normal",
        "family": "demo_sans_400_normal",
        "display_name": "Demo Sans",
        "weight": 400,
        "style": "normal",
        "source_zip": "(demo)",
        "size_kb": 12.3,
        "formats": [
            {
                "file": "demo-sans-regular.woff2",
                "ext": "woff2",
                "format": "woff2",
                "size_kb": 12.3,
                "source_zip": "(demo)",
                "source_path": "(demo)",
            }
        ],
    },
    {
        "id": "demo_sans_700_normal",
        "family": "demo_sans_700_normal",
        "display_name": "Demo Sans Bold",
        "weight": 700,
        "style": "normal",
        "source_zip": "(demo)",
        "size_kb": 13.1,
        "formats": [
            {
                "file": "demo-sans-bold.woff2",
                "ext": "woff2",
                "format": "woff2",
                "size_kb": 13.1,
                "source_zip": "(demo)",
                "source_path": "(demo)",
            }
        ],
    },
    {
        "id": "demo_serif_400_italic",
        "family": "demo_serif_400_italic",
        "display_name": "Demo Serif Italic",
        "weight": 400,
        "style": "italic",
        "source_zip": "(demo)",
        "size_kb": 15.7,
        "formats": [
            {
                "file": "demo-serif-italic.otf",
                "ext": "otf",
                "format": "opentype",
                "size_kb": 15.7,
                "source_zip": "(demo)",
                "source_path": "(demo)",
            }
        ],
    },
    {
        "id": "demo_mono_500_normal",
        "family": "demo_mono_500_normal",
        "display_name": "Demo Mono Medium",
        "weight": 500,
        "style": "normal",
        "source_zip": "(demo)",
        "size_kb": 9.4,
        "formats": [
            {
                "file": "demo-mono-medium.ttf",
                "ext": "ttf",
                "format": "truetype",
                "size_kb": 9.4,
                "source_zip": "(demo)",
                "source_path": "(demo)",
            }
        ],
    },
]


class ReloadState:
    """Thread-safe-enough (GIL) holder for a version string clients poll."""

    def __init__(self):
        self.version = str(time.time())

    def bump(self):
        self.version = str(time.time())


def load_manifest_and_meta(output_dir: Path, demo: bool):
    if demo:
        return DEMO_MANIFEST, 0, "devdemo0000"

    manifest_path = output_dir / "manifest.json"
    meta_path = output_dir / "meta.json"
    if not manifest_path.exists() or not meta_path.exists():
        sys.exit(
            f"No manifest.json/meta.json found in {output_dir}/.\n"
            f"Run fonter.py at least once first, e.g.:\n"
            f"  python3 fonter.py --output {output_dir.name}\n"
            f"...or pass --demo to preview with placeholder fonts instead."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return manifest, meta["zip_count"], meta["page_id"]


def rebuild(output_dir: Path, manifest, zip_count: int, page_id: str) -> None:
    html = fonter.build_html(manifest, zip_count, page_id)
    html = html.replace("</body>", RELOAD_SNIPPET, 1)
    (output_dir / "index.html").write_text(html, encoding="utf-8")


def watch_loop(output_dir, manifest, zip_count, page_id, state, interval=0.5):
    last_mtime = None
    try:
        last_mtime = TEMPLATE_PATH.stat().st_mtime
    except FileNotFoundError:
        sys.exit(f"template.html not found at {TEMPLATE_PATH}")

    while True:
        time.sleep(interval)
        try:
            mtime = TEMPLATE_PATH.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime != last_mtime:
            last_mtime = mtime
            # Small debounce: editors sometimes write in bursts.
            time.sleep(0.1)
            try:
                rebuild(output_dir, manifest, zip_count, page_id)
                state.bump()
                print("Rebuilt (template.html changed)")
            except Exception as e:
                print(f"Rebuild failed: {e}")


def make_handler(output_dir: Path, state: ReloadState):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(output_dir), **kwargs)

        def do_GET(self):
            if self.path == "/__devreload":
                body = state.version.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, fmt, *args):
            pass  # keep the console quiet; we print our own rebuild messages

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Live dev preview for template.html")
    parser.add_argument(
        "--output",
        default="fonter-preview",
        help="Output folder to reuse (must already have manifest.json/meta.json "
        "from a real fonter.py run, unless --demo is passed). Default: fonter-preview",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Local port (default: 8000)"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use fabricated placeholder fonts instead of a real extraction output",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't auto-open a browser tab"
    )
    args = parser.parse_args()

    if not TEMPLATE_PATH.exists():
        sys.exit(f"template.html not found at {TEMPLATE_PATH}")

    output_dir = Path.cwd() / args.output
    if args.demo:
        output_dir.mkdir(exist_ok=True)
        (output_dir / "fonts").mkdir(exist_ok=True)

    manifest, zip_count, page_id = load_manifest_and_meta(output_dir, args.demo)

    rebuild(output_dir, manifest, zip_count, page_id)

    state = ReloadState()
    watcher = threading.Thread(
        target=watch_loop,
        args=(output_dir, manifest, zip_count, page_id, state),
        daemon=True,
    )
    watcher.start()

    handler_cls = make_handler(output_dir, state)
    with socketserver.ThreadingTCPServer(
        ("127.0.0.1", args.port), handler_cls
    ) as httpd:
        url = f"http://127.0.0.1:{args.port}/index.html"
        print(f"Dev preview running at {url}")
        if args.demo:
            print("(using --demo placeholder fonts — no real font files will render)")
        print("Editing template.html will auto-reload the page. Ctrl+C to stop.")
        if not args.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
