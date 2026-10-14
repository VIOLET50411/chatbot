from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from bridge.v1_models import InboundEnvelope
from bridge.v1_store import BridgeStore


class DualAccountBridgeStressTests(unittest.TestCase):
    def test_concurrent_duplicate_burst_stays_isolated_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            stores = {
                "du-jiaying": BridgeStore(root / "du" / "soul_mate.db"),
                "ji-yu": BridgeStore(root / "ji" / "soul_mate.db"),
            }
            contacts = {}
            for account_id, store in stores.items():
                store.initialize_schema()
                contacts[account_id] = store.create_contact(
                    external_id="owner-violet",
                    display_name="VIOLET",
                )

            work: list[tuple[str, int]] = []
            for account_id in stores:
                for index in range(100):
                    work.extend([(account_id, index), (account_id, index)])

            def submit(item: tuple[str, int]) -> tuple[str, str]:
                account_id, index = item
                digest = hashlib.sha256(f"{account_id}:{index}".encode("utf-8")).hexdigest()
                result = stores[account_id].record_inbound(
                    InboundEnvelope(
                        event_id=f"{account_id}:{index}",
                        external_contact_id="owner-violet",
                        occurred_at=datetime.now(timezone.utc),
                        type="text",
                        text=f"message-{index}",
                        attachment_refs=[],
                        message_hash=digest,
                        agent_id=f"windows:{account_id}",
                    )
                )
                return account_id, result.action

            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(executor.map(submit, work))

            for account_id, store in stores.items():
                account_results = [action for current, action in results if current == account_id]
                self.assertEqual(account_results.count("accepted"), 100)
                self.assertEqual(account_results.count("duplicate"), 100)
                self.assertEqual(
                    store.count_inbound(contact_id=contacts[account_id].id),
                    100,
                )

            self.assertNotEqual(
                stores["du-jiaying"].database_path,
                stores["ji-yu"].database_path,
            )


if __name__ == "__main__":
    unittest.main()
