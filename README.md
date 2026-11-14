# ua-dialer

The complete project lives in this folder. Run all commands from here.

## SignalWire and Discord setup

1. In SignalWire, create a Project and obtain its Project ID, API token, and Space hostname for `SIGNALWIRE_SPACE`. Give the API token Voice permissions.
2. Buy or assign a SignalWire phone number for `SIGNALWIRE_FROM_NUMBER`; use E.164 format for the caller ID and `CALL_TO_NUMBER`.
3. Create a Discord application and bot in the Discord Developer Portal, invite the bot to a server shared with the people who should receive alerts, and copy its bot token into `DISCORD_BOT_TOKEN`. Keep the token secret. Recipients must allow DMs from server members.
4. Enable Developer Mode in Discord and copy each recipient's user ID. Set `DIALER_PROFILES_JSON` to a JSON array of profiles, each with a `name`, `discord_user_id`, and list of followed `colors`. A user can follow more than one color, and the same color can notify multiple users. Color names are matched as whole words, case-insensitively; multi-word names such as `light blue` are supported. If Whisper consistently mishears a color, the profile manager can add a global alias, such as `frown` mapping to `brown`; aliases apply to every profile that follows the mapped color. With aliases configured, the manager exports an object containing `profiles` and a shared `color_aliases` map; the legacy array format remains supported.
5. Copy `.env.example` to `.env` and fill in the credentials and call settings. The dialer loads `.env` automatically.
6. Run `python dialer.py --dry-run` to validate the local call settings without placing a call.
7. Run `python dialer.py --discord-test` to DM every configured recipient. This sends real messages and does not place a call.
8. Run `python dialer.py --transcription-test` to place a real call and print the complete transcript in the local terminal without sending Discord DMs. This requires SignalWire settings but not Discord settings. Avoid running this mode in GitHub Actions because the transcript would be visible in workflow logs.
9. Run `python manage_profiles.py` and choose **Call and DM transcript** to place a local test call and send the full transcript to one selected saved profile. The manager asks you to confirm before calling. This requires local SignalWire settings, a Discord bot token, and a valid saved profile; call charges may apply.

The workflow calls `CALL_TO_NUMBER`, sends the configured DTMF tones, and listens to the answering party for `LISTEN_DURATION_SECONDS` (default 60). SignalWire records the caller's audio track; local Whisper transcribes the recording. The dialer checks the transcript for every color in the recipient profiles, then sends each recipient one Discord DM listing the colors they follow that were detected, or “Your color was not called.” when none were detected. Set `WHISPER_MODEL` to a model such as `base` (default) or `small`; the model is downloaded on first use and transcription runs on the GitHub runner without an OpenAI API key.

Keep `.env` private. It contains API keys, Discord bot token, and recipient information and is excluded from Git. Install dependencies with `python -m pip install -r requirements.txt` if needed. SignalWire call charges and account restrictions may apply.

## Run daily on GitHub

The GitHub Actions workflow schedules runs at 10:00 and 11:00 UTC and proceeds only when it is 4:00 a.m. in `America/Denver`. This keeps the scheduled call at 4:00 a.m. Mountain Time across daylight-saving changes; the other UTC trigger is skipped. You can also start a run at any time from the repository's **Actions** tab with **Run workflow**. GitHub may delay scheduled runs, so the call is not guaranteed to start at exactly 4:00 a.m.

In the repository's **Settings > Secrets and variables > Actions**, add these repository secrets: `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_FROM_NUMBER`, `DISCORD_BOT_TOKEN`, `DIALER_PROFILES_JSON`, `CALL_TO_NUMBER`, and `DTMF_DIGITS`. The profile secret contains each recipient's Discord user ID and colors; never commit real profile data to the repository. Add `SIGNALWIRE_SPACE` as a repository variable. Optional variables are `WHISPER_MODEL` (defaults to `base`) and `LISTEN_DURATION_SECONDS` (defaults to `60`).

## Manage recipients

Run `python manage_profiles.py` from this folder to list, add, or remove people. The manager saves the list to a local `dialer-profiles.json` file, which is excluded from Git. Choose **Upload profiles to GitHub** to send it directly to the repository's `DIALER_PROFILES_JSON` Actions secret, or choose **Generate JSON for GitHub** to copy it yourself. For automatic upload, create a fine-grained personal access token limited to this repository with **Actions: Read and write** permission. The manager prompts for the token without displaying it, encrypts the profile list for GitHub, and does not save the token.

Choose **Call and check a color** to place a real SignalWire call, print the full Whisper transcript locally, and check whether a color you enter appears in that transcript. Choose **Add or update a global color alias** to map a known transcription variant (for example, `frown`) to a followed color (`brown`) for all profiles; then use **Upload profiles to GitHub** to update the Actions secret. The manager asks for confirmation before placing calls; call charges may apply. The color check does not send a Discord DM.

Commit and push the workflow to the repository's default branch. Before enabling the schedule, verify that the person answering the call has consented to recurring calls and recordings, and check call, recording, and provider requirements and charges. Calls play only the configured DTMF tones; no spoken prompt or other audio is played. Keep credentials, the Discord bot token, and recipient profiles in GitHub Actions secrets, never in the repository or workflow file.