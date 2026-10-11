#!/usr/bin/env python3
"""Update one existing Selectel DNS actual A RRSet from the public WAN IPv4.

Requires Python 3.9+; uses only the standard library. Never creates DNS records.
"""

import argparse
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
from dataclasses import dataclass, field
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


IP_URL = "https://api.ipify.org"
AUTH_URL = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
DNS_URL = "https://api.selectel.ru/domains/v2"
TTL = 60
LOG = logging.getLogger("selectel-ddns")


class DDNSError(Exception):
    """An error safe to show in the journal (never include response bodies)."""


class ConfigError(DDNSError):
    """Invalid or insecure local configuration."""


def fqdn(value):
    """Normalize a hostname to an ASCII FQDN without accepting wildcards."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("zone и record должны содержать доменные имена.")
    try:
        name = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ConfigError("Не удалось преобразовать доменное имя в IDNA.") from None
    labels = name.split(".")
    if len(name) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ConfigError("zone и record должны быть корректными именами без wildcard.")
    return name + "."


@dataclass(frozen=True)
class Config:
    account_id: str
    project_name: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    zone: str
    record: str
    timeout_seconds: float = 10

    @classmethod
    def from_file(cls, path):
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ConfigError("Конфигурация должна быть обычным файлом.")
                if info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
                    raise ConfigError("У конфигурации должны быть права 0600/0400 и владелец root или текущий пользователь.")
                raw = handle.read(65537)
                if len(raw) > 65536:
                    raise ConfigError("Файл конфигурации слишком большой.")
                data = json.loads(raw)
        except (OSError, UnicodeError):
            raise ConfigError("Не удалось прочитать файл конфигурации.") from None
        except json.JSONDecodeError:
            raise ConfigError("В конфигурации некорректный JSON.") from None

        required = {"account_id", "project_name", "username", "password", "zone", "record"}
        if not isinstance(data, dict) or not required.issubset(data):
            raise ConfigError("Нужны поля account_id, project_name, username, password, zone, record.")
        if set(data) - required - {"timeout_seconds"}:
            raise ConfigError("В конфигурации есть неизвестные поля.")
        if any(not isinstance(data[key], str) or not data[key].strip() for key in required):
            raise ConfigError("Обязательные поля конфигурации должны быть непустыми строками.")

        zone, record = fqdn(data["zone"]), fqdn(data["record"])
        if record != zone and not record.endswith("." + zone):
            raise ConfigError("Обновляемая запись должна принадлежать указанной зоне.")
        timeout = data.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise ConfigError("timeout_seconds должен быть числом от 1 до 60.")
        return cls(
            account_id=data["account_id"].strip(),
            project_name=data["project_name"].strip(),
            username=data["username"].strip(),
            password=data["password"],
            zone=zone,
            record=record,
            timeout_seconds=timeout,
        )


class NoRedirect(HTTPRedirectHandler):
    # Never forward a password or X-Auth-Token to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpClient:
    def __init__(self, timeout):
        self.timeout = timeout
        # The IP probe must use the home connection, not an inherited HTTP proxy.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, url, body=None, token=None):
        headers = {"Accept": "application/json, text/plain", "User-Agent": "selectel-ddns/2.0"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        if token is not None:
            headers["X-Auth-Token"] = token
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status_code = response.status
                response_headers = dict(response.headers.items())
                raw = response.read(2097153)
                if len(raw) > 2097152:
                    raise DDNSError("Ответ API превышает допустимый размер.")
        except HTTPError as error:
            code = error.code
            error.close()
            raise DDNSError("HTTP %s при %s %s" % (code, method, url.split("?")[0])) from None
        except (URLError, OSError, HTTPException, ValueError):
            raise DDNSError("Ошибка сети при %s %s; повтор при следующем запуске." % (method, url.split("?")[0])) from None

        if not raw:
            return status_code, response_headers, None
        try:
            text = raw.decode("utf-8")
            data = text if url == IP_URL else json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            raise DDNSError("API вернул ответ в неожиданном формате.") from None
        return status_code, response_headers, data


def request(client, method, url, expected, body=None, token=None):
    status_code, headers, data = client.request(method, url, body=body, token=token)
    if status_code != expected:
        raise DDNSError("Неожиданный HTTP %s при %s %s" % (status_code, method, url.split("?")[0]))
    return headers, data


def listed(client, path, params, token):
    offset = 0
    for _ in range(100):
        query = dict(params, limit=100, offset=offset)
        _, data = request(client, "GET", DNS_URL + path + "?" + urlencode(query), 200, token=token)
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise DDNSError("API вернул некорректный список DNS-объектов.")
        for item in data["result"]:
            if not isinstance(item, dict):
                raise DDNSError("API вернул некорректный DNS-объект.")
            yield item
        next_offset = data.get("next_offset")
        if next_offset is None or next_offset == 0:
            return
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset <= offset:
            raise DDNSError("API вернул некорректную пагинацию.")
        offset = next_offset
    raise DDNSError("Превышен предел страниц ответа DNS API.")


def object_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DDNSError("API вернул некорректный идентификатор DNS-объекта.") from None


def matches_name(value, name):
    return isinstance(value, str) and value.lower().rstrip(".") + "." == name


def authenticate(config, client):
    auth = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {"user": {
                    "name": config.username,
                    "domain": {"name": config.account_id},
                    "password": config.password,
                }},
            },
            "scope": {"project": {
                "name": config.project_name,
                "domain": {"name": config.account_id},
            }},
        }
    }
    headers, _ = request(client, "POST", AUTH_URL, 201, body=auth)
    token = next((value for key, value in headers.items() if key.lower() == "x-subject-token"), None)
    if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise DDNSError("IAM не вернул корректный X-Subject-Token.")

    return token


def find_zone(config, client, token):
    zones = [zone for zone in listed(client, "/zones", {"filter": config.zone}, token)
             if matches_name(zone.get("name"), config.zone)]
    if len(zones) != 1:
        raise DDNSError("В проекте не найдена ровно одна зона с указанным именем.")
    zone = zones[0]
    if zone.get("disabled") is not False:
        raise DDNSError("DNS-зона отключена или API не подтвердил её активность.")
    zone_id = object_id(zone.get("id"))

    return zone_id


def validate_rrset(rrset):
    rrset_id = object_id(rrset.get("id"))
    if rrset.get("managed_by"):
        raise DDNSError("Группой A-записей управляет другой ресурс Selectel.")
    records = rrset.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise DDNSError("В группе A должна быть ровно одна запись; обновление отменено.")
    current = records[0]
    if not isinstance(current, dict) or current.get("disabled") is not False:
        raise DDNSError("A-запись отключена или её состояние неизвестно.")
    try:
        current_ip = str(ipaddress.IPv4Address(current.get("content")))
    except (ipaddress.AddressValueError, TypeError):
        raise DDNSError("В существующей A-записи некорректный IPv4.") from None
    ttl = rrset.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 604800:
        raise DDNSError("В группе A некорректный TTL.")

    return rrset_id, current_ip, ttl


def run(config, check=False, client=None):
    client = client or HttpClient(config.timeout_seconds)
    _, detected = request(client, "GET", IP_URL, 200)
    try:
        address = ipaddress.IPv4Address(detected.strip() if isinstance(detected, str) else "")
    except ipaddress.AddressValueError:
        raise DDNSError("Сервис определения IP не вернул корректный IPv4.") from None
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise DDNSError("Обнаруженный IPv4 не является публичным unicast-адресом.")
    public_ip = str(address)

    token = authenticate(config, client)
    zone_id = find_zone(config, client, token)

    sets = [item for item in listed(client, "/zones/" + zone_id + "/rrset",
                                    {"name": config.record, "rrset_types": "A"}, token)
            if matches_name(item.get("name"), config.record) and item.get("type") == "A"]
    if len(sets) != 1:
        raise DDNSError("Не найдена ровно одна существующая группа A-записей с указанным именем.")
    rrset = sets[0]
    rrset_id, current_ip, ttl = validate_rrset(rrset)

    LOG.info("%s: DNS=%s, внешний IPv4=%s, TTL=%s", config.record, current_ip, public_ip, ttl)
    if ttl != TTL:
        LOG.warning("Рекомендуемый TTL — 60 секунд. При неизменном IP установите его в панели Selectel.")
    if current_ip == public_ip:
        LOG.info("IP совпадает; обновление не требуется.")
        return "unchanged"
    if check:
        LOG.info("CHECK: требуется обновление %s → %s с TTL=60; DNS не изменён.", current_ip, public_ip)
        return "would-update"

    # PATCH replaces the entire records array. The guards above prevent loss of
    # additional addresses. Omit comment to preserve the existing comment.
    request(client, "PATCH", DNS_URL + "/zones/" + zone_id + "/rrset/" + rrset_id, 204,
            body={"ttl": TTL, "records": [{"content": public_ip, "disabled": False}]}, token=token)
    LOG.info("Обновлено: %s → %s, TTL=60.", config.record, public_ip)
    return "updated"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/selectel-ddns/config.json", help="путь к JSON-конфигурации")
    parser.add_argument("--check", action="store_true", help="проверить IP и DNS без изменения записей")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        run(Config.from_file(args.config), check=args.check)
        return 0
    except ConfigError as error:
        LOG.error("%s", error)
        return 2
    except DDNSError as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Avoid a traceback containing credentials or untrusted response data.
        LOG.error("Непредвиденная ошибка (%s); повтор при следующем запуске.", type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
