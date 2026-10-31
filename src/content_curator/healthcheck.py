"""Container healthcheck: exit 0 when the service answers /healthz. No curl in the image."""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    host = os.environ.get("CURATOR_API_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = os.environ.get("CURATOR_API_PORT", "8090")
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=4) as resp:
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
