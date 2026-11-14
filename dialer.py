"""Place a recorded SignalWire call and DM users whose colors are transcribed."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from xml.etree import ElementTree

from dotenv import load_dotenv

TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
RECORDING_DOWNLOAD_ATTEMPTS = 5
RECORDING_DOWNLOAD_RETRY_DELAY_SECONDS = 15

load_dotenv(Path(__file__).with_name(".env"), override=True)


@dataclass(frozen=True)
class RecipientProfile:
    name: str
    discord_user_id: str
    colors: tuple[str, ...]


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
        }
        profiles_json = os.getenv("DIALER_PROFILES_JSON", "")
        if overrides:
            values.update({name: value for name, value in overrides.items() if value is not None})
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
        if (discord_test or (not dry_run and not transcription_test)) and not profiles_json:
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

        profiles = parse_profiles(profiles_json) if profiles_json else ()
        if (discord_test or (not dry_run and not transcription_test)) and not profiles:
            raise ValueError("DIALER_PROFILES_JSON must contain at least one recipient profile")

        listen_duration_seconds = _positive_int_from_env("LISTEN_DURATION_SECONDS", 60)
        if listen_duration_seconds > 400:
            raise ValueError("LISTEN_DURATION_SECONDS must not exceed 600")
        return cls(
            **values,
            profiles=profiles,
            poll_interval_seconds=_positive_int_from_env("POLL_INTERVAL_SECONDS", 15),
            poll_timeout_seconds=_positive_int_from_env("POLL_TIMEOUT_SECONDS", 1800),
            listen_duration_seconds=listen_duration_seconds,
            recording_format=os.getenv("RECORDING_FORMAT", "mp3"),
        )


def parse_profiles(profiles_json: str) -> tuple[RecipientProfile, ...]:
    try:
        raw_profiles = json.loads(profiles_json)
    except json.JSONDecodeError as exc:
        raise ValueError("DIALER_PROFILES_JSON must be valid JSON") from exc
    if not isinstance(raw_profiles, list):
        raise ValueError("DIALER_PROFILES_JSON must be a JSON array")

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


def find_detected_colors(transcript: str, colors: tuple[str, ...]) -> tuple[str, ...]:
    detected: list[str] = []
    for color in colors:
        color_pattern = r"\s+".join(re.escape(part) for part in color.split())
        if re.search(rf"(?<!\w){color_pattern}(?!\w)", transcript, flags=re.IGNORECASE):
            detected.append(color)
    return tuple(detected)


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
    def __init__(self, model_size: str) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio_path: Path) -> str:
        segments, _ = self._model.transcribe(str(audio_path), beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()


def run(
    config: Config,
    *,
    dry_run: bool = False,
    discord_test: bool = False,
    transcription_test: bool = False,
) -> int:
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
        transcript = WhisperTranscriber(config.whisper_model).transcribe(recording_path)
    finally:
        recording_path.unlink(missing_ok=True)

    if transcription_test:
        print("Full transcript:")
        print(transcript if transcript else "(No speech was transcribed.)")
        return 0

    notification_count = 0
    for profile in config.profiles:
        matched_colors = find_detected_colors(transcript, profile.colors)
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