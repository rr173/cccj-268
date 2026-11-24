"""Tests for the offline result write-off ledger (结果核销账本)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from roadtool.core import Repository, draw_tickets  # noqa: E402
from roadtool.ledger import (  # noqa: E402
    BatchContentConflict,
    CrossTicketParent,
    LedgerCycle,
    MissingParent,
    RecordIdReused,
    RecordNotFound,
    ReceiptBindingConflict,
    ResultLedger,
    replay_ledger,
)


def batch(batch_id, *records):
    return {"batch_id": batch_id, "records": list(records)}


def rec(rid, ticket="T1", status="已查验", parents=None, **extra):
    doc = {"id": rid, "ticket_id": ticket, "status": status}
    if parents is not None:
        doc["parents"] = parents
    doc.update(extra)
    return doc


class ForkMergeWithdrawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger"
        self.ledger = ResultLedger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_ticket_is_uninspected(self):
        report = self.ledger.ticket_report("NEVER")
        self.assertEqual(report["state"], "no_candidate")
        self.assertEqual(report["status"], "未查验")
        self.assertIsNone(report["current_result"])
        self.assertEqual(report["candidates"], [])

    def test_single_root_is_unique_and_adopts_status(self):
        self.ledger.import_batch(batch("B1", rec("R1", status="未到场")))
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["state"], "unique")
        self.assertEqual(report["candidate_ids"], ["R1"])
        self.assertEqual(report["status"], "未到场")
        self.assertFalse(report["conflict"])
        self.assertFalse(report["candidates"][0]["withdrawn"])
        self.assertEqual(report["candidates"][0]["source_chain_ids"], ["R1"])

    def test_fork_is_conflict_even_when_status_text_identical(self):
        self.ledger.import_batch(batch("B1", rec("R1")))
        # Two offline branches both continue R1; same status text on purpose.
        self.ledger.import_batch(
            batch(
                "B2",
                rec("R2", parents=["R1"], by="甲组"),
                rec("R3", parents=["R1"], by="乙组"),
            )
        )
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["state"], "conflict")
        self.assertTrue(report["conflict"])
        self.assertEqual(report["status"], "冲突待核")
        self.assertIsNone(report["current_result"])
        self.assertEqual(report["candidate_ids"], ["R2", "R3"])
        # Complete source chains come back with each candidate.
        for candidate in report["candidates"]:
            self.assertEqual(candidate["source_chain_ids"], ["R1", candidate["id"]])
            ids = [node["id"] for node in candidate["source_chain"]]
            self.assertEqual(ids, ["R1", candidate["id"]])

    def test_fork_with_different_statuses_also_conflicts(self):
        self.ledger.import_batch(batch("B1", rec("R1")))
        self.ledger.import_batch(
            batch(
                "B2",
                rec("R2", status="已查验", parents=["R1"]),
                rec("R3", status="未到场", parents=["R1"]),
            )
        )
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["state"], "conflict")
        statuses = {c["status"] for c in report["candidates"]}
        self.assertEqual(statuses, {"已查验", "未到场"})

    def test_merge_has_multiple_parents_and_single_candidate(self):
        self.ledger.import_batch(
            batch(
                "B1",
                rec("R1"),
                rec("R2", parents=["R1"]),
                rec("R3", parents=["R1"]),
            )
        )
        # Multi-parent record merges both branches into one new result.
        self.ledger.import_batch(
            batch("B2", rec("R4", status="已查验", parents=["R2", "R3"]))
        )
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["candidate_ids"], ["R4"])
        self.assertEqual(report["state"], "unique")
        candidate = report["candidates"][0]
        self.assertEqual(set(candidate["parents"]), {"R2", "R3"})
        # Full ancestry: root, both branches, merge.
        self.assertEqual(
            set(candidate["source_chain_ids"]), {"R1", "R2", "R3", "R4"}
        )
        chain = candidate["source_chain"]
        self.assertEqual(chain[-1]["id"], "R4")
        self.assertEqual(chain[0]["id"], "R1")

    def test_withdraw_merge_restores_branch_conflict(self):
        self.ledger.import_batch(
            batch(
                "B1",
                rec("R1"),
                rec("R2", parents=["R1"]),
                rec("R3", parents=["R1"]),
                rec("R4", parents=["R2", "R3"]),
            )
        )
        self.assertEqual(self.ledger.ticket_report("T1")["candidate_ids"], ["R4"])
        self.ledger.withdraw("R4", reason="复核撤销")
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["state"], "conflict")
        self.assertEqual(report["candidate_ids"], ["R2", "R3"])
        # Withdrawal keeps the record, its multi-parent edges and the reason.
        stored = self.ledger.state.records["R4"]
        self.assertEqual(set(stored["parents"]), {"R2", "R3"})
        self.assertEqual(
            self.ledger.state.withdrawn["R4"]["reason"], "复核撤销"
        )
        # Reopening keeps the withdrawn merge on disk.
        reopened = ResultLedger(self.path)
        self.assertIn("R4", reopened.state.records)
        self.assertIn("R4", reopened.state.withdrawn)

    def test_withdraw_one_fork_keeps_other_branch(self):
        self.ledger.import_batch(
            batch(
                "B1",
                rec("R1"),
                rec("R2", parents=["R1"]),
                rec("R3", parents=["R1"]),
            )
        )
        self.ledger.withdraw("R3")
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["candidate_ids"], ["R2"])
        self.assertEqual(report["state"], "unique")
        # R1 still has an active descendant R2, so it is not candidate.
        self.assertNotIn("R1", report["candidate_ids"])

    def test_withdraw_terminal_restores_predecessor(self):
        self.ledger.import_batch(
            batch("B1", rec("R1"), rec("R2", parents=["R1"]))
        )
        self.assertEqual(self.ledger.ticket_report("T1")["candidate_ids"], ["R2"])
        self.ledger.withdraw("R2")
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["candidate_ids"], ["R1"])
        self.assertEqual(report["status"], "已查验")

    def test_all_records_withdrawn_means_uninspected(self):
        self.ledger.import_batch(batch("B1", rec("R1", status="未到场")))
        self.ledger.withdraw("R1")
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["state"], "no_candidate")
        self.assertEqual(report["status"], "未查验")

    def test_descendant_relation_passes_through_withdrawn_records(self):
        # A <- W(withdrawn middle) <- C(active)
        self.ledger.import_batch(
            batch(
                "B1",
                rec("A"),
                rec("W", status="未到场", parents=["A"]),
                rec("C", parents=["W"]),
            )
        )
        self.ledger.withdraw("W")
        report = self.ledger.ticket_report("T1")
        # C is the only terminal candidate; A must not reappear while an
        # active descendant C exists even though the middle is withdrawn.
        self.assertEqual(report["candidate_ids"], ["C"])

        self.ledger.withdraw("C")
        report = self.ledger.ticket_report("T1")
        # Now the active descendant is gone; A is restored (W stays gone).
        self.assertEqual(report["candidate_ids"], ["A"])

    def test_withdraw_does_not_withdraw_descendants(self):
        self.ledger.import_batch(
            batch("B1", rec("A"), rec("B", parents=["A"]))
        )
        self.ledger.withdraw("A")
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["candidate_ids"], ["B"])
        self.assertNotIn("A", report["candidate_ids"])
        # Withdrawal is idempotent and does not delete the record/edges.
        again = self.ledger.withdraw("A")
        self.assertTrue(again["noop"])
        self.assertIn("A", self.ledger.state.records)
        self.assertEqual(self.ledger.state.records["B"]["parents"], ["A"])

    def test_withdraw_unknown_record_raises(self):
        with self.assertRaises(RecordNotFound):
            self.ledger.withdraw("GHOST")

    def test_parent_may_be_withdrawn(self):
        self.ledger.import_batch(batch("B1", rec("A")))
        self.ledger.withdraw("A")
        # An offline branch may continue an old (even withdrawn) record.
        self.ledger.import_batch(batch("B2", rec("B", parents=["A"])))
        report = self.ledger.ticket_report("T1")
        self.assertEqual(report["candidate_ids"], ["B"])


class BatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger"
        self.ledger = ResultLedger(self.path)
        self.ledger.import_batch(batch("B1", rec("R1", ticket="T1")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_redelivery_same_content(self):
        payload = batch("B1", rec("R1", ticket="T1"))
        again = self.ledger.import_batch(payload)
        self.assertTrue(again["noop"])
        self.assertEqual(self.ledger.report()["record_count"], 1)

    def test_redelivery_with_reordered_rows_is_same_content(self):
        payload1 = batch(
            "B2",
            rec("A2"),
            rec("B2", parents=["A2"]),
        )
        self.ledger.import_batch(payload1)
        reordered = batch(
            "B2",
            rec("B2", parents=["A2"]),
            rec("A2"),
        )
        again = self.ledger.import_batch(reordered)
        self.assertTrue(again["noop"])

    def test_same_batch_id_different_content_rejects_whole_batch(self):
        before = self.ledger.report()["record_count"]
        with self.assertRaises(BatchContentConflict):
            self.ledger.import_batch(
                batch("B1", rec("R1", ticket="T1", status="未到场"))
            )
        # Nothing changed.
        self.assertEqual(self.ledger.report()["record_count"], before)
        reopened = ResultLedger(self.path)
        self.assertEqual(reopened.ticket_report("T1")["status"], "已查验")

    def test_record_id_reuse_in_new_batch_rejected(self):
        with self.assertRaises(RecordIdReused):
            self.ledger.import_batch(batch("B2", rec("R1", ticket="T2")))
        self.assertNotIn("B2", self.ledger.state.batches)

    def test_duplicate_id_inside_batch_rejected(self):
        with self.assertRaises(RecordIdReused):
            self.ledger.import_batch(
                batch("B2", rec("X1"), rec("X1", status="未到场"))
            )

    def test_missing_parent_rejected(self):
        with self.assertRaises(MissingParent):
            self.ledger.import_batch(
                batch("B2", rec("X1", parents=["NOPE"]))
            )
        self.assertEqual(self.ledger.report()["record_count"], 1)

    def test_cross_ticket_parent_rejected(self):
        self.ledger.import_batch(batch("B-OTHER", rec("O1", ticket="T2")))
        with self.assertRaises(CrossTicketParent):
            self.ledger.import_batch(
                batch("B2", rec("X1", ticket="T1", parents=["O1"]))
            )

    def test_intra_batch_forward_reference_allowed_regardless_of_order(self):
        # Child row physically precedes its parent row.  Use a fresh ticket so
        # the setUp root R1 does not join the candidate set.
        self.ledger.import_batch(
            batch(
                "B2",
                rec("CHILD", ticket="TF", parents=["PARENT"]),
                rec("PARENT", ticket="TF"),
            )
        )
        report = self.ledger.ticket_report("TF")
        self.assertEqual(report["candidate_ids"], ["CHILD"])
        self.assertEqual(
            report["candidates"][0]["source_chain_ids"], ["PARENT", "CHILD"]
        )

    def test_intra_batch_cycle_rejected(self):
        with self.assertRaises(LedgerCycle):
            self.ledger.import_batch(
                batch(
                    "CYC",
                    rec("P", parents=["Q"]),
                    rec("Q", parents=["P"]),
                )
            )
        self.assertNotIn("CYC", self.ledger.state.batches)
        self.assertEqual(self.ledger.report()["record_count"], 1)

    def test_self_parent_rejected(self):
        with self.assertRaises(LedgerCycle):
            self.ledger.import_batch(batch("SELF", rec("S", parents=["S"])))

    def test_batch_rejection_leaves_no_jsonl_rows(self):
        size_before = (self.path / "records.jsonl").read_text(
            encoding="utf-8"
        )
        with self.assertRaises(MissingParent):
            self.ledger.import_batch(
                batch("BAD", rec("Z", parents=["MISSING"]))
            )
        size_after = (self.path / "records.jsonl").read_text(encoding="utf-8")
        self.assertEqual(size_before, size_after)

    def test_english_status_aliases_normalized_but_text_preserved(self):
        self.ledger.import_batch(
            batch("EN", rec("E1", status="absent"))
        )
        view = self.ledger.ticket_report("T1")["candidates"][0]
        self.assertEqual(view["status"], "absent")  # verbatim input text
        self.assertEqual(view["status_code"], "not_present")
        self.assertEqual(view["status_zh"], "未到场")


class PersistenceAndReplayTests(unittest.TestCase):
    def test_reopen_and_independent_replay_are_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger"
            ledger = ResultLedger(path)
            ledger.import_batch(batch("B1", rec("R1")))
            ledger.import_batch(
                batch(
                    "B2",
                    rec("R2", parents=["R1"]),
                    rec("R3", parents=["R1"]),
                )
            )
            ledger.import_batch(
                batch("B3", rec("R4", parents=["R2", "R3"]))
            )
            ledger.withdraw("R4", reason="x")
            ledger.withdraw("R3")

            first = json.dumps(
                ledger.report(), sort_keys=True, ensure_ascii=False
            )

            reopened = ResultLedger(path)
            second = json.dumps(
                reopened.report(), sort_keys=True, ensure_ascii=False
            )
            third = json.dumps(
                replay_ledger(path), sort_keys=True, ensure_ascii=False
            )
            self.assertEqual(first, second)
            self.assertEqual(first, third)
            self.assertTrue(reopened.check()["ok"])

            # records.jsonl is genuine one-JSON-object-per-line JSONL.
            lines = [
                line
                for line in (path / "records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            for line in lines:
                obj = json.loads(line)
                self.assertIn("id", obj)
            self.assertEqual(len(lines), 4)

    def test_two_independent_replays_of_same_batches_agree(self):
        payloads = [
            batch("B1", rec("R1")),
            batch("B2", rec("R2", parents=["R1"]), rec("R3", parents=["R1"])),
        ]
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            la = ResultLedger(Path(a) / "L")
            lb = ResultLedger(Path(b) / "L")
            # Different batch import order for the independent B2 pieces is
            # not allowed across missing parents, so swap the two rows inside
            # B2 instead and interleave an unrelated ticket batch.
            la.import_batch(payloads[0])
            la.import_batch(payloads[1])
            la.import_batch(batch("OTHER", rec("O1", ticket="T9")))

            lb.import_batch(batch("OTHER", rec("O1", ticket="T9")))
            lb.import_batch(payloads[0])
            lb.import_batch(
                batch(
                    "B2",
                    rec("R3", parents=["R1"]),
                    rec("R2", parents=["R1"]),
                )
            )
            self.assertEqual(
                json.dumps(la.report(), sort_keys=True, ensure_ascii=False),
                json.dumps(lb.report(), sort_keys=True, ensure_ascii=False),
            )


class ReceiptBindingTests(unittest.TestCase):
    def test_original_point_is_frozen_against_later_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = {
                "routes": ["R"],
                "segments": [
                    {"id": "P1", "route": "R", "order": 0, "length": 6}
                ],
            }
            repo = Repository.initialize(root / "repo", frame)
            sheet = {"tickets": [{"id": "T1", "r": "0"}]}
            v0_doc = draw_tickets(repo, repo.load_version("v0"), sheet)
            self.assertEqual(
                v0_doc["receipts"][0]["point"]["local_coordinate"], 0
            )

            repo.apply_correction(
                "v0",
                [
                    {
                        "op": "replace",
                        "segment": {
                            "id": "P1",
                            "route": "R",
                            "order": 0,
                            "length": 6,
                            "forbidden": [[0, 2]],
                        },
                    }
                ],
                new_version_id="v1",
            )
            v1_doc = draw_tickets(repo, repo.load_version("v1"), sheet)
            self.assertEqual(
                v1_doc["receipts"][0]["point"]["local_coordinate"], 2
            )

            ledger = ResultLedger(root / "ledger")
            ledger.bind_receipts_doc(v0_doc)
            with self.assertRaises(ReceiptBindingConflict):
                ledger.bind_receipts_doc(v1_doc)

            report = ledger.ticket_report("T1")
            self.assertTrue(report["bound"])
            self.assertEqual(report["bound_version_id"], "v0")
            self.assertEqual(report["original_point"]["local_coordinate"], 0)
            self.assertEqual(report["origin_point"]["local_coordinate"], 0)
            self.assertIsNotNone(report["basis"])
            self.assertIn("segment", report["basis"])
            # Full original receipt retained.
            self.assertEqual(
                report["receipt"]["schema"], "road-ticket-receipt/1"
            )

            # Exact redelivery is fine.
            summary = ledger.bind_receipts_doc(v0_doc)
            self.assertEqual(summary["unchanged"], 1)

    def test_report_includes_bound_ticket_without_results(self):
        receipt = {
            "schema": "road-ticket-receipt/1",
            "ticket_id": "T42",
            "version_id": "v0",
            "status": "located",
            "point": {"segment_id": "S", "local_coordinate": 3},
            "evidence": {"hello": "basis"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ResultLedger(Path(tmp) / "L")
            ledger.bind_receipts_doc({"receipts": [receipt]})
            report = ledger.ticket_report("T42")
            self.assertEqual(report["status"], "未查验")
            self.assertEqual(report["original_point"]["local_coordinate"], 3)
            self.assertEqual(report["basis"], {"hello": "basis"})
            full = ledger.report()
            self.assertEqual(full["ticket_count"], 1)


class RandomizedOracleTests(unittest.TestCase):
    def test_candidates_match_bruteforce_oracle(self):
        import random

        rng = random.Random(20261124)
        for _trial in range(40):
            with tempfile.TemporaryDirectory() as tmp:
                ledger = ResultLedger(Path(tmp) / "L")
                n = rng.randint(1, 25)
                records = []
                for i in range(n):
                    rid = f"R{i}"
                    tid = f"T{rng.randrange(3)}"
                    parents = [
                        f"R{j}"
                        for j in range(i)
                        if records[j]["ticket_id"] == tid
                        and rng.random() < 0.22
                    ]
                    records.append(
                        {
                            "id": rid,
                            "ticket_id": tid,
                            "status": rng.choice(["已查验", "未到场"]),
                            "parents": parents,
                        }
                    )
                rows = records[:]
                rng.shuffle(rows)
                ledger.import_batch({"batch_id": "B", "records": rows})

                withdrawn = set()
                for record in records:
                    if rng.random() < 0.3:
                        ledger.withdraw(record["id"])
                        withdrawn.add(record["id"])

                by_id = {r["id"]: r for r in records}

                def descendants(rid):
                    seen = set()
                    stack = [rid]
                    while stack:
                        current = stack.pop()
                        for candidate in records:
                            if (
                                current in candidate["parents"]
                                and candidate["id"] not in seen
                            ):
                                seen.add(candidate["id"])
                                stack.append(candidate["id"])
                    return seen

                for tid in {r["ticket_id"] for r in records}:
                    tids = [
                        r["id"]
                        for r in records
                        if r["ticket_id"] == tid
                    ]
                    active = [i for i in tids if i not in withdrawn]
                    expected = sorted(
                        i
                        for i in active
                        if not any(
                            j != i
                            and j in active
                            and j in descendants(i)
                            for j in tids
                        )
                    )
                    got = sorted(ledger.ticket_report(tid)["candidate_ids"])
                    self.assertEqual(got, expected)

                reopened = ResultLedger(Path(tmp) / "L")
                self.assertEqual(
                    json.dumps(
                        ledger.report(), sort_keys=True, ensure_ascii=False
                    ),
                    json.dumps(
                        reopened.report(), sort_keys=True, ensure_ascii=False
                    ),
                )


class SingleFileJournalTests(unittest.TestCase):
    def test_jsonl_file_layout_supports_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = ResultLedger(path)
            ledger.import_batch(
                batch(
                    "B1",
                    rec("R1"),
                    rec("R2", parents=["R1"]),
                    rec("R3", parents=["R1"]),
                )
            )
            self.assertEqual(
                ledger.ticket_report("T1")["state"], "conflict"
            )
            ledger.withdraw("R3")
            self.assertEqual(
                ledger.ticket_report("T1")["candidate_ids"], ["R2"]
            )
            ledger.bind_receipts_doc(
                {"receipts": [{"ticket_id": "T1", "point": {"p": 1}}]}
            )

            # Every line is a typed JSON event.
            kinds = [
                json.loads(line)["kind"]
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertIn("record", kinds)
            self.assertIn("withdrawal", kinds)
            self.assertIn("batch", kinds)
            self.assertIn("binding", kinds)

            reopened = ResultLedger(path)
            self.assertEqual(
                reopened.ticket_report("T1")["candidate_ids"], ["R2"]
            )
            self.assertEqual(
                reopened.ticket_report("T1")["original_point"], {"p": 1}
            )
            self.assertTrue(reopened.check()["ok"])
            self.assertEqual(
                json.dumps(
                    replay_ledger(path), sort_keys=True, ensure_ascii=False
                ),
                json.dumps(
                    reopened.report(), sort_keys=True, ensure_ascii=False
                ),
            )


class CliLedgerTests(unittest.TestCase):
    """Black-box CLI coverage through road.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger"
        self.batch_file = self.root / "b.json"
        self.batch_file.write_text(
            json.dumps(batch("CB1", rec("C1"))), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, check=True):
        proc = subprocess.run(
            [sys.executable, "-I", str(PROJECT_ROOT / "road.py"), *args],
            capture_output=True,
            text=True,
            cwd=str(self.root),
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"CLI failed ({proc.returncode}): {' '.join(args)}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return proc

    def test_cli_import_ticket_withdraw_flow(self):
        self.run_cli(
            "ledger-import", "--ledger", str(self.ledger),
            "--input", str(self.batch_file),
        )
        proc = self.run_cli(
            "ledger-ticket", "--ledger", str(self.ledger), "--ticket", "T1"
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["candidate_ids"], ["C1"])

        self.run_cli(
            "ledger-withdraw", "--ledger", str(self.ledger),
            "--record", "C1", "--reason", "oops",
        )
        proc = self.run_cli(
            "ledger-ticket", "--ledger", str(self.ledger), "--ticket", "T1"
        )
        self.assertEqual(json.loads(proc.stdout)["status"], "未查验")

        proc = self.run_cli(
            "ledger-check", "--ledger", str(self.ledger)
        )
        self.assertTrue(json.loads(proc.stdout)["ok"])

        # Independent replay command yields the same verdict.
        proc = self.run_cli(
            "ledger-replay", "--ledger", str(self.ledger)
        )
        replay = json.loads(proc.stdout)
        t1 = [t for t in replay["tickets"] if t["ticket_id"] == "T1"][0]
        self.assertEqual(t1["status"], "未查验")

        # Nested spelling shares the same handlers.
        nested = self.run_cli(
            "ledger", "report", "--ledger", str(self.ledger)
        )
        self.assertEqual(
            json.loads(nested.stdout)["record_count"], 1
        )

    def test_cli_bad_batch_exits_2_with_code(self):
        bad = self.root / "bad.json"
        bad.write_text(
            json.dumps(batch("CB1", rec("C1", status="未到场"))),
            encoding="utf-8",
        )
        # First import the good one.
        self.run_cli(
            "ledger-import", "--ledger", str(self.ledger),
            "--input", str(self.batch_file),
        )
        proc = self.run_cli(
            "ledger-import", "--ledger", str(self.ledger),
            "--input", str(bad), check=False,
        )
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr.strip().splitlines()[-1])
        self.assertFalse(err["ok"])
        self.assertEqual(err["code"], "batch_content_conflict")


if __name__ == "__main__":
    unittest.main(verbosity=2)
