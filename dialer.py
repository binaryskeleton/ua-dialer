"""Place a recorded Twilio call and notify when a keyword is transcribed."""

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

TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


@dataclass(frozen=True)
class Config:
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    call_to_number: str
    telegram_bot_token: str
    telegram_chat_id: str
    dtmf_digits: str
    audio_url: str
    keyword: str
    openai_api_key: str
    poll_interval_seconds: int = 15
    poll_timeout_seconds: int = 1800
    recording_format: str = "mp3"

    @classmethod
    def from_environment(
        cls,
        *,
        dry_run: bool = False,
        overrides: Mapping[str, str] | None = None,
    ) -> "Config":
        values = {
            "twilio_account_sid": os.getenv("TWILIO_ACCOUNT_SID", ""),
            "twilio_auth_token": os.getenv("TWILIO_AUTH_TOKEN", ""),
            "twilio_from_number": os.getenv("TWILIO_FROM_NUMBER", ""),
            "call_to_number": os.getenv("CALL_TO_NUMBER", ""),
            "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
            "dtmf_digits": os.getenv("DTMF_DIGITS", ""),
            "audio_url": os.getenv("AUDIO_URL", ""),
            "keyword": os.getenv("KEYWORD", ""),
            "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        }
        if overrides:
            values.update({name: value for name, value in overrides.items() if value is not None})
        required = [
            "twilio_from_number",
            "call_to_number",
            "telegram_chat_id",
            "dtmf_digits",
            "audio_url",
            "keyword",
        ]
        if not dry_run:
            required.extend(["twilio_account_sid", "twilio_auth_token", "openai_api_key", "telegram_bot_token"])
        missing = [name for name in required if not values[name]]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        if not re.fullmatch(r"[0-9*#A-Da-dw]+", values["dtmf_digits"]):
            raise ValueError("DTMF_DIGITS may contain only digits, *, #, A-D, or w pauses")
        parsed_url = urlparse(values["audio_url"])
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("AUDIO_URL must be a complete HTTPS URL")
        return cls(
            **values,
            poll_interval_seconds=_positive_int_from_env("POLL_INTERVAL_SECONDS", 15),
            poll_timeout_seconds=_positive_int_from_env("POLL_TIMEOUT_SECONDS", 1800),
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
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    response.play(digits=config.dtmf_digits)
    response.play(config.audio_url)
    response.say("The message has ended. Please continue speaking.")
    return str(response)


def keyword_found(transcript: str, keyword: str) -> bool:
    escaped_keyword = re.escape(keyword.strip())
    return re.search(rf"(?<!\w){escaped_keyword}(?!\w)", transcript, flags=re.IGNORECASE) is not None


def wait_for_call_completion(call: Any, *, timeout_seconds: int, interval_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        call = call.fetch()
        status = str(call.status).lower()
        if status in TERMINAL_CALL_STATUSES:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the Twilio call to finish")
        time.sleep(interval_seconds)


def wait_for_recording(client: Any, call_sid: str, *, timeout_seconds: int, interval_seconds: int) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while True:
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if recordings:
            return recordings[0]
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the Twilio recording")
        time.sleep(interval_seconds)


def download_recording(recording: Any, config: Config, destination: Path) -> None:
    import requests

    recording_uri = str(recording.uri)
    recording_url = f"https://api.twilio.com{recording_uri.rsplit('.', 1)[0]}.{config.recording_format}"
    response = requests.get(
        recording_url,
        auth=(config.twilio_account_sid, config.twilio_auth_token),
        timeout=60,
    )
    response.raise_for_status()
    destination.write_bytes(response.content)


def send_telegram_message(config: Config, message: str) -> str:
    import requests

    response = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        json={"chat_id": config.telegram_chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result}")
    return str(result["result"]["message_id"])


class OpenAITranscriber:
    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def transcribe(self, audio_path: Path) -> str:
        with audio_path.open("rb") as audio_file:
            result = self._client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
            )
        return str(result.text)


def run(config: Config, *, dry_run: bool = False) -> int:
    twiml = build_twiml(config)
    if dry_run:
        print("Dry run: no call or Telegram message was sent.")
        print(f"Call destination: {config.call_to_number}")
        print(f"DTMF digits: {config.dtmf_digits}")
        print(f"Audio URL: {config.audio_url}")
        print(f"TwiML generated: {'yes' if bool(twiml) else 'no'}")
        return 0

    from twilio.rest import Client

    client = Client(config.twilio_account_sid, config.twilio_auth_token)
    call = client.calls.create(
        to=config.call_to_number,
        from_=config.twilio_from_number,
        twiml=twiml,
        record=True,
        recording_channels="dual",
    )
    call_sid = call.sid
    if not call_sid:
        raise RuntimeError("Twilio did not return a call SID")
    print(f"Twilio call created: {call_sid}")

    call_status = wait_for_call_completion(
        call,
        timeout_seconds=config.poll_timeout_seconds,
        interval_seconds=config.poll_interval_seconds,
    )
    if call_status != "completed":
        print(f"Call ended without a successful completion: {call_status}", file=sys.stderr)
        return 1

    recording = wait_for_recording(
        client,
        call_sid,
        timeout_seconds=config.poll_timeout_seconds,
        interval_seconds=config.poll_interval_seconds,
    )
    recording_path = Path(f"recording.{config.recording_format}")
    try:
        download_recording(recording, config, recording_path)
        transcript = OpenAITranscriber(config.openai_api_key).transcribe(recording_path)
    finally:
        recording_path.unlink(missing_ok=True)

    if keyword_found(transcript, config.keyword):
        print("Keyword was found.")
        return 0

    message = "Its Pee 4 Cops day yay!!!!!"
    message_id = send_telegram_message(config, message)
    print(message)
    print(f"Keyword was not found; Telegram message sent: {message_id}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and generate TwiML without making a call",
    )
    parser.add_argument("--call-to-number", help="Override CALL_TO_NUMBER for this run")
    parser.add_argument("--telegram-chat-id", help="Override TELEGRAM_CHAT_ID for this run")
    parser.add_argument("--dtmf-digits", help="Override DTMF_DIGITS for this run")
    parser.add_argument("--keyword", help="Override KEYWORD for this run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        overrides = {
            name: getattr(args, name)
            for name in ("call_to_number", "telegram_chat_id", "dtmf_digits", "keyword")
        }
        config = Config.from_environment(dry_run=args.dry_run, overrides=overrides)
        return run(config, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Dialer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
