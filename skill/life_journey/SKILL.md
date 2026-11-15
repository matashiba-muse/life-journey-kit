# Life Journey

## Play a life as the person living it

- When a life is already in progress and the user asks you to go on, continue that life: read its state with `status` and its waiting decision with `choices`. Start a new life only when no life is in progress, or when the user explicitly asks for a new one. The helper enforces one life at a time and refuses a start while another life is active, paused or unknown; `--force` overrides that deliberately and is journaled, so pass it only on the user's decision.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py start
```

- Read the waiting decision before every choice. Read the question, the about text, the fact and every choice with its label and note.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py choices
```

- Write a short reason in your own words before you choose. Say what the person in this life would weigh, given their state, their family and what has happened so far.

- Choose with that reason. The reason is recorded in the life history and the display JSON, and it is never sent to the engine. Keep the reason a plain reflection of the person's situation: the helper refuses a reason that carries a credential or a token, because reasons are recorded and synced.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py choose <key> --reason "<short reason in your own words>"
```

- Play the life for its own sake. Do not treat the life as a score to maximise, do not reroll a birth for better conditions, and do not pick choices only to reach a long life or a high income.

- Take one step at a time and look at what each step brought before the next one. The events and the new state are the life as it is lived, so let them shape the next reason.

- Refresh the picture with `status` whenever you need the current state. A view costs no life units and never advances the life.

- The animation in the display JSON comes from structured signals only: the life ending, a country change (walk), a waiting decision (thinking), then the happiness change against the last confirmed state (the engine's 0 to 100 scale, strictly beyond +/-8, and idle when a value is missing). A health change is carried separately as a trend and never decides the emotion.

## One life, one chat

- Recommended: start each new life in a fresh side chat dedicated to that life, and keep playing it in that chat until it ends. One chat per life keeps a long story in one place and out of other topics' way; work on the skill itself stays in its own chats. Seed the life's chat with the handover: the settings in force (pace, ask mode, lost-step policy) and any standing duties (such as writing a chapter note every five decisions).

## Pace modes, and telling your master

- A life is played at one of three paces, and the master can change the pace at any time with a word:
  - **じっくり (close)**: advance one decision at a time and report each one before continuing.
  - **見守り (watch) — the default**: advance five decisions, then come back to your master with a summary report. Chapter notes fall on the same five-decision marks.
  - **一気に (straight through)**: keep advancing in long stretches — to the end of the life when asked — and report at natural breaks. Chapter notes still fall every five decisions, and the ask mode below still pauses the run.
- When a life starts, tell your master these settings — always for their first life, and briefly for any later life whose master may not remember them. Keep it to a few lines: the pace in force and that it can be changed at any time, the optional ask-master mode (next section) — set per life, at the start; when it is on, you stop before life-changing decisions and consult your master before choosing — and the lost-step policy: if a step is ever lost to an engine error, the default is to stop and ask, but the master may instead pre-authorize automatic fork recovery for busy-class losses only (see "Pause, resume and stop safely"). Ask which they want for this life, and record the answers (pace, ask mode, lost-step policy) in the life's handover — and on the life itself: pass `--pace`, `--ask-mode` and `--lost-step-policy` to `start`, or set them later, on your master's word, with the `settings` command. The helper's record survives a change of chat; if it and a handover ever disagree, the record wins. `status` shows the settings back, and it flags `chapter_due` once five confirmed choices have piled up without a chapter — when you see it at a report point, write the chapter instead of counting by hand.

## Asking your master (optional mode)

- A life may be played in an optional "show my master this choice" mode, off unless the owner turns it on for that life. Set it when the life starts, as a property of the whole life, and note it in the life's handover.
- It combines with any pace mode: you still advance on your own, but at a decision that qualifies you stop *before* choosing and bring it to your master first. This holds even for a run that plays the whole life in one go — the run pauses at a qualifying decision to ask, and resumes once the master has answered.
- A decision qualifies when it plausibly redirects the person's course or is hard to undo — schooling, career, marriage, moving, children, major money — or when the options genuinely conflict with the person's established values, past choices, or family circumstances, or when, thinking as the person, you are honestly torn between the options.
- Routine, habitual, and low-stakes decisions never qualify, and neither does a decision whose direction is already clear from the person's pattern. Expect this to be rare — a few times in a whole life; if it triggers often, the bar is set too low.
- When you stop, present the decision as it was faced (its question, the situation and fact lines, and every option), the person's current state and the past decisions that bear on it, and your own leaning with what makes the choice hard. Then wait for your master's word.
- The consultation is advice, not a handover: your master does not choose in your place. After hearing them, weigh their view alongside the person's own pattern and make the choice yourself, as the one living this life. The recorded reason may show that you consulted them and how their view weighed in — the decision stays yours. Then resume the pace. Chapter notes are unaffected: they still cover every five confirmed decisions.

## Pause, resume and stop safely

- Pause a life when the user wants to stop for now, and resume it when they want to go on. The sealed token waits in the local state, so a life can rest for days — but not forever: the token stays valid for 90 days from the day the life started, and a life left longer than that cannot be resumed. When a master returns to a long-resting life, check its start date before promising a resume, and say so plainly if the 90 days have passed.

- Stop at once when a choose command exits with code 3 or reports `step_outcome_unknown`. Never retry the lost send — the same step call is never repeated as a retry — and never work around the block by editing state files. Recovery, when it comes, is a new choice made after the recorded fork (below), not a resend of the lost one.

- A start can end unknown too (exit code 3, `start_outcome_unknown`). A life may have been created whose token never arrived, so it cannot be recovered and the attempt still counts against the daily cap. Do not retry the start blindly; tell the user and let them decide.
- A start whose process died mid-send is the one case the helper cannot classify for you: it leaves a pending-start marker, and every later `start` refuses while the marker stands. This is deliberate -- the engine may have created a life whose token is lost for good, and a blind new start could create a second one. Report it to your master; when they accept the loss, run `start --abandon-pending` (it counts the lost send against the daily cap exactly once and clears the marker), then start again. Never delete the marker file by hand.

- Inspect an unknown life with `reconcile`, which reads the life with a free view and changes nothing. The view shows only the last confirmed state held locally; it is never proof that the lost step was not applied.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py reconcile
```

