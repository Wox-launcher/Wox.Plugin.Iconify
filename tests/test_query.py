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
        
        args, kwargs = mock_urlopen.call_args
        
        request_obj = args[0]
        self.assertEqual(request_obj.get_header('User-agent'), 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        self.assertIn("query=test", request_obj.full_url)

if __name__ == "__main__":
    unittest.main()
