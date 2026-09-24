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


class ExactPhraseMatchingTestCase(unittest.TestCase):
    def test_multiword_phrase_matches_case_accents_and_whitespace(self):
        keyword="Cámara de Compensación de Bajo Valor"
        positives=[
            "La CÁMARA de compensación de bajo valor inició sus operaciones.",
            "Camara   de\tCompensacion\nde Bajo Valor",
            "Cámara de Compensación de Bajo Valor",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(autoweb.exact_phrase_in_text(keyword,text))

    def test_multiword_phrase_rejects_invalid_matches(self):
        keyword="Cámara de Compensación de Bajo Valor"
        negatives=[
            "Cámara digital de Compensación de Bajo Valor",
            "Compensación de Cámara de Bajo Valor",
            "La Cámara informó sobre la Compensación de Bajo Valor",
            "Cámara de Compensación de Bajo Valorado",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(autoweb.exact_phrase_in_text(keyword,text))
        split_article={"title":"Cámara de Compensación", "summary":"de Bajo Valor"}
        self.assertFalse(autoweb.article_matches_keyword(split_article,keyword))

    def test_single_word_keeps_existing_behavior(self):
        self.assertTrue(autoweb.article_matches_keyword({"title":"Texto sin relación"},"economía"))
        self.assertEqual(autoweb.keyword_search_query("economía"),"economía")

    def test_report_fragment_uses_excerpt_and_has_title_fallback(self):
        self.assertEqual(autoweb.report_fragment({"title":"Noticia", "summary":"Primer fragmento."}),"Primer fragmento.")
        long_excerpt="contenido relacionado "*200
        expanded=autoweb.report_fragment({"title":"Noticia", "summary":long_excerpt})
        self.assertGreater(len(expanded),700)
        self.assertLessEqual(len(expanded),2501)
        fallback=autoweb.report_fragment({"title":"Noticia sin resumen", "summary":""})
        self.assertIn("Noticia sin resumen",fallback)
        self.assertNotIn("no entregó un resumen",fallback)

    def test_search_result_summary_removes_html(self):
        value='<a href="x">Titular</a>  Texto&nbsp; del resultado'
        self.assertEqual(autoweb.search_result_summary(value),"Titular Texto del resultado")


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

    def test_user_movements_are_audited_and_admin_can_list_them(self):
        self.assertEqual(self.login().status_code,200)
        events=self.client.get("/api/audit-events").get_json()["items"]
        login_event=next(item for item in events if item["action"]=="Inicio de sesión")
        self.assertEqual(login_event["user_email"],"admin@cotelink.cl")
        self.assertEqual(login_event["status_code"],200)

    def test_keywords_crud_and_duplicate_validation(self):
        self.login()
        created = self.client.post("/api/keywords", json={"phrase": "energía solar"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.post("/api/keywords", json={"phrase": "energía solar"}).status_code, 409)
        item = next(x for x in self.client.get("/api/keywords").get_json()["items"] if x["phrase"] == "energía solar")
        self.assertEqual(item["historical_count"], 0)
        self.assertEqual(item["new_count"], 0)
        self.assertIsNone(item["last_detected_at"])
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

    def test_administration_permission_grants_full_administration_access(self):
        self.login()
        role = {
            "name": "Gestor de usuarios",
            "description": "Administra usuarios y perfiles",
            "permissions": {"dashboard": True, "administration": True},
        }
        self.assertEqual(self.client.post("/api/roles", json=role).status_code, 201)
        user = {
            "name": "Gestora Prueba",
            "email": "gestora@example.com",
            "password": "secreto1",
            "role": "Gestor de usuarios",
        }
        self.assertEqual(self.client.post("/api/users", json=user).status_code, 201)
        self.client.post("/api/logout")
        self.assertEqual(self.login("gestora@example.com", "secreto1").status_code, 200)

        users_response = self.client.get("/api/users")
        self.assertEqual(users_response.status_code, 200)
        self.assertIn("admin@cotelink.cl", [item["email"] for item in users_response.get_json()["items"]])
        self.assertEqual(self.client.get("/api/roles").status_code, 200)
        self.assertEqual(self.client.get("/api/audit-events").status_code, 200)

    def test_ai_api_permission_can_be_assigned_to_role(self):
        self.login()
        role = {"name": "Integrador IA", "description": "Configura proveedores", "permissions": {"dashboard": True, "apis": True}}
        self.assertEqual(self.client.post("/api/roles", json=role).status_code, 201)
        saved_role = next(item for item in self.client.get("/api/roles").get_json()["items"] if item["name"] == "Integrador IA")
        self.assertTrue(saved_role["permissions"]["apis"])
        user = {"name": "Usuario IA", "email": "ia@example.com", "password": "secreto1", "role": "Integrador IA"}
        self.assertEqual(self.client.post("/api/users", json=user).status_code, 201)
        self.client.post("/api/logout")
        login = self.login("ia@example.com", "secreto1")
        self.assertTrue(login.get_json()["user"]["permissions"]["apis"])
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.put("/api/settings", json={"provider": "OpenAI", "model": "gpt-4.1-mini", "active": False}).status_code, 200)
        self.assertEqual(self.client.put("/api/settings", json={"frequency": "daily", "hour": "10:00"}).status_code, 403)
        self.assertEqual(self.client.get("/api/roles").status_code, 403)

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
        self.assertEqual(self.client.put("/api/browser-automation", json={"enabled": True, "publication_date_filter": True, "publication_max_age_days": 14}).status_code, 200)
        engine = {"name": "DuckDuckGo", "browser": "chrome", "search_url": "https://duckduckgo.com/?q={query}", "active": True}
        self.assertEqual(self.client.post("/api/browser-engines", json=engine).status_code, 201)
        config = self.client.get("/api/browser-automation").get_json()
        self.assertTrue(config["enabled"])
        self.assertTrue(config["publication_date_filter"])
        self.assertEqual(config["publication_max_age_days"], 14)
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
        self.assertEqual(list(digest.iter_attachments()), [])
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

    def test_old_article_is_stored_without_alert_when_date_filter_is_enabled(self):
        self.login()
        self.client.put("/api/browser-automation", json={"enabled": True, "publication_date_filter": True, "publication_max_age_days": 30})
        results={"inteligencia artificial":[{"title":"La ruta de alta velocidad del Shinkansen de Leo Soto","url":"https://www.df.cl/df-mas/punto-de-partida/la-ruta-de-alta-velocidad-del-shinkansen-de-leo-soto","source":"Google","published_at":"2024-07-06T04:00:00-04:00"}]}
        with autoweb.db() as connection:
            connection.execute("UPDATE keywords SET active=FALSE")
            connection.execute("INSERT INTO keywords(phrase,active,created_at) VALUES('inteligencia artificial',TRUE,%s) ON CONFLICT(phrase) DO UPDATE SET active=TRUE",(datetime.now(timezone.utc),))
        with patch.object(autoweb,"browser_results",return_value=results), patch.object(autoweb,"notify_new_articles",return_value=(0,None)) as notifier:
            self.assertTrue(autoweb.run_scan_guarded("automatic"))
        with autoweb.db() as connection:
            article=connection.execute("SELECT * FROM articles WHERE url=%s",(results['inteligencia artificial'][0]['url'],)).fetchone()
            run=connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(article["discovered_at"])
        self.assertFalse(article["is_new"])
        self.assertEqual(run["new_count"],0)
        notifier.assert_called_once_with([])

    def test_publication_metadata_precedence_and_visible_spanish_date(self):
        discovered=datetime(2026,9,9,13,0,tzinfo=timezone.utc)
        metadata={"datePublished":"2024-07-06T04:00:00-04:00","article:published_time":"2025-01-01T00:00:00Z","time":"2026-01-01","visible":"Publicado: Sábado 6 de julio de 2024 a las 04:00 hrs."}
        selected,source=autoweb.select_publication_date(metadata,discovered)
        self.assertEqual(source,"datePublished")
        self.assertEqual(selected,datetime(2024,7,6,8,0,tzinfo=timezone.utc))
        visible,source=autoweb.select_publication_date({"visible":metadata["visible"]},discovered)
        self.assertEqual(source,"visible")
        self.assertEqual(visible,datetime(2024,7,6,4,0,tzinfo=timezone.utc))

    def test_pdf_report_uses_only_articles_received_for_that_email(self):
        included={"title":"Incluida","summary":"Resumen incluido.","image_url":"","url":"https://example.com/incluida","source":"Fuente","keyword":"tema"}
        with patch.object(autoweb,"fetch_report_image",return_value=None):
            pdf=autoweb.build_news_report_pdf([included],datetime(2026,9,9,13,0,tzinfo=timezone.utc))
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf),1000)

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

    def test_outbound_url_validation_blocks_ssrf_destinations(self):
        allowed = frozenset({"api.example.com"})
        public_address = [(None, None, None, None, ("93.184.216.34", 443))]
        with patch.object(autoweb.socket, "getaddrinfo", return_value=public_address):
            self.assertEqual(
                autoweb.validate_outbound_url("https://api.example.com/news?q=solar#fragment", allowed),
                "https://api.example.com/news?q=solar",
            )
        for url in (
            "http://api.example.com/news",
            "https://user:secret@api.example.com/news",
            "https://api.example.com:8443/news",
            "https://attacker.example/news",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                autoweb.validate_outbound_url(url, allowed, resolve=False)
        local_address = [(None, None, None, None, ("127.0.0.1", 443))]
        with patch.object(autoweb.socket, "getaddrinfo", return_value=local_address):
            with self.assertRaises(ValueError):
                autoweb.validate_outbound_url("https://api.example.com/news", allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