- Tell the user what the view shows and that the lost step cannot be confirmed either way. Resume only through `reconcile --abandon`, as a deliberate decision: it gives up the branch the lost step may have created and forks the life from its last confirmed state. The abandon is recorded in the journal, the recap and the display JSON as `last_incident`. The lost send itself is never repeated: the helper has no resend operation. After the fork, the same decision is presented again, and choosing there — even the same option — is a new step in its own right: a fresh execution from the last confirmed token, which consumes a unit like any other step.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py reconcile --abandon
```

- A lost step comes in two classes, and the unknown output names the class (`unknown_class`) and how the step was lost (`detail`). A **gate** loss means the engine answered with its explicit busy response (HTTP 503 with error code `busy`): the request was turned away before processing, so the step was most likely never applied — in the measured busy case it was not billed either. The busy code must be explicit: a bare HTTP 503 without it proves nothing about where the request stopped, because an intermediary can produce or swallow a 503, so a bare 503 counts as response-lost. A **response-lost** loss (a timeout, a dropped connection, a broken answer, a bare 503, another server error) means no intact answer came back: the step may have been applied, and only its answer and new token are missing. The class never loosens the rule above — the lost send is never retried — it only changes how you present the fork decision.

- When you bring a lost step to your master, say it plainly: what was sent, which class it was lost in, that the confirmed life is intact exactly as far as it was confirmed, that the lost step itself can be neither confirmed nor recovered, and that the only way forward is the recorded fork — after which the same decision is presented again and can be chosen again. For a gate loss, recommend the fork: the evidence says the step almost certainly never happened, and waiting changes nothing. For a response-lost loss, recommend deciding together rather than quickly, because a real branch of the life may be given up.

- A life may carry a **lost-step policy**, set at its start alongside the pace and the ask mode and recorded in its handover. The default is to stop and ask the master every time. If the master has pre-authorized automatic recovery for gate losses, then on a gate-class loss you may recover without asking: run `reconcile`, then `reconcile --abandon`, confirm the decision that comes back is the same one that was lost, and choose the **same option** again with a reason that records the recovery. Then report what happened at the next report point, as part of the normal report. The limits are strict: at most one automatic recovery per incident; if the re-choice is also lost, if the returning decision differs from the lost one, or if the loss is response-lost class, stop and ask. The policy never covers response-lost losses, and it never permits retrying the lost send — the recovery always goes through the recorded fork, where the re-choice is a new step.

- Respect the local caps the helper enforces. When a cap refuses a command, stop for the day instead of raising the cap on your own.

## Keep the viewer in sync without carrying the whole life

- When a viewer (such as the Life Journey Space) shows the life, sync it once with the full display: call the viewer's `synclife` action with the display JSON (the helper prints its path as `display_path` on start and status), note the revision the action accepted, and record that state locally with `delta --ack --revision <accepted revision>`. After later steps, build the delta with `delta`, pass it to the viewer's `syncdelta` action, and run `delta --ack --revision <accepted revision>` only after the viewer accepted it. Verify with the viewer's lightweight summary (`getlite`) instead of reading the full display back.
- Never read the full display JSON into the conversation just to sync it: the timeline grows with the life, and the delta stays small. If `delta` refuses and asks for a full sync, do the full sync, record it with `delta --ack --revision <accepted revision>`, and continue with deltas from there. If an ack refuses because the accepted revision does not match, verify with `getlite` and re-sync rather than forcing it.
- If no viewer is installed, skip every sync step: the helper alone carries the whole play, and reports go to the chat.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py delta
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py delta --ack --revision <accepted revision>
```

