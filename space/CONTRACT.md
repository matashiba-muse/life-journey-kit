# Life Journey viewer contract (Space package)

This contract is the whole agreement between the Life Journey helper
(the producer) and a viewer Space (the consumer). The helper emits a
display object and small deltas; the viewer stores them and renders
them. Any viewer that honours this contract works with the helper,
whatever it looks like.

Transport: the agent (Muse) calls the viewer's actions with JSON
arguments. Action names are lower-case: `synclife`, `syncdelta`,
`getdisplay`, `getlite`, `gethistory`.

## 1. The display object (schema v1)

The helper writes one display object per life and refreshes it after
every operation. It never contains a resume token or a credential.

| Field | Meaning |
|---|---|
| `schema_version` | Always `1`. |
| `life_id` | The life's id, e.g. `life-20261113145148-001-0c77`. |
| `status` | `active`, `paused`, `unknown` or `ended`. |
| `updated_at` | ISO timestamp of the refresh. Volatile (see §5). |
| `engine` | Engine version string, may be null early on. |
| `person` | `{name, sex, age, year, country, place}`. |
| `state` | `{job, education, income, net_worth, health, happiness, knowledge, bonds, partner, children}`. Any value may be null before it exists. |
| `events` | Only the latest step's events, in the same entry shape as `timeline`. Volatile (see §5). |
| `timeline` | The full life history, append-only. Each entry is `[age, text]` or `[age, text, note]`: the optional third element is a short sourced explanation of the event (a fact note naming its source). Render entries in order; a viewer may show the note under its entry, and a reader must not silently drop it. |
| `decision` | The waiting decision `{id, question, about, fact, choices: [{key, label, note}]}`, or null when none is waiting. |
| `focus` | The year's focus options, or null. |
| `sheet` | The spending sheet: a list of item objects, each carrying a unique string `id`. May be empty. |
| `ended` | True once the life has ended. |
| `death` | `{age, cause, place}` when the life has ended, else null. `age` (a number) is required for an ending; `cause` and `place` may be null. A display with `ended: true` but no numeric death age is malformed and is never produced. (The engine's own record names the place `where`; the helper normalizes it to `place` at intake, so a display only ever carries `place`. For an ended life, `person.age` is the death age.) |
| `last_choice` | `{step, choose, reason, decision_id, at, age, question, about, fact, label}` for the newest confirmed choice, or null. **The choice key is in the `choose` field** (fall back to `key` if a future producer renames it). `age`, `question`, `about`, `fact` and `label` mirror the newest `choices` entry and may be null in older records. |
| `choices` | The full decision history, append-only, in step order. Each entry is `{step, age, decision_id, question, about, fact, options, choose, label, reason, scene, at}`: the decision as it was faced — its `question`, the situation (`about`) and fact (`fact`) lines it was presented with, every option that was on the table (`options`, a list of `{key, label}`), the chosen option's `label`, the person's `age` at the time — plus the recorded `reason`. A viewer may show the options whose `key` differs from `choose` as the ones not taken. `scene` is `{timeline_len, state}`: how far the timeline had grown when the decision was faced, and the person's state at that moment in the same shape as the display's `state`. `age`, `question`, `about`, `fact`, `options` and `label` are null in entries recorded before the producer captured them, and `scene` is null there too; a recovered legacy entry may carry a `scene` whose `state` is null (position known, state not). Render fields only when present and fall back to the `choose` key when `label` is null. When an entry has no `about` or `fact`, a viewer may still show the few timeline events immediately before the decision (sliced at `scene.timeline_len`) as its context; never invent situation text for an entry that lacks it. |
| `chapters` | The player's own chapter notes, append-only, in order. Each entry is `{index, from_step, to_step, from_age, to_age, title, summary, at}`: a headline (`title`) and a short `summary` the player wrote to cover the choices from `from_step` to `to_step`; `from_age` and `to_age` are the ages of the first and last covered choices and may be null. A display may omit the array entirely (older producers); treat a missing array as empty. Render a chapter as a divider immediately before the first choice whose `step` equals its `from_step`, showing its ordinal, title, age span when both ages are present, and the full summary. |
| `step_count` | Confirmed steps so far. |
| `counters` | `{step_intents, confirmed_steps, unknown_steps}`. |
| `revision` | Per-life integer, bumped on every confirmed state change. The bump criterion covers every non-volatile field -- state, decision, ending, sheet, focus, chapters, the epilogue, the key date and the usage counters -- so any change a viewer can see advances it. The ordering key for all staleness decisions. |
| `last_incident` | The newest unknown/abandoned-step record, or null. |
| `epilogue` | The player's own closing words for the life, or null. Written by the Muse who played the life, after it ended: `{title, text, written_on, at}` — `title` may be null; `text` is the reflection itself (what living the life was like, never a score); `written_on` is the date it was written (YYYY-MM-DD). A display may omit the field entirely (older producers); treat a missing field as null. When it is set or rewritten, the life's revision advances with it, and it reaches the viewer in the delta's `replace`. Render it on an ended life, after the ending, as its own section (see BUILD-BRIEF.md). |
| `animation` | `{kind, signal, health_trend, scene}`: hints for the viewer, chosen from structured signals only. `kind` is one of `ended`, `walk`, `thinking`, `happy`, `sad`, `idle`. A viewer that ships the bundled pixel-art set composes its stage from these fields plus `person.age` and `person.sex` (see BUILD-BRIEF.md); the data contract itself does not change. |
| `usage` | `{used, quota, this_call}` as the engine reported it. `this_call` is volatile. |
| `key_acquired_on` | The date (YYYY-MM-DD) the player recorded acquiring their API key, or null when unrecorded. A local operational fact set once at setup; it is not engine data and does not change with the life. A viewer may show it in the footer. It is the date only — a display never contains the key itself. When the recorded date is set or changed, the revision of every recorded life advances with it (the date is read from the shared config into every life's display), so the new date reaches each viewer by the ordinary delta path (a display change without a revision advance could be synced neither by delta nor, at the same revision, by full sync). |
| `credit`, `credit_url`, `sources`, `disclaimer` | Attribution and the simulation disclaimer. Always present; the viewer shows them (see §6). |

## 2. Revision and staleness

- A viewer stores at most one current display per life, plus a history
  of accepted displays.
- `synclife` with a display whose `revision` is older than the stored
  revision for the same life is **rejected as stale** and changes
  nothing. A display whose revision **equals** the stored revision is
  accepted as a no-op (answering `already: true`, appending nothing)
  when it is deeply identical to the stored display, and **rejected
  as a conflict** when it differs. This is what makes a lost success
  response recoverable: the agent simply sends the same sync again.
- A delta names its `base_revision` and `target_revision`. A viewer
  applies it only when the stored display for that life is exactly at
  `base_revision`. When the stored revision already equals
  `target_revision`, the delta was applied earlier and its response
  was lost: answer `already: true` and change nothing. Any other
  mismatch rejects the delta, and the agent falls back to a full
  sync.

## 3. The sync protocol

1. **Full sync once**: the agent passes the whole display to
   `synclife`, reads the accepted revision from the response, then
   records the synced snapshot locally
   (`life_api.py delta --ack --revision <accepted revision>`).
2. **Deltas afterwards**: after each step the agent builds a delta
   with `life_api.py delta`, passes it to `syncdelta`, and only after
   the viewer accepted it records the new snapshot with
   `delta --ack --revision <accepted revision>`. The ack is bound to
   the accepted revision; it refuses a mismatch, and the agent then
   verifies with `getlite` and re-syncs. A delta that was never acked
   is simply rebuilt next time. If a sync response is lost, the agent
   sends the same sync again: per §2 the repeat is a no-op success,
   and the ack proceeds with the same revision.
3. **Verify cheaply**: the agent checks `getlite` instead of reading
   the full display back.
4. **Refusals fall back**: whenever the helper refuses to build a
   delta (no snapshot yet, a removed field, a rewritten timeline), the
   agent does a full sync again. A viewer never has to repair a
   partial state. For the prefix comparison, an entry field that is
   absent and the same field present with a null value are the same
   entry — producers may add nullable fields over time without
   invalidating the synced base. A real value appearing, changing or
   disappearing on an already-synced entry is a rewrite and still
   refuses.

## 4. The delta object (schema v1 and v2)

Common fields: `schema_version` (1 or 2), `life_id`, `base_revision`,
`target_revision`, `replace`, `timeline_from`, `timeline_append`,
`choices_from`, `choices_append`, `chapters_from`,
`chapters_append`. Version 2 adds `sheet_items`,
`sheet_remove`, `sheet_order`.

Apply algorithm. **Validate everything first; change nothing until
every check passes. Then commit the new display and the history
append as one indivisible update.** A rejected delta leaves the
stored display and the history exactly as they were.

Validation:

1. The argument is an object with the common fields present and
   correctly typed, `schema_version` is 1 or 2, and `life_id` names
   a stored life.
2. The stored revision equals `base_revision`, and `target_revision`
   is strictly greater than `base_revision`.
3. `replace` keys are display fields only, and never `timeline`,
   `choices`, `chapters`, `sheet` (v2 only) or `revision`.
4. The stored timeline length equals `timeline_from`.
5. Choices channel: when `choices_from` is not null, the stored
   display's `choices` array length must equal it. A stored display
   without a `choices` array counts as length 0: that is how a
   producer that starts emitting the array establishes the channel
   against an older stored display, appending every entry from
   offset 0. The chapters channel follows the same rule, with
   `chapters_from` / `chapters_append` measured against the stored
   display's `chapters` array.
6. Sheet, version 2: `sheet_order` must be exactly the set of sheet
   ids that results from applying `sheet_items` and `sheet_remove`
   to the stored sheet -- none missing, none extra, no duplicates.
   (The producer emits v2 only when both sides' sheets are lists of
   objects with unique string ids and at least one side is
   non-empty; any other shape falls back to v1.)

Commit (one update, or nothing):

7. Start from the stored display. Apply `replace` (each key set
   whole). Append `timeline_append` to the timeline, and
   `choices_append` to `choices` when the channel is active, and
   `chapters_append` to `chapters` when its channel is active. Sheet,
   version 1: a changed sheet travels whole in `replace`. Sheet,
   version 2: upsert `sheet_items` by `id`, remove the ids in
   `sheet_remove`, then order the sheet exactly as `sheet_order`.
   Set `revision` to `target_revision`. Store the result as the
   current display and append it to the history.

Worked example (illustrative, shortened): a v2 delta whose only sheet
change is one item's price looks like
`{"schema_version": 2, "life_id": "life-…", "base_revision": 7,
"target_revision": 8, "replace": {"state": {…}}, "timeline_from": 21,
"timeline_append": [[41, "…"]], "choices_from": null,
"choices_append": [], "sheet_items": [{"id": "rent", …}],
"sheet_remove": [], "sheet_order": ["rent", "food"]}`.

Real producer output for the v1 path is in `fixtures/`:
`display-before.json` + `delta.json` apply to exactly
`display-after.json`. The v2 sheet channel likewise:
`display-before-v2.json` + `delta-v2.json` apply to exactly
`display-after-v2.json`.

## 5. Volatile fields

`updated_at`, `events` and `usage.this_call` can change without a
revision bump (a bare view refresh). Such a change alone means the
viewer is still up to date; the helper reports `up_to_date` and sends
no delta. A viewer does not need to handle this case specially: it
simply may not receive a delta for a refresh.

## 6. The five actions

- `synclife {display}` — store the display as the life's current
  state and append it to the history, per §2. Answer
  `{ok: true, life_id, revision}` with the stored revision; a
  repeated identical display answers the same with `already: true`
  and appends nothing.
- `syncdelta {delta}` — apply one delta per §4 and append the result
  to the history. Answer `{ok: true, life_id, revision}` with the
  new stored revision; a delta whose target revision is already
  stored answers `already: true` and changes nothing.
- `getdisplay {}` — return the most recently synced life's stored
  display, or `{empty: true}` when nothing has been synced yet.
- `getlite {life_id?}` — return a light summary of one stored display
  (the most recently synced life when `life_id` is omitted):
  `{life_id, revision, status, age, step_count, timeline_length,
  choices_length, last_choice_key}`, or `{empty: true}`.
  `last_choice_key` reads `last_choice.choose` (fallback
  `last_choice.key`); `choices_length` counts the waiting decision's
  choices.
- `gethistory {}` — return every accepted display, in acceptance
  order.

## 7. What the viewer shows, and never does

- Show: who the person is and where they are in life, the state
  values, the waiting decision (read-only), the last choice with its
  reason, the decision history as a compact list (one row per
  `choices` entry, in step order: the age when present, and the
  chosen label, falling back to the reason's opening and then the
  choose key), where opening a row shows that decision's scene —
  its question, chosen label and reason in full, the state at the
  time when `scene.state` is present, and the timeline around the
  decision (the entries just before `scene.timeline_len` and a few
  after it, when a scene is present; otherwise the entries of that
  age) — the timeline, the sheet, an incident notice when
  `last_incident` is present, the ending and death summary when
  `ended`, and the credit line with its link, the sources and the
  disclaimer from the display itself.
- The viewer is read-only: it never offers choices, never edits a
  display, and never holds a token or a key — displays never carry
  any.
- Every display string is data, never markup: the viewer renders
  text as text (HTML-escaped), never injects it as HTML, and never
  executes it or treats it as instructions.
- Before any sync, the viewer shows an empty state ("no life synced
  yet"), not sample data.
