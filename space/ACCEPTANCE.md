# Viewer acceptance test

Run this after building the viewer Space, before the first real
life. It uses the fixtures in `fixtures/`, which are real output of
the kit's own helper running against its mock engine (a synthetic
person — not a real life):

- `display-before.json` — a full display, revision 4, timeline 9
  entries, step_count 3, status `active`.
- `delta.json` — a schema v1 delta, base revision 4 → target
  revision 6, `timeline_from` 9 with 4 appended entries.
- `display-after.json` — the display the delta produces, revision 6,
  timeline 13 entries, step_count 5.
- `display-before-v2.json` — a second synthetic life's display,
  revision 2, with a 3-item sheet.
- `delta-v2.json` — a schema v2 delta, base revision 2 → target
  revision 3: one sheet item changed, one added, one removed, and
  the order changed.
- `display-after-v2.json` — the display the v2 delta produces,
  revision 3.
- `display-before-ch.json` — a third synthetic life's display,
  revision 6, 5 choices, no chapters, `key_acquired_on` null.
- `delta-ch.json` — a schema v1 delta, base revision 6 → target
  revision 8: one chapter appended through the chapters channel,
  and the key-acquisition date set through `replace`.
- `display-after-ch.json` — the display that delta produces,
  revision 8, one chapter, `key_acquired_on` 2026-01-15.
- `display-before-epi.json` — a fourth synthetic life's display,
  revision 9, status `ended`, no epilogue yet.
- `delta-epi.json` — a schema v1 delta, base revision 9 → target
  revision 10: the epilogue set through `replace`.
- `display-after-epi.json` — the display that delta produces,
  revision 10, with the epilogue.

(If the fixtures are ever regenerated, read the expected numbers
from the files; the steps stay the same.)

## Steps

0. **Owner-only access.** Before any data is synced, confirm the
   Space is not published and not shared: it lives only in the
   owner's own Muse environment, with no public link. An
   unauthorised visitor must be unable to open the page or call any
   of the five actions. If the Space is shared or published, stop
   here and fix that first.
1. **Empty state.** Call `getdisplay` and `getlite` on the fresh
   Space. Both must return `{empty: true}`, and the page must show
   its empty state.
2. **Full sync.** Call `synclife` with the contents of
   `display-before.json`. The response must be
   `{ok: true, life_id, revision: 4}` (the fixture's life_id).
   Then:
   - `getlite` returns the fixture's life_id, revision 4,
     timeline length 9, step_count 3, status `active`.
   - `getdisplay` returns an object deeply equal to
     `display-before.json`.
   - The page renders the person, state, waiting decision, the
     timeline, and the decision history with its 3 entries — each
     opening to its scene: the situation lines, the options that
     were not taken, the recorded reason, and the state at the
     time.
3. **Delta sync.** Call `syncdelta` with the contents of
   `delta.json`. The response must carry revision 6. Then:
   - `getlite` returns revision 6, timeline length 13, step_count 5.
   - `getdisplay` returns an object deeply equal to
     `display-after.json`.
   - The decision history now lists 5 entries.
4. **Repeat sync is a no-op success.** Call `synclife` again with
   `display-after.json` (the display already stored). It must
   succeed with `already: true`, and `gethistory` must still hold
   exactly 2 entries. (This is how a lost sync response is
   recovered.) Then call `synclife` with `display-before.json`: it
   must be rejected as stale, and `getlite` must still show
   revision 6.
5. **Mismatch rejection at a matching base.** The stored display is
   at revision 6. Call `syncdelta` with a copy of `delta.json`
   whose `base_revision` is changed to 6, `target_revision` to 7,
   and `timeline_from` to 999. The base revision matches, so only
   the offset validation can catch this: it must be rejected, and
   afterwards `getdisplay` must still be deeply equal to
   `display-after.json` and `gethistory` must still hold exactly 2
   entries (a rejection changes nothing at all).
6. **History.** Call `gethistory`. It must contain the accepted
   states in order (the revision 4 display, then the revision 6
   display) — exactly the two from steps 2–3, nothing added by the
   rejected or repeated calls.
7. **The v2 sheet channel.** Call `synclife` with
   `display-before-v2.json`, then `syncdelta` with `delta-v2.json`.
   Then:
   - `getlite` for the v2 life returns revision 3.
   - `getdisplay` returns an object deeply equal to
     `display-after-v2.json` — in particular its sheet is, in
     order: `own_bike`, `own_car`, `own_tv` (one item changed, one
     added, `dinner` removed, order applied).
   - Re-sending `delta-v2.json` succeeds with `already: true` and
     changes nothing.
8. **The chapters channel and the key-acquisition date.** Call
   `synclife` with `display-before-ch.json`, then `syncdelta` with
   `delta-ch.json`. Then:
   - `getdisplay` returns an object deeply equal to
     `display-after-ch.json` — one chapter recorded, and
     `key_acquired_on` set to 2026-01-15.
   - The page renders the chapter as a divider immediately before
     the first choice it covers, showing its title and summary,
     and the footer shows the key-acquisition date (2026-01-15).
   - Re-sending `delta-ch.json` succeeds with `already: true` and
     changes nothing.
9. **The epilogue.** Call `synclife` with
   `display-before-epi.json` (an ended life, no epilogue yet),
   then `syncdelta` with `delta-epi.json`. Then:
   - `getdisplay` returns an object deeply equal to
     `display-after-epi.json` — the epilogue arrived in the
     delta's `replace`, with its title, text and written date.
   - The page renders the epilogue as its own section after the
     ending and the death summary, showing the title, the full
     text and the date it was written, presented as the player's
     own closing words.
   - Re-sending `delta-epi.json` succeeds with `already: true`
     and changes nothing.

## Pass criteria

All steps behave as described, step 0 included. If any step fails,
do not start a real life yet: fix the viewer's action and re-run
from step 1 on a cleared state.

Note: the fixture lives stay in the viewer's history afterwards,
and the last one synced is the "current" display until the first
real full sync replaces it. That is expected and harmless.
