# ua-dialer

The complete project lives in this folder. Run all commands from here.

## SignalWire setup

1. In SignalWire, create a Project and obtain its Project ID, API token, and Space hostname for `SIGNALWIRE_SPACE`. Give the API token Voice and Messaging permissions.
2. Buy or assign a SignalWire phone number with SMS enabled for `SIGNALWIRE_FROM_NUMBER`; use E.164 format for the caller ID, `CALL_TO_NUMBER`, and `OWNER_NUMBER`.
3. Set `OWNER_NUMBER` to the owner's SMS destination in E.164 format.
4. Copy `.env.example` to `.env` and fill in the credentials and call settings. The dialer loads `.env` automatically.
5. Run `python dialer.py --dry-run` to validate the local settings without placing a call.
6. Run `python dialer.py --sms-test` to test SignalWire SMS by itself. This sends one real SMS to `OWNER_NUMBER` and does not place a call.

The call sends the configured DTMF tones, plays `AUDIO_URL`, prompts the called person to speak, then keeps the call open for `LISTEN_DURATION_SECONDS` (default 60). SignalWire records the caller's audio track; local Whisper transcribes the recording and the dialer checks for `KEYWORD`. Set `WHISPER_MODEL` to a model such as `base` (default) or `small`; the model is downloaded on first use and transcription runs on your computer without an OpenAI API key. If the keyword is absent, the dialer sends `SMS_MESSAGE` to `OWNER_NUMBER` using SignalWire's Platform Messaging API. For Platform Free Trial, the `From` must be the trial-assigned SignalWire number and `OWNER_NUMBER` must be the one verified trial mobile number; trial SMS is limited to that destination. `SMS_MESSAGE` defaults to `Its Pee 4 Cops day yay!!!!!` if omitted.

Keep `.env` private. It contains API keys and is excluded from Git. Install dependencies with `python -m pip install -r requirements.txt` if needed. SignalWire call charges and account restrictions may apply.

## Run daily on GitHub

The GitHub Actions workflow runs every day at 11:00 UTC (5:00 a.m. Mountain Daylight Time) and can also be started from the repository's **Actions** tab with **Run workflow**. GitHub cron uses UTC, so this fixed schedule runs at 4:00 a.m. Mountain Standard Time in winter; adjust the cron to 12:00 UTC after daylight saving time ends if you want it to stay at 5:00 a.m. local. Scheduled runs may be delayed by GitHub.

In the repository's **Settings > Secrets and variables > Actions**, add these repository secrets: `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_FROM_NUMBER`, `OWNER_NUMBER`, `CALL_TO_NUMBER`, `DTMF_DIGITS`, `AUDIO_URL`, and `KEYWORD`. Add `SIGNALWIRE_SPACE` as a repository variable. Optional variables are `WHISPER_MODEL` (defaults to `base`), `LISTEN_DURATION_SECONDS` (defaults to `60`), and `SMS_MESSAGE`.

Commit and push the workflow to the repository's default branch. Before enabling the schedule, verify that the recipient has consented to the recurring calls and recordings, and check local call, recording, and SMS requirements and provider charges. Keep credentials in GitHub Actions secrets, never in the repository or workflow file.