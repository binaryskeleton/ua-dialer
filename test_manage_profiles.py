import json
import tempfile
import unittest
from base64 import b64decode, b64encode
from pathlib import Path
from unittest.mock import Mock, patch

from dialer import RecipientProfile, parse_profile_configuration
from manage_profiles import (
    GITHUB_REPOSITORY,
    GITHUB_SECRET_NAME,
    load_profiles,
    load_profile_configuration,
    profiles_json,
    run_manager,
    run_color_test,
    run_transcription_test_dm,
    save_profiles,
    upload_profiles_to_github,
)
from nacl.public import PrivateKey, SealedBox


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

    def test_save_and_load_round_trip_with_global_color_aliases(self) -> None:
        profiles = (
            RecipientProfile("Alice", "12345678901234567", ("brown",)),
            RecipientProfile("Bob", "12345678901234568", ("brown",)),
        )
        aliases = (("frown", "brown"),)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"

            save_profiles(path, profiles, aliases)
            configuration = load_profile_configuration(path)

        self.assertEqual(configuration.profiles, profiles)
        self.assertEqual(configuration.color_aliases, aliases)

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

    def test_export_includes_global_color_aliases(self) -> None:
        profiles = (
            RecipientProfile("Alice", "12345678901234567", ("brown",)),
            RecipientProfile("Bob", "12345678901234568", ("brown",)),
        )

        encoded = profiles_json(profiles, (("frown", "brown"),))

        self.assertEqual(
            json.loads(encoded),
            {
                "profiles": [
                    {
                        "name": "Alice",
                        "discord_user_id": "12345678901234567",
                        "colors": ["brown"],
                    },
                    {
                        "name": "Bob",
                        "discord_user_id": "12345678901234568",
                        "colors": ["brown"],
                    },
                ],
                "color_aliases": {"frown": "brown"},
            },
        )

    def test_profile_level_aliases_are_migrated_to_global_configuration(self) -> None:
        parsed = parse_profile_configuration(
            '[{"name":"Alice","discord_user_id":"12345678901234567",'
            '"colors":["brown"],"color_aliases":{"frown":"brown"}},'
            '{"name":"Bob","discord_user_id":"12345678901234568","colors":["brown"]}]'
        )

        self.assertEqual(len(parsed.profiles), 2)
        self.assertEqual(parsed.color_aliases, (("frown", "brown"),))


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
                    "9",
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
            answers = iter(["2", "Alice", "not-a-discord-id", "yellow", "9"])
            output: list[str] = []

            result = run_manager(
                path,
                input_fn=lambda _prompt: next(answers),
                output_fn=output.append,
            )

            self.assertEqual(result, 0)
            self.assertFalse(path.exists())
            self.assertTrue(any("valid Discord user ID" in line for line in output))

    def test_upload_menu_prompts_securely_and_reports_success(self) -> None:
        profiles = (RecipientProfile("Alice", "12345678901234567", ("yellow",)),)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            save_profiles(path, profiles)
            answers = iter(["5", "yes", "9"])
            output: list[str] = []

            with patch("manage_profiles.upload_profiles_to_github") as upload:
                result = run_manager(
                    path,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=output.append,
                    token_prompt=lambda _prompt: "sensitive-token",
                )

        self.assertEqual(result, 0)
        upload.assert_called_once_with(profiles, "sensitive-token", color_aliases=())
        self.assertNotIn("sensitive-token", "\n".join(output))
        self.assertTrue(any("Uploaded profiles" in line for line in output))

    def test_transcription_menu_confirms_and_sends_to_selected_profile_only(self) -> None:
        profiles = (
            RecipientProfile("Me", "12345678901234567", ("lime",)),
            RecipientProfile("Other", "12345678901234568", ("blue",)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            save_profiles(path, profiles)
            answers = iter(["6", "1", "yes", "9"])
            output: list[str] = []

            with patch("manage_profiles.run_transcription_test_dm", return_value=0) as run_test:
                result = run_manager(
                    path,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=output.append,
                )

        self.assertEqual(result, 0)
        run_test.assert_called_once_with(profiles[0])
        self.assertTrue(any("real SignalWire call" in line for line in output))
        self.assertTrue(any("completed for Me" in line for line in output))

    def test_transcription_menu_cancel_does_not_place_call(self) -> None:
        profile = RecipientProfile("Me", "12345678901234567", ("lime",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            save_profiles(path, (profile,))
            answers = iter(["6", "1", "no", "9"])

            with patch("manage_profiles.run_transcription_test_dm") as run_test:
                result = run_manager(
                    path,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=lambda _line: None,
                )

        self.assertEqual(result, 0)
        run_test.assert_not_called()

    def test_color_test_menu_prompts_and_calls_only_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            answers = iter(["7", "brown", "yes", "9"])
            output: list[str] = []

            with patch("manage_profiles.run_color_test", return_value=0) as run_test:
                result = run_manager(
                    path,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=output.append,
                )

        self.assertEqual(result, 0)
        run_test.assert_called_once_with("brown")
        self.assertTrue(any("prints the full transcript locally" in line for line in output))
        self.assertTrue(any("Color test completed" in line for line in output))

    def test_color_test_cancel_does_not_place_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            answers = iter(["7", "brown", "no", "9"])

            with patch("manage_profiles.run_color_test") as run_test:
                result = run_manager(
                    path,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=lambda _line: None,
                )

        self.assertEqual(result, 0)
        run_test.assert_not_called()

    def test_alias_menu_saves_transcription_variant_for_followed_color(self) -> None:
        profiles = (
            RecipientProfile("Me", "12345678901234567", ("brown",)),
            RecipientProfile("Other", "12345678901234568", ("brown",)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            save_profiles(path, profiles)
            answers = iter(["8", "frown", "brown", "9"])
            output: list[str] = []

            result = run_manager(
                path,
                input_fn=lambda _prompt: next(answers),
                output_fn=output.append,
            )
            saved_configuration = load_profile_configuration(path)

        self.assertEqual(result, 0)
        self.assertEqual(saved_configuration.profiles, profiles)
        self.assertEqual(saved_configuration.color_aliases, (("frown", "brown"),))
        self.assertTrue(any("Saved global alias 'frown' for 'brown'" in line for line in output))

    def test_color_test_runner_checks_normalized_color_without_profiles(self) -> None:
        config = Mock()
        with (
            patch("manage_profiles.Config.from_environment", return_value=config) as from_env,
            patch("manage_profiles.run", return_value=0) as dialer_run,
        ):
            result = run_color_test("  light   blue ")

        self.assertEqual(result, 0)
        from_env.assert_called_once_with(
            transcription_test=True,
            profiles_override=(),
        )
        dialer_run.assert_called_once_with(
            config,
            transcription_test=True,
            color_test="light blue",
        )

    def test_color_test_runner_rejects_empty_color(self) -> None:
        with patch("manage_profiles.Config.from_environment") as from_env:
            with self.assertRaisesRegex(ValueError, "A color is required"):
                run_color_test("   ")

        from_env.assert_not_called()

    def test_transcription_test_runner_uses_only_selected_profile(self) -> None:
        profile = RecipientProfile("Me", "12345678901234567", ("lime",))
        config = Mock()
        with (
            patch("manage_profiles.Config.from_environment", return_value=config) as from_env,
            patch("manage_profiles.run", return_value=0) as dialer_run,
        ):
            result = run_transcription_test_dm(profile)

        self.assertEqual(result, 0)
        from_env.assert_called_once_with(
            transcription_dm_test=True,
            profiles_override=(profile,),
        )
        dialer_run.assert_called_once_with(config, transcription_dm_test=True)


class GitHubUploadTests(unittest.TestCase):
    def test_encrypts_and_uploads_profiles_to_repository_secret(self) -> None:
        private_key = PrivateKey.generate()
        public_key = b64encode(bytes(private_key.public_key)).decode("ascii")
        key_response = Mock(status_code=200)
        key_response.json.return_value = {"key": public_key, "key_id": "key-id"}
        secret_response = Mock(status_code=201)
        profiles = (RecipientProfile("Alice", "12345678901234567", ("brown",)),)
        aliases = (("frown", "brown"),)

        with (
            patch("manage_profiles.requests.get", return_value=key_response) as get,
            patch("manage_profiles.requests.put", return_value=secret_response) as put,
        ):
            upload_profiles_to_github(profiles, "test-token", color_aliases=aliases)

        base_url = (
            "https://api.github.com/repos/"
            f"{GITHUB_REPOSITORY}/actions/secrets"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer test-token",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        get.assert_called_once_with(f"{base_url}/public-key", headers=headers, timeout=30)
        put.assert_called_once()
        self.assertEqual(put.call_args.args[0], f"{base_url}/{GITHUB_SECRET_NAME}")
        self.assertEqual(put.call_args.kwargs["headers"], headers)
        self.assertEqual(put.call_args.kwargs["timeout"], 30)
        encrypted = put.call_args.kwargs["json"]["encrypted_value"]
        decrypted = SealedBox(private_key).decrypt(b64decode(encrypted))
        self.assertEqual(
            json.loads(decrypted),
            json.loads(profiles_json(profiles, aliases)),
        )

    def test_reports_permission_failure_without_revealing_token(self) -> None:
        response = Mock(status_code=403, text="Resource not accessible")
        with (
            patch("manage_profiles.requests.get", return_value=response),
            patch("manage_profiles.requests.put") as put,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                upload_profiles_to_github(
                    (RecipientProfile("Alice", "12345678901234567", ("yellow",)),),
                    "test-token",
                )

        put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
