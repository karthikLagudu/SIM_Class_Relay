# Product reasoning

## Identity and onboarding

The classroom account is the canonical identity. The app links a Telegram numeric ID through a 30-minute, one-time token whose SHA-256 digest is stored. Teachers create student links within their class and create their own links from an authenticated class page. A person who messages before linking is guided to their private connection link. Telegram handles and display names play no role in identifying school accounts. A student who has already linked can send `/join CODE` to join a class in their existing school. `/join CODE EMAIL` is accepted if the email matches that authenticated account. Knowing a class code and someone else’s email cannot establish ownership of their identity. The invite checks expiry, active status, and use count.

Coordinators create classes and set up teachers in schools they coordinate. Teaching access comes from an explicit teacher membership for each class. Authorized class staff can pre-create a student with a temporary password, enroll them, and create or disable invites. Temporary passwords are entered by the operator and never displayed again.

## Roles, access, and privacy

Roles come from current school and class memberships. A person can coordinate one school, teach several classes, and share a class with another teacher. Coordinators manage their own schools and can use classwork and review actions from the Coordinator workspace. The Teacher workspace and Telegram teaching commands require a teacher membership in the class. Students require both a student class membership and an assignment target. Download routes apply the same workspace permissions.

Staffing previews show the existing and proposed team for each affected class and its enrollment and review counts. Confirmation rechecks the signed preview within the SQLite write transaction, applies membership changes together, and records the change in the activity log. Archived classes keep their history and stop accepting new work or generating reminders.

Candidate assignment queries are restricted before title matching. An explicit ID, normalized exact title, title prefix, and full match of meaningful query terms are tried in that order. The model may return a numeric reference only if the sender wrote it as `#ID`. An explicit classroom and qualifiers such as “today’s” narrow the lookup before title matching. Zero matches and tied matches are non-mutating clarification outcomes. This avoids the dangerous pattern of choosing globally and filtering afterward.

`NotificationDelivery` carries school, class, and assignment context. Teaching views display delivery records for assigned classes. Coordinators can run due reminders for their own schools from the school overview.

## Lifecycle

`Assignment.status` controls shared draft/assigned/cancelled lifecycle, while every target has an independent `StudentAssignmentState`. One central map rejects invalid transitions. Submission safely walks an assigned/acknowledged state through `in_progress` to `submitted`; feedback moves submitted work to `needs_revision` or `completed`.

Overdue is evaluated immediately before a due reminder job. `assigned`, `acknowledged`, `in_progress`, `blocked`, and `needs_revision` become `overdue` after the deadline, with a `student_assignment_overdue` activity event. Submitted, completed, and cancelled states never become overdue. A blocker before the deadline receives supportive blocker handling; after the deadline the visible status is overdue while its stored blocker reason remains available to the teacher.

## Time and quiet hours

All stored instants are UTC; each school has an IANA timezone. Real LLM parsing receives the current aware instant and school timezone. Fake mode deterministically maps morning to 09:00, afternoon to 15:00, and evening to 18:00 and supports named weekdays. Deterministic validation rejects naive or past deadlines after interpretation.

Quiet hours default to 22:00-07:00 in the school timezone. No reminder is sent in that interval—even if already overdue—because predictable no-send behavior is safer for students than inventing an urgency exception. A due job is moved to the next local 07:00 and remains durable. This makes the eventual delay explicit in the job row.

## Reminder policy

Publishing schedules 24-hour, 2-hour, and post-deadline jobs for each target. When a new deadline is less than two hours away, the short-lead job covers both pre-deadline windows. Every deadline edit advances the schedule version, replaces pending jobs, and notifies the targets. A future extension restores work marked overdue to the recorded previous state. Draft corrections stay private. Publishing creates the student states, reminders, and assignment notification. Instruction clarification and cancellation produce their own audited notifications for published work.

