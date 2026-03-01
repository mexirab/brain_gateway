"""
One-Time Google OAuth2 Setup

Run this on your dev machine (not in Docker) to generate a refresh token.
Opens a browser for Google consent, saves the token for the orchestrator.

Usage:
    pip install google-auth google-auth-oauthlib
    python google_setup.py --credentials ./credentials/google_credentials.json

After running, copy credentials/ to Jupiter and mount into the orchestrator container.
"""

import argparse
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def main():
    parser = argparse.ArgumentParser(description="Google OAuth2 setup for Brain Gateway")
    parser.add_argument(
        "--credentials",
        default="./credentials/google_credentials.json",
        help="Path to OAuth2 credentials JSON from Google Cloud Console",
    )
    parser.add_argument(
        "--token-output",
        default="./credentials/google_token.json",
        help="Where to save the refresh token",
    )
    args = parser.parse_args()

    if not os.path.exists(args.credentials):
        print(f"Error: Credentials file not found at {args.credentials}")
        print("Download it from Google Cloud Console → APIs & Services → Credentials")
        return

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(args.token_output, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {args.token_output}")
    print("Copy the credentials/ directory to Jupiter and restart the orchestrator.")


if __name__ == "__main__":
    main()
