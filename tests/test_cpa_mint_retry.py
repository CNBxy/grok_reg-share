from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cpa_xai.mint import _safe_log_callback, mint_and_export
from cpa_xai.sso_build import SSOConversionResult, SSOBuildError


VALID_RESULT = SSOConversionResult(
    access_token="test_access",
    refresh_token="test_refresh",
    id_token="test_id",
    expires_in=3600,
    user_id="usr_123",
    email="user@example.com",
    team_id="team_456",
    user_code="CODE-123",
)


class CpaMintTests(unittest.TestCase):
    def test_log_falls_back_for_gbk_console(self) -> None:
        captured = []

        def gbk_only(message: str) -> None:
            message.encode("gbk")
            captured.append(message)

        _safe_log_callback(gbk_only)("xAI 𝕏 login")
        self.assertEqual(captured, [r"xAI \U0001d54f login"])

    def test_rejects_missing_email(self) -> None:
        result = mint_and_export(email="", sso_token="sso_val", auth_dir="/tmp")
        self.assertFalse(result["ok"])
        self.assertIn("missing email", result.get("error", ""))

    def test_rejects_missing_sso(self) -> None:
        result = mint_and_export(email="u@e.com", sso_token="", auth_dir="/tmp")
        self.assertFalse(result["ok"])
        self.assertIn("missing sso_token", result.get("error", ""))

    @patch("cpa_xai.mint.convert_sso_to_build", return_value=VALID_RESULT)
    def test_writes_auth_on_success(self, _mocked_convert) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = mint_and_export(
                email="user@example.com",
                sso_token="sso_val",
                auth_dir=Path(tmp),
                probe=False,
            )
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(result["user_code"], "CODE-123")

    @patch("cpa_xai.mint.convert_sso_to_build", side_effect=SSOBuildError("bad sso"))
    def test_returns_error_on_conversion_failure(self, _mocked_convert) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = mint_and_export(
                email="user@example.com",
                sso_token="bad_sso",
                auth_dir=Path(tmp),
                probe=False,
            )
            self.assertFalse(result["ok"])
            self.assertIn("bad sso", result.get("error", ""))


if __name__ == "__main__":
    unittest.main()
