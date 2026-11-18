import io
import os
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from dialer import (
    AzureSpeechTranscriber,
    Config,
    ElevenLabsSpeechTranscriber,
    GoogleSpeechTranscriber,
    RecipientProfile,
    WhisperTranscriber,
    color_transcription_prompt,
    download_recording,
    find_detected_colors,
    parse_profile_configuration,
    parse_profiles,
    run,
    recording_to_pcm_wav,
    send_discord_dm,
    send_transcription_dm,
    text_after_color_list_cue,
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

    def test_parse_profiles_normalizes_explicit_color_aliases(self) -> None:
        configuration = parse_profile_configuration(
            '{"profiles":['
            '{"name":"Alice","discord_user_id":"12345678901234567","colors":["Brown"]},'
            '{"name":"Bob","discord_user_id":"12345678901234568","colors":["brown"]}'
            '],"color_aliases":{" Frown ":" brown "}}'
        )

        self.assertEqual(
            configuration.profiles,
            (
                RecipientProfile("Alice", "12345678901234567", ("Brown",)),
                RecipientProfile("Bob", "12345678901234568", ("brown",)),
            ),
        )
        self.assertEqual(configuration.color_aliases, (("Frown", "Brown"),))

    def test_parse_profiles_rejects_alias_for_unfollowed_color(self) -> None:
        with self.assertRaisesRegex(ValueError, "must map to a followed color"):
            parse_profile_configuration(
                '{"profiles":[{"name":"Alice","discord_user_id":"12345678901234567",'
                '"colors":["brown"]}],"color_aliases":{"frown":"yellow"}}'
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
                '{"profiles":[{"name":"Alice","discord_user_id":"12345678901234567",'
                '"colors":["yellow"]}],"color_aliases":{"yelow":"yellow"}}'
            ),
        }

        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_environment()

        self.assertEqual(config.profiles[0].name, "Alice")
        self.assertEqual(config.profiles[0].colors, ("yellow",))
        self.assertEqual(config.color_aliases, (("yelow", "yellow"),))

    def test_from_environment_defaults_to_elevenlabs_and_validates_cloud_limits(self) -> None:
        with patch.dict(
            os.environ,
            {"CALL_TO_NUMBER": "+15551230002", "DTMF_DIGITS": "1234"},
            clear=True,
        ):
            config = Config.from_environment(dry_run=True)

        self.assertEqual(config.transcription_provider, "elevenlabs")
        self.assertEqual(config.elevenlabs_language_code, "eng")
        self.assertEqual(config.listen_duration_seconds, 55)

        with patch.dict(
            os.environ,
            {
                "CALL_TO_NUMBER": "+15551230002",
                "DTMF_DIGITS": "1234",
                "TRANSCRIPTION_PROVIDER": "google",
                "LISTEN_DURATION_SECONDS": "60",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "less than 60"):
                Config.from_environment(dry_run=True)

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

    def test_transcription_dm_config_uses_selected_local_profile(self) -> None:
        environment = {
            "SIGNALWIRE_PROJECT_ID": "project",
            "SIGNALWIRE_API_TOKEN": "api-token",
            "SIGNALWIRE_SPACE": "space.signalwire.com",
            "SIGNALWIRE_FROM_NUMBER": "+15551230000",
            "DISCORD_BOT_TOKEN": "bot-token",
            "CALL_TO_NUMBER": "+15551230002",
            "DTMF_DIGITS": "1234",
            "DIALER_PROFILES_JSON": "invalid and ignored",
        }
        selected_profile = RecipientProfile("Me", "12345678901234567", ("lime",))

        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_environment(
                transcription_dm_test=True,
                profiles_override=(selected_profile,),
            )

        self.assertEqual(config.profiles, (selected_profile,))
        self.assertEqual(config.discord_bot_token, "bot-token")

    def test_find_detected_colors_matches_phrases_case_insensitively_as_words(self) -> None:
        detected = find_detected_colors(
            "The speaker said YELLOW and light blue, but not bluebird.",
            ("yellow", "blue", "light blue"),
        )

        self.assertEqual(detected, ("yellow", "blue", "light blue"))

    def test_find_detected_colors_maps_explicit_alias_to_followed_color(self) -> None:
        detected = find_detected_colors(
            "The speaker said FROWN today.",
            ("brown",),
            (("frown", "brown"),),
        )

        self.assertEqual(detected, ("brown",))


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

    def test_download_retries_when_recording_media_is_not_ready(self) -> None:
        config = SimpleNamespace(
            signalwire_space_url="https://example.signalwire.com",
            signalwire_project_id="project-id",
            signalwire_api_token="api-token",
            recording_format="mp3",
        )
        not_ready = Mock(status_code=404)
        available = Mock(status_code=200, content=b"audio-bytes")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "recording.mp3"
            with (
                patch("requests.get", side_effect=[not_ready, available]) as get,
                patch("dialer.time.sleep") as sleep,
            ):
                download_recording({"sid": "recording-sid"}, config, destination)

            self.assertEqual(get.call_count, 2)
            sleep.assert_called_once_with(15)
            self.assertEqual(destination.read_bytes(), b"audio-bytes")

    def test_download_reports_when_recording_media_remains_unavailable(self) -> None:
        config = SimpleNamespace(
            signalwire_space_url="https://example.signalwire.com",
            signalwire_project_id="project-id",
            signalwire_api_token="api-token",
            recording_format="mp3",
        )
        not_ready = Mock(status_code=404)
        with (
            patch("requests.get", return_value=not_ready) as get,
            patch("dialer.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "after 5 attempts"):
                download_recording({"sid": "recording-sid"}, config, Path("recording.mp3"))

        self.assertEqual(get.call_count, 5)
        self.assertEqual(sleep.call_count, 4)


class AzureSpeechTests(unittest.TestCase):
    def test_recording_is_converted_to_azure_pcm_wav_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recording.wav"
            with wave.open(str(source), "wb") as audio_file:
                audio_file.setnchannels(2)
                audio_file.setsampwidth(2)
                audio_file.setframerate(8000)
                audio_file.writeframes(b"\0\0\0\0" * 8000)

            converted = recording_to_pcm_wav(source)

        with wave.open(io.BytesIO(converted), "rb") as audio_file:
            self.assertEqual(audio_file.getnchannels(), 1)
            self.assertEqual(audio_file.getsampwidth(), 2)
            self.assertEqual(audio_file.getframerate(), 16000)

    def test_transcriber_sends_wav_to_configured_azure_region(self) -> None:
        response = Mock(ok=True)
        response.json.return_value = {
            "RecognitionStatus": "Success",
            "DisplayText": "The groups for today are yellow.",
        }

        with (
            patch("dialer.recording_to_pcm_wav", return_value=b"wav-audio"),
            patch("requests.post", return_value=response) as post,
        ):
            transcript = AzureSpeechTranscriber("azure-key", "eastus", "en-US").transcribe(
                Path("recording.mp3")
            )

        self.assertEqual(transcript, "The groups for today are yellow.")
        post.assert_called_once_with(
            "https://eastus.stt.speech.microsoft.com/"
            "speech/recognition/conversation/cognitiveservices/v1",
            params={"language": "en-US", "format": "simple"},
            headers={
                "Ocp-Apim-Subscription-Key": "azure-key",
                "Content-Type": "audio/wav",
            },
            data=b"wav-audio",
            timeout=60,
        )


class ElevenLabsSpeechTests(unittest.TestCase):
    def test_transcriber_sends_recording_and_parses_transcript(self) -> None:
        response = Mock(ok=True)
        response.json.return_value = {"text": "The groups for today are yellow."}

        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "recording.mp3"
            audio_path.write_bytes(b"mp3-audio")

            def capture_request(url: str, **kwargs: object) -> Mock:
                self.assertEqual(
                    url,
                    "https://api.elevenlabs.io/v1/speech-to-text",
                )
                self.assertEqual(kwargs["files"]["file"][0], "recording.mp3")
                self.assertEqual(kwargs["files"]["file"][1].read(), b"mp3-audio")
                return response

            with patch("requests.post", side_effect=capture_request) as post:
                transcript = ElevenLabsSpeechTranscriber(
                    "elevenlabs-key",
                    "eng",
                ).transcribe(audio_path)

        self.assertEqual(transcript, "The groups for today are yellow.")
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"xi-api-key": "elevenlabs-key"},
        )
        self.assertEqual(
            post.call_args.kwargs["data"],
            {"model_id": "scribe_v3", "language_code": "eng"},
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 60)

    def test_transcriber_retries_rate_limit_using_retry_after_and_reopens_file(self) -> None:
        busy_response = Mock(
            ok=False,
            status_code=429,
            headers={"Retry-After": "1.5"},
        )
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"text": "The groups for today are yellow."}
        uploaded_audio: list[bytes] = []

        def capture_request(_url: str, **kwargs: object) -> Mock:
            uploaded_audio.append(kwargs["files"]["file"][1].read())
            return (busy_response, response)[len(uploaded_audio) - 1]

        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "recording.mp3"
            audio_path.write_bytes(b"mp3-audio")

            with (
                patch("requests.post", side_effect=capture_request) as post,
                patch("dialer.time.sleep") as sleep,
            ):
                transcript = ElevenLabsSpeechTranscriber(
                    "elevenlabs-key",
                    "eng",
                ).transcribe(audio_path)

        self.assertEqual(transcript, "The groups for today are yellow.")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(uploaded_audio, [b"mp3-audio", b"mp3-audio"])
        sleep.assert_called_once_with(1.5)


