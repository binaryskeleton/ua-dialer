"""Place a recorded SignalWire call and notify a Discord channel when a keyword is transcribed."""

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
KEYWORD = "yellow"

load_dotenv(Path(__file__).with_name(".env"), override=True)


@dataclass(frozen=True)
class Config:
    signalwire_project_id: str
    signalwire_api_token: str
    signalwire_space_url: str
    signalwire_from_number: str
    discord_webhook_url: str
    call_to_number: str
    dtmf_digits: str
    whisper_model: str
    discord_message: str
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
        overrides: Mapping[str, str] | None = None,
    ) -> "Config":
        values = {
            "signalwire_project_id": os.getenv("SIGNALWIRE_PROJECT_ID", ""),
            "signalwire_api_token": os.getenv("SIGNALWIRE_API_TOKEN", ""),
            "signalwire_space_url": os.getenv("SIGNALWIRE_SPACE", ""),
            "signalwire_from_number": os.getenv("SIGNALWIRE_FROM_NUMBER", ""),
            "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
            "call_to_number": os.getenv("CALL_TO_NUMBER", ""),
            "dtmf_digits": os.getenv("DTMF_DIGITS", ""),
            "whisper_model": os.getenv("WHISPER_MODEL", "base"),
            "discord_message": os.getenv("DISCORD_MESSAGE", "The word yellow was spoken."),
        }
        if overrides:
            values.update({name: value for name, value in overrides.items() if value is not None})
        required = ["discord_webhook_url"] if discord_test else ["call_to_number", "dtmf_digits"]
        if not dry_run and not discord_test:
            required.extend([
                "signalwire_project_id",
                "signalwire_api_token",
                "signalwire_space_url",
                "signalwire_from_number",
                "discord_webhook_url",
            ])
        missing = [name for name in required if not values[name]]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if values["dtmf_digits"] and not re.fullmatch(r"[0-9*#A-Da-dw]+", values["dtmf_digits"]):
            raise ValueError("DTMF_DIGITS may contain only digits, *, #, A-D, or w pauses")
        if values["discord_webhook_url"]:
            webhook_url = urlparse(values["discord_webhook_url"])
            if (
                webhook_url.scheme != "https"
                or webhook_url.netloc not in {"discord.com", "discordapp.com"}
                or not re.fullmatch(r"/api/webhooks/\d+/[^/]+/?", webhook_url.path)
                or webhook_url.query
                or webhook_url.fragment
            ):
                raise ValueError("DISCORD_WEBHOOK_URL must be a Discord webhook HTTPS URL")
        if not values["discord_message"].strip() or len(values["discord_message"]) > 2000:
            raise ValueError("DISCORD_MESSAGE must contain between 1 and 2000 characters")
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

        listen_duration_seconds = _positive_int_from_env("LISTEN_DURATION_SECONDS", 60)
        if listen_duration_seconds > 400:
            raise ValueError("LISTEN_DURATION_SECONDS must not exceed 600")
        return cls(
            **values,
            poll_interval_seconds=_positive_int_from_env("POLL_INTERVAL_SECONDS", 15),
            poll_timeout_seconds=_positive_int_from_env("POLL_TIMEOUT_SECONDS", 1800),
            listen_duration_seconds=listen_duration_seconds,
            recording_format=os.getenv("RECORDING_FORMAT", "mp3"),
        )


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


def keyword_found(transcript: str) -> bool:
    escaped_keyword = re.escape(KEYWORD)
    return re.search(rf"(?<!\w){escaped_keyword}(?!\w)", transcript, flags=re.IGNORECASE) is not None


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


def send_discord_message(config: Config, message: str) -> str:
    import requests

    try:
        response = requests.post(
            f"{config.discord_webhook_url}?wait=true",
            json={"content": message},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Discord webhook request failed") from exc
    if not response.ok:
        try:
            error_detail = json.dumps(response.json(), ensure_ascii=True)
        except ValueError:
            error_detail = response.text.strip()
        if not error_detail:
            error_detail = response.reason
        raise RuntimeError(
            f"Discord webhook failed with HTTP {response.status_code}: "
            f"{error_detail[:1500]}"
        )
    result = response.json()
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

    recording_uri = str(recording["uri"])
    recording_path = recording_uri.removesuffix(".json")
    recording_url = f"{config.signalwire_space_url}{recording_path}.{config.recording_format}"
    response = requests.get(
        recording_url,
        auth=(config.signalwire_project_id, config.signalwire_api_token),
        timeout=60,
    )
    response.raise_for_status()
    destination.write_bytes(response.content)


class WhisperTranscriber:
    def __init__(self, model_size: str) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio_path: Path) -> str:
        segments, _ = self._model.transcribe(str(audio_path), beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()


def run(config: Config, *, dry_run: bool = False, discord_test: bool = False) -> int:
    if discord_test:
        message_id = send_discord_message(config, "UA Dialer Discord bot test")
        print(f"Discord test message sent: {message_id}")
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

    if keyword_found(transcript):
        message_id = send_discord_message(config, config.discord_message)
        print(f"Keyword detected; Discord bot notification sent: {message_id}")
        return 0

    print("Keyword was not detected; no Discord message was sent.")
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
        help="Send one test message to the configured Discord webhook without placing a call",
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
            overrides=overrides,
        )
        return run(config, dry_run=args.dry_run, discord_test=args.discord_test)
    except Exception as exc:
        print(f"Dialer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())