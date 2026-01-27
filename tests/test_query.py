import unittest
import json
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.main import MyPlugin
from wox_plugin import Query, Context

class TestMyPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = MyPlugin()

    @patch('urllib.request.urlopen')
    async def test_query(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "icons": ["mdi:home", "mdi:account"]
        }).encode('utf-8')
        
        cm = MagicMock()
        cm.__enter__.return_value = mock_response
        cm.__exit__.return_value = None
        mock_urlopen.return_value = cm

        query = MagicMock(spec=Query)
        query.search = "test"
        
        ctx = MagicMock(spec=Context)

        results = await self.plugin.query(ctx, query)
        
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "mdi:home")
        self.assertEqual(results[1].title, "mdi:account")
        
        args, _ = mock_urlopen.call_args
        self.assertIn("query=test", args[0])

if __name__ == "__main__":
    unittest.main()
