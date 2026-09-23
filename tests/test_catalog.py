import io
import json
import sqlite3
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalog import IconCatalog, PackageInfo, SingleFlight, content_length_from_headers, format_bytes  # noqa: E402
from src.icon_svg import icon_to_svg, is_colorful, iter_icons  # noqa: E402
from src.query_text import parse_query  # noqa: E402


HOME = {
    "prefix": "mdi",
    "width": 24,
    "height": 24,
    "icons": {
        "dog": {"body": '<path fill="currentColor" d="M1 1"/>'},
        "dog-side": {"body": '<path fill="currentColor" d="M2 2"/>'},
        "hotdog": {"body": '<path fill="currentColor" d="M3 3"/>'},
        "badge-dog": {"body": '<path fill="#e53935" d="M8 8"/>'},
        "a_b": {"body": '<path fill="currentColor" d="M4 4"/>'},
        "axb": {"body": '<path fill="currentColor" d="M5 5"/>'},
    },
    "aliases": {"house": {"parent": "dog", "hFlip": True}},
}

GUIDE = {
    "prefix": "fa",
    "width": 16,
    "height": 16,
    "icons": {"guide-dog": {"body": '<path fill="currentColor" d="M9 9"/>', "width": 32, "height": 16}},
}


def _tar(path: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (("mdi", HOME), ("fa", GUIDE)):
            raw = json.dumps(payload).encode("utf-8")
            info = tarfile.TarInfo(name=f"package/json/{name}.json")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        extra = b"{}"
        info = tarfile.TarInfo(name="package/collections.json")
        info.size = len(extra)
        archive.addfile(info, io.BytesIO(extra))


class LayoutTests(unittest.TestCase):
    def test_download_result_is_list_and_icons_are_grid(self) -> None:
        from src.main import _grid_response, _list_response

        listed = json.loads(_list_response([]).to_json())
        grid = json.loads(_grid_response([]).to_json())
        self.assertNotIn("GridLayout", listed["Layout"])
        self.assertEqual(grid["Layout"]["GridLayout"]["Columns"], 10)
        self.assertEqual(grid["Layout"]["GridLayout"]["ItemPadding"], 12)
        self.assertEqual(grid["Layout"]["GridLayout"]["ItemMargin"], 6)
        self.assertFalse(grid["Layout"]["GridLayout"].get("ShowTitle", False))


class IconDataTests(unittest.TestCase):
    def test_alias_flip_and_non_square_svg(self) -> None:
        records = {record.name: record for record in iter_icons(HOME, "mdi")}
        self.assertIn("house", records)
        self.assertTrue(records["house"].h_flip)
        self.assertEqual(records["house"].body, records["dog"].body)

        svg = icon_to_svg(records["house"])
        self.assertIn("scale(-1 1)", svg)
        self.assertIn('fill="currentColor"', svg)
        colored = icon_to_svg(records["dog"], "#f00")
        self.assertNotIn("currentColor", colored)
        self.assertIn('fill="#f00"', colored)

        wide = icon_to_svg(iter_icons(GUIDE, "fa")[0])
        self.assertIn('height="1em"', wide)
        self.assertIn('width="2em"', wide)
        self.assertIn('viewBox="0 0 32 16"', wide)

    def test_colorful_detects_chromatic_paint(self) -> None:
        self.assertFalse(is_colorful('<path fill="currentColor" d="M0 0"/>'))
        self.assertFalse(is_colorful('<path fill="#000"/><path fill="#fff"/>'))
        self.assertTrue(is_colorful('<path fill="#e53935"/><path fill="#43a047"/>'))
        self.assertTrue(is_colorful('<path fill="red"/>'))

    def test_parse_query_color(self) -> None:
        self.assertEqual(parse_query("dog red"), ("dog", "red"))
        self.assertEqual(parse_query("dog #f00"), ("dog", "#f00"))
        self.assertEqual(parse_query("red"), ("red", None))


class CatalogTests(unittest.TestCase):
    def test_index_and_search_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "icons.tgz"
            _tar(archive)
            catalog = IconCatalog(root)
            catalog.index_tarball(archive, "test")
            self.assertTrue(catalog.is_ready())
            names = [record.name for record in catalog.search("dog")]
            self.assertEqual(names[:3], ["dog", "dog-side", "hotdog"])
            self.assertIn("guide-dog", names)
            self.assertEqual([record.name for record in catalog.search("mdi:dog")][:3], ["dog", "dog-side", "hotdog"])
            self.assertEqual([record.name for record in catalog.search("a_b")], ["a_b"])
            self.assertEqual([record.name for record in catalog.search("dog", palette="color")], ["badge-dog"])
            self.assertNotIn("badge-dog", [record.name for record in catalog.search("dog", palette="mono")])
            house = catalog.get("mdi", "house")
            self.assertIsNotNone(house)
            assert house is not None
            self.assertTrue(house.h_flip)
            catalog.close()

    def test_existing_index_gains_palette_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = IconCatalog(Path(tmp))
            catalog.root.mkdir(parents=True)
            conn = sqlite3.connect(catalog.db_path)
            conn.executescript(
                """
                CREATE TABLE icons (
                    id INTEGER PRIMARY KEY, body TEXT NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
                    rotate INTEGER NOT NULL, hflip INTEGER NOT NULL, vflip INTEGER NOT NULL
                );
                CREATE TABLE names (id INTEGER PRIMARY KEY, prefix TEXT NOT NULL, name TEXT NOT NULL, key TEXT NOT NULL);
                INSERT INTO icons VALUES (1, '<path fill="#e53935"/>', 16, 16, 0, 0, 0, 0, 0);
                INSERT INTO icons VALUES (2, '<path fill="currentColor"/>', 16, 16, 0, 0, 0, 0, 0);
                INSERT INTO names VALUES (1, 'mdi', 'flag', 'mdi:flag');
                INSERT INTO names VALUES (2, 'mdi', 'card', 'mdi:card');
                """
            )
            conn.commit()
            conn.close()
            self.assertEqual([record.name for record in catalog.search("flag", palette="color")], ["flag"])
            self.assertEqual([record.name for record in catalog.search("card", palette="mono")], ["card"])
            self.assertEqual(catalog.search("card", palette="color"), [])
            catalog.close()

    def test_single_flight_rejects_second_caller(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingCatalog(IconCatalog):
            def _download_and_index(self, package: PackageInfo, cancel: threading.Event) -> None:
                del package, cancel
                started.set()
                release.wait(2)

        with tempfile.TemporaryDirectory() as tmp:
            catalog = BlockingCatalog(Path(tmp))
            package = PackageInfo(version="1", tarball_url="https://example.test/a.tgz", integrity="", download_size=1)
            results: list[bool] = []

            def run() -> None:
                results.append(catalog.download_and_index(package, threading.Event()))

            first = threading.Thread(target=run)
            first.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(catalog.download_and_index(package, threading.Event()))
            release.set()
            first.join(2)
            self.assertEqual(results, [True])
            self.assertFalse(catalog.flight.running)

    def test_format_bytes_and_flight(self) -> None:
        self.assertEqual(format_bytes(99 * 1024 * 1024), "99.0 MB")

    def test_content_length_prefers_full_archive_size(self) -> None:
        self.assertEqual(content_length_from_headers({"Content-Length": "103809123"}), 103809123)
        ranged = {"Content-Length": "1", "Content-Range": "bytes 0-0/103809123"}
        self.assertEqual(content_length_from_headers(ranged), 103809123)
        flight = SingleFlight()
        self.assertTrue(flight.try_begin())
        self.assertFalse(flight.try_begin())
        flight.end()
        self.assertTrue(flight.try_begin())


if __name__ == "__main__":
    unittest.main()
