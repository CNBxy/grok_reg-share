from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cpa_xai.mint import _is_browser_disconnect_error, _safe_log_callback, mint_and_export


class PageDisconnectedError(RuntimeError):
    pass


TOKENS = {
    "access_token": "access",
    "refresh_token": "refresh",
    "id_token": "id",
    "expires_in": 3600,
    "user_code": "CODE",
}


class CpaMintRetryTests(unittest.TestCase):
    def test_log_falls_back_for_gbk_console(self) -> None:
        captured = []

        def gbk_only(message: str) -> None:
            message.encode("gbk")
            captured.append(message)

        _safe_log_callback(gbk_only)("xAI 𝕏 login")
        self.assertEqual(captured, [r"xAI \U0001d54f login"])

    def test_disconnect_detection(self) -> None:
        self.assertTrue(_is_browser_disconnect_error(PageDisconnectedError("与页面的连接已断开")))
        self.assertFalse(_is_browser_disconnect_error(RuntimeError("auth failed: password")))

    @patch("cpa_xai.mint.time.sleep", return_value=None)
    @patch(
        "cpa_xai.mint.mint_with_browser",
        side_effect=[PageDisconnectedError("与页面的连接已断开"), TOKENS],
    )
    def test_retries_disconnect_then_writes_auth(self, mocked_mint, _mocked_sleep) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = mint_and_export(
                email="user@example.com",
                password="secret",
                auth_dir=Path(tmp),
                probe=False,
                browser_retries=2,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(mocked_mint.call_count, 2)
            self.assertTrue(Path(result["path"]).is_file())

    @patch("cpa_xai.mint.time.sleep", return_value=None)
    @patch("cpa_xai.mint.mint_with_browser", side_effect=RuntimeError("auth failed: password"))
    def test_does_not_retry_auth_failure(self, mocked_mint, _mocked_sleep) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = mint_and_export(
                email="user@example.com",
                password="wrong",
                auth_dir=Path(tmp),
                probe=False,
                browser_retries=2,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(mocked_mint.call_count, 1)


if __name__ == "__main__":
    unittest.main()
