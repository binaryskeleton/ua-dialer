# ua-dialer

## Telegram notifications

1. Create a bot with `@BotFather` and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Send `/start` to the bot from the destination Telegram account.
3. Get that conversation's chat ID and set it as `TELEGRAM_CHAT_ID`.
4. Run `setup.ps1` to save the configuration, then validate with `python dialer.py --dry-run`.

When the transcript does not contain `KEYWORD`, the dialer sends the notification through the Telegram Bot API instead of SMS.