class RoutingTests(unittest.TestCase):
    def test_missing_transcription_dependency_fails_before_placing_call(self) -> None:
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(RecipientProfile("Alice", "12345678901234567", ("yellow",)),),
        )

        with (
            patch.dict("sys.modules", {"faster_whisper": None}),
            patch("dialer.signalwire_request") as signalwire_request,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "python -m pip install -r requirements.txt",
            ):
                run(config)

        signalwire_request.assert_not_called()

    def test_run_notifies_profiles_with_detected_or_missing_colors(self) -> None:
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

        output = io.StringIO()
        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch(
                "dialer.send_discord_dm",
                side_effect=["message-1", "message-2", "message-3"],
            ) as send_dm,
            redirect_stdout(output),
        ):
            transcriber.return_value.transcribe.return_value = (
                "Earlier, red and green were mentioned. "
                "The groups for today, Monday, November 16, are yellow and BLUE."
            )

            result = run(config)

        self.assertEqual(result, 0)
        self.assertIn("Configured profile colors:", output.getvalue())
        self.assertIn("  Alice: yellow, red", output.getvalue())
        self.assertIn("  Bob: blue", output.getvalue())
        self.assertIn("  Casey: green", output.getvalue())
        self.assertEqual(
            send_dm.call_args_list,
            [
                call(config, "12345678901234567", "Today's color(s) you follow: yellow."),
                call(config, "12345678901234568", "Today's color(s) you follow: blue."),
                call(config, "12345678901234569", "Your color was not called."),
            ],
        )

    def test_run_applies_global_alias_to_all_profiles_following_color(self) -> None:
        profiles = (
            RecipientProfile("Alice", "12345678901234567", ("brown",)),
            RecipientProfile("Bob", "12345678901234568", ("Brown",)),
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
            color_aliases=(("frown", "brown"),),
            recording_format="test",
        )

        output = io.StringIO()
        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm", side_effect=["message-1", "message-2"]) as send_dm,
            redirect_stdout(output),
        ):
            transcriber.return_value.transcribe.return_value = (
                "The groups for today, Monday, November 16, are frown."
            )

            result = run(config)

        self.assertEqual(result, 0)
        self.assertIn("Configured color aliases: frown -> brown", output.getvalue())
        self.assertEqual(
            send_dm.call_args_list,
            [
                call(config, "12345678901234567", "Today's color(s) you follow: brown."),
                call(config, "12345678901234568", "Today's color(s) you follow: Brown."),
            ],
        )

    def test_run_does_not_send_notifications_when_color_cue_is_missing(self) -> None:
        profiles = (RecipientProfile("Alice", "12345678901234567", ("yellow",)),)
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
        error = io.StringIO()

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm") as send_dm,
            redirect_stderr(error),
        ):
            transcriber.return_value.transcribe.return_value = "The colors are yellow and blue."

            result = run(config)

        self.assertEqual(result, 1)
        self.assertIn("no notifications were sent", error.getvalue())
        send_dm.assert_not_called()

    def test_color_transcription_prompt_includes_profile_colors_and_aliases(self) -> None:
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(RecipientProfile("Me", "12345678901234567", ("lime", "rose")),),
            color_aliases=(("line", "lime"), ("rows", "rose")),
        )

        prompt = color_transcription_prompt(config)

        assert prompt is not None
        self.assertIn("The groups for today", prompt)
        self.assertIn("lime, rose, line, rows", prompt)

    def test_whisper_transcriber_passes_initial_prompt_to_model(self) -> None:
        transcriber = object.__new__(WhisperTranscriber)
        transcriber._model = Mock()
        transcriber._model.transcribe.return_value = (
            [SimpleNamespace(text=" Lime and rose.")],
            None,
        )

        transcript = transcriber.transcribe(
            Path("recording.mp3"),
            initial_prompt="Possible color names include lime and rose.",
        )
        self.assertEqual(transcript, "Lime and rose.")
        transcriber._model.transcribe.assert_called_once_with(
            "recording.mp3",
            beam_size=5,
            initial_prompt="Possible color names include lime and rose.",
        )

    def test_google_transcriber_sends_mp3_and_phrase_hints(self) -> None:
        class RecognitionConfig:
            class AudioEncoding:
                MP3 = "MP3"

            def __init__(self, **options: object) -> None:
                self.options = options

        class SpeechContext:
            def __init__(self, **options: object) -> None:
                self.options = options

        class RecognitionAudio:
            def __init__(self, **options: object) -> None:
                self.options = options

        client = Mock()
        client.recognize.return_value.results = [
            SimpleNamespace(alternatives=[SimpleNamespace(transcript=" Lime was said.")]),
            SimpleNamespace(alternatives=[]),
        ]
        speech = SimpleNamespace(
            SpeechClient=Mock(return_value=client),
            RecognitionConfig=RecognitionConfig,
            SpeechContext=SpeechContext,
            RecognitionAudio=RecognitionAudio,
        )

        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "recording.mp3"
            recording.write_bytes(b"mp3-audio")
            with patch.object(
                GoogleSpeechTranscriber,
                "_load_speech_module",
                return_value=speech,
            ):
                transcript = GoogleSpeechTranscriber("en-US").transcribe(
                    recording,
                    speech_hints=["lime"],
                )

        self.assertEqual(transcript, "Lime was said.")
        request = client.recognize.call_args.kwargs
        self.assertEqual(request["audio"].options, {"content": b"mp3-audio"})
        self.assertEqual(request["config"].options["language_code"], "en-US")
        self.assertEqual(request["config"].options["encoding"], "MP3")
        self.assertEqual(
            request["config"].options["speech_contexts"][0].options["phrases"],
            ["The groups for today", "lime"],
        )

    def test_run_uses_google_transcriber_when_selected(self) -> None:
        profile = RecipientProfile("Alice", "12345678901234567", ("yellow",))
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(profile,),
            transcription_provider="google",
            recording_format="test",
        )

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"sid": "recording-id"}),
            patch("dialer.download_recording"),
            patch("dialer.GoogleSpeechTranscriber") as google_transcriber,
            patch("dialer.WhisperTranscriber.check_dependency") as check_whisper,
            patch("dialer.send_discord_dm", return_value="message-id"),
        ):
            google_transcriber.return_value.transcribe.return_value = (
                "The groups for today are yellow."
            )
            result = run(config)

        self.assertEqual(result, 0)
        google_transcriber.check_dependency.assert_called_once_with()
        google_transcriber.return_value.transcribe.assert_called_once_with(
            Path("recording.test"),
            speech_hints=["yellow"],
        )
        check_whisper.assert_not_called()

    def test_run_uses_elevenlabs_transcriber_when_selected(self) -> None:
        profile = RecipientProfile("Alice", "12345678901234567", ("yellow",))
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(profile,),
            transcription_provider="elevenlabs",
            elevenlabs_api_key="elevenlabs-key",
            elevenlabs_language_code="eng",
            recording_format="test",
        )

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"sid": "recording-id"}),
            patch("dialer.download_recording"),
            patch("dialer.ElevenLabsSpeechTranscriber") as elevenlabs_transcriber,
            patch("dialer.AzureSpeechTranscriber.check_dependency") as check_azure,
            patch("dialer.GoogleSpeechTranscriber.check_dependency") as check_google,
            patch("dialer.WhisperTranscriber.check_dependency") as check_whisper,
            patch("dialer.send_discord_dm", return_value="message-id"),
        ):
            elevenlabs_transcriber.return_value.transcribe.return_value = (
                "The groups for today are yellow."
            )
            result = run(config)

        self.assertEqual(result, 0)
        elevenlabs_transcriber.check_dependency.assert_called_once_with("elevenlabs-key")
        elevenlabs_transcriber.assert_called_once_with("elevenlabs-key", "eng")
        elevenlabs_transcriber.return_value.transcribe.assert_called_once_with(
            Path("recording.test")
        )
        check_azure.assert_not_called()
        check_google.assert_not_called()
        check_whisper.assert_not_called()

    def test_run_uses_azure_transcriber_when_selected(self) -> None:
        profile = RecipientProfile("Alice", "12345678901234567", ("yellow",))
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(profile,),
            transcription_provider="azure",
            azure_speech_key="azure-key",
            azure_speech_region="eastus",
            recording_format="test",
        )

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"sid": "recording-id"}),
            patch("dialer.download_recording"),
            patch("dialer.AzureSpeechTranscriber") as azure_transcriber,
            patch("dialer.GoogleSpeechTranscriber.check_dependency") as check_google,
            patch("dialer.WhisperTranscriber.check_dependency") as check_whisper,
            patch("dialer.send_discord_dm", return_value="message-id"),
        ):
            azure_transcriber.return_value.transcribe.return_value = (
                "The groups for today are yellow."
            )
            result = run(config)

        self.assertEqual(result, 0)
        azure_transcriber.check_dependency.assert_called_once_with("azure-key", "eastus")
        azure_transcriber.return_value.transcribe.assert_called_once_with(
            Path("recording.test")
        )
        check_google.assert_not_called()
        check_whisper.assert_not_called()

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

    def test_color_test_prints_transcript_and_reports_case_insensitive_match(self) -> None:
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
            transcriber.return_value.transcribe.return_value = "Brown was said today."

            result = run(config, color_test="brown")

        self.assertEqual(result, 0)
        self.assertIn("Brown was said today.", output.getvalue())
        self.assertIn("Color check: 'brown' was detected.", output.getvalue())
        send_dm.assert_not_called()

    def test_color_test_reports_when_color_is_not_in_transcript(self) -> None:
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
            transcriber.return_value.transcribe.return_value = "The transcript says yellow."

            result = run(config, color_test="brown")

        self.assertEqual(result, 0)
        self.assertIn("The transcript says yellow.", output.getvalue())
        self.assertIn("Color check: 'brown' was not detected.", output.getvalue())
        send_dm.assert_not_called()

    def test_color_list_test_sends_full_spoken_list_after_cue(self) -> None:
        profiles = (
            RecipientProfile("Me", "12345678901234567", ("yellow", "blue", "brown")),
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
            color_aliases=(("frown", "brown"),),
            recording_format="test",
        )
        output = io.StringIO()

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm", return_value="message-id") as send_dm,
            redirect_stdout(output),
        ):
            transcriber.return_value.transcribe.return_value = (
                "Earlier, red was mentioned. The group's for today: "
                "YELLOW, frown, blue, and purple."
            )

            result = run(
                config,
                color_list_test=True,
                color_list_recipient=profiles[0],
            )

        self.assertEqual(result, 0)
        self.assertIn(
            "Full color list after cue: YELLOW, frown, blue, and purple.",
            output.getvalue(),
        )
        self.assertIn("Full color list sent to Me", output.getvalue())
        self.assertNotIn("red", send_dm.call_args.args[2])
        self.assertIn("purple", send_dm.call_args.args[2])
        self.assertEqual(send_dm.call_args.args[1], "12345678901234567")
        prompt = transcriber.return_value.transcribe.call_args.kwargs["initial_prompt"]
        self.assertIn("including colors not configured for any profile", prompt)

    def test_text_after_color_list_cue_handles_punctuation_and_apostrophe_variants(self) -> None:
        suffix = text_after_color_list_cue(
            "The groups for today are: Yellow and light blue."
        )

        self.assertEqual(suffix, "are: Yellow and light blue.")
        self.assertIsNone(text_after_color_list_cue("No cue phrase was spoken."))

    def test_transcription_dm_test_sends_full_transcript_to_only_selected_profile(self) -> None:
        profile = RecipientProfile("Me", "12345678901234567", ("lime",))
        config = Config(
            signalwire_project_id="project",
            signalwire_api_token="token",
            signalwire_space_url="space.signalwire.com",
            signalwire_from_number="+15551230000",
            discord_bot_token="bot-token",
            call_to_number="+15551230002",
            dtmf_digits="1234",
            whisper_model="base",
            profiles=(profile,),
            recording_format="test",
        )
        output = io.StringIO()

        with (
            patch("dialer.signalwire_request", return_value={"sid": "call-id"}),
            patch("dialer.wait_for_call_completion", return_value="completed"),
            patch("dialer.wait_for_recording", return_value={"uri": "/recording.json"}),
            patch("dialer.download_recording"),
            patch("dialer.WhisperTranscriber") as transcriber,
            patch("dialer.send_discord_dm", return_value="message-id") as send_dm,
            redirect_stdout(output),
        ):
            transcriber.return_value.transcribe.return_value = "The full color transcript says Lime."

            result = run(config, transcription_dm_test=True)

        self.assertEqual(result, 0)
        send_dm.assert_called_once_with(
            config,
            profile.discord_user_id,
            "Transcription test (1/1):\nThe full color transcript says Lime.",
        )
        self.assertIn("Full transcript sent to Me", output.getvalue())

    def test_transcription_dm_splits_long_transcript_into_discord_messages(self) -> None:
        profile = RecipientProfile("Me", "12345678901234567", ("lime",))
        config = SimpleNamespace(discord_bot_token="bot-token")
        transcript = "word " * 900

        with patch("dialer.send_discord_dm") as send_dm:
            count = send_transcription_dm(config, profile, transcript)

        self.assertEqual(count, 3)
        self.assertEqual(send_dm.call_count, 3)
        self.assertTrue(all(len(call.args[2]) <= 2000 for call in send_dm.call_args_list))


if __name__ == "__main__":
    unittest.main()
