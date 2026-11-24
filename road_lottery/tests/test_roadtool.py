import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from roadtool.core import (  # noqa: E402
    BadCorrection,
    BadVersion,
    Repository,
    build_audit_bundle,
    pair_receipt_docs,
    verify_audit_bundle,
    verify_receipts_doc,
)


SIMPLE_FRAME = {
    "routes": ["R1", "R2"],
    "segments": [
        {"id": "S1", "route": "R1", "order": 0, "length": 10, "forbidden": []},
        {"id": "S2", "route": "R1", "order": 1, "length": 10},
        {"id": "S3", "route": "R2", "order": 0, "length": 10,
         "forbidden": [[2, 4]]},
    ],
}


class SegmentLocateTests(unittest.TestCase):
    def test_merged_intervals_and_rank_selection(self):
        repo = Repository(tempfile.mkdtemp())
        seg = repo and None
        from roadtool.core import parse_segment

        segment = parse_segment(
            {
                "id": "S",
                "route": "R",
                "order": 0,
                "length": 100,
                "forbidden": [[30, 35], [10, 20], [15, 18]],
            },
            {"R"},
        )
        # [10,20) and [15,18) union remains [10,20); plus [30,35)
        self.assertEqual(segment.effective_count, 85)
        for rank, local in [
            (0, 0),
            (9, 9),
            (10, 20),
            (19, 29),
            (20, 35),
            (21, 36),
            (84, 99),
        ]:
            found, _ = segment.locate(rank)
            self.assertEqual(found, local, rank)

    def test_huge_length_and_denominator_are_not_expanded(self):
        huge = 10 ** 40
        from roadtool.core import parse_segment

        segment = parse_segment(
            {"id": "BIG", "route": "R", "order": 0, "length": huge,
             "forbidden": [[10 ** 20, 10 ** 20 + 7]]},
            {"R"},
        )
        self.assertEqual(segment.effective_count, huge - 7)
        local, evidence = segment.locate(10 ** 20 + 3)
        # Seven forbidden positions precede this post-hole rank, so shift +7.
        self.assertEqual(local, 10 ** 20 + 10)
        self.assertEqual(evidence["forbidden_before_local"], 7)


class RepositoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.tmp.name) / "repo"
        self.repo = Repository.initialize(self.repo_path, SIMPLE_FRAME)

    def tearDown(self):
        self.tmp.cleanup()

    def draw(self, version, tickets):
        from roadtool.core import draw_tickets

        manifest = self.repo.load_version(version)
        return draw_tickets(self.repo, manifest, {"tickets": tickets})

    def test_total_and_path_evidence(self):
        v0 = self.repo.load_version("v0")
        self.assertEqual(v0["total_effective_coordinates"], 28)
        self.assertEqual(v0["segment_count"], 3)

        doc = self.draw(
            "v0",
            [
                {"id": "A", "r": "0"},
                {"id": "B", "r": "10/28"},
                {"id": "C", "r": "22/28"},
            ],
        )
        points = [r["point"] for r in doc["receipts"]]
        self.assertEqual(points[0]["segment_id"], "S1")
        self.assertEqual(points[0]["local_coordinate"], 0)
        self.assertEqual(points[1]["segment_id"], "S2")
        self.assertEqual(points[2]["segment_id"], "S3")
        self.assertEqual(points[2]["local_coordinate"], 4)

        receipt = doc["receipts"][2]
        ev = receipt["evidence"]
        self.assertEqual(receipt["global_rank"], 22)
        self.assertEqual(ev["route"]["before_effective"], 20)
        self.assertEqual(ev["segment"]["before_effective_on_route"], 0)
        self.assertEqual(ev["segment"]["intra_rank"], 2)
        self.assertEqual(ev["valid_before_point_global"], 22)
        self.assertEqual(
            ev["route"]["before_effective"]
            + ev["segment"]["before_effective_on_route"]
            + ev["segment"]["intra_rank"],
            22,
        )

    def test_full_segment_retirement_causes_cross_segment_change(self):
        self.repo.apply_correction(
            "v0",
            [{"op": "retire", "id": "S1"}],
            new_version_id="v1",
        )
        old = self.draw("v0", [{"id": "A", "r": "0"}])
        new = self.draw("v1", [{"id": "A", "r": "0"}])
        self.assertEqual(old["receipts"][0]["point"]["segment_id"], "S1")
        self.assertEqual(new["receipts"][0]["point"]["segment_id"], "S2")

        pair = pair_receipt_docs(old, new)
        p = pair["pairs"][0]
        self.assertEqual(p["change"], "segment_changed")
        self.assertTrue(p["physical_point_changed"])
        self.assertTrue(p["requires_new_inspection"])
        self.assertEqual(p["old_receipt"]["version_id"], "v0")
        self.assertEqual(p["new_receipt"]["version_id"], "v1")

    def test_same_segment_intra_move(self):
        self.repo.apply_correction(
            "v0",
            [{
                "op": "replace",
                "segment": {
                    "id": "S1", "route": "R1", "order": 0, "length": 10,
                    "forbidden": [[0, 1]],
                },
            }],
            new_version_id="v-move",
        )
        old = self.draw("v0", [{"id": "A", "r": "0"}])
        new = self.draw("v-move", [{"id": "A", "r": "0"}])
        self.assertEqual(old["receipts"][0]["point"]["local_coordinate"], 0)
        self.assertEqual(new["receipts"][0]["point"]["segment_id"], "S1")
        self.assertEqual(new["receipts"][0]["point"]["local_coordinate"], 1)
        pair = pair_receipt_docs(old, new)
        self.assertEqual(pair["pairs"][0]["change"], "intra_segment_moved")

    def test_route_only_change_is_not_physical_recheck(self):
        # S2 is last in R1 and becomes first in R2 via order 0 + ID tie.
        # Its global prefix remains 10.
        self.repo.apply_correction(
            "v0",
            [{
                "op": "replace",
                "segment": {
                    "id": "S2", "route": "R2", "order": 0, "length": 10,
                },
            }],
            new_version_id="v-route",
        )
        old = self.draw("v0", [{"id": "A", "r": "15/28"}])
        new = self.draw("v-route", [{"id": "A", "r": "15/28"}])
        self.assertEqual(old["receipts"][0]["point"]["segment_id"], "S2")
        self.assertEqual(new["receipts"][0]["point"]["segment_id"], "S2")
        self.assertEqual(old["receipts"][0]["point"]["local_coordinate"], 5)
        self.assertEqual(new["receipts"][0]["point"]["local_coordinate"], 5)
        pair = pair_receipt_docs(old, new)
        p = pair["pairs"][0]
        self.assertEqual(p["change"], "unchanged")
        self.assertFalse(p["physical_point_changed"])
        self.assertTrue(p["route_changed"])
        self.assertEqual(len(pair["route_only_notices"]), 1)

    def test_empty_frame_recovery_and_loss(self):
        empty_repo_path = Path(self.tmp.name) / "empty"
        repo = Repository.initialize(
            empty_repo_path,
            {"routes": ["R"], "segments": [
                {"id": "E", "route": "R", "order": 0, "length": 0}
            ]},
        )
        v0 = repo.load_version("v0")
        self.assertEqual(v0["total_effective_coordinates"], 0)
        doc = draw_tickets_safe(repo, "v0", [{"id": "X", "r": "0"}])
        self.assertIsNone(doc["receipts"][0]["point"])

        repo.apply_correction(
            "v0",
            [{"op": "add", "segment": {
                "id": "N", "route": "R", "order": 1, "length": 4
            }}],
            new_version_id="v1",
        )
        recovered = draw_tickets_safe(repo, "v1", [{"id": "X", "r": "0"}])
        self.assertEqual(recovered["receipts"][0]["point"]["segment_id"], "N")
        pair = pair_receipt_docs(doc, recovered)
        self.assertEqual(pair["pairs"][0]["change"], "empty_to_point")

        repo.apply_correction(
            "v1", [{"op": "retire", "id": "N"}], new_version_id="v2"
        )
        lost = draw_tickets_safe(repo, "v2", [{"id": "X", "r": "0"}])
        pair = pair_receipt_docs(recovered, lost)
        self.assertEqual(pair["pairs"][0]["change"], "point_to_empty")

    def test_bad_correction_is_atomic(self):
        with self.assertRaises(BadCorrection):
            self.repo.apply_correction(
                "v0",
                [
                    {"op": "add", "segment": {
                        "id": "S1", "route": "R1", "order": 9, "length": 1
                    }},
                    {"op": "retire", "id": "MISSING"},
                ],
                new_version_id="vbad",
            )
        self.assertFalse((self.repo_path / "versions" / "vbad.json").exists())
        v0 = self.repo.load_version("v0")
        self.assertEqual(v0["segment_count"], 3)

    def test_duplicate_batch_target_rejected(self):
        with self.assertRaises(BadCorrection):
            self.repo.apply_correction(
                "v0",
                [
                    {"op": "retire", "id": "S1"},
                    {"op": "replace", "segment": {
                        "id": "S1", "route": "R1", "order": 0, "length": 2
                    }},
                ],
            )

    def test_old_version_remains_queryable_after_branching(self):
        self.repo.apply_correction(
            "v0", [{"op": "retire", "id": "S1"}], new_version_id="v1"
        )
        self.repo.apply_correction(
            "v0",
            [{"op": "add", "segment": {
                "id": "B", "route": "R2", "order": 5, "length": 3
            }}],
            new_version_id="v-branch",
        )
        old = self.draw("v0", [{"id": "A", "r": "0"}])
        branch = self.repo.load_version("v-branch")
        self.assertEqual(branch["segment_count"], 4)
        self.assertEqual(old["receipts"][0]["point"]["segment_id"], "S1")

    def test_receipt_reverify_and_audit_bundle(self):
        doc = self.draw("v0", [{"id": "A", "r": "7/28"}, {"id": "B", "r": "0"}])
        report = verify_receipts_doc(self.repo, doc)
        self.assertTrue(report["ok"], report)

        bundle = build_audit_bundle(
            self.repo, "v0", {"tickets": [{"id": "A", "r": "7/28"}]}
        )
        audit_report = verify_audit_bundle(bundle)
        self.assertTrue(audit_report["ok"], audit_report)

    def test_same_order_tie_uses_segment_id(self):
        from roadtool.core import draw_tickets

        with tempfile.TemporaryDirectory() as d:
            repo = Repository.initialize(
                Path(d) / "tie",
                {
                    "routes": ["R"],
                    "segments": [
                        {"id": "B", "route": "R", "order": 7, "length": 1},
                        {"id": "A", "route": "R", "order": 7, "length": 1},
                    ],
                },
            )
            doc = draw_tickets(
                repo,
                repo.load_version("v0"),
                {"r": "0", "tickets": ["T"]},
            )
            self.assertEqual(doc["receipts"][0]["point"]["segment_id"], "A")

    def test_denominator_above_64_bits(self):
        from roadtool.core import draw_tickets

        q = 10 ** 40
        p = 3 * 10 ** 39  # exactly 3/10
        with tempfile.TemporaryDirectory() as d:
            repo = Repository.initialize(
                Path(d) / "bigden",
                {
                    "routes": ["R"],
                    "segments": [{"id": "S", "route": "R", "order": 0, "length": 10}],
                },
            )
            doc = draw_tickets(
                repo,
                repo.load_version("v0"),
                {"tickets": [{"id": "T", "r": {"p": p, "q": q}}]},
            )
            receipt = doc["receipts"][0]
            self.assertEqual(receipt["formula_rank"], 3)
            self.assertEqual(receipt["point"]["local_coordinate"], 3)

    def test_persistent_corrections_match_naive_model(self):
        from roadtool.core import draw_tickets, parse_segment

        rng = random.Random(20261124)
        route_index = {"R0": 0, "R1": 1, "R2": 2}
        with tempfile.TemporaryDirectory() as d:
            repo = Repository.initialize(
                Path(d) / "property",
                {"routes": ["R0", "R1", "R2"], "segments": []},
            )
            naive = {}
            base = "v0"

            for step in range(120):
                ops = []
                next_naive = dict(naive)
                used = set()
                for j in range(rng.randint(1, 3)):
                    existing = list(naive)
                    if rng.random() < 0.4 or not existing:
                        sid = f"S{step}_{j}_{rng.randrange(10**9)}"
                        used.add(sid)
                        raw = {
                            "id": sid,
                            "route": f"R{rng.randrange(3)}",
                            "order": rng.randrange(-5, 15),
                            "length": rng.randrange(0, 30),
                        }
                        op = "add"
                    else:
                        sid = rng.choice(existing)
                        if sid in used:
                            continue
                        used.add(sid)
                        if rng.random() < 0.75:
                            raw = {
                                "id": sid,
                                "route": f"R{rng.randrange(3)}",
                                "order": rng.randrange(-5, 15),
                                "length": rng.randrange(0, 30),
                            }
                            op = "replace"
                        else:
                            ops.append({"op": "retire", "id": sid})
                            next_naive.pop(sid)
                            continue

                    if raw["length"] > 2 and rng.random() < 0.7:
                        a = rng.randrange(raw["length"])
                        b = rng.randrange(a, raw["length"] + 1)
                        raw["forbidden"] = [[a, b]]
                    ops.append({"op": op, "segment": raw})
                    next_naive[sid] = parse_segment(
                        raw, {"R0", "R1", "R2"}
                    )

                version = f"p{step}"
                manifest = repo.apply_correction(
                    base, ops, new_version_id=version
                )
                base = version
                naive = next_naive

                ordered = sorted(
                    naive.values(),
                    key=lambda s: (
                        route_index[s.route_id],
                        s.order,
                        s.seg_id,
                    ),
                )
                expected_w = sum(s.effective_count for s in ordered)
                self.assertEqual(
                    manifest["total_effective_coordinates"], expected_w
                )
                self.assertEqual(manifest["segment_count"], len(naive))

                ranks = []
                if expected_w:
                    ranks = [0, expected_w - 1, rng.randrange(expected_w)]
                sheet = {
                    "tickets": [
                        {"id": f"T{i}", "r": f"{rank}/{expected_w}"}
                        if expected_w
                        else {"id": f"T{i}", "r": "0"}
                        for i, rank in enumerate(ranks or [0])
                    ]
                }
                doc = draw_tickets(repo, manifest, sheet)
                for rank, receipt in zip(ranks, doc["receipts"]):
                    remaining = rank
                    expected_point = None
                    for segment in ordered:
                        if remaining < segment.effective_count:
                            local, _ = segment.locate(remaining)
                            expected_point = (segment.seg_id, local)
                            break
                        remaining -= segment.effective_count
                    got = (
                        receipt["point"]["segment_id"],
                        receipt["point"]["local_coordinate"],
                    )
                    self.assertEqual(got, expected_point)

    def test_bad_frame_duplicate_and_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(BadVersion):
                Repository.initialize(
                    Path(d) / "bad1",
                    {
                        "routes": ["R"],
                        "segments": [
                            {"id": "S", "route": "R", "order": 0, "length": 3},
                            {"id": "S", "route": "R", "order": 1, "length": 3},
                        ],
                    },
                )
            with self.assertRaises(BadVersion):
                Repository.initialize(
                    Path(d) / "bad2",
                    {
                        "routes": ["R"],
                        "segments": [{
                            "id": "S", "route": "R", "order": 0, "length": 3,
                            "forbidden": [[2, 4]],
                        }],
                    },
                )


def draw_tickets_safe(repo, version, tickets):
    from roadtool.core import draw_tickets

    return draw_tickets(
        repo, repo.load_version(version), {"tickets": tickets}
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
