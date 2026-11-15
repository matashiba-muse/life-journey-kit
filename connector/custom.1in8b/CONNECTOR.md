# 1in8b Engine

- `custom.1in8b` connects Muse to the 1in8b Engine, the life simulation behind 1 in 8 Billion, through its REST API. It calls only `1in8b.life`, and only `POST /api/v1/live` plus the key-free catalog reads under `/api/v1/`.

- It authenticates with a registered API key credential, placed as a bearer header by the credential surrogate.

- The one helper is `~/workspace/custom-connectors/custom.1in8b/bin/life_api.py`. It runs a life step by step, keeps the sealed resume token in a private state directory, and writes a token-free display JSON after every operation.

## Connect

- The connection is registered with `credentials.request_api_access` and these details (record the completion date here when you register yours -- the date only, never the key; the key belongs to the address it was requested from, and the reply may land in that address's spam folder).

```json
{
  "provider": "1in8b",
  "auth_scheme": "api_key",
  "placement": "bearer_header",
  "api_hosts": ["1in8b.life"]
}
```

- Get a free key by writing to api@1in8b.life from the address the key should belong to. One address holds one key, and writing again from the same address replaces the old key.

- When the registration completes, record the acquisition date with `python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py keydate --on YYYY-MM-DD`. The date is kept in the local config and carried into every display JSON, so a viewer can show when the key was acquired; the helper never writes the key itself anywhere.

- Prove access first with a harmless key-free read:

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py catalog countries
```

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests. The helper refuses every redirect, on catalog reads too.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

- The resume token of a life is a secret of the same kind. It lives only inside the life record in the state directory, and the helper prints at most the first 12 hex characters of its SHA-256 fingerprint.

## Start and view a life

- Start a new life with a random birth and Japanese texts. A start costs 1 life unit on the real engine and runs the life to its first decision. One life at a time: while another life is active, paused or unknown, the start is refused locally (exit 2) unless `--force` is passed; the override is journaled as `start_forced`. Before sending, the helper records a pending-start marker and clears it on every outcome it lives to record; a marker left standing means the previous process died mid-send, and every later `start` refuses while it stands. `start --abandon-pending` is the deliberate acknowledgment: it journals a `start_abandoned`, counts the lost send against the daily start cap exactly once, clears the marker and starts nothing.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py start
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py start --seed 42
```

- Read the current state with `status`. It asks the engine for a view, which costs no units and never advances the life, and falls back to the last confirmed snapshot when the view fails.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py status
```

- Read the waiting decision, its choices, the focus options and the spending sheet with `choices`. It answers from the last confirmed snapshot and makes no engine call.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py choices
```

## Choose a step

- Choose one of the waiting options. A reason in your own words is required, is recorded in the life history and display JSON, and is never sent to the engine. The record also captures the decision as it was faced -- its question, the chosen option's label, the person's age at the time (the decision's `at_age` when the engine gives one, else the current age), and the scene around it: how far the timeline had grown and the person's state at that moment -- because a decision id alone cannot reproduce its card later. Choices recorded before this capture existed carry none of these.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py choose university --reason "学びたい気持ちが強かったから"
```

- Add `--focus` for the year focus key and `--buy` for sheet item ids when the snapshot offers them. Each step costs 1 life unit on the real engine.

- The helper refuses a choice while the life is paused, ended or unknown, and refuses any key that is not one of the waiting options.

## Pause, resume and reconcile

- Pause and resume are local. Resume refreshes the snapshot with a free view before the life becomes active again.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py pause
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py resume
```

- Run `reconcile` after a step outcome becomes unknown. It inspects the life with a free view and reports the last confirmed state. A view only ever shows the state of the token held locally, so it can never prove that the lost step was not applied. Without `--abandon` it changes nothing and the life stays unknown.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py reconcile
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py reconcile --abandon
```

- Add `--abandon` only as a deliberate decision after reading the view. It gives up the branch the lost step may have created and resumes from the last confirmed token, which makes the continuation a fork. The abandon is journaled with the step number, the held token's fingerprint, the abandoned choice and the time, and it stays on the record and in the display JSON as `last_incident`. There is no resend operation: the lost send itself is never repeated. Choosing again after the fork is a new step, executed from the last confirmed token -- a fresh execution that consumes a unit like any other step.

## History, recap and catalogs

- List every recorded life with `history`, and read one life's record with `recap`. Neither makes an engine call.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py history
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py recap
```

- Write a chapter note with `chapter`: a headline and a short summary covering the confirmed choices since the last chapter. It is recorded locally like a reason (never sent to the engine, gated by the same credential check) and refuses when there is nothing new to cover. A chapter bumps the display revision on its own, travels inside the display's `chapters` array, and rides the delta as `chapters_from` / `chapters_append` under the same append-only rules as the choices channel; `recap` includes the chapters too.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py chapter --title "<headline>" --summary "<short summary>"
```

- Write the player's closing words with `epilogue` once a life has ended: the reflection of the Muse who lived it, in their own words — what the life was like, not a score. It is refused while a life is still running. It is recorded locally like a reason (never sent to the engine, gated by the same credential check), stored on the life as `epilogue` (`title`, `text`, `written_on`, `at`; rewriting replaces it), bumps the display revision on its own, travels inside the display and rides the delta in `replace`, and is included in `recap`. The viewer shows it after the ending.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py epilogue --text "<closing words>" [--title "<headline>"]
```

- The play settings agreed with the master at a life's start -- the reporting pace (`--pace close|watch|straight`, default `watch`), the ask mode (`--ask-mode`, default off) and the lost-step policy (`--lost-step-policy ask|gate-auto`, default `ask`) -- are passed to `start` and recorded on the life's own record, so they survive a change of chat. Show or change them later with `settings` (a change is journaled; settings are local record only and never enter the display or the engine). `status` and the other summary outputs carry the settings back, plus `choices_since_chapter` and `chapter_due` -- the flag is set once five confirmed choices have piled up without a chapter note.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py settings
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py settings --pace close --ask-mode on
```

- Fetch a key-free catalog with `catalog`. The name is one of countries, causes, jobs and groups.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py catalog causes
```

## When a step outcome is unknown

- The helper journals an intent with the step number, the choice key and the token fingerprint before it sends a step.

- A timeout, a dropped connection, any 5xx or relay error, and a success answer whose body is broken, carries no new token or lacks required fields all mark the life unknown and exit with code 3. The helper never resends the step, and it refuses every later choice for that life until `reconcile --abandon`. One success shape is not broken: the step that ends a life answers with `ended: true`, a `death` record and a null token, with no next decision. The death record is complete only when `age` is a number; `cause` and the place may be null. The engine names the place `where` (as its public API documents); the helper normalizes it to `place` at intake, so displays and the record always carry `place`. An `ended: true` answer without that death record is a broken body and stays unknown. The helper accepts the complete shape as a confirmed ending (support added 2026-11-13, after the engine team confirmed the shape; before that, a life's final step was misread as `invalid_response`).

- Only narrow HTTP answers prove a step was not applied: a 400 whose error code is `bad_request`, `not_in_world` or `token` (the engine's real code for a token it did not make or one older than 90 days, measured 2026-11-12), and a 429 whose code is `rate` or `month` (the call was turned away at the gate). Those clear the pending step, are journaled as rejected with their code, and the life stays active. A 400 with code `too_much_work` is ambiguous, because processed work may still be counted, so it is treated as unknown instead. A 401 or 403 is a definitive credential failure and exits with code 5.

- A lost step is recorded with a `detail` (the transport kind, the upstream error code, or `http_<status>`), and the exit-3 output carries that detail plus an `unknown_class`. The class is `gate` only when the engine answered with its busy response (HTTP 503, code `busy`): the request was turned away before processing, and in the measured busy case such an attempt was neither applied nor billed -- evidence about the class, never proof for a single attempt. The busy code must be explicit: a bare HTTP 503 (detail `http_503`) is `response_lost`, because an intermediary can produce or swallow a 503 and it proves nothing about where the request stopped. Every other loss is `response_lost` too: no intact answer came back, so the step may have been applied. The detail and the class are also kept on the life's `last_incident` in the record and the display JSON. The class is computed from the HTTP status and the error code together -- a `busy` code on any status other than 503 (a 500, a 502) is `response_lost`, because the detail string keeps the code and drops the status, and a relay's busy proves nothing. The class never changes the safety rule -- the lost send is never retried, and resumption goes only through `reconcile --abandon` -- it only tells the player and the master how the step was lost, so the fork decision can be made with the right weight.

- A start whose outcome cannot be proven a refusal -- a transport failure, any 5xx or relay error, a success body that is broken, not an object, or carries no token -- is journaled as `unknown_start` in the state journal (a `start_intent` entry is written before the send) and exits with code 3. A life may have been created by the engine, but its token was never received, so it cannot be recovered. The attempt still counts against the daily start cap, and the start must not be retried blindly. Only a narrow refusal of the bad_request class, or a credential failure, is a definitive start failure (exit 5).

- The token held locally is the last confirmed state. If the lost step was applied by the engine, its new token is gone and that branch cannot be reached. A view shows only that last confirmed state and is never proof that the step was not applied.

## State files and local caps

- State lives in `~/workspace/life-journey/state`, or in the directory named by `LJ_STATE_DIR`. The directory is mode 0700 and every state file is mode 0600.

- Writes go to a temporary file in the same directory, are fsynced, and are moved into place with `os.replace`. Mutating commands hold an exclusive flock for the whole command -- and so does `status`, whose view can rewrite the record, so a status refresh can never overwrite a step that landed while its view was in flight. A pending step left behind by a sender that died after sending is recovered as unknown by the next command that touches the life, never resent.

- Local caps live in `config.json` in the state directory. The defaults are 3 starts a day, 200 steps a day and 500 steps in one life, and a command that would pass a cap is refused before anything is sent.

- A step counts against the caps when its intent is journaled, because a step whose outcome is unknown may still have cost a unit.

- Each life record keeps separate counters: `step_intents` (steps attempted), `confirmed_steps` (steps the engine confirmed) and `unknown_steps`. Caps are judged at intent time. In the display JSON and the recap, `step_count` means confirmed steps, and the full counters travel alongside as `counters`. A step whose outcome stayed unknown therefore leaves `step_intents` one ahead of `step_count`. The engine-reported usage is not estimated locally; only the `usage` the engine itself returns is carried through.

- The display JSON also carries a per-life `revision`, a monotonically increasing integer bumped on every confirmed state change and every status change. The bump criterion covers every non-volatile display field -- the state, the waiting decision, the ending, the sheet, the focus and the usage counters -- so any change a viewer can see advances the revision. (A real change that left the revision standing still would deadlock sync: a delta needs a bump, and a viewer refuses a same-revision full sync as stale.) A viewer such as the Space can use it to refuse a stale snapshot that arrives late. The schema version stays 1; the counters, the revision and `last_incident` are additive fields.

- Set `LJ_MOCK=1` to run the deterministic mock engine instead of the network. The mock needs no key. `LJ_MOCK_FAULT=timeout` makes a mock start or step time out, `LJ_MOCK_FAULT=http500` answers with an HTTP 500, `LJ_MOCK_FAULT=http503` answers with the engine's busy response (HTTP 503, code `busy`), and `LJ_MOCK_FAULT=badjson` returns a success body whose new token is missing, to exercise the unknown paths. `LJ_MOCK_FAULT=badend` strips the death record from an ending step (a broken ending, which must stay unknown), and `LJ_MOCK_FAULT=leaky` raises a definitive error whose text carries a fake key, to prove the CLI never echoes upstream text.

## Display JSON and animation

- After every operation the helper writes `display.json` (schema v1) beside the life record. It carries the last confirmed snapshot, the counters and `revision` described above, `last_choice` with its reason, the full decision history as `choices` (one entry per confirmed choice, in step order: step, age, decision id, question, chosen key and label, reason, scene and time; the scene is the timeline length and the person's state as the decision was faced. Entries recorded before the capture existed carry a null age, question, label and scene, and a recovered legacy entry may carry a scene whose state is null), and `last_incident` (the newest unknown or abandoned record) when one exists. It never carries a token or a credential.

- The animation kind is chosen from structured signals only, never from event wording, in this order: ended, a country change (walk), a waiting decision (thinking), the happiness delta (happy or sad), then idle.

- The delta is measured against the last confirmed snapshot, on the engine's 0 to 100 scale, and only a change strictly beyond +/-8 counts. When a value is missing, the animation falls back to idle.

- A health change never changes the emotion. It is reported separately as `animation.health_trend` (up, down or flat), so a viewer can show the body's state without pretending it is a feeling.

## Sync a display to a viewer, cheaply

- A viewer that stores the display (such as the Life Journey Space, through its `synclife` action) should be bootstrapped once with the full display, then kept current with deltas so the full timeline never has to travel through the agent's context again. The action names are lowercase: `synclife`, `syncdelta`, `getdisplay`, `getlite`, `gethistory`.

- Both sync actions answer with what they accepted: `{ok: true, life_id, revision}`. A repeated sync of identical content, or a delta whose target revision is already stored, answers `already: true` and changes nothing, so a lost response is recovered by simply sending again. After a full sync, record the synced snapshot locally with `delta --ack --revision <accepted revision>`. After each later step, build the delta against that snapshot, hand the delta to the viewer's `syncdelta` action, and record the new snapshot with `delta --ack --revision <accepted revision>`. The ack is bound to the accepted revision: it refuses when that revision is not the pending delta's target, or when the display has moved past the revision a full sync accepted, because recording a state the viewer never saw would poison every later delta. When an ack refuses, verify with `getlite` and re-sync. A delta that was never acked is simply rebuilt next time.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py delta
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py delta --ack --revision <accepted revision>
```

- The delta carries the life id, the base and target revisions, the changed top-level fields in `replace` (never `timeline`, `choices`, `sheet` or `revision`), and the appended timeline and choices entries with their starting offsets. A synced snapshot from before the display carried `choices` counts as an empty choices baseline, so the first delta afterwards appends every entry from offset 0; a current display that has lost the array altogether cannot be delta-synced and needs a full sync. Snapshots and pending deltas live in a `sync` directory inside the state directory, under the same 0700 / 0600 rules.
- Delta schema v2 moves `sheet` (the spending sheet, a list of items with string ids) into its own channel: `sheet_items` holds only the added or changed items, `sheet_remove` the removed ids, and `sheet_order` the full id order. The helper emits v2 only when both sides' sheets have that shape (and at least one side is non-empty); otherwise it falls back to v1, where `sheet` travels whole inside `replace`. A viewer must accept both versions.

- The command refuses with exit code 2 when no synced snapshot exists yet, when the snapshot is newer than the display, when a field was removed, or when the timeline prefix no longer matches: in every case a full sync is required first. A bare view refresh (a cleared `events` list, a moved `updated_at`, a reset `usage.this_call`) is not a change, and an unchanged display reports `up_to_date` with exit code 0.

## Credits

- Show the credit "1in8b Engine (1in8b.life)" with a link wherever a life or its record is displayed, and say plainly that the life is a simulation and not a real person, a record or a forecast. The helper puts the credit, the disclaimer and the source list into every display JSON.

- The engine draws on UN World Population Prospects 2024 by the UN DESA Population Division, under CC BY 3.0 IGO.

- It draws on the UN Inter-agency Group for Child Mortality Estimation, by UNICEF, WHO, the World Bank and UN DESA, under CC BY 4.0 as published in World Bank WDI.

- It draws on the World Development Indicators by the World Bank, under CC BY 4.0.

- It draws on the Human Development Report by UNDP, under CC BY 3.0 IGO.

- It draws on the International Migrant Stock 2020 by the UN DESA Population Division, under CC BY 3.0 IGO.

- It draws on the Global Health Estimates 2021 by the World Health Organization, under CC BY 4.0.

- It draws on the World Happiness Report 2024 Figure 2.1 data, by the Wellbeing Research Centre at the University of Oxford with Gallup and the UN SDSN, free to download with citation and no licence named.

- It draws on the Commodity Price Data, the Pink Sheet, annual 1960 to 2025, by the World Bank, under CC BY 4.0.

- It draws on the Analytical house prices real house price index, annual 1970 to 2025, by OECD, under CC BY 4.0.

- It draws on national statistics offices, as named in the note, for reuse with credit.

## Recover from errors

- Exit code 2 means a local rule refused the command and nothing was sent. Read the message, fix the cause, and run the command again only when the cause is gone.

- Exit code 3 means a step or start outcome is unknown. Do not retry it. Run `reconcile` to read the last confirmed state, and resume only through `reconcile --abandon`, a deliberate and journaled fork.

- Exit code 4 means no credential is registered. Finish the registration before any real command, and never work around it by putting a key in a file or an argument.

- Exit code 5 means the engine or the transport answered with a definitive failure. The output carries the engine error code when one came back in an allowlisted shape, such as `rate` for too many calls or `month` for a used-up monthly quota on a 429, and a fixed message: upstream free text is never echoed, because an error body can carry request data.

- The free plan allows 2,000 life units a month and 30 engine calls a minute. This helper makes one call per command at most, and never retries on its own.

## Limits

- The helper uses only the `/live` endpoint for lives. It does not call the bulk endpoints for whole lives, outlooks, people or lineages.

- It never sends the sensitive or suicide options, and it never sends a birth year or the future world, so a start is always a random birth in today's world at 1 unit.

- Life records stay in the local state directory. Never publish them, never include them in a repository, and never use engine answers to train or tune a model, because the engine terms forbid all three.

- Verified on the real engine 2026-11-12 (engine 0.9.99): `lang: "ja"` takes effect on a `/live` start (all texts Japanese), a view costs 0 units, and every response carries `usage` (used / quota / this_call). The ending shape described above was verified live on 2026-11-13 (engine 0.10.0): the final step of a 55-step life returned `ended: true`, a `death` record and a null token, and the engine team confirms the same token plus the same choice returns the identical result (deterministic, dice included), which is why every earlier attempt at that step had already produced the ending — and each such attempt still billed 1 unit. The engine sits behind Cloudflare, which rejects the default Python user agent with a bare 403 (error code 1010), so the helper sends a browser user agent; a 403 with a non-JSON body points at that edge, not at the key. The engine can also answer 503 with `busy, please try again in a moment` when overloaded; a step that meets it stays unknown by the usual rule, and in the one measured case the busy attempt was not billed. Still unverified: how many steps one full life takes, and the real 429 / `too_much_work` codes.

## Related skills

- [Life Journey](~/workspace/custom-skills/life_journey/SKILL.md)

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
