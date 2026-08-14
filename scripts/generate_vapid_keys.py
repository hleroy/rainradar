# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "py-vapid>=1.7.0",
#   "cryptography>=2.6.1",
# ]
# ///
"""Generate a VAPID keypair for Rain Radar's background storm alerts (Web Push).

Run it locally with uv — no project setup or virtualenv needed; the inline metadata
above makes uv fetch the two dependencies into an isolated environment:

    uv run scripts/generate_vapid_keys.py
    uv run scripts/generate_vapid_keys.py --subject mailto:you@example.com

It prints the four lines to add to ``.envs/.production/.django`` (or
``.envs/.local/.django`` to test locally). Keep ``VAPID_PRIVATE_KEY`` secret and out of
git — it is the signing key; the public key is safe to expose. The keys are
self-generated (no Google/Apple/Mozilla account); rotating them invalidates every
existing subscription. See the README's storm-alerts section for the full procedure.

Output formats match what the app expects: ``VAPID_PUBLIC_KEY`` is the base64url
uncompressed public point the browser uses as its ``applicationServerKey``, and
``VAPID_PRIVATE_KEY`` is the base64url raw private scalar ``pywebpush`` signs with.
"""

from __future__ import annotations

import argparse
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


def _b64url(raw: bytes) -> str:
    """URL-safe base64 without padding — the encoding Web Push/VAPID use everywhere."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a VAPID keypair for Rain Radar Web Push alerts.",
    )
    parser.add_argument(
        "--subject",
        default="mailto:you@example.com",
        help="VAPID subject: a mailto: or https: URL identifying the sender (contact).",
    )
    args = parser.parse_args()

    vapid = Vapid()
    vapid.generate_keys()

    private_key = _b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public_key = _b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        ),
    )

    print("# --- Add to .envs/.production/.django (keep VAPID_PRIVATE_KEY secret) ---")
    print("PUSH_ALERTS_ENABLED=true")
    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY={private_key}")
    print(f"VAPID_SUBJECT={args.subject}")


if __name__ == "__main__":
    main()
