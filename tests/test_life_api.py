#!/usr/bin/env python3
"""End-to-end tests for the custom.1in8b helper, against the mock engine.

Every test runs the real CLI in a subprocess with LJ_MOCK=1 and a fresh
temporary LJ_STATE_DIR, so state handling, locking, permissions and exit
codes are all exercised the way Muse will use them. No network is touched.
The one real-mode test points the credential daemon socket at a path that
does not exist, which makes the surrogate fetch fail before any connection
can be attempted, and checks that the helper stops as not_connected.

Run: python3 -m unittest discover -s ~/workspace/life-journey-skill/tests
"""

import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve()
_INSTALLED = _HERE.parents[2] / "custom-connectors" / "custom.1in8b" / "bin" / "life_api.py"
_IN_KIT = _HERE.parents[1] / "connector" / "custom.1in8b" / "bin" / "life_api.py"
HELPER = _INSTALLED if _INSTALLED.exists() else _IN_KIT

_spec = importlib.util.spec_from_file_location("life_api_module", HELPER)
life_api_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(life_api_module)


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def keys_recursive(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append(str(key))
            found.extend(keys_recursive(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(keys_recursive(value))
    return found


class LifeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, mock=True, extra_env=None, expect=None):
        env = dict(os.environ)
        env["LJ_STATE_DIR"] = str(self.state)
        if mock:
            env["LJ_MOCK"] = "1"
        else:
            env.pop("LJ_MOCK", None)
        env.pop("LJ_MOCK_FAULT", None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(HELPER), *args],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "args=%s stdout=%s stderr=%s" % (args, proc.stdout, proc.stderr))
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:
            return proc.returncode, {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr}

    def start(self, seed=42):
        rc, out = self.run_cli("start", "--seed", str(seed), expect=0)
        self.assertTrue(out["ok"])
        return out

    def life_json(self, life_id):
        return json.loads((self.state / "lives" / life_id / "life.json").read_text())

    def display_json(self, life_id):
        return json.loads((self.state / "lives" / life_id / "display.json").read_text())

    def synced_path(self, life_id):
        return self.state / "sync" / ("%s.json" % life_id)

    def pending_delta_path(self, life_id):
        return self.state / "sync" / ("%s.delta.json" % life_id)

    def first_choice_key(self):
        rc, out = self.run_cli("choices", expect=0)
        return out["decision"]["choices"][0]["key"]

    def ack(self, life_id):
        """delta --ack bound to the revision the viewer accepted: the
        pending delta's target when one is pending, otherwise the
        current display's revision (a full-sync ack)."""
        pending = self.pending_delta_path(life_id)
        if pending.exists():
            revision = json.loads(pending.read_text())["target_revision"]
        else:
            revision = self.display_json(life_id)["revision"]
        return self.run_cli("delta", "--ack", "--revision", str(revision),
                            "--life", life_id, expect=0)

    # ------------------------------------------------------------------

    def test_start_permissions_and_display_has_no_token(self):
        out = self.start()
        life_id = out["life_id"]
        life_file = self.state / "lives" / life_id / "life.json"
        display_file = self.state / "lives" / life_id / "display.json"

        self.assertEqual(mode_of(self.state), 0o700)
        self.assertEqual(mode_of(self.state / "lives" / life_id), 0o700)
        self.assertEqual(mode_of(life_file), 0o600)
        self.assertEqual(mode_of(display_file), 0o600)
        self.assertEqual(mode_of(self.state / "index.json"), 0o600)
        self.assertEqual(mode_of(self.state / "config.json"), 0o600)

        life = json.loads(life_file.read_text())
        token = life["token"]
        self.assertTrue(token)
        display_text = display_file.read_text()
        self.assertNotIn(token, display_text)
        display = json.loads(display_text)
        self.assertNotIn("token", keys_recursive(display))
        self.assertEqual(display["schema_version"], 1)
        self.assertEqual(display["credit"], "1in8b Engine (1in8b.life)")
        self.assertTrue(display["sources"])
        self.assertEqual(display["animation"]["kind"], "thinking")

        rc, cat = self.run_cli("catalog", "countries", expect=0)
        self.assertTrue(cat["ok"])

    def test_choose_progress_reason_pause_resume(self):
        out = self.start(seed=7)
        life_id = out["life_id"]
        key = self.first_choice_key()
        rc, chosen = self.run_cli("choose", key, "--reason", "最初の理由テスト", expect=0)
        self.assertEqual(chosen["step_count"], 1)
        self.assertEqual(chosen["age"], 15)

        history = (self.state / "lives" / life_id / "history.jsonl").read_text()
        self.assertIn("最初の理由テスト", history)
        display = json.loads((self.state / "lives" / life_id / "display.json").read_text())
        self.assertEqual(display["last_choice"]["reason"], "最初の理由テスト")

        self.run_cli("pause", expect=0)
        rc, refused = self.run_cli("choose", "local", "--reason", "止まっているはず")
        self.assertEqual(rc, 2)
        self.assertFalse(refused["ok"])

        rc, resumed = self.run_cli("resume", expect=0)
        self.assertEqual(resumed["status"], "active")
        key2 = self.first_choice_key()
        rc, chosen2 = self.run_cli("choose", key2, "--reason", "再開後の理由", expect=0)
        self.assertEqual(chosen2["step_count"], 2)

    def test_timeout_marks_unknown_and_never_resends(self):
        out = self.start(seed=3)
        life_id = out["life_id"]
        token_before = self.life_json(life_id)["token"]
        key = self.first_choice_key()

        rc, failed = self.run_cli(
            "choose", key, "--reason", "落ちるはずの選択",
            extra_env={"LJ_MOCK_FAULT": "timeout"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        # A timeout is a response_lost loss, and the output says so.
        self.assertEqual(failed["detail"], "timeout")
        self.assertEqual(failed["unknown_class"], "response_lost")

        life = self.life_json(life_id)
        self.assertEqual(life["status"], "unknown")
        # The mock state travels inside the token, and the helper still holds
        # the pre-step token, so the life provably did not advance.
        self.assertEqual(life["token"], token_before)
        journal = (self.state / "lives" / life_id / "journal.jsonl").read_text()
        self.assertIn('"intent"', journal)
        self.assertIn('"unknown"', journal)

        rc, refused = self.run_cli("choose", key, "--reason", "再送はできない")
        self.assertEqual(rc, 2)

        rc, seen = self.run_cli("reconcile", expect=0)
        self.assertTrue(seen["was_unknown"])
        self.assertFalse(seen["abandoned"])
        rc, refused2 = self.run_cli("choose", key, "--reason", "まだだめ")
        self.assertEqual(rc, 2)

        rc, cleared = self.run_cli("reconcile", "--abandon", expect=0)
        self.assertTrue(cleared["abandoned"])
        self.assertEqual(self.life_json(life_id)["status"], "active")
        journal = (self.state / "lives" / life_id / "journal.jsonl").read_text()
        self.assertIn('"abandoned"', journal)

        rc, manual = self.run_cli("choose", key, "--reason", "手動で選び直した", expect=0)
        self.assertTrue(manual["ok"])

    def test_http500_marks_unknown(self):
        out = self.start(seed=13)
        life_id = out["life_id"]
        token_before = self.life_json(life_id)["token"]
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "500になるはず",
            extra_env={"LJ_MOCK_FAULT": "http500"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        # A plain 500 is not the busy answer: it stays response_lost.
        self.assertEqual(failed["detail"], "internal")
        self.assertEqual(failed["unknown_class"], "response_lost")
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "unknown")
        self.assertEqual(life["token"], token_before)
        self.assertEqual(life["counters"]["unknown_steps"], 1)

    def test_http503_is_gate_class_unknown_and_recovers(self):
        out = self.start(seed=18)
        life_id = out["life_id"]
        token_before = self.life_json(life_id)["token"]
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "混雑で弾かれるはず",
            extra_env={"LJ_MOCK_FAULT": "http503"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        # The busy answer is a gate loss, and the output names its
        # detail and class so the player can tell it apart from a
        # lost response without reading the journal.
        self.assertEqual(failed["detail"], "busy")
        self.assertEqual(failed["unknown_class"], "gate")
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "unknown")
        self.assertEqual(life["token"], token_before)
        self.assertEqual(life["last_incident"]["detail"], "busy")
        display = json.loads(
            (self.state / "lives" / life_id / "display.json").read_text())
        self.assertEqual(display["last_incident"]["detail"], "busy")

        rc, cleared = self.run_cli("reconcile", "--abandon", expect=0)
        self.assertTrue(cleared["abandoned"])
        rc, manual = self.run_cli(
            "choose", key, "--reason", "分岐から選び直した", expect=0)
        self.assertTrue(manual["ok"])

    def test_settings_recorded_on_life_and_chapter_due_flag(self):
        # Settings agreed at the start live on the life's own
        # record, so they survive a change of chat.
        rc, out = self.run_cli(
            "start", "--seed", "29", "--pace", "straight",
            "--ask-mode", "--lost-step-policy", "gate-auto", expect=0)
        life_id = out["life_id"]
        self.assertEqual(out["settings"], {
            "pace": "straight", "ask_mode": True,
            "lost_step_policy": "gate-auto"})
        self.assertFalse(out["chapter_due"])
        life = self.life_json(life_id)
        self.assertEqual(life["settings"]["pace"], "straight")

        # A later change (on the master's word) is recorded in the
        # same place; showing the settings changes nothing.
        rc, changed = self.run_cli("settings", "--pace", "close", expect=0)
        self.assertEqual(changed["changed"], {"pace": "close"})
        self.assertEqual(changed["settings"]["ask_mode"], True)
        rc, shown = self.run_cli("settings", expect=0)
        self.assertEqual(shown["changed"], {})
        self.assertEqual(shown["settings"]["pace"], "close")

        # After five confirmed choices without a chapter, status
        # flags the chapter as due; writing it clears the flag.
        for _ in range(5):
            key = self.first_choice_key()
            self.run_cli("choose", key, "--reason", "章の区切り確認", expect=0)
        rc, status = self.run_cli("status", expect=0)
        self.assertEqual(status["choices_since_chapter"], 5)
        self.assertTrue(status["chapter_due"])
        self.run_cli("chapter", "--title", "五つの決断",
                     "--summary", "章メモの検出テスト。", expect=0)
        rc, status = self.run_cli("status", expect=0)
        self.assertEqual(status["choices_since_chapter"], 0)
        self.assertFalse(status["chapter_due"])

        # Settings never enter the display JSON.
        display = self.display_json(life_id)
        self.assertNotIn("settings", display)

    def test_timeline_keeps_sourced_explanation_element(self):
        out = self.start(seed=23)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "出典つきイベントの確認", expect=0)
        # The mock's step events carry a third element (a sourced
        # explanation). It must survive into the stored timeline and
        # the display whole -- trimming it to [age, text] here would
        # lose it from the history for good.
        display = self.display_json(life_id)
        noted = [e for e in display["timeline"] if len(e) >= 3]
        self.assertTrue(noted, "no 3-element timeline entry survived")
        self.assertIn("モック統計年鑑", noted[-1][2])
        life = self.life_json(life_id)
        self.assertEqual(life["timeline"], display["timeline"])

    def test_bare_503_is_response_lost_not_gate(self):
        out = self.start(seed=19)
        life_id = out["life_id"]
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "コード無しの503で失われる",
            extra_env={"LJ_MOCK_FAULT": "http503bare"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        # A bare HTTP 503 carries no proof that the engine turned
        # the request away before processing -- an intermediary can
        # produce one -- so it is response_lost, never gate, and the
        # automatic fork recovery of a lost-step policy must not
        # cover it. Only an explicit busy code is gate (covered by
        # the http503 test above).
        self.assertEqual(failed["detail"], "http_503")
        self.assertEqual(failed["unknown_class"], "response_lost")
        life = self.life_json(life_id)
        self.assertEqual(life["last_incident"]["detail"], "http_503")

        # Manual recovery through the recorded fork still works.
        rc, cleared = self.run_cli("reconcile", "--abandon", expect=0)
        self.assertTrue(cleared["abandoned"])
        rc, manual = self.run_cli(
            "choose", key, "--reason", "分岐から選び直した", expect=0)
        self.assertTrue(manual["ok"])

    def test_keydate_records_date_into_config_and_display(self):
        # Unrecorded at first, and a fresh display carries null.
        rc, shown = self.run_cli("keydate", expect=0)
        self.assertIsNone(shown["key_acquired_on"])
        out = self.start(seed=7)
        life_id = out["life_id"]
        self.assertIsNone(self.display_json(life_id)["key_acquired_on"])

        # A date that is not a real calendar date is refused locally
        # and changes nothing.
        rc, refused = self.run_cli("keydate", "--on", "2026-13-40")
        self.assertEqual(rc, 2)
        self.assertEqual(refused["error"], "rejected")
        rc, shown = self.run_cli("keydate", expect=0)
        self.assertIsNone(shown["key_acquired_on"])

        # Recording the date lands in the config and refreshes the
        # current life's display, so a viewer can show when the key
        # was acquired. Only the date is stored, never the key.
        rc, recorded = self.run_cli("keydate", "--on", "2026-01-15", expect=0)
        self.assertEqual(recorded["key_acquired_on"], "2026-01-15")
        config = json.loads((self.state / "config.json").read_text())
        self.assertEqual(config["key_acquired_on"], "2026-01-15")
        self.assertEqual(
            self.display_json(life_id)["key_acquired_on"], "2026-01-15")

    def test_keydate_change_bumps_revision_and_syncs_by_delta(self):
        out = self.start(seed=11)
        life_id = out["life_id"]
        base_display = self.display_json(life_id)
        rc, acked = self.ack(life_id)
        self.assertEqual(acked["revision"], base_display["revision"])

        # Recording the acquisition date advances the life's
        # revision, so the change travels by delta like any other
        # display change instead of deadlocking the sync (a delta
        # needs the bump; a same-revision full sync is a conflict).
        rc, recorded = self.run_cli("keydate", "--on", "2026-01-15", expect=0)
        self.assertEqual(recorded["revision"], base_display["revision"] + 1)
        current_display = self.display_json(life_id)
        self.assertEqual(current_display["key_acquired_on"], "2026-01-15")
        self.assertEqual(current_display["revision"],
                         base_display["revision"] + 1)

        rc, delta = self.run_cli("delta", expect=0)
        self.assertEqual(delta["base_revision"], base_display["revision"])
        self.assertEqual(delta["target_revision"],
                         current_display["revision"])
        self.assertEqual(delta["replace"]["key_acquired_on"], "2026-01-15")

        # Recording the same date again changes nothing and does
        # not advance the revision.
        rc, again = self.run_cli("keydate", "--on", "2026-01-15", expect=0)
        self.assertIsNone(again["revision"])
        self.assertEqual(self.display_json(life_id)["revision"],
                         current_display["revision"])

    def test_broken_success_body_marks_unknown(self):
        out = self.start(seed=14)
        life_id = out["life_id"]
        token_before = self.life_json(life_id)["token"]
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "応答が壊れるはず",
            extra_env={"LJ_MOCK_FAULT": "badjson"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "unknown")
        # The new token never arrived intact, so the held token is the
        # pre-step one even though the mock state advanced behind it.
        self.assertEqual(life["token"], token_before)

    def test_abandon_resumes_and_leaves_record(self):
        out = self.start(seed=15)
        life_id = out["life_id"]
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "失われる選択",
            extra_env={"LJ_MOCK_FAULT": "timeout"},
        )
        self.assertEqual(rc, 3)

        rc, cleared = self.run_cli("reconcile", "--abandon", expect=0)
        self.assertTrue(cleared["abandoned"])
        display = json.loads((self.state / "lives" / life_id / "display.json").read_text())
        self.assertEqual(display["last_incident"]["type"], "abandoned")
        self.assertEqual(display["last_incident"]["choose"], key)

        rc, manual = self.run_cli("choose", key, "--reason", "旧トークンから選び直した", expect=0)
        self.assertTrue(manual["ok"])

        journal = (self.state / "lives" / life_id / "journal.jsonl").read_text()
        self.assertIn('"abandoned"', journal)
        rc, recap = self.run_cli("recap", expect=0)
        self.assertEqual(recap["unknown_steps"], 1)
        self.assertEqual(recap["step_intents"], 2)
        self.assertEqual(len(recap["choices"]), 1)
        incident_types = [row["type"] for row in recap["incidents"]]
        self.assertIn("unknown", incident_types)
        self.assertIn("abandoned", incident_types)

    def test_counters_split_after_unknown(self):
        out = self.start(seed=16)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli(
            "choose", key, "--reason", "不明になる",
            extra_env={"LJ_MOCK_FAULT": "timeout"},
        )
        self.run_cli("reconcile", "--abandon", expect=0)
        self.run_cli("choose", key, "--reason", "確定する選択", expect=0)
        display = json.loads((self.state / "lives" / life_id / "display.json").read_text())
        self.assertEqual(display["counters"]["step_intents"], 2)
        self.assertEqual(display["counters"]["confirmed_steps"], 1)
        self.assertEqual(display["counters"]["unknown_steps"], 1)
        # Display step_count means confirmed steps only.
        self.assertEqual(display["step_count"], 1)
        life = self.life_json(life_id)
        self.assertEqual(life["counters"]["step_intents"], 2)
        self.assertEqual(life["step_count"], 1)

    def test_revision_increases_per_step(self):
        out = self.start(seed=17)
        life_id = out["life_id"]
        display_path = self.state / "lives" / life_id / "display.json"
        r0 = json.loads(display_path.read_text())["revision"]
        self.assertGreaterEqual(r0, 1)
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "一歩目", expect=0)
        r1 = json.loads(display_path.read_text())["revision"]
        self.assertGreater(r1, r0)
        key2 = self.first_choice_key()
        self.run_cli("choose", key2, "--reason", "二歩目", expect=0)
        r2 = json.loads(display_path.read_text())["revision"]
        self.assertGreater(r2, r1)

    def test_delta_requires_synced_snapshot(self):
        out = self.start(seed=31)
        life_id = out["life_id"]
        rc, refused = self.run_cli("delta", "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertFalse(refused["ok"])
        self.assertIn("full sync", refused["message"])
        self.assertFalse(self.pending_delta_path(life_id).exists())

    def test_delta_after_mock_step_and_ack_cycle(self):
        out = self.start(seed=42)
        life_id = out["life_id"]
        base_display = self.display_json(life_id)

        rc, acked = self.ack(life_id)
        self.assertEqual(acked["status"], "synced")
        self.assertEqual(acked["revision"], base_display["revision"])
        self.assertEqual(mode_of(self.state / "sync"), 0o700)
        self.assertEqual(mode_of(self.synced_path(life_id)), 0o600)
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()), base_display)

        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "差分テストの一歩", expect=0)
        current_display = self.display_json(life_id)
        self.assertGreater(current_display["revision"], base_display["revision"])

        rc, delta = self.run_cli("delta", expect=0)
        self.assertEqual(delta["schema_version"], 1)
        self.assertEqual(delta["life_id"], life_id)
        self.assertEqual(delta["base_revision"], base_display["revision"])
        self.assertEqual(delta["target_revision"], current_display["revision"])
        self.assertNotIn("revision", delta["replace"])
        self.assertNotIn("timeline", delta["replace"])
        self.assertEqual(delta["replace"]["last_choice"]["choose"], key)
        self.assertEqual(delta["replace"]["person"]["age"],
                         current_display["person"]["age"])
        self.assertEqual(delta["timeline_from"], len(base_display["timeline"]))
        self.assertEqual(delta["timeline_append"],
                         current_display["timeline"][delta["timeline_from"]:])
        self.assertGreater(len(delta["timeline_append"]), 0)
        # The display carries the choices array from the start (empty
        # until the first choice), so the channel runs from offset 0.
        self.assertEqual(delta["choices_from"], 0)
        self.assertEqual(len(delta["choices_append"]), 1)
        self.assertEqual(delta["choices_append"][0]["choose"], key)
        self.assertEqual(delta["choices_append"][0]["reason"], "差分テストの一歩")
        self.assertEqual(mode_of(self.pending_delta_path(life_id)), 0o600)
        self.assertEqual(json.loads(self.pending_delta_path(life_id).read_text()), delta)

        rc, acked2 = self.ack(life_id)
        self.assertEqual(acked2["revision"], current_display["revision"])
        self.assertFalse(self.pending_delta_path(life_id).exists())
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()),
                         current_display)

        rc, again = self.run_cli("delta", expect=0)
        self.assertEqual(again["status"], "up_to_date")
        self.assertEqual(again["revision"], current_display["revision"])
        self.assertFalse(self.pending_delta_path(life_id).exists())

    def test_choose_records_question_label_and_age(self):
        out = self.start(seed=7)
        life_id = out["life_id"]
        rc, waiting = self.run_cli("choices", expect=0)
        decision = waiting["decision"]
        key = decision["choices"][0]["key"]
        label = decision["choices"][0]["label"]
        before = self.display_json(life_id)
        self.run_cli("choose", key, "--reason", "記録テストの理由", expect=0)

        rows = [json.loads(line) for line in
                (self.state / "lives" / life_id / "history.jsonl")
                .read_text().splitlines() if line.strip()]
        choice_rows = [r for r in rows if r.get("type") == "choice"]
        self.assertEqual(len(choice_rows), 1)
        row = choice_rows[0]
        self.assertEqual(row["question"], decision["question"])
        self.assertEqual(row["about"], decision["about"])
        self.assertEqual(row["fact"], decision["fact"])
        self.assertEqual(
            row["options"],
            [{"key": c["key"], "label": c["label"]}
             for c in decision["choices"]])
        self.assertEqual(row["label"], label)
        self.assertEqual(row["age"], decision["at_age"])

        display = self.display_json(life_id)
        self.assertEqual(len(display["choices"]), 1)
        entry = display["choices"][0]
        self.assertEqual(entry["choose"], key)
        self.assertEqual(entry["label"], label)
        self.assertEqual(entry["question"], decision["question"])
        self.assertEqual(entry["about"], decision["about"])
        self.assertEqual(entry["fact"], decision["fact"])
        self.assertEqual(
            entry["options"],
            [{"key": c["key"], "label": c["label"]}
             for c in decision["choices"]])
        self.assertEqual(entry["age"], decision["at_age"])
        self.assertEqual(entry["reason"], "記録テストの理由")
        scene = entry["scene"]
        self.assertEqual(scene["timeline_len"], len(before["timeline"]))
        for field in ("health", "happiness", "knowledge", "bonds"):
            self.assertEqual(scene["state"][field], before["state"][field])
        self.assertEqual(row["scene"], scene)
        self.assertEqual(display["last_choice"]["label"], label)
        self.assertEqual(display["last_choice"]["question"],
                         decision["question"])
        self.assertEqual(display["last_choice"]["about"],
                         decision["about"])
        self.assertEqual(display["last_choice"]["fact"],
                         decision["fact"])

        rc, recap = self.run_cli("recap", expect=0)
        self.assertEqual(recap["choices"][0]["question"],
                         decision["question"])
        self.assertEqual(recap["choices"][0]["label"], label)

    def test_delta_choices_channel_from_missing_baseline(self):
        # A synced snapshot from before the choices array existed (no
        # "choices" key at all) is an empty baseline: the next delta
        # appends every entry from offset 0, and the ack reconstruction
        # lands on exactly the current display.
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "基準なしチャネル", expect=0)
        legacy = {k: v for k, v in self.display_json(life_id).items()
                  if k != "choices"}
        self.synced_path(life_id).parent.mkdir(exist_ok=True)
        self.synced_path(life_id).write_text(json.dumps(legacy))

        key2 = self.first_choice_key()
        self.run_cli("choose", key2, "--reason", "二歩目", expect=0)
        current = self.display_json(life_id)
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["choices_from"], 0)
        self.assertEqual(len(delta["choices_append"]), 2)
        merged = life_api_module.apply_delta_to_display(legacy, delta)
        self.assertEqual(merged["choices"], current["choices"])
        self.assertEqual(merged["revision"], current["revision"])

        rc, acked = self.ack(life_id)
        self.assertEqual(acked["revision"], current["revision"])
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()),
                         current)

    def test_chapter_records_display_and_revision(self):
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "章テストの一歩", expect=0)
        before = self.display_json(life_id)

        rc, written = self.run_cli(
            "chapter", "--title", "はじまりの章",
            "--summary", "最初の決断までの歩み。", expect=0)
        self.assertTrue(written["ok"])
        chapter = written["chapter"]
        self.assertEqual(chapter["index"], 1)
        self.assertEqual(chapter["from_step"], 1)
        self.assertEqual(chapter["to_step"], 1)
        self.assertEqual(chapter["from_age"], before["choices"][0]["age"])
        self.assertEqual(chapter["to_age"], before["choices"][0]["age"])
        self.assertEqual(chapter["title"], "はじまりの章")

        display = self.display_json(life_id)
        self.assertEqual(display["chapters"], [chapter])
        # A chapter is viewer-visible, so it bumps the revision by
        # itself, without any step in between.
        self.assertEqual(display["revision"], before["revision"] + 1)

        rc, recap = self.run_cli("recap", expect=0)
        self.assertEqual(recap["chapters"], [chapter])

        # Nothing new to cover: a second chapter right away refuses.
        rc, refused = self.run_cli(
            "chapter", "--title", "空の章", "--summary", "まだ何もない。")
        self.assertEqual(rc, 2)
        self.assertIn("no confirmed choices", refused["message"])

        key2 = self.first_choice_key()
        self.run_cli("choose", key2, "--reason", "章テストの二歩", expect=0)
        rc, written2 = self.run_cli(
            "chapter", "--title", "つぎの章",
            "--summary", "二歩目だけを覆う。", expect=0)
        self.assertEqual(written2["chapter"]["index"], 2)
        self.assertEqual(written2["chapter"]["from_step"], 2)
        self.assertEqual(written2["chapter"]["to_step"], 2)

    def test_chapter_refuses_credential_shaped_text(self):
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "通常の一歩", expect=0)
        rc, refused = self.run_cli(
            "chapter", "--title", "だめな章",
            "--summary", "鍵は k8b_" + "a" * 40 + " です")
        self.assertEqual(rc, 2)
        self.assertIn("credential", refused["message"])
        self.assertEqual(self.display_json(life_id)["chapters"], [])

    def test_delta_chapters_channel_and_ack_cycle(self):
        out = self.start(seed=42)
        life_id = out["life_id"]
        self.ack(life_id)

        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "章差分の一歩", expect=0)
        self.run_cli("chapter", "--title", "差分の章",
                     "--summary", "章は delta で運ばれる。", expect=0)
        current = self.display_json(life_id)

        rc, delta = self.run_cli("delta", expect=0)
        self.assertEqual(delta["chapters_from"], 0)
        self.assertEqual(len(delta["chapters_append"]), 1)
        self.assertEqual(delta["chapters_append"][0]["title"], "差分の章")
        self.assertNotIn("chapters", delta["replace"])
        merged = life_api_module.apply_delta_to_display(
            json.loads(self.synced_path(life_id).read_text()), delta)
        self.assertEqual(merged["chapters"], current["chapters"])
        self.assertEqual(merged["revision"], current["revision"])

        rc, acked = self.ack(life_id)
        self.assertEqual(acked["revision"], current["revision"])
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()),
                         current)

    def test_delta_chapters_channel_from_missing_baseline(self):
        # A synced snapshot from before the chapters array existed is
        # an empty baseline, exactly like the choices channel: the
        # snapshot predates the chapter (and its revision bump), and
        # the next delta appends every chapter from offset 0.
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "基準なしの章", expect=0)
        legacy = {k: v for k, v in self.display_json(life_id).items()
                  if k != "chapters"}
        self.synced_path(life_id).parent.mkdir(exist_ok=True)
        self.synced_path(life_id).write_text(json.dumps(legacy))

        self.run_cli("chapter", "--title", "旧基準の章",
                     "--summary", "配列ごと運ばれる。", expect=0)
        current = self.display_json(life_id)
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["chapters_from"], 0)
        self.assertEqual(len(delta["chapters_append"]), 1)
        merged = life_api_module.apply_delta_to_display(legacy, delta)
        self.assertEqual(merged["chapters"], current["chapters"])
        self.assertEqual(merged["revision"], current["revision"])

        rc, acked = self.ack(life_id)
        self.assertEqual(acked["revision"], current["revision"])
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()),
                         current)

    def test_legacy_history_rows_render_null_choice_fields(self):
        out = self.start(seed=7)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "旧形式の行", expect=0)
        history_path = self.state / "lives" / life_id / "history.jsonl"
        rows = [json.loads(line) for line in
                history_path.read_text().splitlines() if line.strip()]
        for row in rows:
            if row.get("type") == "choice":
                for field in ("age", "question", "label", "scene"):
                    row.pop(field, None)
        history_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        self.run_cli("status", expect=0)
        display = self.display_json(life_id)
        self.assertEqual(len(display["choices"]), 1)
        entry = display["choices"][0]
        self.assertIsNone(entry["age"])
        self.assertIsNone(entry["question"])
        self.assertIsNone(entry["label"])
        self.assertIsNone(entry["scene"])
        self.assertEqual(entry["choose"], key)
        self.assertEqual(entry["reason"], "旧形式の行")

    def test_delta_ignores_view_only_refresh(self):
        # A bare status view refreshes the display without a revision
        # bump: it clears the transient events list, moves updated_at
        # and resets usage.this_call. None of that is a sync-worthy
        # change, so delta must report up_to_date rather than refusing.
        out = self.start(seed=33)
        life_id = out["life_id"]
        self.ack(life_id)
        self.run_cli("status", expect=0)
        rc, result = self.run_cli("delta", expect=0)
        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(result["revision"],
                         self.display_json(life_id)["revision"])
        self.assertFalse(self.pending_delta_path(life_id).exists())

    def rewrite_display(self, life_id, mutate):
        path = self.state / "lives" / life_id / "display.json"
        display = json.loads(path.read_text())
        mutate(display)
        path.write_text(json.dumps(display, ensure_ascii=False))
        return json.loads(path.read_text())

    @staticmethod
    def apply_delta_reference(base, delta):
        """A small reference merge: replace + timeline append + sheet
        remove/upsert/order, mirroring what a v1/v2 viewer must do."""
        merged = copy.deepcopy(base)
        for key, value in delta["replace"].items():
            merged[key] = copy.deepcopy(value)
        merged["revision"] = delta["target_revision"]
        merged["timeline"] = (
            merged["timeline"][:delta["timeline_from"]]
            + copy.deepcopy(delta["timeline_append"]))
        if "sheet_items" in delta:
            by_id = {item["id"]: item for item in merged["sheet"]}
            for removed_id in delta["sheet_remove"]:
                by_id.pop(removed_id, None)
            for item in delta["sheet_items"]:
                by_id[item["id"]] = copy.deepcopy(item)
            ordered = [by_id[item_id] for item_id in delta["sheet_order"]
                       if item_id in by_id]
            ordered += [item for item_id, item in by_id.items()
                        if item_id not in delta["sheet_order"]]
            merged["sheet"] = ordered
        return merged

    def sheet_delta_scenario(self):
        """A life whose synced snapshot has a 3-item sheet, after which
        the current display flips one item, adds one, removes one and
        reorders, with a revision bump."""
        out = self.start(seed=34)
        life_id = out["life_id"]
        baseline = [
            {"id": "own_tv", "name": "テレビ・冷蔵庫", "does": "幸福 +5",
             "price_ppp": 1682, "can": False, "why": "お金が足りません"},
            {"id": "own_bike", "name": "自転車", "does": "幸福 +3",
             "price_ppp": 421, "can": True, "why": None},
            {"id": "dinner", "name": "外食", "does": "幸福 +2",
             "price_ppp": 242, "can": True, "why": None},
        ]

        def set_baseline(display):
            display["sheet"] = copy.deepcopy(baseline)

        base_display = self.rewrite_display(life_id, set_baseline)
        self.ack(life_id)

        def mutate(display):
            television = dict(display["sheet"][0])
            television["can"] = True
            television["why"] = None
            car = {"id": "own_car", "name": "車", "does": "幸福 +8",
                   "price_ppp": 15431, "can": False,
                   "why": "お金が足りません"}
            bicycle = display["sheet"][1]
            display["sheet"] = [bicycle, car, television]
            display["revision"] += 1

        current_display = self.rewrite_display(life_id, mutate)
        return life_id, base_display, current_display

    def test_delta_v2_sheet_item_channel(self):
        life_id, base_display, current_display = self.sheet_delta_scenario()
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["schema_version"], 2)
        self.assertEqual(delta["base_revision"], base_display["revision"])
        self.assertEqual(delta["target_revision"],
                         current_display["revision"])
        self.assertNotIn("sheet", delta["replace"])
        items_by_id = {item["id"]: item for item in delta["sheet_items"]}
        self.assertEqual(sorted(items_by_id), ["own_car", "own_tv"])
        self.assertEqual(items_by_id["own_tv"], current_display["sheet"][2])
        self.assertEqual(items_by_id["own_car"], current_display["sheet"][1])
        self.assertEqual(delta["sheet_remove"], ["dinner"])
        self.assertEqual(delta["sheet_order"],
                         ["own_bike", "own_car", "own_tv"])
        self.assertEqual(
            json.loads(self.pending_delta_path(life_id).read_text()), delta)

    def test_delta_v2_reconstructs_current_display(self):
        life_id, base_display, current_display = self.sheet_delta_scenario()
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        merged = self.apply_delta_reference(base_display, delta)
        self.assertEqual(merged, current_display)

    def test_delta_sheet_bad_shape_falls_back_to_v1(self):
        out = self.start(seed=35)
        life_id = out["life_id"]
        self.ack(life_id)

        def mutate(display):
            display["sheet"] = "シートではない"
            display["revision"] += 1

        self.rewrite_display(life_id, mutate)
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["schema_version"], 1)
        self.assertEqual(delta["replace"]["sheet"], "シートではない")
        self.assertNotIn("sheet_items", delta)

    def test_delta_out_of_band_sheet_edit_without_bump_refused(self):
        # Display files edited behind the helper's back carry no
        # revision bump, so delta refuses and a full sync is required.
        # (The same reorder arriving through an engine answer bumps
        # the revision and syncs -- see the through-a-view test.)
        out = self.start(seed=36)
        life_id = out["life_id"]
        baseline = [
            {"id": "own_tv", "name": "テレビ・冷蔵庫", "does": "幸福 +5",
             "price_ppp": 1682, "can": False, "why": "お金が足りません"},
            {"id": "own_bike", "name": "自転車", "does": "幸福 +3",
             "price_ppp": 421, "can": True, "why": None},
        ]

        def set_baseline(display):
            display["sheet"] = copy.deepcopy(baseline)

        self.rewrite_display(life_id, set_baseline)
        self.ack(life_id)

        def swap(display):
            display["sheet"] = [display["sheet"][1], display["sheet"][0]]

        self.rewrite_display(life_id, swap)
        rc, refused = self.run_cli("delta", "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertFalse(refused["ok"])
        self.assertIn("without a revision bump", refused["message"])
        self.assertFalse(self.pending_delta_path(life_id).exists())

    def test_delta_timeline_prefix_mismatch_refused(self):
        out = self.start(seed=32)
        life_id = out["life_id"]
        self.ack(life_id)
        snapshot = json.loads(self.synced_path(life_id).read_text())
        snapshot["timeline"][0] = [snapshot["timeline"][0][0], "書き換えられた年表"]
        self.synced_path(life_id).write_text(json.dumps(snapshot, ensure_ascii=False))
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "年表が壊れているはず", expect=0)
        rc, refused = self.run_cli("delta")
        self.assertEqual(rc, 2)
        self.assertFalse(refused["ok"])
        self.assertIn("timeline", refused["message"])
        self.assertIn("full sync", refused["message"])
        self.assertFalse(self.pending_delta_path(life_id).exists())

    def test_delta_tolerates_null_field_extension(self):
        # A snapshot and history written before the producer recorded
        # the about / fact / options fields: the snapshot's entry
        # lacks the keys, and the row lacks them too, so the
        # regenerated entry carries them as null. Absent and null are
        # the same information, not a rewrite: the delta must still
        # append only the new entry, and keep working after the ack
        # re-bases the snapshot on the merged display.
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "一歩目", expect=0)
        self.ack(life_id)
        snapshot = json.loads(self.synced_path(life_id).read_text())
        for entry in snapshot["choices"]:
            for field in ("about", "fact", "options"):
                entry.pop(field, None)
        self.synced_path(life_id).write_text(json.dumps(snapshot))
        history_path = self.state / "lives" / life_id / "history.jsonl"
        rows = [json.loads(line)
                for line in history_path.read_text().splitlines()
                if line.strip()]
        for row in rows:
            if row.get("type") == "choice":
                for field in ("about", "fact", "options"):
                    row.pop(field, None)
        history_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        self.run_cli("status", "--life", life_id, expect=0)
        entry = self.display_json(life_id)["choices"][0]
        self.assertIn("about", entry)
        self.assertIsNone(entry["about"])
        self.assertIsNone(entry["options"])

        key2 = self.first_choice_key()
        self.run_cli("choose", key2, "--reason", "二歩目", expect=0)
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["choices_from"], 1)
        self.assertEqual(len(delta["choices_append"]), 1)

        rc, acked = self.ack(life_id)
        self.assertTrue(acked["ok"])
        key3 = self.first_choice_key()
        self.run_cli("choose", key3, "--reason", "三歩目", expect=0)
        rc, delta2 = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta2["choices_from"], 2)
        self.assertEqual(len(delta2["choices_append"]), 1)

    def test_delta_refuses_real_value_change_on_old_entry(self):
        # The null-field tolerance must not mask a genuine rewrite:
        # an old entry whose recorded situation text changes is still
        # refused, and a full sync is required.
        out = self.start(seed=42)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "一歩目", expect=0)
        self.ack(life_id)
        history_path = self.state / "lives" / life_id / "history.jsonl"
        rows = [json.loads(line)
                for line in history_path.read_text().splitlines()
                if line.strip()]
        for row in rows:
            if row.get("type") == "choice":
                row["about"] = "書き換えられた状況"
        history_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        self.run_cli("status", "--life", life_id, expect=0)
        self.assertEqual(self.display_json(life_id)["choices"][0]["about"],
                         "書き換えられた状況")
        key2 = self.first_choice_key()
        self.run_cli("choose", key2, "--reason", "二歩目", expect=0)
        rc, refused = self.run_cli("delta", "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertFalse(refused["ok"])
        self.assertIn("choices", refused["message"])
        self.assertIn("full sync", refused["message"])

    def test_start_timeout_is_unknown_start(self):
        rc, failed = self.run_cli(
            "start", "--seed", "21",
            extra_env={"LJ_MOCK_FAULT": "timeout"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "start_outcome_unknown")
        journal = (self.state / "journal.jsonl").read_text()
        self.assertIn('"unknown_start"', journal)
        rc, hist = self.run_cli("history", expect=0)
        self.assertEqual(hist["lives"], [])
        counters = json.loads((self.state / "counters.json").read_text())
        days = list(counters["days"].values())
        self.assertEqual(days[0]["starts"], 1)

    def test_animation_health_only_change_is_not_emotion(self):
        classify = life_api_module.classify_animation
        prev = {"country": "JPN", "health": 90, "happiness": 70}
        snap = {"ended": False, "decision": None,
                "state": {"country": "JPN", "health": 50, "happiness": 70}}
        kind, signal, trend = classify(prev, snap)
        self.assertEqual(kind, "idle")
        self.assertEqual(trend, "down")

        snap_happy = {"ended": False, "decision": None,
                      "state": {"country": "JPN", "health": 90, "happiness": 95}}
        kind, signal, trend = classify(
            {"country": "JPN", "health": 90, "happiness": 70}, snap_happy)
        self.assertEqual(kind, "happy")
        self.assertEqual(trend, "flat")

        kind, signal, trend = classify({}, {"ended": False, "decision": None, "state": {}})
        self.assertEqual(kind, "idle")
        self.assertEqual(trend, "flat")

    def test_start_cap_refuses_second_start(self):
        self.state.mkdir(parents=True)
        (self.state / "config.json").write_text(json.dumps({"max_starts_per_day": 1}))
        self.start(seed=1)
        rc, refused = self.run_cli("start", "--seed", "2")
        self.assertEqual(rc, 2)
        self.assertIn("max_starts_per_day", refused["message"])

    def test_concurrent_starts_keep_index_intact(self):
        # Both starts deliberately override the one-life guard with
        # --force; what is under test is index integrity under the lock.
        def one(seed):
            return self.run_cli("start", "--seed", str(seed), "--force",
                                expect=0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(one, [11, 22]))
        ids = {r[1]["life_id"] for r in results}
        self.assertEqual(len(ids), 2)
        index = json.loads((self.state / "index.json").read_text())
        self.assertEqual(len(index["lives"]), 2)
        rc, hist = self.run_cli("history", expect=0)
        self.assertEqual(len(hist["lives"]), 2)

    def test_full_mock_life_ends_and_recap_reports_death(self):
        self.start(seed=5)
        for _ in range(30):
            rc, out = self.run_cli("choices", expect=0)
            if not out["decision"]:
                break
            key = out["decision"]["choices"][0]["key"]
            self.run_cli("choose", key, "--reason", "流れに任せた", expect=0)
        life_id = json.loads((self.state / "index.json").read_text())["current_life_id"]
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "ended")
        rc, recap = self.run_cli("recap", expect=0)
        self.assertTrue(recap["ended"])
        self.assertIsInstance(recap["age_at_death"], int)
        self.assertGreater(recap["age_at_death"], 0)
        self.assertEqual(len(recap["choices"]), recap["step_count"])
        display = self.display_json(life_id)
        self.assertEqual(display["animation"]["kind"], "ended")
        # The mock now sends the death place under the engine's real
        # key ("where"); the display must carry it as "place" and
        # never expose the raw key.
        self.assertTrue(display["death"]["place"])
        self.assertNotIn("where", display["death"])

    def test_no_tmp_files_left_behind(self):
        self.start(seed=9)
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "掃除チェック", expect=0)
        leftovers = [p for p in self.state.rglob("*") if ".tmp-" in p.name or p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_walk_animation_on_country_change(self):
        self.start(seed=42)
        # Play deterministically to the move decision, then choose abroad.
        plan = {0: None, 1: None, 2: None, 3: None, 4: None, 5: "abroad"}
        for step in range(6):
            rc, out = self.run_cli("choices", expect=0)
            wanted = plan[step]
            key = wanted or out["decision"]["choices"][0]["key"]
            self.run_cli("choose", key, "--reason", "移動テスト", expect=0)
        life_id = json.loads((self.state / "index.json").read_text())["current_life_id"]
        display = json.loads((self.state / "lives" / life_id / "display.json").read_text())
        self.assertEqual(display["animation"]["kind"], "walk")
        self.assertEqual(display["animation"]["signal"], "country_changed")

    def test_real_mode_without_credential_stops_not_connected(self):
        rc, out = self.run_cli(
            "start", mock=False,
            extra_env={"JARVIS_AUTHD_SOCK": "/nonexistent-1in8b-test/authd.sock"},
        )
        self.assertEqual(rc, 4)
        self.assertEqual(out["error"], "not_connected")

    def test_start_guard_refuses_second_life_unless_forced(self):
        first = self.start(seed=61)
        rc, refused = self.run_cli("start", "--seed", "62")
        self.assertEqual(rc, 2)
        self.assertIn("already in progress", refused["message"])
        self.assertIn(first["life_id"], refused["message"])
        rc, second = self.run_cli("start", "--seed", "62", "--force",
                                  expect=0)
        self.assertTrue(second["ok"])
        journal = (self.state / "journal.jsonl").read_text()
        self.assertIn('"start_forced"', journal)

    def test_start_http500_is_unknown_start(self):
        rc, failed = self.run_cli(
            "start", "--seed", "22",
            extra_env={"LJ_MOCK_FAULT": "http500"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "start_outcome_unknown")
        journal = (self.state / "journal.jsonl").read_text()
        self.assertIn('"start_intent"', journal)
        self.assertIn('"unknown_start"', journal)
        counters = json.loads((self.state / "counters.json").read_text())
        days = list(counters["days"].values())
        self.assertEqual(days[0]["starts"], 1)
        rc, hist = self.run_cli("history", expect=0)
        self.assertEqual(hist["lives"], [])

    def test_start_tokenless_success_is_unknown_start(self):
        rc, failed = self.run_cli(
            "start", "--seed", "23",
            extra_env={"LJ_MOCK_FAULT": "badjson"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "start_outcome_unknown")

    def test_pending_step_recovers_as_unknown_without_resend(self):
        # Simulate a sender killed after the intent was saved and the
        # step sent, before the outcome was recorded: the life still
        # reads active with a pending step set. The next choose must
        # recover it as unknown and send nothing.
        out = self.start(seed=71)
        life_id = out["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "確定する一歩", expect=0)
        next_key = self.first_choice_key()
        token_before = self.life_json(life_id)["token"]

        life_path = self.state / "lives" / life_id / "life.json"
        life = json.loads(life_path.read_text())
        life["counters"]["step_intents"] = 2
        life["pending_step"] = {"step": 2, "choose": next_key}
        life_path.write_text(json.dumps(life, ensure_ascii=False))

        rc, failed = self.run_cli("choose", next_key,
                                  "--reason", "再送されてはいけない")
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "unknown")
        self.assertEqual(life["step_count"], 1)
        self.assertEqual(life["token"], token_before)
        journal = (self.state / "lives" / life_id
                   / "journal.jsonl").read_text()
        self.assertIn("recovered_pending", journal)

        rc, refused = self.run_cli("choose", next_key,
                                   "--reason", "まだ送れない")
        self.assertEqual(rc, 2)
        rc, cleared = self.run_cli("reconcile", "--abandon", expect=0)
        self.assertTrue(cleared["abandoned"])

    def test_status_takes_the_state_lock(self):
        # status used to run outside the state lock and could save a
        # stale record over a concurrent choose. Hold the lock from
        # another process: status must wait for it, then succeed.
        import time
        out = self.start(seed=72)
        lock_path = self.state / ".lock"
        self.assertTrue(lock_path.exists())
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time; "
             "f = open(sys.argv[1], 'r+'); "
             "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
             "time.sleep(2.5)",
             str(lock_path)],
        )
        try:
            time.sleep(0.5)
            started = time.time()
            rc, status = self.run_cli("status", expect=0)
            elapsed = time.time() - started
        finally:
            holder.wait(timeout=30)
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertEqual(status["life_id"], out["life_id"])

    def test_reason_carrying_a_credential_is_refused(self):
        out = self.start(seed=73)
        life_id = out["life_id"]
        key = self.first_choice_key()
        rc, refused = self.run_cli(
            "choose", key,
            "--reason", "うっかり鍵 k8b_" + "A" * 16 + " を書いた")
        self.assertEqual(rc, 2)
        self.assertIn("credential", refused["message"])
        life = self.life_json(life_id)
        self.assertEqual(life["counters"]["step_intents"], 0)
        history = (self.state / "lives" / life_id
                   / "history.jsonl").read_text()
        self.assertNotIn("k8b_", history)

    def test_upstream_message_is_never_echoed(self):
        out = self.start(seed=74)
        key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", key, "--reason", "漏洩テスト",
            extra_env={"LJ_MOCK_FAULT": "leaky"},
        )
        self.assertEqual(rc, 5)
        self.assertEqual(failed["error"], "upstream")
        self.assertNotIn("k8b_", json.dumps(failed))
        self.assertNotIn("exploded", json.dumps(failed))
        # A definitive refusal leaves the life active and retryable.
        self.assertEqual(self.life_json(out["life_id"])["status"],
                         "active")

    def test_no_secrets_shape_checks(self):
        module = life_api_module
        with self.assertRaises(AssertionError):
            module.assert_no_secrets({"x": "途中に hsurr:abc が混ざった"}, [])
        with self.assertRaises(AssertionError):
            module.assert_no_secrets({"x": "鍵は k8b_" + "A" * 16}, [])
        with self.assertRaises(AssertionError):
            module.assert_no_secrets({"x": "t" * 130}, [])
        module.assert_no_secrets({"x": "普通の文章です"}, [])

    def test_broken_ending_marks_unknown_not_ended(self):
        self.start(seed=75)
        total = len(life_api_module.MOCK_DECISIONS)
        for _ in range(total - 1):
            key = self.first_choice_key()
            self.run_cli("choose", key, "--reason", "進める", expect=0)
        last_key = self.first_choice_key()
        rc, failed = self.run_cli(
            "choose", last_key, "--reason", "最期の一歩",
            extra_env={"LJ_MOCK_FAULT": "badend"},
        )
        self.assertEqual(rc, 3)
        self.assertEqual(failed["error"], "step_outcome_unknown")
        life_id = json.loads(
            (self.state / "index.json").read_text())["current_life_id"]
        self.assertEqual(self.life_json(life_id)["status"], "unknown")
        self.run_cli("reconcile", "--abandon", expect=0)
        rc, ended = self.run_cli("choose", last_key,
                                 "--reason", "改めて最期", expect=0)
        self.assertTrue(ended["ended"])
        self.assertEqual(self.life_json(life_id)["status"], "ended")

    def test_ack_is_bound_to_the_accepted_revision(self):
        out = self.start(seed=76)
        life_id = out["life_id"]
        rc, refused = self.run_cli("delta", "--ack", "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertIn("--revision", refused["message"])
        rc, refused = self.run_cli(
            "delta", "--ack", "--revision", "999", "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertFalse(self.synced_path(life_id).exists())
        self.ack(life_id)

        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "結び付けテスト", expect=0)
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        target = delta["target_revision"]
        rc, refused = self.run_cli(
            "delta", "--ack", "--revision", str(target + 5),
            "--life", life_id)
        self.assertEqual(rc, 2)
        self.assertTrue(self.pending_delta_path(life_id).exists())
        snapshot_before = json.loads(
            self.synced_path(life_id).read_text())
        self.assertNotEqual(snapshot_before["revision"], target)
        rc, acked = self.ack(life_id)
        self.assertEqual(acked["revision"], target)
        self.assertFalse(self.pending_delta_path(life_id).exists())
        # The recorded snapshot is the reconstruction of what the
        # viewer accepted -- and it equals the current display.
        self.assertEqual(json.loads(self.synced_path(life_id).read_text()),
                         self.display_json(life_id))

    def test_delta_sheet_reorder_through_a_view_syncs(self):
        # A sheet reorder that arrives through a real engine answer
        # bumps the revision (the sheet is part of the revision
        # criterion), so it syncs as a v2 delta instead of deadlocking
        # between a refused delta and a stale full sync.
        out = self.start(seed=37)
        life_id = out["life_id"]
        baseline = [
            {"id": "own_tv", "name": "テレビ", "does": "幸福 +5",
             "price_ppp": 1682, "can": False, "why": "お金が足りません"},
            {"id": "own_bike", "name": "自転車", "does": "幸福 +3",
             "price_ppp": 421, "can": True, "why": None},
        ]
        module = life_api_module
        root = str(self.state)

        def view_with(sheet):
            life = module.load_life(root, life_id)
            snap = life["snapshot"]
            response = {
                "state": snap["state"], "events": [],
                "decision": snap["decision"], "focus": snap["focus"],
                "sheet": copy.deepcopy(sheet), "ended": False,
                "death": None, "token": life["token"],
                "meta": snap.get("meta") or {},
                "usage": snap.get("usage"),
            }
            module.apply_response(root, life, response,
                                  events_are_new=False)

        view_with(baseline)
        base_display = self.display_json(life_id)
        self.assertEqual(base_display["sheet"], baseline)
        self.ack(life_id)

        view_with([baseline[1], baseline[0]])
        current_display = self.display_json(life_id)
        self.assertGreater(current_display["revision"],
                           base_display["revision"])
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["schema_version"], 2)
        self.assertEqual(delta["sheet_items"], [])
        self.assertEqual(delta["sheet_remove"], [])
        self.assertEqual(delta["sheet_order"], ["own_bike", "own_tv"])
        merged = self.apply_delta_reference(base_display, delta)
        self.assertEqual(merged, current_display)

    def test_token_error_code_classifies_as_rejected(self):
        # Measured on the real engine 2026-11-12: a token the engine did
        # not make (or one older than 90 days) answers HTTP 400 with code
        # "token". The step was refused at the gate, so it is rejected,
        # never unknown.
        module = life_api_module
        exc = module.UpstreamError("bad token", code="token", status=400)
        self.assertEqual(module.classify_step_error(exc), "rejected")


    def test_interrupted_start_blocks_until_abandoned(self):
        # A start whose process died after the send but before the
        # outcome was recorded leaves only its pending-start marker.
        # A new start must refuse until the lost send is acknowledged
        # deliberately, and the acknowledgment counts it against the
        # daily start cap exactly once.
        marker = self.state / "pending_start.json"
        self.state.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(
            {"type": "pending_start", "at": "2026-01-01T00:00:00+09:00",
             "seed": 7}))
        rc, out = self.run_cli("start", "--seed", "8", expect=2)
        self.assertIn("pending start", out["message"])
        rc, out = self.run_cli("start", "--abandon-pending", expect=0)
        self.assertTrue(out["ok"])
        self.assertFalse(marker.exists())

        def total_starts():
            data = json.loads((self.state / "counters.json").read_text())
            return sum(day.get("starts", 0)
                       for day in data["days"].values())

        self.assertEqual(total_starts(), 1)
        started = self.start(seed=8)
        self.assertTrue(started["ok"])
        self.assertEqual(total_starts(), 2)
        # The successful start cleared its own marker.
        self.assertFalse(marker.exists())

    def test_ending_without_state_uses_death_age(self):
        # An ending answer that carries no state must not leave the
        # person at their pre-ending age: the death record's age is
        # authoritative for a finished life.
        out = self.start(seed=11)
        life_id = out["life_id"]
        module = life_api_module
        root = str(self.state)
        life = module.load_life(root, life_id)
        pre_age = (life["snapshot"].get("state") or {}).get("age")
        response = {
            "state": None, "events": [[76, "最期の年"]],
            "decision": None, "focus": None, "sheet": [],
            "ended": True,
            "death": {"age": 76, "cause": "ihd", "where": "UZB"},
            "token": None, "meta": life["snapshot"].get("meta") or {},
            "usage": life["snapshot"].get("usage"),
        }
        module.apply_response(root, life, response)
        display = self.display_json(life_id)
        self.assertTrue(display["ended"])
        self.assertEqual(display["person"]["age"], 76)
        self.assertNotEqual(pre_age, 76)
        self.assertEqual(display["death"]["place"], "UZB")

    def test_busy_code_on_a_500_is_response_lost_not_gate(self):
        # The gate class needs BOTH the HTTP 503 status and the busy
        # code: a busy code riding a 500 came through a relay and
        # proves nothing about whether the step was applied. The
        # classification is also stored on the incident record.
        self.start()
        key = self.first_choice_key()
        rc, out = self.run_cli(
            "choose", key, "--reason", "busy on a 500",
            extra_env={"LJ_MOCK_FAULT": "http500busy"}, expect=3)
        self.assertEqual(out["detail"], "busy")
        self.assertEqual(out["unknown_class"], "response_lost")
        life_id = out["life_id"]
        incident = self.life_json(life_id)["last_incident"]
        self.assertEqual(incident["unknown_class"], "response_lost")
        # And the true gate -- 503 plus busy -- still classifies gate
        # and stores it too.
        self.run_cli("reconcile", "--abandon", expect=0)
        key = self.first_choice_key()
        rc, out = self.run_cli(
            "choose", key, "--reason", "the true gate",
            extra_env={"LJ_MOCK_FAULT": "http503"}, expect=3)
        self.assertEqual(out["unknown_class"], "gate")
        incident = self.life_json(life_id)["last_incident"]
        self.assertEqual(incident["unknown_class"], "gate")

    def test_unconfirmed_choice_row_is_not_shown(self):
        # Simulate a choose killed after appending the choice row but
        # before the confirmed state was saved: the row sits in the
        # history with no journal confirmation, and the display must
        # not present it as a decision that happened.
        started = self.start()
        life_id = started["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "confirmed normally",
                     expect=0)
        self.assertEqual(len(self.display_json(life_id)["choices"]), 1)
        history = self.state / "lives" / life_id / "history.jsonl"
        with open(history, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "choice", "step": 99, "choose": "x",
                 "reason": "never confirmed", "label": "phantom"}) + "\n")
        rc, out = self.run_cli("status", expect=0)
        choices = self.display_json(life_id)["choices"]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["step"], 1)

    def test_forked_rechoice_is_shown_despite_intent_numbering(self):
        # After a fork, the confirmed re-choice carries a higher
        # intent number than the confirmed count (step 2, count 1).
        # The confirmed-set filter must still show it.
        started = self.start()
        life_id = started["life_id"]
        key = self.first_choice_key()
        rc, out = self.run_cli(
            "choose", key, "--reason", "lost in transit",
            extra_env={"LJ_MOCK_FAULT": "timeout"}, expect=3)
        self.run_cli("reconcile", "--abandon", expect=0)
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason",
                     "chosen again after the fork", expect=0)
        life = self.life_json(life_id)
        self.assertEqual(life["counters"]["confirmed_steps"], 1)
        choices = self.display_json(life_id)["choices"]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["step"], 2)

    def test_choices_and_recap_wait_for_the_state_lock(self):
        # choices and recap run pending-step recovery, so they take
        # the same exclusive lock as the writing commands. While the
        # lock is held (a choose still waiting on the engine), they
        # must wait -- otherwise they would mistake the in-flight step
        # for a crashed one and mark the life unknown. Hold the flock
        # directly, check both commands block, release, check they run
        # and the life was never falsely marked.
        import fcntl
        started = self.start()
        life_id = started["life_id"]
        env = dict(os.environ)
        env["LJ_STATE_DIR"] = str(self.state)
        env["LJ_MOCK"] = "1"
        lock_fd = os.open(str(self.state / ".lock"),
                          os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            for command in ("choices", "recap"):
                proc = subprocess.Popen(
                    [sys.executable, str(HELPER), command,
                     "--life", life_id],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env)
                try:
                    proc.wait(timeout=3)
                    self.fail("%s completed while the state lock was "
                              "held (rc=%s)" % (command, proc.returncode))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        rc, out = self.run_cli("choices", "--life", life_id, expect=0)
        self.assertTrue(out["ok"])
        self.assertEqual(self.life_json(life_id)["status"], "active")

    def test_not_connected_start_leaves_no_pending_marker(self):
        # First run with no credential registered: the start fails
        # before anything is sent (exit 4). The pending-start marker
        # must not survive it -- if it did, every later start would
        # be refused as if this one had died mid-send, and a new user
        # would stay blocked until abandoning a start that was never
        # sent.
        no_credential = {
            "JARVIS_AUTHD_SOCK": "/nonexistent-1in8b-test/authd.sock"}
        rc, out = self.run_cli("start", mock=False,
                               extra_env=no_credential, expect=4)
        self.assertEqual(out["error"], "not_connected")
        self.assertFalse((self.state / "pending_start.json").exists())
        rc, out = self.run_cli("start", mock=False,
                               extra_env=no_credential, expect=4)
        self.assertEqual(out["error"], "not_connected")
        # And once the connection exists, starting works right away.
        started = self.start(seed=3)
        self.assertTrue(started["ok"])

    def test_not_connected_choose_is_not_recovered_as_unknown(self):
        # Same pre-send failure on a step: the pending marker is
        # cleared and journaled as not sent, so the life stays
        # active with its decision still waiting -- it must not be
        # recovered as an unknown step on the next command.
        started = self.start()
        life_id = started["life_id"]
        key = self.first_choice_key()
        rc, out = self.run_cli(
            "choose", key, "--reason", "never sent",
            extra_env={"LJ_MOCK_FAULT": "notconnected"}, expect=4)
        self.assertEqual(out["error"], "not_connected")
        life = self.life_json(life_id)
        self.assertEqual(life["status"], "active")
        self.assertIsNone(life["pending_step"])
        rc, out = self.run_cli("choices", expect=0)
        self.assertIsNotNone(out["decision"])
        self.run_cli("choose", key, "--reason", "sent for real",
                     expect=0)
        self.assertEqual(self.life_json(life_id)["step_count"], 1)

    def test_view_does_not_decay_step_animation(self):
        # After a step that improves health, the display's health
        # trend is "up". A bare status view returns the same state,
        # so it must not recompute the animation against the new
        # state (which decays the trend to "flat") -- that changed
        # the display while the revision stood still, and the next
        # delta was refused as an unbumped change.
        self.start(seed=42)
        for _ in range(6):
            key = self.first_choice_key()
            self.run_cli("choose", key, "--reason", "進める", expect=0)
        rc, out = self.run_cli("choices", expect=0)
        self.assertEqual(out["decision"]["id"], "midlife")
        self.run_cli("choose", "health_first", "--reason", "体を大事に",
                     expect=0)
        life_id = json.loads(
            (self.state / "index.json").read_text())["current_life_id"]
        display = self.display_json(life_id)
        self.assertEqual(display["animation"]["health_trend"], "up")
        self.ack(life_id)
        self.run_cli("status", expect=0)
        after = self.display_json(life_id)
        self.assertEqual(after["animation"]["health_trend"], "up")
        self.assertEqual(after["revision"], display["revision"])
        rc, result = self.run_cli("delta", expect=0)
        self.assertEqual(result["status"], "up_to_date")

    def test_confirmed_journal_record_is_restored_after_crash_gap(self):
        # Simulate a choose killed after the confirmed state was
        # saved but before the journal's "confirmed" record landed:
        # the history row and the saved record (counters advanced,
        # last_choice naming the step) are in place, the journal
        # record is not. The display must not drop a choice the
        # engine did confirm -- the missing record is restored,
        # marked as restored, from the record's own proof.
        started = self.start()
        life_id = started["life_id"]
        key = self.first_choice_key()
        self.run_cli("choose", key, "--reason", "confirmed normally",
                     expect=0)
        base = self.state / "lives" / life_id
        history_rows = [json.loads(line) for line in
                        (base / "history.jsonl").read_text().splitlines()
                        if line.strip()]
        row = [r for r in history_rows if r.get("type") == "choice"][0]
        row2 = dict(row)
        row2["step"] = 2
        row2["reason"] = "confirmed, then the process died"
        with open(base / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row2, ensure_ascii=False) + "\n")
        life = self.life_json(life_id)
        life["counters"]["step_intents"] = 2
        life["counters"]["confirmed_steps"] = 2
        life["step_count"] = 2
        last_choice = dict(life["last_choice"])
        last_choice["step"] = 2
        life["last_choice"] = last_choice
        (base / "life.json").write_text(json.dumps(life, ensure_ascii=False))

        self.run_cli("status", expect=0)
        choices = self.display_json(life_id)["choices"]
        self.assertEqual([c["step"] for c in choices], [1, 2])
        journal_rows = [json.loads(line) for line in
                        (base / "journal.jsonl").read_text().splitlines()
                        if line.strip()]
        restored = [r for r in journal_rows
                    if r.get("type") == "confirmed" and r.get("step") == 2]
        self.assertEqual(len(restored), 1)
        self.assertTrue(restored[0]["restored"])
        # A second refresh restores nothing further.
        self.run_cli("status", expect=0)
        journal_rows = [json.loads(line) for line in
                        (base / "journal.jsonl").read_text().splitlines()
                        if line.strip()]
        self.assertEqual(len([r for r in journal_rows
                              if r.get("type") == "confirmed"
                              and r.get("step") == 2]), 1)

    def test_keydate_change_bumps_every_life(self):
        # The acquisition date is read from the shared config into
        # every life's display, so changing it must advance every
        # life's revision -- bumping only the current life strands
        # the others with a changed display at a standing revision,
        # whose delta is then refused.
        first = self.start(seed=21)
        first_id = first["life_id"]
        base_first = self.display_json(first_id)
        self.ack(first_id)
        second = self.run_cli("start", "--seed", "22", "--force",
                              expect=0)[1]
        second_id = second["life_id"]
        rc, recorded = self.run_cli("keydate", "--on", "2026-02-01",
                                    expect=0)
        self.assertEqual(recorded["revisions"][first_id],
                         base_first["revision"] + 1)
        self.assertEqual(recorded["revisions"][second_id],
                         self.display_json(second_id)["revision"])
        self.assertEqual(self.display_json(first_id)["key_acquired_on"],
                         "2026-02-01")
        rc, delta = self.run_cli("delta", "--life", first_id, expect=0)
        self.assertEqual(delta["replace"]["key_acquired_on"], "2026-02-01")
        self.assertEqual(delta["base_revision"], base_first["revision"])

    def test_epilogue_written_after_ending_and_syncs(self):
        # An epilogue belongs to a finished life: while the life is
        # still running it is refused.
        self.start(seed=5)
        rc, refused = self.run_cli("epilogue", "--text", "まだ途中")
        self.assertEqual(rc, 2)
        for _ in range(30):
            rc, out = self.run_cli("choices", expect=0)
            if not out["decision"]:
                break
            key = out["decision"]["choices"][0]["key"]
            self.run_cli("choose", key, "--reason", "流れに任せた",
                         expect=0)
        life_id = json.loads(
            (self.state / "index.json").read_text())["current_life_id"]
        self.assertEqual(self.life_json(life_id)["status"], "ended")
        ended_display = self.display_json(life_id)
        self.assertIsNone(ended_display["epilogue"])
        self.ack(life_id)

        # Credential-shaped text is refused, like a reason or a
        # chapter: epilogues are stored and synced.
        rc, refused = self.run_cli(
            "epilogue", "--text", "鍵は k8b_" + "x" * 24 + " です")
        self.assertEqual(rc, 2)
        self.assertIsNone(self.display_json(life_id)["epilogue"])

        text = "静かな一生だった。大きなことは何も起きなかったけれど、悪くなかった。"
        rc, written = self.run_cli(
            "epilogue", "--text", text, "--title", "短い一生によせて",
            expect=0)
        self.assertEqual(written["epilogue"]["text"], text)
        self.assertEqual(written["revision"],
                         ended_display["revision"] + 1)
        display = self.display_json(life_id)
        self.assertEqual(display["epilogue"]["text"], text)
        self.assertEqual(display["epilogue"]["title"], "短い一生によせて")
        self.assertTrue(display["epilogue"]["written_on"])
        rc, recap = self.run_cli("recap", expect=0)
        self.assertEqual(recap["epilogue"]["text"], text)
        # The epilogue travels by delta in the replace channel.
        rc, delta = self.run_cli("delta", "--life", life_id, expect=0)
        self.assertEqual(delta["replace"]["epilogue"]["text"], text)
        self.ack(life_id)


if __name__ == "__main__":
    unittest.main()
