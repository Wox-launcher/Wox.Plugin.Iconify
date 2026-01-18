import unittest

from src.main import MyPlugin


class TestMyPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = MyPlugin()

    def test_query(self):
        results = self.plugin.query(None, {"search": "test"})
        self.assertIsNotNone(results)


if __name__ == "__main__":
    unittest.main()
