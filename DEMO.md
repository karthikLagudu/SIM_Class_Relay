# Interview demo

## Offline run with no API credentials

Use the [README setup](README.md#quick-start) and seed a fresh database. To run the acceptance scenario at any hour, set these values in your shell or in the development `.env` before starting the server:

```dotenv
LLM_MODE=fake
TELEGRAM_MODE=log
REMINDER_INTERVAL_SECONDS=0
QUIET_HOUR_START=0
QUIET_HOUR_END=0
```

Use `python -m uvicorn app.main:app --reload --no-access-log` in one terminal and `python -m scripts.demo` in another, using the same virtual environment and webhook secret. The script signs in with the seeded credentials and sends representative Telegram updates to localhost. It verifies the relative deadline, duplicate protection, persisted deliveries, deadline change, progress and blocker, different manual reminder decisions, text submission, feedback, final status, and teacher/student access boundaries. For another port, pass `--base-url http://127.0.0.1:8766`.

The output gives the assignment ID. Open `/teacher/assignments/ID` as `teacher@demo.local` and `/student/assignments/ID` as `student1@demo.local`. Stop and restart the server with the same database URL. Reload both URLs and confirm the feedback, final state, and reminder history still appear.

## Real Telegram and OpenAI walkthrough

1. Follow [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) with `TELEGRAM_MODE=real` and `LLM_MODE=real` and seed a fresh database. Sign in as `teacher@demo.local` with `Teacher123!`. Open Grade 8 - A and use **Connect my Telegram**. Press **Start** on the private link with the teacher's Telegram account.
2. Use **Generate Telegram link** for Aarav (`student1@demo.local`) and open it in a second Telegram account. Leave Diya (`student2@demo.local`) unlinked and silent. Her web account still exists and will receive the assignment there.
3. In the teacher chat, send `Give Grade 8-A 10 algebra questions due tomorrow evening.` Check that the bot confirms the assignment and that Aarav receives the assignment and local deadline. Note its ID.
4. In the teacher chat, try `Move it to Friday at 6 PM.` Check that both web views show the new deadline. To make the manual reminder due during the demo, edit the deadline in the teacher UI to roughly 75 minutes from the current school-local time. If you are in school quiet hours, the expected result is deferral until 07:00. For a live interview outside the scheduled window, choose an explicit demo configuration with equal quiet-hour start and end values, then restart.
5. In Aarav's chat, send `Got it`, `I'm halfway done`, and `I'm stuck on question 4`. Leave Diya silent. The teacher dashboard highlights the blocker and the unacknowledged assignment.
6. Click **Run reminders** on the teacher dashboard or use `python -m scripts.run_reminders`. Aarav receives supportive help. Diya's separate silent reminder decision appears as `skipped` because her Telegram is unlinked. A second run does not duplicate the processed jobs.
7. As Aarav, send `/submit ID My solution is x = 5.` You can instead attach a photo or document with the caption `/submit ID`. The teacher assignment page shows the submitted work and an authorized preview for supported images.
8. In the teacher assignment page, enter feedback and select **Complete**. Aarav receives feedback in Telegram. Sign in at `/student` as `student1@demo.local` with `Student123!` and open the assignment to see the same feedback, submission, reminder history, and final state.
9. As the teacher, visit `/api/teacher/classes/2` in the fresh seed database. The unrelated school is rejected. As Diya, try opening Aarav's `/submissions/SUBMISSION_ID/file` URL and confirm it returns 403. Her own assignment page contains neither his submission nor his feedback.
10. Stop and restart with the same database, then revisit both assignment views to verify persisted state and job history.

For a targeted assignment, choose only Aarav on the class page. Diya then receives neither the assignment nor its reminders and her direct assignment URL is rejected. To onboard her after the demo, generate her private connection link, open it with her own Telegram account, and send `/join CLASS8A` to verify membership. The class invitation cannot claim her account by email.
