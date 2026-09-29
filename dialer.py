"""Place a recorded SignalWire call and notify when a keyword is transcribed."""

from __future__ import annotations

import argparse
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

load_dotenv(Path(__file__).with_name(".env"), override=True)


@dataclass(frozen=True)
class Config:
    signalwire_project_id: str
    signalwire_api_token: str
    signalwire_space_url: str
    signalwire_from_number: str
    owner_number: str
    call_to_number: str
    dtmf_digits: str
    audio_url: str
    keyword: str
    whisper_model: str
    sms_message: str
    poll_interval_seconds: int = 15
    poll_timeout_seconds: int = 1800
    listen_duration_seconds: int = 60
    recording_format: str = "mp3"

    @classmethod
    def from_environment(
        cls,
        *,
        dry_run: bool = False,
        overrides: Mapping[str, str] | None = None,
    ) -> "Config":
        values = {
            "signalwire_project_id": os.getenv("SIGNALWIRE_PROJECT_ID", ""),
            "signalwire_api_token": os.getenv("SIGNALWIRE_API_TOKEN", ""),
            "signalwire_space_url": os.getenv("SIGNALWIRE_SPACE", ""),
            "signalwire_from_number": os.getenv("SIGNALWIRE_FROM_NUMBER", ""),
            "owner_number": os.getenv("OWNER_NUMBER", ""),
            "call_to_number": os.getenv("CALL_TO_NUMBER", ""),
            "dtmf_digits": os.getenv("DTMF_DIGITS", ""),
            "audio_url": os.getenv("AUDIO_URL", ""),
            "keyword": os.getenv("KEYWORD", ""),
            "whisper_model": os.getenv("WHISPER_MODEL", "base"),
            "sms_message": os.getenv("SMS_MESSAGE", "Its Pee 4 Cops day yay!!!!!"),
        }
        if overrides:
            values.update({name: value for name, value in overrides.items() if value is not None})
        required = ["call_to_number", "dtmf_digits", "audio_url", "keyword"]
        if not dry_run:
            required.extend([
                "signalwire_project_id",
                "signalwire_api_token",
                "signalwire_space_url",
                "signalwire_from_number",
                "owner_number",
            ])
        missing = [name for name in required if not values[name]]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if not re.fullmatch(r"[0-9*#A-Da-dw]+", values["dtmf_digits"]):
            raise ValueError("DTMF_DIGITS may contain only digits, *, #, A-D, or w pauses")
        if values["owner_number"] and not re.fullmatch(r"\+[1-9]\d{1,14}", values["owner_number"]):
            raise ValueError("OWNER_NUMBER must be a phone number in E.164 format, such as +15551234567")
        if not values["sms_message"].strip() or len(values["sms_message"]) > 1600:
            raise ValueError("SMS_MESSAGE must contain between 1 and 1600 characters")
        audio_url = urlparse(values["audio_url"])
        if audio_url.scheme != "https" or not audio_url.netloc:
            raise ValueError("AUDIO_URL must be a complete HTTPS URL")
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
    audio = ElementTree.SubElement(response, "Play")
    audio.text = config.audio_url
    prompt = ElementTree.SubElement(response, "Say")
    prompt.text = "The message has ended. Please continue speaking."
    ElementTree.SubElement(response, "Pause", {"length": str(config.listen_duration_seconds)})
    return ElementTree.tostring(response, encoding="unicode", xml_declaration=True)


def keyword_found(transcript: str, keyword: str) -> bool:
    escaped_keyword = re.escape(keyword.strip())
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
    response.raise_for_status()
    return response.json()


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


def run(config: Config, *, dry_run: bool = False) -> int:
    twiml = build_twiml(config)
    if dry_run:
        print("Dry run: no call or SMS was sent.")
        print(f"Call destination: {config.call_to_number}")
        print(f"DTMF digits: {config.dtmf_digits}")
        print(f"Audio URL: {config.audio_url}")
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

    if keyword_found(transcript, config.keyword):
        print("Keyword was found.")
        return 0

    sms = signalwire_request(
        config,
        "POST",
        "Messages.json",
        data={
            "To": config.owner_number,
            "From": config.signalwire_from_number,
            "Body": config.sms_message,
        },
    )
    sms_sid = sms.get("sid")
    if not sms_sid:
        raise RuntimeError("SignalWire did not return an SMS message SID")
    print(f"SMS sent to owner number: {sms_sid}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and generate the call flow without making a call",
    )
    parser.add_argument("--call-to-number", help="Override CALL_TO_NUMBER for this run")
    parser.add_argument("--dtmf-digits", help="Override DTMF_DIGITS for this run")
    parser.add_argument("--keyword", help="Override KEYWORD for this run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        overrides = {
            name: getattr(args, name)
            for name in ("call_to_number", "dtmf_digits", "keyword")
        }
        config = Config.from_environment(dry_run=args.dry_run, overrides=overrides)
        return run(config, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Dialer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())