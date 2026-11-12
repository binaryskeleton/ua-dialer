# ua-dialer

The complete project lives in this folder. Run all commands from here.

## SignalWire and Discord setup

1. In SignalWire, create a Project and obtain its Project ID, API token, and Space hostname for `SIGNALWIRE_SPACE`. Give the API token Voice permissions.
2. Buy or assign a SignalWire phone number for `SIGNALWIRE_FROM_NUMBER`; use E.164 format for the caller ID and `CALL_TO_NUMBER`.
3. In Discord, create a webhook for the channel where alerts should appear, then copy its webhook URL into `DISCORD_WEBHOOK_URL`. Keep this URL secret: anyone who has it can post to that channel.
4. Copy `.env.example` to `.env` and fill in the credentials and call settings. The dialer loads `.env` automatically.
5. Run `python dialer.py --dry-run` to validate the local settings without placing a call.
6. Run `python dialer.py --discord-test` to post a test message to the configured Discord channel without placing a call.

The call sends the configured DTMF tones, then stays silent while it listens for `yellow` for `LISTEN_DURATION_SECONDS` (default 60). SignalWire records the caller's audio track; local Whisper transcribes the recording and the dialer checks for that word. Set `WHISPER_MODEL` to a model such as `base` (default) or `small`; the model is downloaded on first use and transcription runs on your computer without an OpenAI API key. If `yellow` is detected, the dialer posts `DISCORD_MESSAGE` to the configured Discord channel. The message defaults to `The word yellow was spoken.` The call and transcription flow is unchanged.

Keep `.env` private. It contains API keys and the Discord webhook URL and is excluded from Git. Install dependencies with `python -m pip install -r requirements.txt` if needed. SignalWire call charges and account restrictions may apply.

## Run daily on GitHub

The GitHub Actions workflow schedules runs at 10:00 and 11:00 UTC and proceeds only when it is 4:00 a.m. in `America/Denver`. This keeps the scheduled call at 4:00 a.m. Mountain Time across daylight-saving changes; the other UTC trigger is skipped. You can also start a run at any time from the repository's **Actions** tab with **Run workflow**. GitHub may delay scheduled runs, so the call is not guaranteed to start at exactly 4:00 a.m.

In the repository's **Settings > Secrets and variables > Actions**, add these repository secrets: `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_FROM_NUMBER`, `DISCORD_WEBHOOK_URL`, `CALL_TO_NUMBER`, and `DTMF_DIGITS`. Add `SIGNALWIRE_SPACE` as a repository variable. Optional variables are `WHISPER_MODEL` (defaults to `base`), `LISTEN_DURATION_SECONDS` (defaults to `60`), and `DISCORD_MESSAGE`.

Commit and push the workflow to the repository's default branch. Before enabling the schedule, verify that the recipient has consented to the recurring calls and recordings, and check local call, recording, and provider requirements and charges. Calls play only the configured DTMF tones; no spoken prompt or other audio is played. Keep credentials and the Discord webhook URL in GitHub Actions secrets, never in the repository or workflow file.