The processor reads current state. It suppresses submitted, completed, or cancelled work and defers eligible jobs during quiet hours. A blocker receives supportive wording at most once per 12 hours. A student with no acknowledgement or activity receives a different reminder from an active student. Recent progress can suppress a 24-hour reminder. Work awaiting revision gets a distinct prompt to review feedback and resubmit. At most one overdue notice is attempted per target and deadline version. The LLM phrases the selected reminder and a provider failure uses a state-specific template. An unlinked student has a recorded skipped delivery that remains visible to their teacher.

## Conversation context

Informal references (“move it”) and separately arriving photos need short-lived state. `ConversationContext` is unique per user/chat and stores an active assignment, pending action, optional payload, and expiry. Thirty minutes is long enough for a normal chat exchange and short enough to reduce stale-action risk. Context is set after an explicit assignment interaction, such as a button, a reply to an assignment notification, or a confirmed choice. New assignment notifications leave any existing focus or pending request intact. Every read checks expiry and every use re-authorizes the class and assignment. Follow-up choices match an ID or exact name, so a reply cannot quietly redirect an ambiguous mutation.

“Here’s my homework” is a `submit_attachment` intent. Once an assignment is selected, the following document or photo is attached there. When a photo arrives first, its Telegram file ID and metadata stay in SQLite while the student chooses from eligible work. Bytes are fetched only after authorization. One attachment can wait per chat, and `/cancel` clears it. A restart preserves unexpired context.

## Risk engine

Risk selection is deterministic. Blocked adds 70, overdue adds 80, revision adds 45, due within 24 hours adds 30, no acknowledgement or activity near the deadline adds 25, and inactivity beyond two days adds 20; recent activity subtracts 10. Scores are clamped to 0-100, submitted/completed/cancelled states are excluded, and items below 25 are omitted. Levels are medium (25-59), high (60-79), and critical (80-100). Only these already-authorized structured facts go to the LLM for concise prose; the model never decides who is at risk.

## Duplicates and failure handling

The webhook reserves the update ID atomically before dispatch. Telegram update IDs, assignment operation keys, submission keys, feedback keys, delivery keys, reminder keys, targets, states, and memberships are database-unique. Deadline keys and `schedule_version` make repeated updates harmless. Callback buttons use the same deduplicated command path.

A valid domain operation is preserved when Telegram fails. The client records `failed`, error type/text, attempt count, and next attempt without recursively trying to report failure through the broken channel. The worker retries failed sends after delays of 1, 5, and 15 minutes as allowed by the configured attempt limit. Retry eligibility checks current linkage, cancellation, and deadline versions. Unlinked recipients are `skipped`; log mode is explicitly `logged`. The SQLite delivery table acts as the outbox. Domain changes and the pending row commit together. Each dispatch rechecks linkage, assignment eligibility, version, and quiet hours. The Bot API call runs outside the writer lock. A two-minute claim lets another cycle resume interrupted work. A remote success whose result was lost locally can still be retried.

LLM calls record operation, actor, input, validated output, confidence, success, and a safe error type. Secrets are never included. Low confidence, invalid structure, or provider errors return clarification/factual fallback instead of mutation. Malformed or unsupported Telegram updates return safe 200-class ignored results so Telegram does not retry forever.

## File safety

Telegram files use `getFile`, bounded HTTP timeouts, a streamed byte limit, UUID storage names, path confinement, SHA-256 hashes, and transport idempotency. Original names are metadata only. Upload storage is not public static content: an authenticated route resolves a `Submission`, applies teacher/student authorization, rechecks that its stored path is below `UPLOAD_DIR`, and then serves it. A production version adds object storage, antivirus/content scanning, signed short-lived URLs, retention, and image transformations.

## SQLite and production migration

SQLite is appropriate for a single-process 2-3 day take-home: foreign keys, WAL, busy timeout, a writer reservation on mutating entry points, and uniqueness constraints provide a runnable persistent slice. It is not a distributed scheduler or multi-writer production database. `create_all` is acceptable for fresh demo/test databases, but production must move to PostgreSQL with Alembic, an outbox, durable queue, leased/distributed workers, object storage, managed identity, secret management, and centralized observability.
