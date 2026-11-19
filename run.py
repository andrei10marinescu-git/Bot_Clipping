"""Start ClipBot: python run.py [--port 8765] [--no-browser]"""

import argparse
import sys
import threading
import urllib.request
import webbrowser


def already_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/share", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - nothing (or something else) listens on the port
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if already_running(url):
        print(f"ClipBot rulează deja pe {url} – deschid doar interfața.")
        if not args.no_browser:
            webbrowser.open(url)
        sys.exit(0)

    import uvicorn

    from clipbot.server import app
    from clipbot.winjob import kill_children_on_exit

    kill_children_on_exit()
    print(f"ClipBot rulează pe {url}  (închide fereastra sau Ctrl+C ca să-l oprești)")
    if not args.no_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
