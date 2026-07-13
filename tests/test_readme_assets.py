from __future__ import annotations

from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class ReadmeAssetTests(unittest.TestCase):
    def test_hero_avoids_svg_filters_that_rasterize_text_in_safari(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r'<img src="(docs/assets/hero-[^"]+\.svg)"', readme)
        self.assertIsNotNone(match, "README hero SVG reference is required")
        hero = ROOT / match.group(1)
        root = ET.parse(hero).getroot()
        namespace = {"svg": "http://www.w3.org/2000/svg"}

        self.assertEqual(root.findall(".//svg:filter", namespace), [])
        self.assertEqual(
            [element for element in root.iter() if "filter" in element.attrib],
            [],
        )


if __name__ == "__main__":
    unittest.main()
