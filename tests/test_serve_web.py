import os
import unittest
from unittest.mock import patch

import serve_web


class ServeWebTests(unittest.TestCase):
    def test_public_bind_requires_admin_password(self):
        with patch.dict(
            os.environ,
            {"WEB_HOST": "0.0.0.0", "WEB_ADMIN_PASSWORD": ""},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                serve_web.main()

    def test_public_bind_rejects_short_admin_password(self):
        with patch.dict(
            os.environ,
            {"WEB_HOST": "0.0.0.0", "WEB_ADMIN_PASSWORD": "too-short"},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                serve_web.main()

    def test_waitress_runs_single_process_with_threads(self):
        with patch.dict(
            os.environ,
            {
                "WEB_HOST": "0.0.0.0",
                "WEB_PORT": "5050",
                "WEB_THREADS": "12",
                "WEB_ADMIN_PASSWORD": "strong-password-123",
            },
            clear=False,
        ):
            with patch.object(serve_web, "serve") as serve_mock:
                serve_web.main()

        serve_mock.assert_called_once()
        kwargs = serve_mock.call_args.kwargs
        self.assertEqual("0.0.0.0", kwargs["host"])
        self.assertEqual(5050, kwargs["port"])
        self.assertEqual(12, kwargs["threads"])


if __name__ == "__main__":
    unittest.main()
