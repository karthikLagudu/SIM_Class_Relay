# Class Relay demo

## Full reviewer dataset

The full demo uses fictional accounts, synthetic Telegram IDs, and a local SQLite database. It includes three schools, nine classes, twelve teachers, and 75 students. Run these PowerShell commands from the repository root. If you already installed the development dependencies in `.venv`, start at the seed command.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:ENVIRONMENT = 'development'
.\.venv\Scripts\python.exe -m scripts.seed --full-demo

$env:DATABASE_URL = 'sqlite:///./.demo-data/multi-school-demo/class_relay.db'
$env:UPLOAD_DIR = '.demo-data/multi-school-demo/uploads'
$env:DEMO_MODE = 'true'
$env:LLM_MODE = 'fake'
$env:TELEGRAM_MODE = 'log'
$env:TELEGRAM_UPDATES = 'webhook'
$env:TELEGRAM_BOT_USERNAME = 'classroom_demo_bot'
$env:REMINDER_INTERVAL_SECONDS = '0'
$env:QUIET_HOUR_START = '0'
$env:QUIET_HOUR_END = '0'
$env:BASE_URL = 'http://127.0.0.1:8767'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8767 --no-access-log
```

Open [Class Relay on port 8767](http://127.0.0.1:8767). Choose **Show demo accounts**, then **Use this account** to fill the sign-in form. Select **Sign in** to continue. The picker appears with `DEMO_MODE=true` on a seeded development server using the fake LLM. The same local page supports live Telegram when configured in real mode. On startup, live mode clears the seed’s simulated Telegram connections so those accounts can link to the live bot. The expanded seed uses a separate directory so the earlier demo database can remain in use. The upload path above is required for the seeded image preview. With the offline commands above, the background reminder worker is paused and manual reminders can run at any hour.

Running `python -m scripts.seed --full-demo` again preserves all work and original deadlines. Stop the demo server and use the following command if you want to discard the reviewer activity and recalculate all seed deadlines relative to the current run. The reset requires a database previously created by the full seed and resets its application records. Files in the demo upload directory are kept.

```powershell
.\.venv\Scripts\python.exe -m scripts.seed --full-demo --reset
```

### Accounts and class access

Every seeded account uses **`ClassRelayDemo!42`**. The schools use Asia/Kolkata, Asia/Singapore, and Asia/Manila respectively.

| School | Classes | Coordinator |
|---|---|---|
| A · Willowbend Academy (`1`) | 8A Science (`1`), 8B Science (`2`), 8C Lab (`4`) | Mira Sen |
| B · Cedar Harbor School (`2`) | 9H Field Studies (`3`), 9M Mapping (`5`), 9D Coastal Design (`6`) | Mira Sen |
| C · Meridian Grove School (`3`) | 7A Ecology (`7`), 7B Data (`8`), 7C Studio (`9`) | Nell Brooks |

| Account | Email | Workspace and class access | Telegram |
|---|---|---|---|
| Mira Sen | `mira@willowbend.example` | Coordinates Willowbend and Cedar Harbor. Teaches 8A only. | Linked |
| Arun Vale | `arun@willowbend.example` | Teacher for 8A and 8B | Linked |
| Tessa Lin | `tessa@willowbend.example` | Teacher for 8B | Linked |
| Evan Sol | `evan@willowbend.example` | Unassigned teacher at Willowbend | Linked |
| Nell Brooks | `nell@cedarharbor.example` | Coordinates Meridian Grove. Teaches 9H at Cedar Harbor. | Linked |
| Jonah Reed | `jonah@cedarharbor.example` | Teacher for 9H and 9M | Linked |
| Luca Kim | `luca@cedarharbor.example` | Teacher for 9D | Linked |
| Pia Chen | `pia@cedarharbor.example` | Unassigned teacher at Cedar Harbor | Linked |
| Sam Finch | `sam@meridiangrove.example` | Teacher for 7A and 7C | Linked |
| Amina Cole | `amina@meridiangrove.example` | Teacher for 7A | Linked |
| Eli Vale | `elivale@meridiangrove.example` | Teacher for 7B | Linked |
| Rose Nash | `rose@meridiangrove.example` | Teacher for 7C | Linked |
| Isha Rao | `isha@willowbend.example` | Student in 8A and 8B | Linked |
| Dev Malik | `dev@willowbend.example` | Student in 8A | Unlinked |
| Leena Das | `leena@willowbend.example` | Student in 8A | Linked |
| Rafi Khan | `rafi@willowbend.example` | Student in 8A | Linked |
| Nila Roy | `nila@willowbend.example` | Student in 8A | Unlinked |
| Kavi Bose | `kavi@willowbend.example` | Student in 8A | Linked |
| Meera Shah | `meera@willowbend.example` | Student in 8A | Linked |
| Sana Paul | `sana@willowbend.example` | Student in 8B | Unlinked |
| Tara Iyer | `tara@willowbend.example` | Student in 8B | Linked |
| Omar Dey | `omar@willowbend.example` | Student in 8B | Unlinked |
| Rin Mathew | `rin@willowbend.example` | Student in 8B | Linked |
| Kit Avery | `kit@cedarharbor.example` | Student in 9H | Linked |
| Noa Quinn | `noa@cedarharbor.example` | Student in 9H | Unlinked |
| Eli Finch | `eli@cedarharbor.example` | Student in 9H | Linked |

The remaining student logins use the same password. Willowbend uses `willow01`–`willow14` at `willowbend.example` (8A 01–04, 8B 05–08, 8C 09–14). Cedar Harbor uses `cedar01`–`cedar22` at `cedarharbor.example` (9H 01–06, 9M 07–14, 9D 15–22). Meridian Grove uses `grove01`–`grove25` at `meridiangrove.example` (7A 01–09, 7B 10–17, 7C 18–25). Every third generated student starting at `01` is linked.

### Manage two schools

1. Sign in as Mira. In Willowbend, open **School settings**, **Teacher directory**, and **Student directory**. The overview shows four teachers, 25 students, and three classes. Grade 8C Lab has no assigned teacher and has a submission awaiting review. As coordinator, open **Classwork** to see it.
2. Create a class, add a teacher, and add a student from **Student directory**. Enroll the student in the new class and create an invitation from that class. For a transfer or removal, review the confirmation with the school, source class, and destination before saving.
3. From the class or a confirmation page, use **Active school** to switch to Cedar Harbor. Its overview has the same starting totals, its own teaching teams and work, and the expired `OLDMAP9` invitation in 9M. The switch opens Cedar Harbor’s overview with fresh filters and selections. Repeat class, teacher, and student setup there using distinct demo emails.
4. Sign in as Nell to see Meridian Grove in the Coordinator workspace. Switch to **Teacher** to open Cedar Harbor’s 9H. Nell’s Cedar Harbor teaching access permits reviewing Kit’s submission in that class. Meridian Grove’s Coordinator access permits managing its classes.

### Reviewer walkthrough

1. **Onboard a class.** Sign in as Mira. Open 8A to see Mira and Arun under **Assigned teachers** and Leena and Dev’s different link statuses. In 8B, `WILLOW8B` is active and `OLDWILLOW8B` is expired. Return to the school overview, create **Grade 8 Demo Lab**, and add **Ava Cole** (`ava@willowbend.example`) as its teacher with the demo password. Open the new class, enroll **Zoya Vale** (`zoya@willowbend.example`) with the same temporary password, and generate a class invitation. Switch to live Telegram to generate Zoya’s private connection link.
2. **Switch roles and classes.** Mira can use the **Teacher** switcher and sees only 8A. Sign in as Arun to switch between 8A and 8B using **Active class**. Check **Students** and **Submissions** in each class. 8A includes Dev and Leena, 8B includes Sana and Tara, and Isha is in both. In 8A, **Rain gauge observations** has Leena’s image awaiting review, Rafi’s revision request, and Meera’s completed response. Open Leena’s image preview to see the synthetic rainfall chart. **Cloud cover notes** is already overdue for Kavi. **Pond habitat poster** is cancelled in 8B.
3. **Publish and reschedule.** As Arun in 8B, open the **Soil texture lab** draft. Confirm Isha, Tara, and Sana as recipients and check the IST deadline before publishing. After publishing, change the deadline to a future school-local time different from the original. Under **Notifications**, check the cancelled original reminder schedule and the replacement schedule. Linked recipients have **Simulated** deliveries. Sana’s delivery is **Skipped** because she is unlinked. To try fake LLM interpretation, open **New assignment**, enter `Complete the leaf sketch by tomorrow evening`, and save a draft. Check the proposed deadline before publishing.
4. **Submit, revise, and complete.** Sign in as Isha, filter by 8B, and open **Field sketch practice**. Submit a written description and attach a small image. As Arun, open **Needs review** from **Your classes** and find Isha’s work. As Mira in Willowbend, open **School submissions** and filter by 8B, the assignment, Isha, and **Awaiting review**. Both detail pages show the same text and image. As Mira, enter feedback such as `Label a second feature and explain its purpose` and choose **Request revision**. As Isha, read the feedback and resubmit. Both staff views show the latest attempt and link to the original. As Arun, open the latest submission and choose **Mark complete**. Mira can find it with the **Complete** filter, and Isha sees both receipts and both reviewers’ notes.
5. **Process reminders.** As Arun in 8A, expand **Telegram delivery** and select **Run reminders for Grade 8A Science**. Open **Watershed vocabulary** and check **Notifications**. Before the assignment is due, Isha gets a **Simulated** blocked-support reminder and Dev gets a **Skipped** silent-due-soon reminder. Dev has yet to acknowledge the assignment. Run it again to verify those reminders are deduplicated. As Jonah, open **Shoreline survey** in 9H to see Kit’s text submission and the preprocessed reminder examples for Eli and Noa. The full demo uses fake LLM wording and the Telegram log ledger, so simulated deliveries stay local.

### Staffing handover

Start with a fresh full demo. As Mira, open **Teacher directory** in Willowbend. Arun supports 19 unique students across classes with 11 and nine enrollments, since Isha is in both. Grade 8A Science is co-taught, and Leena’s image is awaiting review.

1. Add Ava Cole (`ava@willowbend.example`) as an unassigned teacher with the demo password. In 8A, open **Manage teaching team**, clear Arun, select Ava, and leave Mira selected. The preview shows the school, both teams, 11 enrolled students, and one submission awaiting review. Save the confirmed change.
2. Sign in as Ava. Open 8A and complete Leena’s review. As Arun, try the old `/teacher/classes/1/assignments/2` bookmark and `/teacher/submissions/1/file` download. Both return 403, and 8B remains available.
3. Return to Mira’s Coordinator workspace. Open 8A **Classwork & reviews** and **Management activity** to see the original authorship, previous feedback, Ava’s review, and the staffing change.

### School-wide student removal

1. In a disposable full demo, sign in as Mira, open Willowbend’s **Students** directory, and choose **Remove from school** for Isha, who is enrolled in 8A and 8B.
2. Check both class memberships and the history summary on the confirmation page. Confirm the removal.
3. Verify Isha disappears from the school directory. Existing work remains available to authorized staff in submission history, and Isha can no longer sign in without another workspace membership.

### Access checks

These URLs are stable in a fresh full demo. Use the same local origin as the walkthrough.

| Signed in as | Open | Expected result |
|---|---|---|
| Mira | `/teacher?class_id=2`, then `/coordinator/classes/2` | Teaching access returns 403. The coordinator view opens 8B for school management. |
| Arun | `/api/teacher/classes/3` | 403. Cedar Harbor’s class and records are outside Arun’s teaching scope. |
| Mira | `/coordinator?school_id=2` or `/coordinator/classes/3` | Cedar Harbor opens in the Coordinator workspace. |
| Mira | `/coordinator?school_id=3` or `/coordinator/classes/7` | 403. Meridian Grove is outside Mira’s coordinator scope. |
| Nell | `/coordinator/classes/3`, then `/teacher?class_id=3` | Cedar Harbor management returns 403. Nell’s teaching workspace opens 9H. |
| Nell | `/coordinator?school_id=3`, then `/teacher?class_id=7` | Meridian Grove opens in the Coordinator workspace. Its Teacher workspace class returns 403 for Nell. |
| Rafi | `/student/classes/1/assignments/2`, then `/api/student/submissions/1` and `/student/submissions/1/file?download=true` | The assignment shows Rafi’s own text and revision feedback. Leena’s submission details and image download both return 403. |
| Isha | `/student/classes/1/assignments/2` | 403. Isha is enrolled in 8A and was outside the recipients for this assignment. |

## Quick offline acceptance script

Use the [README setup](README.md#quick-start) with `python -m scripts.seed` and a separate fresh database for the original acceptance script. To run that scenario at any hour, set these values in your shell or in the development `.env` before starting the server:

```dotenv
LLM_MODE=fake
TELEGRAM_MODE=log
TELEGRAM_UPDATES=webhook
REMINDER_INTERVAL_SECONDS=0
QUIET_HOUR_START=0
QUIET_HOUR_END=0
```

Use `python -m uvicorn app.main:app --host 127.0.0.1 --port 8767 --no-access-log` in one terminal and `python -m scripts.demo` in another, using the same virtual environment and webhook secret. The script signs in with the seeded credentials and sends representative Telegram updates to localhost. It verifies deadlines, duplicate protection, progress and blockers, reminder decisions, text submission, feedback, and access boundaries.

The output gives the assignment ID. Open `/teacher/assignments/ID` as `teacher@demo.local` and `/student/assignments/ID` as `student1@demo.local`. Stop and restart the server with the same database URL. Reload both URLs and confirm the feedback, final state, and reminder history still appear.

## Real Telegram and OpenAI walkthrough

1. Follow [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) with `TELEGRAM_MODE=real` and `LLM_MODE=real` and seed a fresh database. Sign in as `teacher@demo.local` with `Teacher123!`. Open Grade 8 - A and use **Connect my Telegram**. Press **Start** on the private link with the teacher's Telegram account.
2. Use **Generate Telegram link** for Aarav (`student1@demo.local`) and open it in a second Telegram account. Leave Diya (`student2@demo.local`) unlinked and silent. Her web account still exists and will receive the assignment there.
3. In the teacher chat, send `Give Grade 8-A questions 1–10 from the energy worksheet by tomorrow evening.` Check that the bot confirms the assignment and that Aarav receives the assignment and local deadline. Note its ID.
4. In the teacher chat, send `Move the energy worksheet to Friday at 6 PM.` Use `next Friday` if Friday evening is already the deadline. Show the cancelled jobs and replacement version in the teacher view, then verify the new deadline in the student view. To make the manual reminder due during the demo, edit the deadline in the teacher UI to roughly 75 minutes from the current school-local time. If you are in school quiet hours, the expected result is deferral until 07:00. For a live interview outside the scheduled window, choose an explicit demo configuration with equal quiet-hour start and end values, then restart.
5. In Aarav's chat, reply to the assignment notification with `Got it`. If you send it as a separate message and the bot asks which assignment, reply with the new assignment’s `#ID`. Then send `I've completed around 60%.` and `I'm stuck on question 4.` Leave Diya silent. The teacher dashboard highlights the blocker and the unacknowledged assignment.
6. Click **Run reminders** on the teacher dashboard or use `python -m scripts.run_reminders`. Aarav receives supportive help. Diya's separate silent reminder decision appears as `skipped` because her Telegram is unlinked. Real sends appear as pending until the dispatch worker attempts them. Refresh the assignment page to show each result. A second run does not duplicate processed jobs.
7. As Aarav, send `Here's my homework.` Wait for the bot to ask for the file, then send a photo separately. The teacher assignment page shows the submitted state and an authorized image preview. To test the file-first path, send an unbound photo before the announcement and choose its assignment by `#ID` when prompted. The existing photo is used without a second upload.
8. In the teacher assignment page, enter feedback and select **Complete**. To demonstrate a revision instead, choose **Needs revision**, then submit updated work and complete the latest submission. Aarav receives feedback in Telegram. Sign in at `/student` as `student1@demo.local` with `Student123!` and open the assignment to see the same feedback, submission, reminder history, and final state.
9. As the teacher, visit `/api/teacher/classes/2` in the fresh seed database. The unrelated school is rejected. As Diya, try opening Aarav's `/submissions/SUBMISSION_ID/file` URL and confirm it returns 403. Her own assignment page contains neither his submission nor his feedback.
10. Stop and restart with the same database, then revisit both assignment views to verify persisted state and job history.

For a targeted assignment, choose only Aarav on the class page. Diya then receives neither the assignment nor its reminders and her direct assignment URL is rejected. To onboard her after the demo, generate her private connection link, open it with her own Telegram account, and send `/join CLASS8A` to verify membership. The class invitation cannot claim her account by email.
