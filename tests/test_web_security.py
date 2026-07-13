import base64
import os
import unittest
from unittest.mock import patch

import grok_register_ttk as reg
import web_app


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = web_app.app.test_client()

    @staticmethod
    def _basic(user="admin", password="strong-password"):
        value = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {value}"}

    def test_loopback_keeps_local_no_password_compatibility(self):
        with patch.dict(os.environ, {"WEB_ADMIN_PASSWORD": ""}, clear=False):
            response = self.client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})

        self.assertEqual(200, response.status_code)

    def test_remote_admin_is_disabled_when_password_is_missing(self):
        with patch.dict(os.environ, {"WEB_ADMIN_PASSWORD": ""}, clear=False):
            response = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.10"})

        self.assertEqual(503, response.status_code)

    def test_remote_admin_requires_basic_auth(self):
        with patch.dict(
            os.environ,
            {"WEB_ADMIN_USER": "admin", "WEB_ADMIN_PASSWORD": "strong-password"},
            clear=False,
        ):
            response = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.10"})

        self.assertEqual(401, response.status_code)
        self.assertIn("Basic", response.headers.get("WWW-Authenticate", ""))

    def test_correct_basic_auth_can_open_admin(self):
        with patch.dict(
            os.environ,
            {"WEB_ADMIN_USER": "admin", "WEB_ADMIN_PASSWORD": "strong-password"},
            clear=False,
        ):
            response = self.client.get(
                "/",
                headers=self._basic(),
                environ_base={"REMOTE_ADDR": "203.0.113.10"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("DENY", response.headers.get("X-Frame-Options"))

    def test_cross_origin_admin_write_is_rejected(self):
        headers = {**self._basic(), "Origin": "https://evil.example"}
        with patch.dict(
            os.environ,
            {"WEB_ADMIN_USER": "admin", "WEB_ADMIN_PASSWORD": "strong-password"},
            clear=False,
        ):
            response = self.client.post(
                "/api/stop",
                headers=headers,
                environ_base={"REMOTE_ADDR": "203.0.113.10", "HTTP_HOST": "reg.example.com"},
            )

        self.assertEqual(403, response.status_code)

    def test_webhook_is_public_but_keeps_its_own_secret(self):
        original_config = reg.config
        reg.config = {
            **reg.DEFAULT_CONFIG,
            "openai_cpa_webhook_secret": "mail-secret",
        }
        payload = {
            "message_id": "message-1",
            "to_addr": "user@example.com",
            "raw_content": "Subject: ABC-123 xAI verification\n\nCode: ABC-123",
        }
        try:
            with patch.dict(
                os.environ,
                {"WEB_ADMIN_USER": "admin", "WEB_ADMIN_PASSWORD": "strong-password"},
                clear=False,
            ):
                with patch.object(reg, "load_config", return_value=reg.config):
                    response = self.client.post(
                        "/api/webhook/email",
                        json=payload,
                        headers={"X-Webhook-Secret": "mail-secret"},
                        environ_base={"REMOTE_ADDR": "203.0.113.10"},
                    )
        finally:
            reg.config = original_config
            with reg._openai_cpa_code_pool_lock:
                reg._openai_cpa_code_pool.clear()

        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
