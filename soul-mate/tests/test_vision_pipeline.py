from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.vision import _description_instruction, _prepare_inline_image


class VisionPipelineTests(unittest.TestCase):
    def test_small_screenshot_is_upscaled_for_ocr(self) -> None:
        source = io.BytesIO()
        Image.new("RGB", (162, 360), "white").save(source, format="JPEG")
        prepared, mime_type = _prepare_inline_image(source.getvalue(), "image/jpeg")
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertGreaterEqual(image.width, 648)
            self.assertGreaterEqual(image.height, 1400)
        self.assertEqual(mime_type, "image/png")

    def test_vision_instruction_prioritizes_ocr_and_forbids_number_guessing(self) -> None:
        instruction = _description_instruction()
        self.assertIn("优先 OCR", instruction)
        self.assertIn("不要把连续数字擅自猜成乐谱", instruction)
        self.assertIn("看不清", instruction)


if __name__ == "__main__":
    unittest.main()
