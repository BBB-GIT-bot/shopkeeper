"""Run with python -m unittest discover -s tests -p test_settings.py -v."""
import os
import json
import hashlib
import hmac
import time
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db
from app.auth import SECRET, _b64
from app.main import app, model_completion
from app.settings import decrypt_key


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_path = db.DB_PATH
        db.DB_PATH = Path(tempfile.mkdtemp(prefix="shopkeeper-settings-")) / "test.db"
        cls.environment = patch.dict(os.environ, {"CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode(), "DATABASE_URL": 'sqlite:///' + db.DB_PATH.as_posix()})
        cls.environment.start()
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.tokens = {}
        for username in ("operator", "manager"):
            response = cls.client.post("/api/auth/login", json={"username": username, "password": username + "123"})
            assert response.status_code == 200, response.text
            cls.tokens[username] = {"Authorization": "Bearer " + response.json()["access_token"]}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.environment.stop()
        db.dispose_engines()
        db.DB_PATH = cls.original_path

    def setUp(self):
        with db.transaction() as connection:
            connection.execute("UPDATE service_configs SET base_url='',model='',api_key='',api_key_ciphertext='',temperature=0.2,request_timeout_seconds=60")
            connection.execute("UPDATE queue_configs SET concurrency=2,execution_timeout_seconds=60,max_auto_retries=2,poll_interval_seconds=2,dispatch_interval_seconds=1,heartbeat_interval_seconds=10")

    def update(self, section, body, role="manager"):
        return self.client.patch("/api/settings/" + section, json=body, headers=self.tokens[role])

    def test_service_encryption_retention_and_reread(self):
        response = self.update("services", {"api_key": "test-secret-value", "base_url": "https://example.invalid/v1", "model": "demo-model"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["api_key_configured"])
        self.assertNotIn("test-secret-value", response.text)
        with db.connect() as connection:
            config = dict(connection.execute("SELECT * FROM service_configs").fetchone())
        self.assertEqual(config["api_key"], "")
        self.assertEqual(decrypt_key(config["api_key_ciphertext"]), "test-secret-value")
        self.assertEqual(self.update("services", {"temperature": 0.5}).status_code, 200)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT api_key_ciphertext FROM service_configs").fetchone()[0], config["api_key_ciphertext"])
        reread = self.client.get("/api/settings/services", headers=self.tokens["manager"]).json()
        self.assertEqual(reread["temperature"], 0.5)
        self.assertNotIn("api_key", reread)
        self.assertNotIn("api_key_ciphertext", reread)
        self.assertEqual(self.update("services", {"api_key": "replacement-key"}).status_code, 200)

    def test_key_missing_invalid_and_atomic_save(self):
        for value in ("", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"CONFIG_ENCRYPTION_KEY": value}):
                response = self.update("services", {"api_key": "secret-test", "model": "must-not-save"})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("secret-test", response.text)
        self.assertEqual(self.client.get("/api/settings/services", headers=self.tokens["manager"]).json()["model"], "")

    def test_legacy_key_migration(self):
        with db.transaction() as connection:
            connection.execute("UPDATE service_configs SET api_key='legacy-test'")
        response = self.update("services", {"model": "updated"})
        self.assertEqual(response.status_code, 200)
        with db.connect() as connection:
            value = connection.execute("SELECT api_key,api_key_ciphertext FROM service_configs").fetchone()
            self.assertEqual(value[0], "")
            self.assertEqual(decrypt_key(value[1]), "legacy-test")

    def test_service_validation_does_not_echo_credentials(self):
        for body in ({"api_key": ""}, {"api_key": "••••••••"}, {"temperature": -1}, {"temperature": 3}, {"request_timeout_seconds": True}, {"request_timeout_seconds": 0}, {"multimodal_mode": "real"}, {"base_url": "https://username:secret@example.invalid"}, {"model": None}, {"api_key": {"secret": "must-not-echo"}}, {}):
            with self.subTest(body=body):
                response = self.update("services", body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("must-not-echo", response.text)
                self.assertNotIn("username:secret", response.text)

    def test_retry_zero_and_queue_persistence(self):
        response = self.update("queue", {"max_auto_retries": 0, "dispatch_interval_seconds": 2})
        self.assertEqual(response.status_code, 200, response.text)
        reread = self.client.get("/api/settings/queue", headers=self.tokens["manager"]).json()
        self.assertEqual(reread["max_auto_retries"], 0)
        self.assertEqual(reread["dispatch_interval_seconds"], 2)
        self.assertNotIn("heartbeat_seconds", reread)

    def test_invalid_queue_is_atomic(self):
        for body in ({"concurrency": True}, {"concurrency": 0}, {"max_auto_retries": -1}, {"poll_interval_seconds": 1.5}, {"dispatch_interval_seconds": "2"}, {"execution_timeout_seconds": 10}, {"heartbeat_interval_seconds": 60}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.update("queue", body).status_code, 422)
        value = self.client.get("/api/settings/queue", headers=self.tokens["manager"]).json()
        self.assertEqual(value["execution_timeout_seconds"], 60)
        self.assertEqual(self.update("queue", {"execution_timeout_seconds": 5, "heartbeat_interval_seconds": 2}).status_code, 200)

    def test_operator_cannot_access_settings(self):
        for section in ("queue", "services"):
            self.assertEqual(self.client.get("/api/settings/" + section, headers=self.tokens["operator"]).status_code, 403)
            self.assertEqual(self.update(section, {"concurrency": 1}, role="operator").status_code, 403)
            self.assertEqual(self.client.get("/api/settings/" + section).status_code, 401)

    def test_authorization_and_no_password_storage(self):
        created = self.client.post("/api/shops", headers=self.tokens["operator"], json={"shop_code": "CONFIG-DEMO", "platform": "demo", "name": "教学店铺", "platform_shop_id": "demo-id"}).json()
        path = "shops/" + str(created["id"]) + "/authorization"
        for status in ("authorized", "expired", "not_configured"):
            self.assertEqual(self.update(path, {"authorization_status": status}).json()["authorization_status"], status)
        self.assertEqual(self.update(path, {"authorization_status": "authorized"}, role="operator").status_code, 403)
        self.assertEqual(self.update(path, {"authorization_status": "authorized", "password": "must-not-echo"}).status_code, 422)
        self.assertEqual(self.update("shops/999999/authorization", {"authorization_status": "authorized"}).status_code, 404)

    def test_model_receives_decrypted_key_only(self):
        self.update("services", {"api_key": "test-model-key", "base_url": "https://example.invalid/v1", "model": "configured-model"})
        response = httpx.Response(200, request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"), json={"choices": [{"message": {"content": '{"result":"ok"}'}}]})
        with patch("app.main.httpx.post", return_value=response) as post:
            self.assertEqual(model_completion("diagnosis", {}, {})["generation_mode"], "real")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-model-key")

    def test_model_error_does_not_echo_secrets(self):
        self.update("services", {"api_key": "test-model-key", "base_url": "https://example.invalid/v1", "model": "configured-model"})
        with patch("app.main.httpx.post", side_effect=httpx.HTTPError("test-model-key")):
            with self.assertRaises(Exception) as caught:
                model_completion("diagnosis", {}, {})
        self.assertNotIn("test-model-key", str(caught.exception.detail))

    def test_invalid_token_returns_401(self):
        for token in ("bad", "bad.bad.bad", "a.b.c"):
            response = self.client.get("/api/settings/queue", headers={"Authorization": "Bearer " + token})
            self.assertEqual(response.status_code, 401)

    def test_signed_expired_and_wrong_algorithm_tokens(self):
        for alg, exp in (("HS256", int(time.time()) - 10), ("none", int(time.time()) + 100)):
            header = _b64(json.dumps({"alg": alg}).encode())
            payload = _b64(json.dumps({"sub": "1", "exp": exp}).encode())
            signature = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
            response = self.client.get("/api/settings/queue", headers={"Authorization": f"Bearer {header}.{payload}.{signature}"})
            self.assertEqual(response.status_code, 401)

    def test_user_role_and_disabled_status_checked_each_request(self):
        with db.transaction() as connection:
            connection.execute("UPDATE users SET role='operator' WHERE username='manager'")
        try:
            self.assertEqual(self.client.get("/api/settings/queue", headers=self.tokens["manager"]).status_code, 403)
            with db.transaction() as connection:
                connection.execute("UPDATE users SET enabled=0 WHERE username='manager'")
            self.assertEqual(self.client.get("/api/settings/queue", headers=self.tokens["manager"]).status_code, 401)
        finally:
            with db.transaction() as connection:
                connection.execute("UPDATE users SET role='manager',enabled=1 WHERE username='manager'")

    def test_schema_upgrade_is_idempotent(self):
        db.init_db(); db.init_db()
        self.assertEqual(self.client.get("/api/settings/queue", headers=self.tokens["manager"]).status_code, 200)

    def test_old_configuration_schema_is_upgraded(self):
        previous = db.DB_PATH
        db.DB_PATH = previous.parent / 'legacy.db'
        try:
            with sqlite3.connect(db.DB_PATH) as connection:
                connection.executescript("CREATE TABLE service_configs(id INTEGER PRIMARY KEY,api_key TEXT); INSERT INTO service_configs VALUES(1,'legacy'); CREATE TABLE queue_configs(id INTEGER PRIMARY KEY,heartbeat_seconds INTEGER); INSERT INTO queue_configs VALUES(1,7);")
            with patch.dict(os.environ, {'DATABASE_URL': 'sqlite:///' + db.DB_PATH.as_posix()}):
                db.init_db(); db.init_db()
                with db.connect() as connection:
                    self.assertEqual(connection.execute("SELECT heartbeat_interval_seconds FROM queue_configs").fetchone()[0], 7)
                    self.assertEqual(connection.execute("SELECT api_key_ciphertext FROM service_configs").fetchone()[0], '')
        finally:
            db.DB_PATH = previous


if __name__ == "__main__":
    unittest.main()
