"""Open the service's persistent Patchright profile for manual sign-in."""

from __future__ import annotations

import os
import time
from pathlib import Path

from patchright.sync_api import Error, sync_playwright


def main() -> None:
    profile_dir = Path(os.environ.get("BROWSER_PROFILE_DIR", "data/browser-profile"))
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir.resolve()),
                channel="chrome",
                headless=False,
            )
        except Error as exc:
            raise SystemExit(
                "Could not open the browser profile. Stop the app or any Chrome "
                "process using this profile, then try again."
            ) from exc

        print(f"Opened the internet-use browser profile at {profile_dir.resolve()}.")
        print("Sign in to the sites you need, then close all browser windows or press Ctrl+C.")
        try:
            while True:
                try:
                    if not any(not page.is_closed() for page in context.pages):
                        break
                except Error:
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                context.close()
            except Error:
                pass


if __name__ == "__main__":
    main()
