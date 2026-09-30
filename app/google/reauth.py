"""Re-run the OAuth consent flow.

    python -m app.google.reauth --minted-under testing
    python -m app.google.reauth --minted-under production

Say which publishing status the OAuth app is in *right now* (Cloud console,
OAuth consent screen). It is stored with the token, because whether the
refresh token lapses after seven days depends on the status at the moment of
consent. See `app/google/tokens.py` for why that deadline is tracked by hand.

The flag is required rather than defaulted: a guess recorded as fact is the
failure M15 exists to rule out.
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.google.auth import run_consent_flow, token_store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re-run the Google OAuth consent flow.")
    parser.add_argument(
        "--minted-under",
        choices=("testing", "production"),
        required=True,
        help="The OAuth app's publishing status right now, as the Cloud console shows it.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()

    print("Opening browser for Google consent...")
    print('An "unverified app" warning is expected -- continue via the advanced link.')
    run_consent_flow(settings, minted_under=args.minted_under)

    health = token_store(settings).health()
    if args.minted_under == "production":
        print(
            "Token stored, minted under production. The seven-day limit is not expected "
            "to apply; M15 confirms that with a successful refresh after day seven."
        )
    else:
        print(
            f"Token stored. Refresh token valid until {health.expires_at:%Y-%m-%d %H:%M %Z} "
            f"({health.days_remaining:.1f} days)."
        )


if __name__ == "__main__":
    main()
