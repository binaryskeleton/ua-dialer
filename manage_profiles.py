"""Manage the private local list used to update the DIALER_PROFILES_JSON secret."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from dialer import RecipientProfile, parse_profiles

PROFILES_FILE = Path(__file__).with_name("dialer-profiles.json")


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
) -> int:
    read_input = input if input_fn is None else input_fn
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
        output_fn("5. Exit")
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
            output_fn(f"Saved user list: {path}")
            return 0
        else:
            output_fn("Enter a number from 1 to 5.")


if __name__ == "__main__":
    raise SystemExit(run_manager())
