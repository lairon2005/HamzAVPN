import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_subscription_redirect_preserves_token_and_query(self):
        template = (ROOT / "etc/nginx/templates/default.conf.template").read_text()
        start = template.index("location ~ ^/sub/(.*)$")
        end = template.index("\n    }", start)
        location = template[start:end]
        self.assertIn("return 302 https://$subscription_host/$1$is_args$args;", location)
        self.assertIn('add_header Cache-Control "no-store" always;', location)
        self.assertNotIn("proxy_pass", location)

    def test_standard_https_and_both_webhooks_share_the_bot_port(self):
        template = (ROOT / "etc/nginx/templates/default.conf.template").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('"443:443"', compose)
        self.assertIn("return 301 https://$host$request_uri;", template)
        self.assertIn("location = /platega", template)
        self.assertIn("location = $LOCATION", template)
        self.assertEqual(template.count("proxy_pass http://vpn_bot:8081;"), 2)
        self.assertNotIn("listen 127.0.0.1:10000", template)
        self.assertIn("REMNAWAVE_SUB_HOST: ${REMNAWAVE_SUB_HOST:-}", compose)

    def test_refresh_waits_for_database_and_checks_nginx(self):
        refresh = (ROOT / "refresh.sh").read_text()
        self.assertLess(refresh.index("--wait postgres"), refresh.index("python3 fix_db.py"))
        self.assertLess(refresh.index("nginx nginx -t"), refresh.index("--force-recreate nginx"))
        self.assertNotIn("docker compose down", refresh)
        self.assertNotIn("docker stop", refresh)


if __name__ == "__main__":
    unittest.main()
