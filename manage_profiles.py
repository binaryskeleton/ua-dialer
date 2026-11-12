"""Manage the private local list used to update the DIALER_PROFILES_JSON secret."""

from __future__ import annotations

import json
import sys
from base64 import b64decode, b64encode
from getpass import getpass
from pathlib import Path
from typing import Callable

import requests
from nacl.public import PublicKey, SealedBox

from dialer import RecipientProfile, parse_profiles

PROFILES_FILE = Path(__file__).with_name("dialer-profiles.json")
GITHUB_REPOSITORY = "binaryskeleton/ua-dialer"
GITHUB_SECRET_NAME = "DIALER_PROFILES_JSON"


def load_profiles(path: Path = PROFILES_FILE) -> tuple[RecipientProfile, ...]:
    if not path.exists():
        return ()
    return parse_profiles(path.read_text(encoding="utf-8"))


def save_profiles(path: Path, profiles: tuple[RecipientProfile, ...]) -> None:
    serialized = [
        {
            "name": profile.name,
            "discord_user_id": profile.discord_user_id,
            "colors": list(profile.colors),
        }
        for profile in profiles
    ]
    path.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def profiles_json(profiles: tuple[RecipientProfile, ...]) -> str:
    serialized = [
        {
            "name": profile.name,
            "discord_user_id": profile.discord_user_id,
            "colors": list(profile.colors),
        }
        for profile in profiles
    ]
    return json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))


def upload_profiles_to_github(
    profiles: tuple[RecipientProfile, ...],
    token: str,
    *,
    repository: str = GITHUB_REPOSITORY,
) -> None:
    if not token.strip():
        raise ValueError("A GitHub token is required")
    try:
        owner, repo = repository.split("/", maxsplit=1)
    except ValueError as exc:
        raise ValueError("Repository must have the format owner/name") from exc
    if not owner or not repo or "/" in repo:
        raise ValueError("Repository must have the format owner/name")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"https://api.github.com/repos/{owner}/{repo}/actions/secrets"
    try:
        key_response = requests.get(f"{base_url}/public-key", headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError("Could not reach GitHub to retrieve the repository encryption key") from exc
    if key_response.status_code != 200:
        raise RuntimeError(
            f"GitHub could not provide the repository encryption key "
            f"(HTTP {key_response.status_code}): {key_response.text[:1000]}"
        )
    try:
        public_key_data = key_response.json()
        public_key = public_key_data["key"]
        key_id = public_key_data["key_id"]
        encrypted_value = SealedBox(
            PublicKey(b64decode(public_key, validate=True))
        ).encrypt(profiles_json(profiles).encode("utf-8"))
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("GitHub returned an invalid repository encryption key") from exc

    try:
        secret_response = requests.put(
            f"{base_url}/{GITHUB_SECRET_NAME}",
            headers=headers,
            json={
                "encrypted_value": b64encode(encrypted_value).decode("ascii"),
                "key_id": key_id,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Could not reach GitHub to update the recipient profiles") from exc
    if secret_response.status_code not in (201, 204):
        raise RuntimeError(
            f"GitHub could not update {GITHUB_SECRET_NAME} "
            f"(HTTP {secret_response.status_code}): {secret_response.text[:1000]}"
        )


def _show_profiles(profiles: tuple[RecipientProfile, ...], output_fn: Callable[[str], None]) -> None:
    if not profiles:
        output_fn("No users are saved yet.")
        return
    for index, profile in enumerate(profiles, start=1):
        output_fn(
            f"{index}. {profile.name} - Discord ID: {profile.discord_user_id} "
            f"— colors: {', '.join(profile.colors)}"
        )


def run_manager(
    path: Path = PROFILES_FILE,
    *,
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print,
    token_prompt: Callable[[str], str] | None = None,
) -> int:
    read_input = input if input_fn is None else input_fn
    read_token = getpass if token_prompt is None else token_prompt
    try:
        profiles = load_profiles(path)
    except (OSError, ValueError) as exc:
        print(f"Could not load {path}: {exc}", file=sys.stderr)
        return 1

    while True:
        output_fn("\nUser profile manager")
        output_fn("1. List users")
        output_fn("2. Add user")
        output_fn("3. Remove user")
        output_fn("4. Generate JSON for GitHub")
        output_fn("5. Upload profiles to GitHub")
        output_fn("6. Exit")
        choice = read_input("Choose an option: ").strip()

        if choice == "1":
            _show_profiles(profiles, output_fn)
        elif choice == "2":
            name = read_input("Name: ").strip()
            discord_user_id = read_input("Discord user ID: ").strip()
            colors = [
                color.strip()
                for color in read_input("Colors (comma-separated): ").split(",")
                if color.strip()
            ]
            try:
                updated = parse_profiles(
                    json.dumps(
                        [
                            {
                                "name": profile.name,
                                "discord_user_id": profile.discord_user_id,
                                "colors": list(profile.colors),
                            }
                            for profile in (*profiles, RecipientProfile(name, discord_user_id, tuple(colors)))
                        ]
                    )
                )
                save_profiles(path, updated)
            except (OSError, ValueError) as exc:
                output_fn(f"Could not add user: {exc}")
            else:
                profiles = updated
                output_fn(f"Added {name}.")
        elif choice == "3":
            _show_profiles(profiles, output_fn)
            if not profiles:
                continue
            selection = read_input("Enter the number to remove, or press Enter to cancel: ").strip()
            if not selection:
                continue
            try:
                index = int(selection)
                if not 1 <= index <= len(profiles):
                    raise ValueError("Choose a number from the list.")
                updated = profiles[:index - 1] + profiles[index:]
                save_profiles(path, updated)
            except (OSError, ValueError) as exc:
                output_fn(f"Could not remove user: {exc}")
            else:
                removed = profiles[index - 1]
                profiles = updated
                output_fn(f"Removed {removed.name}.")
        elif choice == "4":
            if not profiles:
                output_fn("Add at least one user before generating the JSON.")
                continue
            output_fn("Copy this JSON into the GitHub DIALER_PROFILES_JSON secret:")
            output_fn(profiles_json(profiles))
        elif choice == "5":
            if not profiles:
                output_fn("Add at least one user before uploading profiles.")
                continue
            output_fn(f"This will replace the {GITHUB_SECRET_NAME} secret in {GITHUB_REPOSITORY}.")
            if read_input("Type yes to continue: ").strip().casefold() != "yes":
                output_fn("Upload canceled.")
                continue
            token = read_token("GitHub token (input hidden): ")
            try:
                upload_profiles_to_github(profiles, token)
            except (ValueError, RuntimeError) as exc:
                output_fn(f"Upload failed: {exc}")
            else:
                output_fn(f"Uploaded profiles to {GITHUB_REPOSITORY}/{GITHUB_SECRET_NAME}.")
        elif choice == "6":
            output_fn(f"Saved user list: {path}")
            return 0
        else:
            output_fn("Enter a number from 1 to 6.")


if __name__ == "__main__":
    raise SystemExit(run_manager())