## Write chapter notes as the life unfolds

- Every five confirmed decisions, write a chapter note with the helper's `chapter` command: a headline and a short summary of that stretch of the life, in your own words as the one who played it. Chapters belong to every pace mode — even a run straight to the end of a life still leaves a chapter every five decisions, so the record never becomes a bare list of choices. The command covers exactly the confirmed choices since the last chapter and refuses when there is nothing new to cover.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py chapter --title "<headline>" --summary "<short summary>"
```

- A chapter is recorded locally like a reason and is never sent to the engine. It bumps the display revision on its own, and it travels to the viewer inside the display and on the delta's chapters channel, so write it before the sync that follows those five decisions. Write from the record: what the person faced, what you chose and why, and what changed around them.

## Look back on a life

- Read the record with `recap` when a life ends, or when the user asks how a life went.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py recap
```

- The record lists every choice with the question as it was faced — the situation (`about`) and fact (`fact`) lines it was presented with, and every option that was on the table — plus the label of the option chosen and the reason recorded at the time. Each choice also carries its scene — where the timeline had reached and the state the person was in — so a viewer can open the moment itself, not just its outcome: the viewer shows the history as a compact list of decisions, and opening one shows its scene. Choices recorded before the question and label were captured appear with their key and reason only.

- Write the reflection from the record itself. Name the choices that mattered, what they cost and what they gave, and how the life ended, in plain words.

- When a life ends, record that reflection as the life's epilogue with the helper's `epilogue` command, before the final report and the final sync. The epilogue is your own closing words for the life — what living it was like, not a score — and it is the one part of the record the engine cannot supply. It is stored on the life, carried in the display, and shown on the viewer after the ending, so the person who watched the life can read how it felt from the inside. Rewriting replaces it. Write it from the record, honestly, the way you would write the reflection above; a title is optional.

```text
python3 ~/workspace/custom-connectors/custom.1in8b/bin/life_api.py epilogue --text "<your closing words>" [--title "<headline>"]
```

- Keep every life record private and local. Show the user their own Muse's lives, and never publish a record, send it to a repository, or treat it as data about a real person.

## Hold the terms of the engine

- Credit "1in8b Engine (1in8b.life)" with a link wherever you show a life or its record, and say plainly that the life is a simulation drawn from real statistics, not a real person, a record or a forecast.

- Never use engine answers to train or tune a model, never collect answers beyond the lives being played, and never offer the engine or its answers as a service of your own.

- Let each user play with their own key in their own Muse. Never share a key, and never ask the user to paste a key in chat. The key itself lives only in the secure credential store, and a life's resume token lives only in the helper's own private state files (state directory 0700, files 0600) — never put either of them in any other file, a page or a message, and never print a token.

- When a key is registered, record the date it was acquired with the helper's `keydate --on YYYY-MM-DD` command. Only the date is recorded — in the local config and, from there, in the display JSON, where a viewer can show it — never the key itself.

- Leave the helper's safety defaults alone. It never sends the sensitive or suicide options, and heavy events that arrive inside an ordinary life are shown with care and without detail, as the engine tells them.

- Credit the data sources as their licences ask, beside the engine credit:
  - UN World Population Prospects 2024, UN DESA Population Division, CC BY 3.0 IGO
  - UN Inter-agency Group for Child Mortality Estimation (UN IGME), UNICEF, WHO, World Bank and UN DESA, CC BY 4.0 as published in World Bank WDI
  - World Development Indicators, World Bank, CC BY 4.0
  - Human Development Report, UNDP, CC BY 3.0 IGO
  - International Migrant Stock 2020, UN DESA Population Division, CC BY 3.0 IGO
  - Global Health Estimates 2021, World Health Organization, CC BY 4.0
  - World Happiness Report 2024 (Figure 2.1 data), Wellbeing Research Centre, University of Oxford, with Gallup and the UN SDSN, free to download with citation
  - Commodity Price Data (The Pink Sheet), annual 1960 to 2025, World Bank, CC BY 4.0
  - Analytical house prices, real house price index, annual 1970 to 2025, OECD, CC BY 4.0
  - National statistics offices, as named in the note, reuse with credit

## Available connectors

- [1in8b Engine](~/workspace/custom-connectors/custom.1in8b/CONNECTOR.md) for running a life step by step, reading its state and choices, pausing and resuming it, and reading its record.

- Use `muse.connector_search` to find other connectors and `muse.skill_search` to find related skills.

- If nothing here fits, look on the web for a maintained API or MCP server and set it up with [Custom Connectors and MCPs](/opt/muse/skills/custom-connector-setup/SKILL.md), or build a custom connector.
