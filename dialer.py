"""Place a recorded SignalWire call and DM users whose colors are transcribed."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from xml.etree import ElementTree

from dotenv import load_dotenv

TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
RECORDING_DOWNLOAD_ATTEMPTS = 5
RECORDING_DOWNLOAD_RETRY_DELAY_SECONDS = 15
COLOR_LIST_CUE_PHRASE = "The groups for today"

load_dotenv(Path(__file__).with_name(".env"), override=True)


@dataclass(frozen=True)
class RecipientProfile:
    name: str
    discord_user_id: str
    colors: tuple[str, ...]


@dataclass(frozen=True)
class ProfileConfiguration:
    profiles: tuple[RecipientProfile, ...]
    color_aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Config:
    signalwire_project_id: str
    signalwire_api_token: str
    signalwire_space_url: str
    signalwire_from_number: str
    discord_bot_token: str
    call_to_number: str
    dtmf_digits: str
    whisper_model: str
    profiles: tuple[RecipientProfile, ...]
    transcription_provider: str = "whisper"
    google_speech_language_code: str = "en-US"
    elevenlabs_api_key: str = ""
    elevenlabs_language_code: str = "eng"
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_speech_language_code: str = "en-US"
    color_aliases: tuple[tuple[str, str], ...] = ()
    poll_interval_seconds: int = 15
    poll_timeout_seconds: int = 1800
    listen_duration_seconds: int = 60
    recording_format: str = "mp3"

    @classmethod
    def from_environment(
        cls,
        *,
        dry_run: bool = False,
        discord_test: bool = False,
        transcription_test: bool = False,
        transcription_dm_test: bool = False,
        profiles_override: tuple[RecipientProfile, ...] | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> "Config":
        values = {
            "signalwire_project_id": os.getenv("SIGNALWIRE_PROJECT_ID", ""),
            "signalwire_api_token": os.getenv("SIGNALWIRE_API_TOKEN", ""),
            "signalwire_space_url": os.getenv("SIGNALWIRE_SPACE", ""),
            "signalwire_from_number": os.getenv("SIGNALWIRE_FROM_NUMBER", ""),
            "discord_bot_token": os.getenv("DISCORD_BOT_TOKEN", ""),
            "call_to_number": os.getenv("CALL_TO_NUMBER", ""),
            "dtmf_digits": os.getenv("DTMF_DIGITS", ""),
            "whisper_model": os.getenv("WHISPER_MODEL", "base"),
            "transcription_provider": os.getenv("TRANSCRIPTION_PROVIDER", "elevenlabs").strip().lower(),
            "google_speech_language_code": os.getenv("GOOGLE_SPEECH_LANGUAGE_CODE", "en-US").strip(),
            "elevenlabs_api_key": os.getenv("ELEVENLABS_API_KEY", ""),
            "elevenlabs_language_code": os.getenv("ELEVENLABS_LANGUAGE_CODE", "eng").strip(),
            "azure_speech_key": os.getenv("AZURE_SPEECH_KEY", ""),
            "azure_speech_region": os.getenv("AZURE_SPEECH_REGION", "").strip().lower(),
            "azure_speech_language_code": os.getenv("AZURE_SPEECH_LANGUAGE_CODE", "en-US").strip(),
        }
        profiles_json = os.getenv("DIALER_PROFILES_JSON", "")
        if overrides:
            values.update({name: value for name, value in overrides.items() if value is not None})
        values["transcription_provider"] = values["transcription_provider"].strip().lower()
        values["google_speech_language_code"] = values["google_speech_language_code"].strip()
        if values["transcription_provider"] not in {"azure", "elevenlabs", "google", "whisper"}:
            raise ValueError(
                "TRANSCRIPTION_PROVIDER must be 'elevenlabs', 'azure', 'google', or 'whisper'"
            )
        if discord_test:
            required = ["discord_bot_token"]
        else:
            required = ["call_to_number", "dtmf_digits"]
        if not dry_run and not discord_test:
            required.extend([
                "signalwire_project_id",
                "signalwire_api_token",
                "signalwire_space_url",
                "signalwire_from_number",
            ])
        if not dry_run and not discord_test and not transcription_test:
            required.append("discord_bot_token")
        missing = [name for name in required if not values[name]]
        needs_profiles = discord_test or (
            not dry_run and not transcription_test
        )
        if needs_profiles and profiles_override is None and not profiles_json:
            missing.append("DIALER_PROFILES_JSON")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if values["dtmf_digits"] and not re.fullmatch(r"[0-9*#A-Da-dw]+", values["dtmf_digits"]):
            raise ValueError("DTMF_DIGITS may contain only digits, *, #, A-D, or w pauses")
        if values["signalwire_space_url"]:
            space_value = values["signalwire_space_url"].strip()
            if "://" not in space_value:
                space_value = f"https://{space_value}"
            values["signalwire_space_url"] = space_value
            space_url = urlparse(values["signalwire_space_url"])
            if (
                space_url.scheme != "https"
                or not space_url.netloc
                or space_url.path not in ("", "/")
                or space_url.query
                or space_url.fragment
            ):
                raise ValueError("SIGNALWIRE_SPACE must be a SignalWire Space hostname or HTTPS URL")
            values["signalwire_space_url"] = values["signalwire_space_url"].rstrip("/")

        if profiles_override is not None:
            profiles = profiles_override
            color_aliases = ()
        else:
            profile_configuration = (
                parse_profile_configuration(profiles_json)
                if profiles_json
                else ProfileConfiguration(())
            )
            profiles = profile_configuration.profiles
            color_aliases = profile_configuration.color_aliases
        if (discord_test or (not dry_run and not transcription_test)) and not profiles:
            raise ValueError("DIALER_PROFILES_JSON must contain at least one recipient profile")

        default_listen_duration = (
            55 if values["transcription_provider"] in {"azure", "elevenlabs", "google"} else 60
        )
        listen_duration_seconds = _positive_int_from_env(
            "LISTEN_DURATION_SECONDS",
            default_listen_duration,
        )
        if listen_duration_seconds > 400:
            raise ValueError("LISTEN_DURATION_SECONDS must not exceed 600")
        recording_format = os.getenv("RECORDING_FORMAT", "mp3").strip().lower()
        if values["transcription_provider"] in {"azure", "google"}:
            if listen_duration_seconds >= 60:
                raise ValueError(
                    f"{values['transcription_provider'].title()} speech recognition requires "
                    "LISTEN_DURATION_SECONDS to be less than 60"
                )
        if values["transcription_provider"] == "google":
            if recording_format != "mp3":
                raise ValueError("Google Cloud Speech transcription requires RECORDING_FORMAT=mp3")
            if not values["google_speech_language_code"]:
                raise ValueError("GOOGLE_SPEECH_LANGUAGE_CODE must not be empty")
        if values["transcription_provider"] == "elevenlabs":
            if not values["elevenlabs_language_code"]:
                raise ValueError("ELEVENLABS_LANGUAGE_CODE must not be empty")
        if values["transcription_provider"] == "azure":
            if not values["azure_speech_language_code"]:
                raise ValueError("AZURE_SPEECH_LANGUAGE_CODE must not be empty")
        return cls(
            **values,
            profiles=profiles,
            color_aliases=color_aliases,
            poll_interval_seconds=_positive_int_from_env("POLL_INTERVAL_SECONDS", 15),
            poll_timeout_seconds=_positive_int_from_env("POLL_TIMEOUT_SECONDS", 1800),
            listen_duration_seconds=listen_duration_seconds,
            recording_format=recording_format,
        )


def parse_profiles(profiles_json: str) -> tuple[RecipientProfile, ...]:
    return parse_profile_configuration(profiles_json).profiles


def parse_profile_configuration(profiles_json: str) -> ProfileConfiguration:
    try:
        raw_configuration = json.loads(profiles_json)
    except json.JSONDecodeError as exc:
        raise ValueError("DIALER_PROFILES_JSON must be valid JSON") from exc
    if isinstance(raw_configuration, list):
        raw_profiles = raw_configuration
        raw_aliases: object = {}
    elif isinstance(raw_configuration, dict):
        raw_profiles = raw_configuration.get("profiles")
        raw_aliases = raw_configuration.get("color_aliases", {})
    else:
        raise ValueError("DIALER_PROFILES_JSON must be a JSON array or object")
    if not isinstance(raw_aliases, dict):
        raise ValueError("Color aliases must be a JSON object")
    merged_aliases = dict(raw_aliases)
    if isinstance(raw_profiles, list):
        for raw_profile in raw_profiles:
            if not isinstance(raw_profile, dict):
                continue
            profile_aliases = raw_profile.get("color_aliases", {})
            if not isinstance(profile_aliases, dict):
                raise ValueError("Color aliases must be a JSON object")
            for alias, color in profile_aliases.items():
                if alias in merged_aliases and merged_aliases[alias] != color:
                    raise ValueError(f"Color alias {alias!r} has conflicting mappings")
                merged_aliases[alias] = color
    profiles = _parse_profiles(raw_profiles)
    aliases = _parse_color_aliases(merged_aliases, profiles)
    return ProfileConfiguration(profiles, aliases)


def _parse_profiles(raw_profiles: object) -> tuple[RecipientProfile, ...]:
    if not isinstance(raw_profiles, list):
        raise ValueError("DIALER_PROFILES_JSON profiles must be a JSON array")

    profiles: list[RecipientProfile] = []
    seen_user_ids: set[str] = set()
    for index, raw_profile in enumerate(raw_profiles, start=1):
        if not isinstance(raw_profile, dict):
            raise ValueError(f"Profile {index} must be a JSON object")
        name = raw_profile.get("name")
        user_id = raw_profile.get("discord_user_id")
        colors = raw_profile.get("colors")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError(f"Profile {index} must have a name of 1 to 80 characters")
        if not isinstance(user_id, str) or not re.fullmatch(r"\d{17,20}", user_id):
            raise ValueError(f"Profile {index} must have a valid Discord user ID")
        if user_id in seen_user_ids:
            raise ValueError(f"Discord user ID appears in more than one profile: {user_id}")
        if not isinstance(colors, list) or not colors:
            raise ValueError(f"Profile {index} must have at least one color")

        normalized_colors: list[str] = []
        seen_colors: set[str] = set()
        for color in colors:
            if not isinstance(color, str):
                raise ValueError(f"Every color in profile {index} must be a string")
            normalized = " ".join(color.split())
            if not normalized or len(normalized) > 60:
                raise ValueError(f"Colors in profile {index} must contain 1 to 60 characters")
            color_key = normalized.casefold()
            if color_key not in seen_colors:
                normalized_colors.append(normalized)
                seen_colors.add(color_key)

        profiles.append(
            RecipientProfile(
                name=name.strip(),
                discord_user_id=user_id,
                colors=tuple(normalized_colors),
            )
        )
        seen_user_ids.add(user_id)

    return tuple(profiles)


def _parse_color_aliases(
    raw_aliases: object,
    profiles: tuple[RecipientProfile, ...],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw_aliases, dict):
        raise ValueError("Color aliases must be a JSON object")

    colors_by_key: dict[str, str] = {}
    for profile in profiles:
        for color in profile.colors:
            colors_by_key.setdefault(color.casefold(), color)
    aliases: list[tuple[str, str]] = []
    seen_aliases: set[str] = set()
    for alias, color in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(color, str):
            raise ValueError("Color aliases must map strings to strings")
        normalized_alias = " ".join(alias.split())
        normalized_color = " ".join(color.split())
        if not normalized_alias or len(normalized_alias) > 60:
            raise ValueError("Color aliases must contain 1 to 60 characters")
        alias_key = normalized_alias.casefold()
        color_key = normalized_color.casefold()
        if color_key not in colors_by_key:
            raise ValueError(f"Color alias {normalized_alias!r} must map to a followed color")
        if alias_key in seen_aliases:
            raise ValueError(f"Color alias {normalized_alias!r} appears more than once")
        if alias_key in colors_by_key and alias_key != color_key:
            raise ValueError(f"Color alias {normalized_alias!r} conflicts with a followed color")
        if alias_key == color_key:
            raise ValueError(f"Color alias {normalized_alias!r} must differ from its followed color")
        aliases.append((normalized_alias, colors_by_key[color_key]))
        seen_aliases.add(alias_key)

    return tuple(aliases)


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def build_twiml(config: Config) -> str:
    response = ElementTree.Element("Response")
    ElementTree.SubElement(response, "Play", {"digits": config.dtmf_digits})
    ElementTree.SubElement(response, "Pause", {"length": str(config.listen_duration_seconds)})
    return ElementTree.tostring(response, encoding="unicode", xml_declaration=True)


def find_detected_colors(
    transcript: str,
    colors: tuple[str, ...],
    color_aliases: tuple[tuple[str, str], ...] = (),
) -> tuple[str, ...]:
    detected: list[str] = []
    for color in colors:
        phrases = (color, *(alias for alias, target in color_aliases if target.casefold() == color.casefold()))
        for phrase in phrases:
            color_pattern = r"\s+".join(re.escape(part) for part in phrase.split())
            if re.search(rf"(?<!\w){color_pattern}(?!\w)", transcript, flags=re.IGNORECASE):
                detected.append(color)
                break
    return tuple(detected)


def text_after_color_list_cue(transcript: str) -> str | None:
    cue_tokens = [
        match.group().casefold().replace("'", "").replace("’", "")
        for match in re.finditer(r"\w+(?:['’]\w+)?", COLOR_LIST_CUE_PHRASE)
    ]
    transcript_matches = list(re.finditer(r"\w+(?:['’]\w+)?", transcript))
    transcript_tokens = [
        match.group().casefold().replace("'", "").replace("’", "")
        for match in transcript_matches
    ]
    for start in range(len(transcript_tokens) - len(cue_tokens), -1, -1):
        if transcript_tokens[start:start + len(cue_tokens)] == cue_tokens:
            cue_end = transcript_matches[start + len(cue_tokens) - 1].end()
            return transcript[cue_end:].lstrip(" \t\r\n:,-")
    return None


def color_transcription_prompt(config: Config) -> str | None:
    color_terms = color_transcription_hints(config)
    if not color_terms:
        return None
    return (
        "This is a telephone recording. The color list follows the phrase "
        f"{COLOR_LIST_CUE_PHRASE!r}. A weekday and date may come before the colors. "
        f"Possible color names include {', '.join(color_terms)}."
    )


def color_transcription_hints(config: Config) -> list[str]:
    color_terms: list[str] = []
    seen: set[str] = set()
    for profile in config.profiles:
        for color in profile.colors:
            if color.casefold() not in seen:
                color_terms.append(color)
                seen.add(color.casefold())
    for alias, color in config.color_aliases:
        for term in (alias, color):
            if term.casefold() not in seen:
                color_terms.append(term)
                seen.add(term.casefold())
    return color_terms


def signalwire_request(config: Config, method: str, resource: str, **kwargs: Any) -> dict[str, Any]:
    import requests

    api_url = (
        f"{config.signalwire_space_url}/api/laml/2010-04-01/Accounts/"
        f"{config.signalwire_project_id}/{resource}"
    )
    response = requests.request(
        method,
        api_url,
        auth=(config.signalwire_project_id, config.signalwire_api_token),
        timeout=30,
        **kwargs,
    )
    if not response.ok:
        try:
            error_detail = json.dumps(response.json(), ensure_ascii=True)
        except ValueError:
            error_detail = response.text.strip()
        if not error_detail:
            error_detail = response.reason
        raise RuntimeError(
            f"SignalWire {method} {resource} failed with HTTP "
            f"{response.status_code}: {error_detail[:1500]}"
        )
    return response.json()


def _discord_post(config: Config, url: str, payload: Mapping[str, str], operation: str) -> dict[str, Any]:
    import requests

    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bot {config.discord_bot_token}"},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Discord {operation} request failed") from exc
    if not response.ok:
        try:
            error_detail = json.dumps(response.json(), ensure_ascii=True)
        except ValueError:
            error_detail = response.text.strip()
        if not error_detail:
            error_detail = response.reason
        raise RuntimeError(
            f"Discord {operation} failed with HTTP {response.status_code}: "
            f"{error_detail[:1500]}"
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Discord {operation} returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Discord {operation} returned an invalid response")
    return result


def send_discord_dm(config: Config, user_id: str, message: str) -> str:
    channel = _discord_post(
        config,
        "https://discord.com/api/v10/users/@me/channels",
        {"recipient_id": user_id},
        "DM channel creation",
    )
    channel_id = channel.get("id")
    if not channel_id:
        raise RuntimeError("Discord did not return a DM channel ID")
    result = _discord_post(
        config,
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        {"content": message},
        "direct message",
    )
    message_id = result.get("id")
    if not message_id:
        raise RuntimeError("Discord did not return a message ID")
    return str(message_id)


def send_transcription_dm(config: Config, profile: RecipientProfile, transcript: str) -> int:
    content = transcript or "(No speech was transcribed.)"
    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = min(start + 1800, len(content))
        if end < len(content):
            boundary = content.rfind(" ", start + 1, end + 1)
            if boundary > start:
                end = boundary
        chunks.append(content[start:end])
        start = end

    for index, chunk in enumerate(chunks, start=1):
        message = f"Transcription test ({index}/{len(chunks)}):\n{chunk}"
        send_discord_dm(config, profile.discord_user_id, message)
    return len(chunks)


def wait_for_call_completion(
    config: Config,
    call_sid: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        call = signalwire_request(config, "GET", f"Calls/{call_sid}.json")
        status = str(call.get("status", "")).lower()
        if status in TERMINAL_CALL_STATUSES:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the SignalWire call to finish")
        time.sleep(interval_seconds)


def wait_for_recording(
    config: Config,
    call_sid: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = signalwire_request(
            config,
            "GET",
            "Recordings.json",
            params={"CallSid": call_sid, "PageSize": 1},
        )
        recordings = result.get("recordings", [])
        if recordings:
            recording = recordings[0]
            status = str(recording.get("status", "")).lower()
            if status == "completed":
                return recording
            if status == "absent":
                raise RuntimeError("SignalWire did not produce a call recording")
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the SignalWire recording")
        time.sleep(interval_seconds)


def download_recording(recording: Mapping[str, Any], config: Config, destination: Path) -> None:
    import requests

    recording_sid = recording.get("sid")
    if not recording_sid:
        raise RuntimeError("SignalWire recording did not include a recording SID")
    recording_url = (
        f"{config.signalwire_space_url}/api/laml/2010-04-01/Accounts/"
        f"{config.signalwire_project_id}/Recordings/{recording_sid}.{config.recording_format}"
    )
    for attempt in range(RECORDING_DOWNLOAD_ATTEMPTS):
        response = requests.get(
            recording_url,
            auth=(config.signalwire_project_id, config.signalwire_api_token),
            timeout=60,
        )
        if response.status_code == 404:
            if attempt < RECORDING_DOWNLOAD_ATTEMPTS - 1:
                time.sleep(RECORDING_DOWNLOAD_RETRY_DELAY_SECONDS)
                continue
            raise RuntimeError(
                f"SignalWire recording {recording_sid} media was unavailable (HTTP 404) "
                f"after {RECORDING_DOWNLOAD_ATTEMPTS} attempts. Check that the recording "
                "has completed and its media is accessible in SignalWire."
            )
        response.raise_for_status()
        destination.write_bytes(response.content)
        return


class WhisperTranscriber:
    @staticmethod
    def _load_model_class() -> Any:
        try:
            from faster_whisper import WhisperModel
        except ModuleNotFoundError as error:
            if error.name != "faster_whisper":
                raise
            raise RuntimeError(
                "Transcription requires faster-whisper. Install project dependencies "
                "with `python -m pip install -r requirements.txt` before placing a call."
            ) from error
        return WhisperModel

    @classmethod
    def check_dependency(cls) -> None:
        cls._load_model_class()

    def __init__(self, model_size: str) -> None:
        model_class = self._load_model_class()
        self._model = model_class(model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio_path: Path, *, initial_prompt: str | None = None) -> str:
        options: dict[str, Any] = {"beam_size": 5}
        if initial_prompt is not None:
            options["initial_prompt"] = initial_prompt
        segments, _ = self._model.transcribe(str(audio_path), **options)
        return " ".join(segment.text.strip() for segment in segments).strip()


class GoogleSpeechTranscriber:
    @staticmethod
    def _load_speech_module() -> Any:
        try:
            from google.cloud import speech
        except ModuleNotFoundError as error:
            if error.name not in {"google", "google.cloud"}:
                raise
            raise RuntimeError(
                "Google Cloud Speech requires google-cloud-speech. Install project "
                "dependencies with `python -m pip install -r requirements.txt`."
            ) from error
        return speech

    @classmethod
    def _create_client(cls) -> tuple[Any, Any]:
        speech = cls._load_speech_module()
        from google.auth.exceptions import DefaultCredentialsError

        try:
            return speech, speech.SpeechClient()
        except DefaultCredentialsError as error:
            raise RuntimeError(
                "Google Cloud credentials are unavailable. Configure "
                "GOOGLE_APPLICATION_CREDENTIALS or Application Default Credentials "
                "before placing a call."
            ) from error

    @classmethod
    def check_dependency(cls) -> None:
        cls._create_client()

    def __init__(self, language_code: str) -> None:
        self._speech, self._client = self._create_client()
        self._language_code = language_code

    def transcribe(self, audio_path: Path, *, speech_hints: list[str] | None = None) -> str:
        config_options: dict[str, Any] = {
            "encoding": self._speech.RecognitionConfig.AudioEncoding.MP3,
            "language_code": self._language_code,
            "model": "phone_call",
            "enable_automatic_punctuation": True,
        }
        phrases = [COLOR_LIST_CUE_PHRASE, *(speech_hints or [])]
        config_options["speech_contexts"] = [
            self._speech.SpeechContext(phrases=phrases, boost=10.0)
        ]
        response = self._client.recognize(
            config=self._speech.RecognitionConfig(**config_options),
            audio=self._speech.RecognitionAudio(content=audio_path.read_bytes()),
        )
        return " ".join(
            result.alternatives[0].transcript.strip()
            for result in response.results
            if result.alternatives
        ).strip()


class ElevenLabsSpeechTranscriber:
    MODEL_ID = "scribe_v3"

    @staticmethod
    def check_dependency(api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("ELEVENLABS_API_KEY is required for ElevenLabs transcription")

    def __init__(self, api_key: str, language_code: str) -> None:
        self._api_key = api_key
        self._language_code = language_code

    def transcribe(self, audio_path: Path) -> str:
        import requests

        try:
            with audio_path.open("rb") as audio_file:
                response = requests.post(
                    "https://api.elevenlabs.io/v1/speech-to-text",
                    headers={"xi-api-key": self._api_key},
                    data={
                        "model_id": self.MODEL_ID,
                        "language_code": self._language_code,
                    },
                    files={"file": (audio_path.name, audio_file)},
                    timeout=60,
                )
        except requests.RequestException as error:
            raise RuntimeError("ElevenLabs Speech-to-Text request failed") from error
        if not response.ok:
            raise RuntimeError(
                f"ElevenLabs Speech-to-Text failed with HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError("ElevenLabs returned invalid JSON") from error
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("ElevenLabs returned an invalid transcription response")
        return result["text"].strip()


def recording_to_pcm_wav(audio_path: Path) -> bytes:
    import av

    output = io.BytesIO()
    with av.open(str(audio_path)) as container:
        if not container.streams.audio:
            raise RuntimeError("The SignalWire recording contains no audio stream")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            for frame in container.decode(audio=0):
                for resampled_frame in resampler.resample(frame):
                    wav_file.writeframes(resampled_frame.to_ndarray().tobytes())
            for resampled_frame in resampler.resample(None):
                wav_file.writeframes(resampled_frame.to_ndarray().tobytes())
    return output.getvalue()


class AzureSpeechTranscriber:
    def __init__(self, key: str, region: str, language_code: str) -> None:
        self._key = key
        self._region = region
        self._language_code = language_code

    @staticmethod
    def check_dependency(key: str, region: str) -> None:
        if not key:
            raise ValueError("AZURE_SPEECH_KEY is required for Azure transcription")
        if not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError(
                "AZURE_SPEECH_REGION must be an Azure region name such as 'eastus'"
            )
        try:
            import av
        except ModuleNotFoundError as error:
            if error.name != "av":
                raise
            raise RuntimeError(
                "Azure audio conversion requires PyAV. Install project dependencies "
                "with `python -m pip install -r requirements.txt`."
            ) from error
        av.AudioResampler(format="s16", layout="mono", rate=16000)

    def transcribe(self, audio_path: Path) -> str:
        import requests

        url = (
            f"https://{self._region}.stt.speech.microsoft.com/"
            "speech/recognition/conversation/cognitiveservices/v1"
        )
        try:
            response = requests.post(
                url,
                params={"language": self._language_code, "format": "simple"},
                headers={
                    "Ocp-Apim-Subscription-Key": self._key,
                    "Content-Type": "audio/wav",
                },
                data=recording_to_pcm_wav(audio_path),
                timeout=60,
            )
        except requests.RequestException as error:
            raise RuntimeError("Azure Speech transcription request failed") from error
        if not response.ok:
            raise RuntimeError(
                f"Azure Speech transcription failed with HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError("Azure Speech returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("Azure Speech returned an invalid response")
        status = result.get("RecognitionStatus")
        if status == "NoMatch":
            return ""
        if status != "Success" or not isinstance(result.get("DisplayText"), str):
            raise RuntimeError(
                f"Azure Speech did not transcribe the recording: "
                f"{result.get('RecognitionStatus', 'unknown status')}"
            )
        return result["DisplayText"].strip()


def run(
    config: Config,
    *,
    dry_run: bool = False,
    discord_test: bool = False,
    transcription_test: bool = False,
    transcription_dm_test: bool = False,
    color_test: str | None = None,
    color_list_test: bool = False,
) -> int:
    if transcription_test and transcription_dm_test:
        raise ValueError("Choose either local transcription output or Discord transcription DM")
    if color_list_test and (
        dry_run
        or discord_test
        or transcription_test
        or transcription_dm_test
        or color_test is not None
    ):
        raise ValueError("A color-list test cannot be combined with other run modes")
    if color_test is not None:
        color_test = " ".join(color_test.split())
        if not color_test:
            raise ValueError("A color is required for a color test")
        if dry_run or discord_test or transcription_dm_test or color_list_test:
            raise ValueError("A color test cannot be combined with other run modes")
    if transcription_dm_test:
        if not config.discord_bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is required to send the transcription by Discord DM")
        if len(config.profiles) != 1:
            raise ValueError("A transcription DM test must have exactly one recipient profile")

    if discord_test:
        for profile in config.profiles:
            message_id = send_discord_dm(
                config,
                profile.discord_user_id,
                "UA Dialer Discord bot test",
            )
            print(f"Discord test message sent to {profile.name}: {message_id}")
        return 0

    twiml = build_twiml(config)
    if dry_run:
        print("Dry run: no call or Discord message was sent.")
        print(f"Call destination: {config.call_to_number}")
        print(f"DTMF digits: {config.dtmf_digits}")
        print(f"Listening window: {config.listen_duration_seconds} seconds")
        print(f"Call flow generated: {'yes' if bool(twiml) else 'no'}")
        return 0

    if config.transcription_provider == "elevenlabs":
        ElevenLabsSpeechTranscriber.check_dependency(config.elevenlabs_api_key)
    elif config.transcription_provider == "google":
        GoogleSpeechTranscriber.check_dependency()
    elif config.transcription_provider == "azure":
        AzureSpeechTranscriber.check_dependency(
            config.azure_speech_key,
            config.azure_speech_region,
        )
    else:
        WhisperTranscriber.check_dependency()

    call = signalwire_request(
        config,
        "POST",
        "Calls.json",
        data={
            "To": config.call_to_number,
            "From": config.signalwire_from_number,
            "Twiml": twiml,
            "Record": "true",
            "RecordingChannels": "mono",
            "RecordingTrack": "inbound",
        },
    )
    call_sid = call.get("sid")
    if not call_sid:
        raise RuntimeError("SignalWire did not return a call SID")
    print(f"SignalWire call created: {call_sid}")

    call_status = wait_for_call_completion(
        config,
        call_sid,
        timeout_seconds=config.poll_timeout_seconds,
        interval_seconds=config.poll_interval_seconds,
    )
    if call_status != "completed":
        print(f"Call ended without a successful completion: {call_status}", file=sys.stderr)
        return 1

    recording = wait_for_recording(
        config,
        call_sid,
        timeout_seconds=config.poll_timeout_seconds,
        interval_seconds=config.poll_interval_seconds,
    )
    recording_path = Path(f"recording.{config.recording_format}")
    try:
        download_recording(recording, config, recording_path)
        if config.transcription_provider == "elevenlabs":
            transcript = ElevenLabsSpeechTranscriber(
                config.elevenlabs_api_key,
                config.elevenlabs_language_code,
            ).transcribe(recording_path)
        elif config.transcription_provider == "google":
            transcript = GoogleSpeechTranscriber(config.google_speech_language_code).transcribe(
                recording_path,
                speech_hints=color_transcription_hints(config),
            )
        elif config.transcription_provider == "azure":
            transcript = AzureSpeechTranscriber(
                config.azure_speech_key,
                config.azure_speech_region,
                config.azure_speech_language_code,
            ).transcribe(recording_path)
        else:
            transcript = WhisperTranscriber(config.whisper_model).transcribe(
                recording_path,
                initial_prompt=color_transcription_prompt(config),
            )
    finally:
        recording_path.unlink(missing_ok=True)

    if transcription_test or transcription_dm_test or color_test is not None or color_list_test:
        if not color_list_test:
            print("Full transcript:")
            print(transcript if transcript else "(No speech was transcribed.)")
        if color_test is not None:
            detected = find_detected_colors(transcript, (color_test,))
            result = "was detected" if detected else "was not detected"
            print(f"Color check: {color_test!r} {result}.")
        if color_list_test:
            transcript_suffix = text_after_color_list_cue(transcript)
            if transcript_suffix is None:
                print(f"Color cue phrase {COLOR_LIST_CUE_PHRASE!r} was not found.")
            else:
                colors_by_key: dict[str, str] = {}
                for profile in config.profiles:
                    for color in profile.colors:
                        colors_by_key.setdefault(color.casefold(), color)
                detected = find_detected_colors(
                    transcript_suffix,
                    tuple(colors_by_key.values()),
                    config.color_aliases,
                )
                if detected:
                    print(f"Colors detected: {', '.join(detected)}.")
                else:
                    print("No followed colors were detected after the cue phrase.")
        if transcription_dm_test:
            profile = config.profiles[0]
            message_count = send_transcription_dm(config, profile, transcript)
            print(
                f"Full transcript sent to {profile.name} in "
                f"{message_count} Discord message(s)."
            )
        return 0

    print("Configured profile colors:")
    for profile in config.profiles:
        print(f"  {profile.name}: {', '.join(profile.colors)}")
    if config.color_aliases:
        aliases = ", ".join(
            f"{alias.casefold()} -> {color.casefold()}"
            for alias, color in config.color_aliases
        )
        print(f"Configured color aliases: {aliases}")

    transcript_suffix = text_after_color_list_cue(transcript)
    if transcript_suffix is None:
        print(
            f"Color cue phrase {COLOR_LIST_CUE_PHRASE!r} was not found; "
            "no notifications were sent.",
            file=sys.stderr,
        )
        return 1

    notification_count = 0
    for profile in config.profiles:
        matched_colors = find_detected_colors(
            transcript_suffix,
            profile.colors,
            config.color_aliases,
        )
        if not matched_colors:
            message = "Your color was not called."
        else:
            message = f"Today's color(s) you follow: {', '.join(matched_colors)}."
        if len(message) > 2000:
            raise ValueError(f"Discord notification for {profile.name} exceeds 2000 characters")
        message_id = send_discord_dm(config, profile.discord_user_id, message)
        if matched_colors:
            print(f"Detected {', '.join(matched_colors)}; DM sent to {profile.name}: {message_id}")
        else:
            print(f"No followed color was detected; DM sent to {profile.name}: {message_id}")
        notification_count += 1

    if notification_count == 0:
        print("No recipient profiles are configured; no Discord messages were sent.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and generate the call flow without making a call",
    )
    run_mode.add_argument(
        "--discord-test",
        action="store_true",
        help="Send a test DM to every configured user without placing a call",
    )
    run_mode.add_argument(
        "--transcription-test",
        action="store_true",
        help="Place a real call and print its full transcript without sending DMs",
    )
    parser.add_argument("--call-to-number", help="Override CALL_TO_NUMBER for this run")
    parser.add_argument("--dtmf-digits", help="Override DTMF_DIGITS for this run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        overrides = {
            name: getattr(args, name)
            for name in ("call_to_number", "dtmf_digits")
        }
        config = Config.from_environment(
            dry_run=args.dry_run,
            discord_test=args.discord_test,
            transcription_test=args.transcription_test,
            overrides=overrides,
        )
        return run(
            config,
            dry_run=args.dry_run,
            discord_test=args.discord_test,
            transcription_test=args.transcription_test,
        )
    except Exception as exc:
        print(f"Dialer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())