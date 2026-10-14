from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from persona.review_cases import PersonaReviewStore


def _cases(changed_reply: bool = False) -> list[dict[str, str]]:
    result = []
    for index in range(40):
        result.append({
            "category": "日常",
            "user_message": f"场景 {index + 1}",
            "context_note": "同一段上下文",
            "candidate_reply": "改过的回复" if changed_reply and index == 1 else f"回复 {index + 1}",
        })
    return result


class PersonaReviewStoreTests(unittest.TestCase):
    def test_only_exactly_unchanged_ratings_are_carried_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PersonaReviewStore(Path(temporary) / "reviews.db")
            store.seed_cases(candidate_snapshot_id="source", character_id="ji-yu", cases=_cases())
            source = store.list_cases("source")
            for item in source["items"][:2]:
                store.rate_case(
                    item["id"],
                    rating="realistic",
                    reasons=[],
                    note="Owner 已审核",
                    actor="owner",
                )
            store.seed_cases(
                candidate_snapshot_id="target",
                character_id="ji-yu",
                cases=_cases(changed_reply=True),
            )

            copied = store.carry_forward_exact_ratings(
                source_snapshot_id="source",
                target_snapshot_id="target",
            )
            target = store.list_cases("target")

            self.assertEqual(copied, 1)
            self.assertEqual(target["items"][0]["rating"], "realistic")
            self.assertIsNone(target["items"][1]["rating"])

    def test_strict_review_requires_every_case_rated_and_zero_artificial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PersonaReviewStore(Path(temporary) / "reviews.db")
            store.seed_cases(candidate_snapshot_id="candidate", character_id="ji-yu", cases=_cases())
            items = store.list_cases("candidate")["items"]
            for item in items[:-1]:
                store.rate_case(item["id"], rating="realistic", reasons=[], note="", actor="owner")

            incomplete = store.list_cases("candidate")["summary"]
            self.assertFalse(incomplete["passes_strict_review"])

            store.rate_case(items[-1]["id"], rating="artificial", reasons=["断句太碎"], note="", actor="owner")
            with_artificial = store.list_cases("candidate")["summary"]
            self.assertTrue(with_artificial["passes_85_percent"])
            self.assertFalse(with_artificial["passes_strict_review"])

            store.rate_case(items[-1]["id"], rating="acceptable", reasons=[], note="", actor="owner")
            passed = store.list_cases("candidate")["summary"]
            self.assertTrue(passed["passes_strict_review"])


if __name__ == "__main__":
    unittest.main()
