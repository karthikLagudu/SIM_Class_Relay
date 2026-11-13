# Class Relay Telegram setup

Run the web app and the Telegram bot against the same Class Relay database. A local installation can receive Telegram updates by direct polling. A hosted installation can receive them at an HTTPS webhook.

## Connect a local bot

1. Install and seed the app using the [quick start](README.md#quick-start) or [full demo guide](DEMO.md#full-reviewer-dataset). If it is running, stop it before changing its settings.
2. Create or select a bot with [BotFather](https://t.me/BotFather). Put the token and username in your local, Git-ignored .env file.

   ```dotenv
   TELEGRAM_MODE=real
   TELEGRAM_UPDATES=polling
   TELEGRAM_BOT_TOKEN=<BotFather token>
   TELEGRAM_BOT_USERNAME=<bot username>
   ```

   Replace both placeholders with the actual values. The username can include a leading @. Keep the token out of screenshots and source control. Rotate it with BotFather if exposed.

3. Check the bot registration with the virtual environment’s Python.

   ```powershell
   python -m scripts.check_telegram_webhook
   ```

   If this Class Relay instance previously used a webhook, keep its existing BASE_URL and TELEGRAM_WEBHOOK_SECRET while switching. Run the following command to clear that registration and retain pending updates. It checks that the registered URL belongs to the configured instance.

   ```powershell
   python -m scripts.check_telegram_webhook --clear-for-polling
   ```

4. Start one Class Relay process from the repository root.

   ```powershell
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8767 --no-access-log
   ```

   Open [Class Relay](http://127.0.0.1:8767/login). Telegram polling works with the fake LLM. Enable OpenAI separately using the [configuration table](README.md#configuration) if you want model-assisted interpretation or dictation.

## Link accounts and verify delivery

1. From a teaching class or the coordinator dashboard, select **Connect my Telegram**. For a student, select **Generate Telegram link** in the class roster. Each person should open their own private link and press **Start** in Telegram.
2. If the web link does not open the app, use **Open in Telegram** or copy the full one-time command shown on the connection page and paste it into the private bot chat. The default expiry is 30 minutes. Regenerating a link invalidates the previous one.
3. Send /menu in Telegram. Students see **My assignments**, feedback, classes, and reminders. Teachers see classwork and review actions. Coordinators see their school and review summaries.
4. Publish a test assignment from the website to a linked student. Check **Telegram notifications** on the assignment page for **Queued**, **Sent**, **Skipped**, or **Failed**. Submit work from Telegram and confirm it appears in the teacher’s **Review queue**. Send feedback from the web and confirm the student receives it.

A class invitation lets an already linked student join a class within their school. A Telegram connection link proves which existing Class Relay account to link. The link contains a one-time token, and the application stores its digest.

## Hosted webhook

1. Configure your HTTPS deployment with TELEGRAM_MODE=real, TELEGRAM_UPDATES=webhook, the bot credentials, and BASE_URL set to the public HTTPS origin. Generate independent values for SESSION_SECRET and TELEGRAM_WEBHOOK_SECRET, for example with python -c "import secrets; print(secrets.token_urlsafe(48))".
2. Start the application and run the commands below. Disable Uvicorn access logging and redact the webhook path in any proxy logs.

   ```powershell
   python -m scripts.set_telegram_webhook
   python -m scripts.check_telegram_webhook
   ```

   Registration sends Telegram the secret header and the matching secret path. The status command redacts the secret and reports queued updates and the last Telegram error.

## Troubleshooting

- **The bot stays silent.** Confirm the server is running, the username matches the bot you opened, and its token is valid. For polling, check that the bot has no registered webhook and that one app process is polling. For a hosted webhook, verify the current HTTPS URL and rerun registration after URL changes.
- **A connection link expired or was used.** Generate a new link and open it with the intended Telegram account. Disconnect an existing link before replacing the connected account.
- **An assignment notification was skipped.** Open the roster and check that the student is linked. Ask the student to unblock the bot if delivery is failing. Failed sends are retried according to the configured attempt limit.
- **A reminder is delayed.** Check its scheduled time, school quiet hours, assignment status, and REMINDER_INTERVAL_SECONDS. Setting that interval to 0 pauses automatic reminder evaluation. Live deliveries already queued still dispatch.
