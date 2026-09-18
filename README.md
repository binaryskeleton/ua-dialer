# ua-dialer

The complete project lives in this folder. Run all commands from here.

## Telegram notifications

1. Create a bot with `@BotFather` and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Send `/start` to the bot from the destination Telegram account.
3. Get that conversation's chat ID and set it as `TELEGRAM_CHAT_ID`.
4. Copy `.env.example` to `.env` and fill in the values once. The dialer loads `.env` automatically, so you do not need to run `setup.ps1` each time.
5. Validate with `python dialer.py --dry-run`.

When the transcript does not contain `KEYWORD`, the dialer sends the notification through the Telegram Bot API instead of SMS.

Keep `.env` private. It contains API keys and is excluded from Git. Install dependencies with `python -m pip install -r requirements.txt` if needed.