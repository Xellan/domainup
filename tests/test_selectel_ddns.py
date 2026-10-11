"""Offline contract tests for the Selectel DNS v2 updater.

Run with: python3 -m unittest discover -s tests -v
"""

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse


SCRIPT = Path(__file__).resolve().parents[1] / "selectel-ddns.py"
SPEC = importlib.util.spec_from_file_location("selectel_ddns_under_test", SCRIPT)
ddns = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ddns
SPEC.loader.exec_module(ddns)

ZONE_ID = "4495020a-236f-46e0-9453-e3f9cc6476f4"
RRSET_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
ZONE = "example.com."
RECORD = "home.example.com."
OLD_IP = "1.1.1.1"
NEW_IP = "8.8.8.8"
PASSWORD = "very-secret-ddns-test-password"


def as_object(body):
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return json.loads(body) if isinstance(body, str) else body


class FakeClient:
    """Return Selectel-shaped responses while recording every HTTP request."""

    def __init__(self):
        self.calls = []
        self.ip_value = NEW_IP
        self.ip_status = 200
        self.auth_status = 201
        self.auth_headers = {"X-Subject-Token": "test-iam-token"}
        self.auth_payload = {"error": "authentication failed"}
        self.zone = {"id": ZONE_ID, "name": ZONE, "disabled": False}
        self.rrset = {
            "id": RRSET_ID,
            "zone_id": ZONE_ID,
            "name": RECORD,
            "type": "A",
            "ttl": 3600,
            "records": [{"content": OLD_IP, "disabled": False}],
            "comment": "Keep this comment",
            "managed_by": None,
        }
        self.rrset_pages = None
        self.patch_status = 204

    def request(self, method, url, body=None, token=None):
        self.calls.append((method, url, body, token))
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        if method == "GET" and url == "https://api.ipify.org":
            return self.ip_status, {}, self.ip_value

        if method == "POST" and url == "https://cloud.api.selcloud.ru/identity/v3/auth/tokens":
            return self.auth_status, self.auth_headers, self.auth_payload

        if parsed.netloc != "api.selectel.ru" or not parsed.path.startswith("/domains/v2/"):
            raise AssertionError("Unexpected HTTP request: {} {}".format(method, url))

        if method == "GET" and parsed.path == "/domains/v2/zones":
            if query.get("filter") != [ZONE]:
                raise AssertionError("Zone lookup must filter by FQDN")
            return 200, {}, {"count": 1, "next_offset": None, "result": [copy.deepcopy(self.zone)]}

        rrset_collection = "/domains/v2/zones/{}/rrset".format(ZONE_ID)
        if method == "GET" and parsed.path == rrset_collection:
            if query.get("name") != [RECORD] or query.get("rrset_types") != ["A"]:
                raise AssertionError("RRSet lookup must filter by exact name and A type")
            offset = int(query.get("offset", ["0"])[0])
            if self.rrset_pages is not None:
                return 200, {}, copy.deepcopy(self.rrset_pages[offset])
            return 200, {}, {"count": 1, "next_offset": None, "result": [copy.deepcopy(self.rrset)]}

        if method == "PATCH" and parsed.path == rrset_collection + "/" + RRSET_ID:
            return self.patch_status, {}, ""

        raise AssertionError("Unexpected HTTP request: {} {}".format(method, url))

    def calls_for(self, method):
        return [call for call in self.calls if call[0] == method]


class StubResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, count):
        return self.body[:count]


