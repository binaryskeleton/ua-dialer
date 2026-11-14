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

from dialer import (
    Config,
    ProfileConfiguration,
    RecipientProfile,
    parse_profile_configuration,
    run,
)

PROFILES_FILE = Path(__file__).with_name("dialer-profiles.json")
GITHUB_REPOSITORY = "binaryskeleton/ua-dialer"
GITHUB_SECRET_NAME = "DIALER_PROFILES_JSON"


def run_transcription_test_dm(profile: RecipientProfile) -> int:
    config = Config.from_environment(
        transcription_dm_test=True,
        profiles_override=(profile,),
    )
    return run(config, transcription_dm_test=True)


def run_color_test(color: str) -> int:
    normalized_color = " ".join(color.split())
    if not normalized_color:
        raise ValueError("A color is required")
    config = Config.from_environment(
        transcription_test=True,
        profiles_override=(),
    )
    return run(config, transcription_test=True, color_test=normalized_color)


def load_profiles(path: Path = PROFILES_FILE) -> tuple[RecipientProfile, ...]:
    return load_profile_configuration(path).profiles


def load_profile_configuration(path: Path = PROFILES_FILE) -> ProfileConfiguration:
    if not path.exists():
        return ProfileConfiguration(())
    return parse_profile_configuration(path.read_text(encoding="utf-8"))


def _profiles_configuration_data(
    profiles: tuple[RecipientProfile, ...],
    color_aliases: tuple[tuple[str, str], ...],
) -> list[dict[str, object]] | dict[str, object]:
    serialized = [
        {
            "name": profile.name,
            "discord_user_id": profile.discord_user_id,
            "colors": list(profile.colors),
        }
        for profile in profiles
    ]
    if color_aliases:
        return {
            "profiles": serialized,
            "color_aliases": dict(color_aliases),
        }
    return serialized


