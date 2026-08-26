import json
import unittest
from datetime import datetime, time, timezone
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row

import app as autoweb


TEST_SCHEMA = "autoweb_test"
BASE_URL = autoweb.DATABASE_URL


def test_db():
    connection = psycopg.connect(BASE_URL, row_factory=dict_row)
    connection.execute(f"SET search_path TO {TEST_SCHEMA}")
    return connection


class AutowebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = autoweb.db
        autoweb.db = test_db
        autoweb.app.config.update(TESTING=True, SECRET_KEY="autoweb-test-secret")

    @classmethod
    def tearDownClass(cls):
        autoweb.db = cls.original_db
        with psycopg.connect(BASE_URL, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")

    def setUp(self):
        with psycopg.connect(BASE_URL, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
            connection.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        autoweb.init_db()
        self.client = autoweb.app.test_client()

    def login(self, email="admin@cotelink.cl", password="admin123"):
        return self.client.post("/api/login", json={"email": email, "password": password})

    def test_authentication_and_session(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["service"], "Autoweb")
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.login(password="incorrecta").status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["role"], "Administrador")
        self.assertIsNotNone(self.client.get("/api/me").get_json()["user"])
        self.client.post("/api/logout")
        self.assertIsNone(self.client.get("/api/me").get_json()["user"])

    def test_keywords_crud_and_duplicate_validation(self):
        self.login()
        created = self.client.post("/api/keywords", json={"phrase": "energía solar"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.post("/api/keywords", json={"phrase": "energía solar"}).status_code, 409)
        item = next(x for x in self.client.get("/api/keywords").get_json()["items"] if x["phrase"] == "energía solar")
        self.assertEqual(self.client.put(f"/api/keywords/{item['id']}", json={"phrase": "energía solar", "active": False}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/keywords/{item['id']}").status_code, 200)

    def test_users_roles_and_permissions(self):
        self.login()
        role = {"name": "Auditor", "description": "Solo auditoría", "permissions": {"dashboard": True, "logs": True}}
        self.assertEqual(self.client.post("/api/roles", json=role).status_code, 201)
        user = {"name": "Ana Prueba", "email": "ana@example.com", "password": "secreto1", "role": "Auditor"}
        self.assertEqual(self.client.post("/api/users", json=user).status_code, 201)
        self.client.post("/api/logout")
        self.assertEqual(self.login("ana@example.com", "secreto1").status_code, 200)
        self.assertEqual(self.client.get("/api/runs").status_code, 200)
        self.assertEqual(self.client.get("/api/keywords").status_code, 403)

    def test_schedule_calculation_and_api(self):
        self.login()
        response = self.client.put("/api/schedule", json={"frequency": "daily", "hour": "14:45"})
        self.assertEqual(response.status_code, 200)
        setting = self.client.get("/api/schedule").get_json()["setting"]
        self.assertEqual(setting["hour"], "14:45")
        calculated = autoweb.next_scheduled_run(datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc), "daily", time(14, 45))
        self.assertEqual(calculated.astimezone().strftime("%H:%M"), "14:45")
        self.assertEqual(self.client.put("/api/schedule", json={"frequency": "invalid", "hour": "10:00"}).status_code, 400)

    def test_browser_automation_configuration(self):
        self.login()
        self.assertEqual(self.client.put("/api/browser-automation", json={"enabled": True}).status_code, 200)
        engine = {"name": "DuckDuckGo", "browser": "chrome", "search_url": "https://duckduckgo.com/?q={query}", "active": True}
        self.assertEqual(self.client.post("/api/browser-engines", json=engine).status_code, 201)
        config = self.client.get("/api/browser-automation").get_json()
        self.assertTrue(config["enabled"])
        created = next(x for x in config["engines"] if x["name"] == "DuckDuckGo")
        created["active"] = False
        self.assertEqual(self.client.put(f"/api/browser-engines/{created['id']}", json=created).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/browser-engines/{created['id']}").status_code, 200)
        self.assertEqual(self.client.post("/api/browser-engines", json={**engine, "search_url": "https://example.com"}).status_code, 400)

    def test_mail_relay_configuration_template_and_test(self):
        self.login()
        setting = {"enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587, "security": "starttls", "username": "relay", "password": "secret", "sender_name": "Autoweb", "sender_email": "alertas@example.com", "admin_email": "admin@example.com"}
        self.assertEqual(self.client.put("/api/mail-relay", json=setting).status_code, 200)
        saved = self.client.get("/api/mail-relay").get_json()["setting"]
        self.assertTrue(saved["configured"])
        self.assertEqual(saved["admin_email"], "admin@example.com")
        relay = {**setting}
        message = autoweb.notification_message(relay, {"name": "Ana", "email": "ana@example.com"}, {"title": "Hallazgo", "url": "https://example.com/1", "keyword": "tema", "source": "Google"})
        self.assertEqual(message["Bcc"], "admin@example.com")
        self.assertIn("Hola Ana", message.get_body(preferencelist=("html",)).get_content())
        digest = autoweb.digest_notification_message(relay, {"name": "Ana", "email": "ana@example.com"}, [
            {"title": "Hallazgo uno", "url": "https://example.com/1", "keyword": "tema 1", "source": "Google"},
            {"title": "Hallazgo dos", "url": "https://example.com/2", "keyword": "tema 2", "source": "Bing"},
        ])
        digest_html = digest.get_body(preferencelist=("html",)).get_content()
        self.assertIn("2 nuevos registros", digest["Subject"])
        self.assertIn("Hallazgo uno", digest_html)
        self.assertIn("Hallazgo dos", digest_html)
        with patch.object(autoweb, "send_mail") as sender:
            self.assertEqual(self.client.post("/api/mail-relay/test").status_code, 200)
            sender.assert_called_once()

    def test_mail_digest_continues_after_rejected_recipient(self):
        relay = {"enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587,
                 "security": "starttls", "username": "relay", "password": "secret",
                 "sender_name": "Autoweb", "sender_email": "alertas@example.com", "admin_email": ""}
        users = [{"name": "Primero", "email": "rechazado@example.com"},
                 {"name": "Segundo", "email": "aceptado@example.com"}]
        articles = [{"title": "Hallazgo", "url": "https://example.com/1", "keyword": "tema", "source": "Google"}]

        class FakeSMTP:
            def __init__(self, rejected=False): self.rejected = rejected
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def send_message(self, _message):
                if self.rejected: raise RuntimeError("recipient rejected")
                return {}

        with patch.object(autoweb, "rows", side_effect=[[relay], users]), \
             patch.object(autoweb, "smtp_connection", side_effect=[FakeSMTP(True), FakeSMTP(False)]):
            sent, error = autoweb.notify_new_articles(articles)
        self.assertEqual(sent, 1)
        self.assertIn("rechazado@example.com", error)

    def test_password_recovery_token_is_single_use(self):
        self.login()
        setting = {"enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 587,
                   "security": "starttls", "username": "relay", "password": "secret",
                   "sender_name": "Autoweb", "sender_email": "alertas@example.com", "admin_email": ""}
        self.client.put("/api/mail-relay", json=setting)

        class FakeSMTP:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def send_message(self, message): self.message = message; return {}

        token = "token-seguro-de-prueba"
        with patch.object(autoweb.secrets, "token_urlsafe", return_value=token), \
             patch.object(autoweb, "smtp_connection", return_value=FakeSMTP()):
            response = self.client.post("/api/password/forgot", json={"email": "admin@cotelink.cl"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/api/password/reset", json={"token": token, "password": "nueva123"}).status_code, 200)
        self.assertEqual(self.client.post("/api/password/reset", json={"token": token, "password": "otra123"}).status_code, 400)
        self.assertEqual(self.login(password="nueva123").status_code, 200)

    def test_scan_deduplicates_url_tracking_and_title(self):
        self.login()
        self.client.put("/api/browser-automation", json={"enabled": True})
        results = {
            "inteligencia artificial": [
                {"title": "Mismo artículo", "url": "https://example.com/noticia?utm_source=google", "source": "Google", "published_at": ""},
                {"title": "Mismo artículo", "url": "https://example.com/noticia?utm_source=bing", "source": "Bing", "published_at": ""},
            ]
        }
        with autoweb.db() as connection:
            connection.execute("UPDATE keywords SET active=FALSE")
            connection.execute("INSERT INTO keywords(phrase,active,created_at) VALUES('inteligencia artificial',TRUE,%s) ON CONFLICT(phrase) DO UPDATE SET active=TRUE", (datetime.now(timezone.utc),))
        with patch.object(autoweb, "browser_results", return_value=results), patch.object(autoweb, "notify_new_articles", return_value=(0, None)):
            self.assertTrue(autoweb.run_scan_guarded("automatic"))
        with autoweb.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM articles").fetchone()["count"], 1)
            run = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["run_source"], "automatic")
        self.assertEqual(run["new_count"], 1)

    def test_articles_clear_resets_indicators_and_audits_user(self):
        self.login()
        now = datetime.now(timezone.utc)
        with autoweb.db() as connection:
            connection.execute("INSERT INTO articles(url,title,source,keyword,published_at,found_at,is_new,canonical_url,title_key) VALUES('https://example.com/a','Artículo','Test','tema','',%s,TRUE,'https://example.com/a','articulo')", (now,))
            connection.execute("INSERT INTO runs(started_at,finished_at,status,total,new_count,message,event_type) VALUES(%s,%s,'success',1,1,'test','monitoring')", (now, now))
        response = self.client.delete("/api/articles")
        self.assertEqual(response.status_code, 200)
        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertEqual(dashboard["total"], 0)
        self.assertIsNone(dashboard["setting"]["last_run"])
        audit = self.client.get("/api/runs").get_json()["items"][0]
        self.assertEqual(audit["event_type"], "cleanup")
        self.assertIn("admin@cotelink.cl", audit["message"])

    def test_alerts_and_monitoring_log(self):
        self.login()
        with autoweb.db() as connection:
            connection.execute("INSERT INTO alerts(type,title,message,created_at) VALUES('news','Nueva','Detalle',%s)", (datetime.now(timezone.utc),))
        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertTrue(any(not item["read"] for item in dashboard["alerts"]))
        self.assertEqual(self.client.post("/api/alerts/read").status_code, 200)
        self.assertTrue(all(item["read"] for item in self.client.get("/api/dashboard").get_json()["alerts"]))

    def test_canonical_helpers(self):
        first = autoweb.canonical_article_url("HTTPS://Example.COM/news/?utm_source=x&id=2#section")
        second = autoweb.canonical_article_url("https://example.com/news?id=2")
        self.assertEqual(first, second)
        self.assertEqual(autoweb.article_title_key("Transformación   Digital"), "transformacion digital")


if __name__ == "__main__":
    unittest.main(verbosity=2)
