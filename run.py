"""Start the Query Runner and open it in a browser.

    poetry install
    poetry run python run.py

Binds to 127.0.0.1 only. This tool holds database credentials and writes to a
shared database, so it should not be reachable from the network by default.
"""

from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

HOST = os.getenv("QUERY_RUNNER_HOST", "127.0.0.1")
PORT = int(os.getenv("QUERY_RUNNER_PORT", "8600"))


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    print(f"\n  DeepSentinel Query Runner  →  {url}\n")
    if os.getenv("QUERY_RUNNER_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("queryrunner.app:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