def save_profiles(
    path: Path,
    profiles: tuple[RecipientProfile, ...],
    color_aliases: tuple[tuple[str, str], ...] = (),
) -> None:
    path.write_text(
        json.dumps(
            _profiles_configuration_data(profiles, color_aliases),
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def profiles_json(
    profiles: tuple[RecipientProfile, ...],
    color_aliases: tuple[tuple[str, str], ...] = (),
) -> str:
    return json.dumps(
        _profiles_configuration_data(profiles, color_aliases),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def upload_profiles_to_github(
    profiles: tuple[RecipientProfile, ...],
    token: str,
    *,
    color_aliases: tuple[tuple[str, str], ...] = (),
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
        ).encrypt(profiles_json(profiles, color_aliases).encode("utf-8"))
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


def _show_color_aliases(
    color_aliases: tuple[tuple[str, str], ...],
    output_fn: Callable[[str], None],
) -> None:
    if not color_aliases:
        output_fn("No global color aliases are configured.")
        return
    output_fn(
        "Global color aliases: "
        + ", ".join(f"{alias} → {color}" for alias, color in color_aliases)
    )


def run_manager(
    path: Path = PROFILES_FILE,
    *,
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print,
    token_prompt: Callable[[str], str] | None = None,
    transcription_test_runner: Callable[[RecipientProfile], int] | None = None,
    color_test_runner: Callable[[str], int] | None = None,
) -> int:
    read_input = input if input_fn is None else input_fn
    read_token = getpass if token_prompt is None else token_prompt
    run_transcription_test = (
        run_transcription_test_dm
        if transcription_test_runner is None
        else transcription_test_runner
    )
    run_color_check = run_color_test if color_test_runner is None else color_test_runner
    try:
        profile_configuration = load_profile_configuration(path)
    except (OSError, ValueError) as exc:
        print(f"Could not load {path}: {exc}", file=sys.stderr)
        return 1
    profiles = profile_configuration.profiles
    color_aliases = profile_configuration.color_aliases

    while True:
        output_fn("\nUser profile manager")
        output_fn("1. List users")
        output_fn("2. Add user")
        output_fn("3. Remove user")
        output_fn("4. Generate JSON for GitHub")
        output_fn("5. Upload profiles to GitHub")
        output_fn("6. Call and DM transcript (real call)")
        output_fn("7. Call and check a color (real call)")
        output_fn("8. Add or update a global color alias")
        output_fn("9. Exit")
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
                updated_configuration = parse_profile_configuration(
                    profiles_json(
                        (*profiles, RecipientProfile(name, discord_user_id, tuple(colors))),
                        color_aliases,
                    )
                )
                save_profiles(
                    path,
                    updated_configuration.profiles,
                    updated_configuration.color_aliases,
                )
            except (OSError, ValueError) as exc:
                output_fn(f"Could not add user: {exc}")
            else:
                profiles = updated_configuration.profiles
                color_aliases = updated_configuration.color_aliases
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
                remaining_colors = {
                    color.casefold()
                    for profile in updated
                    for color in profile.colors
                }
                updated_aliases = tuple(
                    (alias, color)
                    for alias, color in color_aliases
                    if color.casefold() in remaining_colors
                )
                save_profiles(path, updated, updated_aliases)
            except (OSError, ValueError) as exc:
                output_fn(f"Could not remove user: {exc}")
            else:
                removed = profiles[index - 1]
                profiles = updated
                color_aliases = updated_aliases
                output_fn(f"Removed {removed.name}.")
        elif choice == "4":
            if not profiles:
                output_fn("Add at least one user before generating the JSON.")
                continue
            output_fn("Copy this JSON into the GitHub DIALER_PROFILES_JSON secret:")
            output_fn(profiles_json(profiles, color_aliases))
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
                upload_profiles_to_github(
                    profiles,
                    token,
                    color_aliases=color_aliases,
                )
            except (ValueError, RuntimeError) as exc:
                output_fn(f"Upload failed: {exc}")
            else:
                output_fn(f"Uploaded profiles to {GITHUB_REPOSITORY}/{GITHUB_SECRET_NAME}.")
        elif choice == "6":
            if not profiles:
                output_fn("Add a user before running a transcription test.")
                continue
            _show_profiles(profiles, output_fn)
            selection = read_input(
                "Enter the profile number to receive the transcript, or press Enter to cancel: "
            ).strip()
            if not selection:
                output_fn("Transcription test canceled.")
                continue
            try:
                index = int(selection)
                if not 1 <= index <= len(profiles):
                    raise ValueError("Choose a number from the list.")
            except ValueError as exc:
                output_fn(f"Invalid profile selection: {exc}")
                continue
            profile = profiles[index - 1]
            output_fn(
                "This places a real SignalWire call and sends the full transcript "
                f"to {profile.name} by Discord DM. Call charges may apply."
            )
            if read_input("Type yes to place the call: ").strip().casefold() != "yes":
                output_fn("Transcription test canceled.")
                continue
            try:
                result = run_transcription_test(profile)
            except (OSError, RuntimeError, TimeoutError, ValueError, requests.RequestException) as exc:
                output_fn(f"Transcription test failed: {exc}")
            else:
                if result == 0:
                    output_fn(f"Transcription test completed for {profile.name}.")
                else:
                    output_fn(f"Transcription test failed with exit code {result}.")
        elif choice == "7":
            color = read_input("Color to check: ").strip()
            if not color:
                output_fn("Enter a color to check.")
                continue
            output_fn(
                "This places a real SignalWire call, prints the full transcript locally, "
                "and checks for the selected color. Call charges may apply."
            )
            if read_input("Type yes to place the call: ").strip().casefold() != "yes":
                output_fn("Color test canceled.")
                continue
            try:
                result = run_color_check(color)
            except (OSError, RuntimeError, TimeoutError, ValueError, requests.RequestException) as exc:
                output_fn(f"Color test failed: {exc}")
            else:
                if result == 0:
                    output_fn("Color test completed.")
                else:
                    output_fn(f"Color test failed with exit code {result}.")
        elif choice == "8":
            if not profiles:
                output_fn("Add a user before configuring global color aliases.")
                continue
            _show_color_aliases(color_aliases, output_fn)
            alias = read_input("Alternate word or phrase Whisper may transcribe: ").strip()
            if not alias:
                output_fn("Color alias canceled.")
                continue
            color = read_input("Followed color this alias should match: ").strip()
            aliases = tuple(
                (saved_alias, saved_color)
                for saved_alias, saved_color in color_aliases
                if saved_alias.casefold() != alias.casefold()
            ) + ((alias, color),)
            try:
                updated_configuration = parse_profile_configuration(
                    profiles_json(profiles, aliases)
                )
                save_profiles(
                    path,
                    updated_configuration.profiles,
                    updated_configuration.color_aliases,
                )
            except (OSError, ValueError) as exc:
                output_fn(f"Could not save color alias: {exc}")
            else:
                color_aliases = updated_configuration.color_aliases
                output_fn(f"Saved global alias {alias!r} for {color!r} across all profiles.")
        elif choice == "9":
            output_fn(f"Saved user list: {path}")
            return 0
        else:
            output_fn("Enter a number from 1 to 9.")


if __name__ == "__main__":
    raise SystemExit(run_manager())
