from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.audio import audio_configured, transcribe_audio


class AudioTests(unittest.TestCase):
    def test_unconfigured_audio_is_safe_noop(self) -> None:
        with patch.dict(os.environ, {"AUDIO_API_KEY": ""}, clear=False):
            self.assertFalse(audio_configured())
            self.assertEqual(
                asyncio.run(transcribe_audio(Path("missing.bin"))),
                "",
            )


if __name__ == "__main__":
    unittest.main()
