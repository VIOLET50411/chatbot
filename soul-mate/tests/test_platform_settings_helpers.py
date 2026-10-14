from __future__ import annotations

import base64
import unittest

from web.routes.platform_settings import _vision_test_image


class PlatformSettingsHelperTests(unittest.TestCase):
    def test_vision_connection_test_uses_valid_nontrivial_png(self) -> None:
        value = _vision_test_image()
        self.assertTrue(value.startswith("data:image/png;base64,"))
        payload = base64.b64decode(value.split(",", 1)[1])
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(payload), 100)
        self.assertEqual(int.from_bytes(payload[16:20], "big"), 64)
        self.assertEqual(int.from_bytes(payload[20:24], "big"), 64)


if __name__ == "__main__":
    unittest.main()
