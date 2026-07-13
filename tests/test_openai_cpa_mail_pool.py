import unittest
from unittest.mock import patch

import grok_register_ttk as reg
import web_app


class OpenAICPAMailPoolTests(unittest.TestCase):
    def setUp(self):
        self.original_config = reg.config
        reg.config = {
            **reg.DEFAULT_CONFIG,
            "email_provider": "openai_cpa",
            "defaultDomains": "mail.example.com",
            "openai_cpa_webhook_secret": "test-webhook-secret",
        }
        with reg._openai_cpa_code_pool_lock:
            reg._openai_cpa_code_pool.clear()

    def tearDown(self):
        reg.config = self.original_config
        with reg._openai_cpa_code_pool_lock:
            reg._openai_cpa_code_pool.clear()

    @staticmethod
    def _payload(code="ABC-123", email="user@mail.example.com"):
        return {
            "message_id": "message-1",
            "to_addr": email,
            "raw_content": (
                f"From: no-reply@x.ai\n"
                f"To: {email}\n"
                f"Subject: {code} xAI verification\n\n"
                f"Your verification code is {code}."
            ),
        }

    def test_valid_webhook_stores_and_returns_code(self):
        status, result = reg.accept_openai_cpa_webhook(
            self._payload(),
            "test-webhook-secret",
        )

        self.assertEqual(200, status)
        self.assertTrue(result["stored"])
        code = reg.openai_cpa_get_oai_code(
            "openai_cpa_catch_all",
            "user@mail.example.com",
            timeout=1,
        )
        self.assertEqual("ABC-123", code)
        self.assertEqual(0, reg.openai_cpa_memory_pool_stats()["pending"])

    def test_invalid_webhook_secret_is_rejected(self):
        status, result = reg.accept_openai_cpa_webhook(self._payload(), "wrong-secret")

        self.assertEqual(401, status)
        self.assertFalse(result["ok"])
        self.assertEqual(0, reg.openai_cpa_memory_pool_stats()["pending"])

    def test_numeric_six_digit_code_is_supported(self):
        status, _ = reg.accept_openai_cpa_webhook(
            self._payload(code="654321"),
            "test-webhook-secret",
        )

        self.assertEqual(200, status)
        self.assertEqual(
            "654321",
            reg.openai_cpa_get_oai_code("", "user@mail.example.com", timeout=1),
        )

    def test_openai_cpa_provider_generates_catch_all_address(self):
        email, token = reg.get_email_and_token()

        self.assertTrue(email.endswith("@mail.example.com"))
        self.assertEqual("openai_cpa_catch_all", token)

    def test_local_cloudmail_fallback_works_without_webhook_secret(self):
        reg.config.update({
            "openai_cpa_webhook_secret": "",
            "openai_cpa_cloudmail_fallback": True,
            "cloudmail_url": "https://mail.example.com",
            "cloudmail_admin_email": "admin@example.com",
            "cloudmail_password": "password",
        })
        with patch.object(reg, "_cloudmail_get_shared_token", return_value="public-token"):
            with patch.object(reg, "_cloudmail_poll_code_once", return_value="XYZ-789"):
                email, token = reg.get_email_and_token()
                code = reg.openai_cpa_get_oai_code(token, email, timeout=1)

        self.assertTrue(email.endswith("@mail.example.com"))
        self.assertEqual("XYZ-789", code)

    def test_memory_pool_wins_before_cloudmail_fallback(self):
        reg.config.update({
            "openai_cpa_cloudmail_fallback": True,
            "cloudmail_url": "https://mail.example.com",
            "cloudmail_admin_email": "admin@example.com",
            "cloudmail_password": "password",
        })
        status, _ = reg.accept_openai_cpa_webhook(
            self._payload(),
            "test-webhook-secret",
        )
        self.assertEqual(200, status)

        with patch.object(reg, "_cloudmail_get_shared_token", return_value="public-token"):
            with patch.object(reg, "_cloudmail_poll_code_once") as cloudmail_poll:
                code = reg.openai_cpa_get_oai_code(
                    "openai_cpa_catch_all",
                    "user@mail.example.com",
                    timeout=1,
                )

        self.assertEqual("ABC-123", code)
        cloudmail_poll.assert_not_called()

    def test_missing_webhook_and_incomplete_fallback_are_rejected(self):
        reg.config.update({
            "openai_cpa_webhook_secret": "",
            "openai_cpa_cloudmail_fallback": True,
            "cloudmail_url": "",
            "cloudmail_admin_email": "",
            "cloudmail_password": "",
        })

        with self.assertRaisesRegex(Exception, "CloudMail"):
            reg.get_email_and_token()

    def test_flask_webhook_route_uses_expected_protocol(self):
        client = web_app.app.test_client()
        with patch.object(reg, "load_config", return_value=reg.config):
            response = client.post(
                "/api/webhook/email",
                json=self._payload(),
                headers={"X-Webhook-Secret": "test-webhook-secret"},
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["stored"])


if __name__ == "__main__":
    unittest.main()
