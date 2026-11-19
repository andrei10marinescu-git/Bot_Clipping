"""Start ClipBot: python run.py [--port 8765] [--no-browser]"""

import argparse
import threading
import webbrowser

import uvicorn

from clipbot.server import app
from clipbot.winjob import kill_children_on_exit

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    kill_children_on_exit()
    url = f"http://127.0.0.1:{args.port}"
    print(f"ClipBot rulează pe {url}  (Ctrl+C ca să oprești)")
    if not args.no_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
