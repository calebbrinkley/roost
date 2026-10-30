# Jobs

Roost owns scheduling, execution and notifications.

## API and editor

The existing `/api/jobs` URLs return `jobs` and `runs` wrappers. Create and update accept:

- `name` (1–500 characters), `prompt` (1–16000 characters).
- `schedule`: `{kind:"cron",expr:"0 8 * * 1-5"}`, `{kind:"every",value:2,unit:"hours"}`, or `{kind:"once",at:"2026-12-01T08:00"}`. Every accepts minutes, hours or days; its value is a positive integer up to 525600. Once takes a local ISO datetime without an offset.
- `time_zone`: an IANA name, defaulting to Roost's saved zone or UTC when unset. Changing the display zone later does not change existing jobs.
- `enabled` (default true), optional `model` and `persona` identifiers (null means the current default).
- `web` (default true), `notify` (`always` or `changed`, default always), `allow_memory` (default false).

Requests reject `deliver` and unknown fields. The same sign-in, origin and request-header guards apply as for chat. POST `/{jid}/pause`, `/resume`, `/trigger` retains the existing action URLs. DELETE `/{jid}` needs `{confirm:true}`; DELETE `/api/jobs` needs `{confirm:"DELETE"}`. Resume schedules the next future occurrence, without replaying paused time. Trigger works while paused and does not move a recurring schedule.

The editor offers the three schedule kinds, zone, notification and memory toggles, and default/explicit model and persona pickers. Each run links to its conversation. These conversations stay outside the Messages list and its count, but search includes them. Replies, transcript controls and exports work normally. Delete-all conversations preserves job conversations. Deleting a job cancels its current execution, removes its run records and pending notifications, and preserves its conversations and memory provenance.

## Clock and execution

One app-owned asyncio scheduler ticks once per second and stops before the runner/store close. SQLite's unique `(job_id,scheduled_for)` claim prevents duplicate scheduled runs. A busy job records skipped occurrences, including triggers. Downtime produces only the latest missed occurrence; earlier occurrences are not replayed. Once jobs disable when claimed. Cron supports numeric values, ranges, lists, steps and Sunday 0/7; when both day-of-month and weekday are restricted either may match. A day field starting with `*` (including `*/1`) is unrestricted for this rule: `0 8 */1 * 1` runs Mondays, and `0 8 15 * */1` runs on the 15th. The search horizon is eight years. No cron dependency was added.

Every intervals use the creation time as a local wall-clock anchor: a daily job keeps its local hour across DST. Cron and once also resolve in the job's zone. A gap advances to the first valid local minute; an overlap chooses the first instant, once. Seconds in an every anchor or once datetime are retained outside gaps.

Each accepted run gets a separate private conversation and an initial turn in the existing admission queue. Its prompt, persona/model choice and web setting use normal runner snapshots. Job turns explicitly disable fallback, even on regeneration. Only transient failures before any reply or tool execution retry, after 1, 5 and 15 minutes, strictly before the next occurrence. Retries still waiting for admission at their deadline are cancelled and failed before the next scheduled claim. Editing a schedule or its zone recomputes active retry deadlines. A once run has a 24-hour retry deadline. Partial output is retained and never automatically replayed. On restart, queued turns resume; interrupted running turns are marked failed. A user reply before a pending retry stops that retry rather than overwriting the conversation.

Memory tools are offered only when allowed by both the job and model. Saved facts retain `job_id` and a snapshot of `job_name`; Settings Memory shows “Job: name”. Replies to a run use normal chat memory policy and are not attributed to the scheduled job.

## Notifications and persistence

Migration 7 (after attachments' migration 6) adds jobs, job_runs, push_outbox, a conversation marker and nullable memory provenance.Empty downgrade removes these additions. Populated jobs, outbox, run conversations or tagged memories cause an atomic refusal: preserve/export and explicitly remove that data before downgrading. Nothing is silently discarded.

Run settlement and its result commit with turn completion, so notification processing cannot block the next occurrence. A null changed flag records pending notification work across restarts; the changed flag and notification enqueue then commit together. Push payloads use the job name, the result's first line and `/?c=<conversation_id>`. The stable run identity deduplicates enqueue. The outbox persists payload, pending recipients, attempts and next retry; successes on one device are not repeated when another fails. Deleted/expired subscriptions are removed from the pending set. A notification with no subscriptions is settled without delivery. Pending notifications retry indefinitely at 1, 5, then 15-minute intervals. No subscriptions or model credentials are stored in the outbox.

Delivery is at least once: a crash after a push service accepts a payload but before SQLite records success can repeat it. The stable notification tag lets the browser replace that notification. Old settled outbox rows are retained. Ordinary chat completion push also enqueues on this outbox, retaining presence suppression. Its enqueue runs in the runner completion callback; job enqueue is recovered from the persisted run even if the process stops immediately after generation.

For changed-only notifications the first completed run always notifies. Later runs compare against the preceding completed result with one primary-model, non-streaming call, thinking off, no tools. `SAME` suppresses push; `CHANGED` sends it. A failed or malformed comparison conservatively notifies. The run and its changed flag are saved either way. Comparison runs independently of scheduling, with a 30-second limit, after the job is free to run again.

## Verification

`tests/test_jobs.py` uses an injectable fake clock and loopback provider, covering parser/DST, intervals, once, claims, catch-up, overlap, trigger/pause/resume/delete, retries, memory provenance, change comparison, hidden/searchable run conversations, replies, migrations, validation/auth and durable partial-delivery retries after reopening SQLite.
