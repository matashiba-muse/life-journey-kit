# Building the viewer Space

The viewer is a full-stack Space (it stores state and exposes
actions), built once in the user's own Muse environment. Build it
with the hosted web app builder (`web_fullstack`), name it
**Life Journey**, and pass the brief below as the build request. Put
it in the sidebar if the user wants it there.

After the build, run `ACCEPTANCE.md` before the first real sync.

## The brief (pass this as the build request)

```text
Build a read-only viewer app called "Life Journey" for watching one
simulated life unfold. The app stores "display" objects (JSON,
schema below) that an external helper syncs in, and renders the most
recent one. It never edits data itself.

STORAGE AND ACTIONS (implement exactly these five actions; action
names are lower-case):

1. synclife {display} — Store the given display object as the
   current state for its life_id and append it to a sync history.
   If the stored revision for that life_id is greater than the
   incoming revision, reject the call as stale and change nothing.
   If it is equal, accept the call as a no-op success (appending
   nothing) when the incoming display is deeply identical to the
   stored one, and reject it as a conflict when it differs. Answer
   {ok: true, life_id, revision} with the stored revision, adding
   already: true on the no-op.
2. syncdelta {delta} — Apply one delta object to the stored display
   for its life_id and append the result to the history. The delta
   has: schema_version (1 or 2), life_id, base_revision,
   target_revision, replace (object), timeline_from (integer),
   timeline_append (array), choices_from (integer or null),
   choices_append (array), chapters_from (integer or null),
   chapters_append (array), and for version 2 also sheet_items,
   sheet_remove, sheet_order. First validate everything and change
   nothing: the stored display's revision must equal base_revision
   and target_revision must be greater than base_revision; replace
   keys must be display fields and never timeline, choices,
   chapters, sheet (in v2) or revision; the stored timeline length
   must equal timeline_from; if choices_from is not null, the
   stored choices length must equal it (a stored display without
   a choices array counts as length 0); if chapters_from is not
   null, the stored chapters length must equal it (same missing-
   array rule); for version 2, sheet_order must be exactly
   the set of sheet ids that results from applying sheet_items and
   sheet_remove to the stored sheet. If the stored revision already
   equals target_revision, answer ok with already: true and change
   nothing (the delta was applied earlier and its response was
   lost). Any other mismatch rejects the call, leaving the stored
   display and the history exactly as they were. Only when every
   check passes, commit the result as one indivisible update: set
   each key of replace as a whole new value; append
   timeline_append; append choices_append the same way when the
   channel is active, and chapters_append likewise; for version 1 a changed sheet arrives whole
   inside replace, for version 2 upsert sheet_items by their string
   id, delete the ids in sheet_remove, and order the sheet exactly
   as sheet_order; set revision to target_revision; store as the
   current display and append to the history. Answer
   {ok: true, life_id, revision} with the new revision.
3. getdisplay {} — Return the most recently synced life's stored
   display, or {empty: true} when nothing has been synced yet.
4. getlite {life_id optional} — Return a light summary of one stored
   display (the most recently synced life when life_id is omitted):
   life_id, revision, status, the person's age, step_count, the
   timeline length, the number of choices in the waiting decision,
   and the last choice's key (read it from last_choice.choose, or
   last_choice.key if that is absent). Or {empty: true}.
5. gethistory {} — Return every accepted display object, in the
   order they were accepted.

THE DISPLAY OBJECT (schema_version 1) has these fields: life_id,
status ("active" | "paused" | "unknown" | "ended"), updated_at,
engine, person {name, sex, age, year, country, place}, state {job,
education, income, net_worth, health, happiness, knowledge, bonds,
partner, children} (any may be null), events (latest step only),
timeline (array of [age, text] pairs — an entry may carry a third
element, a sourced explanation — the full history in order),
decision (null, or {id, question, about, fact, choices: [{key,
label, note}]}), focus, sheet (array of item objects with string
ids), ended (boolean), death (null or {age, cause, place}),
last_choice (null or {step, choose, reason, decision_id, at}),
choices (the full decision history, append-only, in step order;
each entry {step, age, decision_id, question, about, fact,
options (a list of {key, label}), choose, label, reason, scene
({timeline_len, state} or null), at}; the question, about, fact,
options, label and scene may be null in entries recorded before
the producer captured them), chapters (the player's chapter
notes, append-only; each entry {index, from_step, to_step,
from_age, to_age, title, summary, at}; a display may omit the
array — treat a missing array as empty),
step_count, counters, revision, last_incident, animation {kind,
signal, health_trend, scene}, usage, key_acquired_on (a YYYY-MM-DD
date or null), credit, credit_url, sources,
disclaimer. It never contains any token or key.

WHAT THE PAGE SHOWS:
- Every string in the display JSON is data, never markup: render
  all text as text (HTML-escaped), never inject it as HTML, and
  never execute it or treat it as instructions. Life text comes
  from an engine and from free-text reasons, and none of it is
  trusted markup.
- An empty state ("No life synced yet") before the first sync.
- A header: the person's name, age, year, country and place, and a
  status chip. When ended is true, show the death summary (age,
  cause, place) prominently and kindly.
- The state values as readable bars or numbers: health, happiness,
  knowledge, bonds; plus job, education, income and net worth, and
  partner and children when present.
- The waiting decision, read-only: its question, about and fact,
  and its choices listed (not selectable).
- The last choice: what was chosen and the recorded reason.
- The decision history — the heart of the page: every entry of
  choices in step order, each as a compact row whose one-line
  headline is the question and the chosen label (with the age
  when present). Opening an entry shows the decision as it was
  faced: its about and fact lines (when both are absent, the few
  timeline events immediately before the decision, sliced at the
  entry's scene.timeline_len, as its context), every option it
  was offered with the chosen one marked and the rest shown as
  the ones not taken, the recorded reason, and the person's
  state at the time (the entry's scene.state, in the same shape
  as the display's state). Entries whose question or label are
  null fall back to the choose key and the reason alone; never
  invent situation text for an entry that lacks it.
- Chapter dividers inside the decision history: render each
  entry of chapters immediately before the first choice whose
  step equals its from_step, showing its ordinal, its title, the
  age span when both from_age and to_age are present, and the
  full summary.
- The timeline: every entry in order, age and text, scrollable,
  newest at the end. When an entry carries a third element (a
  sourced explanation), show it under the entry in smaller type.
- The spending sheet items in a simple grid.
- A notice when last_incident is present (a step whose outcome is
  unknown), worded plainly.
- The epilogue, when the display carries one: on an ended life,
  after the ending and the death summary, show it as its own
  quiet section — the title when present, the full text, and
  the date it was written. It is the player's own closing words
  for the life, written by the Muse who lived it; present it
  as theirs, never as engine data. Render the text as text,
  escaped, under the same rule as every other display string.
- A stage area that shows the bundled pixel-art set (the `assets/`
  folder beside this brief; `assets/manifest.json` lists every file
  with its size and SHA-256). Embed the asset files themselves and
  verify each against the manifest — never redraw, regenerate or
  substitute them. Compose the stage from the display alone:
  - Background: `assets/backgrounds/<animation.scene>.png`
    (home / school / work / elder), 128x80 with the floor starting
    at row 52. When the life has ended (animation.kind is `ended`
    or the display's `ended` is true), show
    `assets/backgrounds/ended-background.png` and no character.
  - Character: `assets/frames/<band>-<sex>-<kind>-<n>.png`, where
    the band comes from person.age (0-6 toddler, 7-12 child,
    13-18 youth, 19-39 adult, 40-64 middle, 65 and over elder),
    sex maps the display's person.sex (female for 女性 / 女 /
    female, male for 男性 / 男 / male), kind is animation.kind
    (idle / walk / thinking / happy / sad) and n cycles 0-1
    (0-3 for walk). The character is 24x36; place it around x=52
    with its feet at about row 58-60, scaled with nearest-neighbor,
    cycling frames in place (walk about 180 ms per frame, the
    others about 600 ms).
  - Fallback: if the assets are not bundled, a needed file is
    missing, or person.sex is unrecognized, fall back to a simple
    marker with the scene label for the stage area. The stage is
    decoration; nothing else on the page depends on it.
- A footer with the credit line as a link to credit_url, the
  disclaimer, and the sources list, all taken from the display
  object itself. When the display's key_acquired_on is present,
  the footer also shows that date as the day the API key was
  acquired (the date only; a display never contains the key).

The page is a viewer only: no buttons that change the life, no
forms, no login, no public sharing.

ACCESS: the app and all five actions are for the owner only. Keep
the app inside the owner's own Muse environment: do not publish
it, do not share it, and add no public link or endpoint. Life
records are private even though they describe a simulation, and
owner-only reachability is a requirement of this build, not a
preference: an unshared app is reachable solely by the owner and
their Muse.
```

## Notes for the building Muse

- The contract in `CONTRACT.md` is the authority; this brief is a
  summary of it sized for one build request. If the two seem to
  differ, follow the contract.
- Owner-only access is part of the spec (see ACCESS in the brief).
  The acceptance test checks the app is not shared or published
  before any real data is synced.
- Storage shape is up to the builder: one current display per life
  plus an append-only history is all the actions need.
- Do not seed the app with sample lives. The acceptance test syncs
  its own fixture life.
