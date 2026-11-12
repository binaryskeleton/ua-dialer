import json
import tempfile
import unittest
from pathlib import Path

from dialer import RecipientProfile
from manage_profiles import load_profiles, profiles_json, run_manager, save_profiles


class ProfileStorageTests(unittest.TestCase):
    def test_load_returns_empty_tuple_when_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"

            self.assertEqual(load_profiles(path), ())

    def test_save_and_load_round_trip(self) -> None:
        profiles = (
            RecipientProfile("Alice", "12345678901234567", ("yellow", "light blue")),
            RecipientProfile("Bob", "12345678901234568", ("blue",)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"

            save_profiles(path, profiles)

            self.assertEqual(load_profiles(path), profiles)

    def test_export_is_compact_json_compatible_with_dialer(self) -> None:
        profiles = (RecipientProfile("Alice", "12345678901234567", ("yellow",)),)

        encoded = profiles_json(profiles)

        self.assertEqual(
            json.loads(encoded),
            [
                {
                    "name": "Alice",
                    "discord_user_id": "12345678901234567",
                    "colors": ["yellow"],
                }
            ],
        )


class ProfileManagerTests(unittest.TestCase):
    def test_add_remove_and_export_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            answers = iter(
                [
                    "2",
                    "Alice",
                    "12345678901234567",
                    "yellow, blue",
                    "2",
                    "Bob",
                    "12345678901234568",
                    "red",
                    "4",
                    "3",
                    "1",
                    "1",
                    "4",
                    "5",
                ]
            )
            output: list[str] = []

            result = run_manager(
                path,
                input_fn=lambda _prompt: next(answers),
                output_fn=output.append,
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                load_profiles(path),
                (RecipientProfile("Bob", "12345678901234568", ("red",)),),
            )
            exports = [line for line in output if line.startswith("[")]
            self.assertEqual(len(exports), 2)
            self.assertEqual(len(json.loads(exports[0])), 2)
            self.assertEqual(json.loads(exports[1])[0]["name"], "Bob")
            self.assertEqual(json.loads(exports[1])[0]["colors"], ["red"])

    def test_invalid_profile_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            answers = iter(["2", "Alice", "not-a-discord-id", "yellow", "5"])
            output: list[str] = []

            result = run_manager(
                path,
                input_fn=lambda _prompt: next(answers),
                output_fn=output.append,
            )

            self.assertEqual(result, 0)
            self.assertFalse(path.exists())
            self.assertTrue(any("valid Discord user ID" in line for line in output))


if __name__ == "__main__":
    unittest.main()
