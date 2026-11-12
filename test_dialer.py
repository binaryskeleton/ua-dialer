import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from dialer import (
    Config,
    RecipientProfile,
    download_recording,
    find_detected_colors,
    parse_profiles,
    run,
    send_discord_dm,
)


class ProfileTests(unittest.TestCase):
    def test_parse_profiles_normalizes_and_deduplicates_colors(self) -> None:
        profiles = parse_profiles(
            '[{"name":" Alice ","discord_user_id":"12345678901234567",'
            '"colors":[" Yellow ", "yellow", "light   blue"]}]'
        )

        self.assertEqual(
            profiles,
            (RecipientProfile("Alice", "12345678901234567", ("Yellow", "light blue")),),
        )

    def test_parse_profiles_rejects_duplicate_user_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than one profile"):
            parse_profiles(
                '[{"name":"Alice","discord_user_id":"12345678901234567","colors":["yellow"]},'
                '{"name":"Bob","discord_user_id":"12345678901234567","colors":["blue"]}]'
            )

    def test_from_environment_loads_profiles_for_a_scheduled_run(self) -> None:
        environment = {
            "SIGNALWIRE_PROJECT_ID": "project",
            "SIGNALWIRE_API_TOKEN": "api-token",
            "SIGNALWIRE_SPACE": "space.signalwire.com",
            "SIGNALWIRE_FROM_NUMBER": "+15551230000",
            "DISCORD_BOT_TOKEN": "bot-token",
            "CALL_TO_NUMBER": "+15551230002",
            "DTMF_DIGITS": "1234",
            "DIALER_PROFILES_JSON": (
                '[{"name":"Alice","discord_user_id":"12345678901234567",'
                '"colors":["yellow"]}]'
            ),
        }

        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_environment()

        self.assertEqual(config.profiles[0].name, "Alice")
        self.assertEqual(config.profiles[0].colors, ("yellow",))

    def test_dry_run_does_not_require_discord_profiles_or_bot_token(self) -> None:
        with patch.dict(
            os.environ,
            {"CALL_TO_NUMBER": "+15551230002", "DTMF_DIGITS": "1234"},
            clear=True,
        ):
            config = Config.from_environment(dry_run=True)

        self.assertEqual(config.profiles, ())

    def test_transcription_test_does_not_require_discord_settings(self) -> None:
        environment = {
            "SIGNALWIRE_PROJECT_ID": "project",
            "SIGNALWIRE_API_TOKEN": "api-token",
            "SIGNALWIRE_SPACE": "space.signalwire.com",
            "SIGNALWIRE_FROM_NUMBER": "+15551230000",
            "CALL_TO_NUMBER": "+15551230002",
            "DTMF_DIGITS": "1234",
        }

        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_environment(transcription_test=True)

        self.assertEqual(config.profiles, ())

    def test_find_detected_colors_matches_phrases_case_insensitively_as_words(self) -> None:
        detected = find_detected_colors(
            "The speaker said YELLOW and light blue, but not bluebird.",
            ("yellow", "blue", "light blue"),
        )

        self.assertEqual(detected, ("yellow", "blue", "light blue"))


class DiscordTests(unittest.TestCase):
    def test_send_discord_dm_creates_channel_then_sends_message(self) -> None:
        created_channel = Mock(ok=True)
        created_channel.json.return_value = {"id": "channel-id"}
        sent_message = Mock(ok=True)
        sent_message.json.return_value = {"id": "message-id"}
        config = SimpleNamespace(discord_bot_token="test-token")

        with patch("requests.post", side_effect=[created_channel, sent_message]) as post:
            result = send_discord_dm(config, "12345678901234567", "Yellow detected.")

        self.assertEqual(result, "message-id")
        self.assertEqual(
            post.call_args_list,
            [
                call(
                    "https://discord.com/api/v10/users/@me/channels",
                    headers={"Authorization": "Bot test-token"},
                    json={"recipient_id": "12345678901234567"},
                    timeout=30,
                ),
                call(
                    "https://discord.com/api/v10/channels/channel-id/messages",
                    headers={"Authorization": "Bot test-token"},
                    json={"content": "Yellow detected."},
                    timeout=30,
                ),
            ],
        )


class RecordingTests(unittest.TestCase):
    def test_download_uses_canonical_recording_media_endpoint(self) -> None:
        config = SimpleNamespace(
            signalwire_space_url="https://example.signalwire.com",
            signalwire_project_id="project-id",
            signalwire_api_token="api-token",
            recording_format="mp3",
        )
        response = Mock()
        response.content = b"audio-bytes"

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.mp3"
            with patch("requests.get", return_value=response) as get:
                download_recording(
                    {
                        "sid": "recording-sid",
                        "uri": "/Calls/call/Recordings/recording-sid.json",
                    },
                    config,
                    destination,
                )

            get.assert_called_once_with(
                "https://example.signalwire.com/api/laml/2010-04-01/"
                "Accounts/project-id/Recordings/recording-sid.mp3",
                auth=("project-id", "api-token"),
                timeout=60,
            )
            response.raise_for_status.assert_called_once_with()
            self.assertEqual(destination.read_bytes(), b"audio-bytes")

    def test_download_fails_clearly_if_recording_sid_is_missing(self) -> None:
        config = SimpleNamespace(
            signalwire_space_url="https://example.signalwire.com",
            signalwire_project_id="project-id",
            signalwire_api_token="api-token",
            recording_format="mp3",
        )
        with patch("requests.get") as get:
            with self.assertRaisesRegex(RuntimeError, "recording SID"):
                download_recording({"uri": "/recording.json"}, config, Path("recording.mp3"))
        get.assert_not_called()


class RoutingTests(unittest.TestCase):
    def test_run_notifies_only_profiles_with_detected_colors(self) -> None:
        profiles = (
            RecipientProfile("Alice", "12345678901234567", ("yellow", "red")),
            RecipientProfile("Bob", "12345678901234568", ("blue",)),
            RecipientProfile("Casey", "12345678901234569", ("green",)),
        )
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=profiles,
            recording_format="test",
        )

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm", side_effect=["message-1", "message-2"]) as send_dm,
        ):
            transcriber.return_value.transcribe.return_value = "I heard yellow and BLUE."

            result = run(config)

        self.assertEqual(result, 0)
        self.assertEqual(
            send_dm.call_args_list,
            [
                call(config, "12345678901234567", "Today's color(s) you follow: yellow."),
                call(config, "12345678901234568", "Today's color(s) you follow: blue."),
            ],
        )

    def test_transcription_test_prints_full_transcript_without_sending_dms(self) -> None:
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(),
            recording_format="test",
        )
        output = io.StringIO()

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm") as send_dm,
            redirect_stdout(output),
        ):
            transcriber.return_value.transcribe.return_value = "The complete recognized speech."

            result = run(config, transcription_test=True)

        self.assertEqual(result, 0)
        self.assertIn("The complete recognized speech.", output.getvalue())
        send_dm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