class HttpClientTest(unittest.TestCase):
    def test_json_request_sends_token_and_timeout(self):
        client = ddns.HttpClient(3)
        body = {"ttl": 60, "records": [{"content": NEW_IP, "disabled": False}]}
        response = StubResponse(204, b"")
        with mock.patch.object(client.opener, "open", return_value=response) as open_request:
            status, headers, payload = client.request(
                "PATCH", "https://api.selectel.ru/domains/v2/zones/test/rrset/test", body=body, token="iam-token"
            )
        self.assertEqual((status, headers, payload), (204, {}, None))
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(json.loads(request.data.decode("utf-8")), body)
        self.assertEqual(dict((key.lower(), value) for key, value in request.header_items())["x-auth-token"], "iam-token")
        self.assertEqual(open_request.call_args.kwargs["timeout"], 3)

    def test_bad_json_is_a_safe_error(self):
        client = ddns.HttpClient(3)
        with mock.patch.object(client.opener, "open", return_value=StubResponse(200, b"{not-json")):
            with self.assertRaises(ddns.DDNSError) as failure:
                client.request("GET", "https://api.selectel.ru/domains/v2/zones")
        self.assertNotIn("not-json", str(failure.exception))

    def test_http_error_does_not_expose_reason_or_response_body(self):
        client = ddns.HttpClient(3)
        error = HTTPError(
            "https://cloud.api.selcloud.ru/identity/v3/auth/tokens",
            401,
            "rejected " + PASSWORD,
            {},
            io.BytesIO(PASSWORD.encode("utf-8")),
        )
        with mock.patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(ddns.DDNSError) as failure:
                client.request("POST", "https://cloud.api.selcloud.ru/identity/v3/auth/tokens", body={"password": PASSWORD})
        self.assertNotIn(PASSWORD, str(failure.exception))
        self.assertIn("401", str(failure.exception))

    def test_transport_errors_are_safe_and_retryable(self):
        for error in (URLError(PASSWORD), TimeoutError(PASSWORD)):
            with self.subTest(error=type(error).__name__):
                client = ddns.HttpClient(3)
                with mock.patch.object(client.opener, "open", side_effect=error):
                    with self.assertRaises(ddns.DDNSError) as failure:
                        client.request("GET", "https://api.ipify.org")
                self.assertNotIn(PASSWORD, str(failure.exception))


class SelectelDDNSTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "account_id": "123456",
                    "project_name": "homelab",
                    "username": "ddns-service",
                    "password": PASSWORD,
                    "zone": ZONE[:-1],
                    "record": RECORD[:-1],
                }
            ),
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)

    def config(self):
        return ddns.Config.from_file(self.config_path)

    def test_config_uses_default_timeout_and_errors_have_distinct_type(self):
        config = self.config()
        self.assertEqual(config.timeout_seconds, 10)
        self.assertTrue(issubclass(ddns.ConfigError, ddns.DDNSError))

    def test_config_rejects_insecure_permissions_and_record_outside_zone(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(ddns.ConfigError):
            self.config()

        self.config_path.chmod(0o600)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["record"] = "home.another.example"
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ddns.ConfigError):
            self.config()

    def test_unchanged_ip_does_not_patch_even_when_ttl_differs(self):
        client = FakeClient()
        client.ip_value = OLD_IP
        result = ddns.run(self.config(), client=client)
        self.assertEqual(result, "unchanged")
        self.assertEqual(client.calls_for("PATCH"), [])
        self.assertEqual(len(client.calls_for("GET")), 3)

    def test_changed_ip_updates_only_the_exact_rrset(self):
        client = FakeClient()
        result = ddns.run(self.config(), client=client)
        self.assertEqual(result, "updated")
        self.assertEqual(len(client.calls_for("PATCH")), 1)
        _, url, body, token = client.calls_for("PATCH")[0]
        self.assertEqual(
            url,
            "https://api.selectel.ru/domains/v2/zones/{}/rrset/{}".format(ZONE_ID, RRSET_ID),
        )
        self.assertEqual(token, "test-iam-token")
        self.assertEqual(as_object(body), {"ttl": 60, "records": [{"content": NEW_IP, "disabled": False}]})

        auth_call = client.calls_for("POST")[0]
        auth_body = as_object(auth_call[2])
        self.assertEqual(auth_body["auth"]["identity"]["password"]["user"]["password"], PASSWORD)
        self.assertEqual(auth_body["auth"]["scope"]["project"]["name"], "homelab")
        for method, request_url, request_body, _ in client.calls:
            if method != "POST":
                self.assertNotIn(PASSWORD, request_url)
                self.assertNotIn(PASSWORD, json.dumps(request_body))

    def test_check_reports_change_without_patching(self):
        client = FakeClient()
        self.assertEqual(ddns.run(self.config(), check=True, client=client), "would-update")
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_rrset_lookup_follows_pagination_and_matches_name(self):
        client = FakeClient()
        other = copy.deepcopy(client.rrset)
        other["id"] = "f7e3a6cd-cea7-4243-9dd9-c2926809e364"
        other["name"] = "other.example.com."
        client.rrset_pages = {
            0: {"count": 2, "next_offset": 1, "result": [other]},
            1: {"count": 2, "next_offset": None, "result": [client.rrset]},
        }
        self.assertEqual(ddns.run(self.config(), client=client), "updated")
        self.assertEqual(len([c for c in client.calls_for("GET") if "/rrset?" in c[1]]), 2)
        self.assertTrue(client.calls_for("PATCH")[0][1].endswith("/" + RRSET_ID))

    def test_non_public_or_malformed_ip_never_reaches_patch(self):
        for value in ("10.0.0.12", "127.0.0.1", "192.0.2.3", "not-an-ip", "2001:4860:4860::8888"):
            with self.subTest(value=value):
                client = FakeClient()
                client.ip_value = value
                with self.assertRaises(ddns.DDNSError):
                    ddns.run(self.config(), client=client)
                self.assertEqual(client.calls_for("PATCH"), [])

    def test_multiple_a_values_including_disabled_are_ambiguous(self):
        client = FakeClient()
        client.rrset["records"].append({"content": "9.9.9.9", "disabled": True})
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_managed_rrset_and_disabled_zone_are_rejected(self):
        client = FakeClient()
        client.rrset["managed_by"] = "d66b59ce-4322-4910-8d10-1d389ae8f1ba"
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(client.calls_for("PATCH"), [])

        client = FakeClient()
        client.zone["disabled"] = True
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_auth_failure_or_missing_token_stops_before_dns(self):
        client = FakeClient()
        client.auth_status = 401
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(len(client.calls_for("GET")), 1)
        self.assertEqual(client.calls_for("PATCH"), [])

        client = FakeClient()
        client.auth_headers = {}
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(len(client.calls_for("GET")), 1)
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_lowercase_auth_response_header_is_accepted(self):
        client = FakeClient()
        client.auth_headers = {"x-subject-token": "test-iam-token"}
        self.assertEqual(ddns.run(self.config(), client=client), "updated")
        self.assertEqual(client.calls_for("PATCH")[0][3], "test-iam-token")

    def test_patch_error_statuses_are_not_reported_as_success(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                client = FakeClient()
                client.patch_status = status
                with self.assertRaises(ddns.DDNSError):
                    ddns.run(self.config(), client=client)
                self.assertEqual(len(client.calls_for("PATCH")), 1)

    def test_missing_rrset_or_disabled_a_value_never_patches(self):
        client = FakeClient()
        client.rrset_pages = {0: {"count": 0, "next_offset": None, "result": []}}
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(client.calls_for("PATCH"), [])

        client = FakeClient()
        client.rrset["records"][0]["disabled"] = True
        with self.assertRaises(ddns.DDNSError):
            ddns.run(self.config(), client=client)
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_main_hides_password_on_auth_error(self):
        client = FakeClient()
        client.auth_status = 401
        client.auth_payload = {"error": PASSWORD}
        with mock.patch.object(ddns, "HttpClient", return_value=client), self.assertLogs(ddns.LOG, level="ERROR") as captured:
            result = ddns.main(["--config", str(self.config_path)])
        self.assertEqual(result, 1)
        self.assertNotIn(PASSWORD, "\n".join(captured.output))

    def test_main_check_option_is_read_only(self):
        client = FakeClient()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(ddns, "HttpClient", return_value=client), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = ddns.main(["--config", str(self.config_path), "--check"])
        self.assertEqual(result, 0)
        self.assertEqual(client.calls_for("PATCH"), [])

    def test_main_returns_config_error_status_without_network(self):
        self.config_path.write_text("{invalid json", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(ddns, "HttpClient") as client_class, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = ddns.main(["--config", str(self.config_path)])
        self.assertEqual(result, 2)
        client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
