#!/usr/bin/env python3
"""Fill in an install's public URL in the realm export before importing it into Keycloak.

    python3 deploy/keycloak/render.py https://tabayyun-stg.siralabs.org > tabayyun-realm.json

The export carries no secrets: the Google and GitHub client secrets and the `tabayyun-api`
client secret are set in the admin console after the import (deploy/caprover.md).
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

TEMPLATE = Path(__file__).with_name("tabayyun-realm.json")


def render(public_url: str) -> str:
    """The realm JSON with `__PUBLIC_URL__` replaced; the URL must be an origin without a path."""
    parts = urlsplit(public_url)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path not in ("", "/"):
        raise ValueError(f"expected an origin such as https://tabayyun.example.org, got {public_url!r}")
    origin = f"{parts.scheme}://{parts.netloc}"
    text = TEMPLATE.read_text().replace("__PUBLIC_URL__", origin)
    json.loads(text)  # still valid JSON
    return text


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: render.py <public url>")
    try:
        sys.stdout.write(render(sys.argv[1]))
    except ValueError as exc:
        sys.exit(str(exc))
