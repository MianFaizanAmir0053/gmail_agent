"""Re-run the OAuth consent flow.

    python -m app.google.reauth

Expect to run this weekly. While the OAuth app is in "Testing" publishing
status, Google invalidates the refresh token after seven days -- see
`app/google/tokens.py` for why that deadline is tracked by hand.
"""

from __future__ import annotations

from app.config import get_settings
from app.google.auth import run_consent_flow, token_store


def main() -> None:
    settings = get_settings()

    print("Opening browser for Google consent...")
    print('An "unverified app" warning is expected -- continue via the advanced link.')
    run_consent_flow(settings)

    health = token_store(settings).health()
    print(
        f"Token stored. Refresh token valid until {health.expires_at:%Y-%m-%d %H:%M %Z} "
        f"({health.days_remaining:.1f} days)."
    )


if __name__ == "__main__":
    main()
