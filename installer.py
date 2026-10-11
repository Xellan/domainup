#!/usr/bin/env python3
"""Interactive installation; invoked by install.sh after dependency setup."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import getpass
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("selectel_ddns", SOURCE / "selectel-ddns.py")
ddns = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ddns
SPEC.loader.exec_module(ddns)


class InstallError(Exception):
    pass


def command(args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        # Commands contain paths/unit names only, never credentials.
        raise InstallError("Команда завершилась с ошибкой: " + " ".join(args) + "\n" + result.stdout.strip())
    return result


def yes_no(question, default=True):
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        value = input(question + suffix).strip().lower()
        if not value:
            return default
        if value in ("y", "yes", "д", "да"):
            return True
        if value in ("n", "no", "н", "нет"):
            return False
        print("Ответьте да или нет.")


def ask(question, default="", secret=False):
    while True:
        suffix = " [Enter — сохранить пароль]" if secret and default else (" [" + default + "]" if default else "")
        value = getpass.getpass(question + suffix + ": ") if secret else input(question + suffix + ": ").strip()
        value = value or default
        if value.strip():
            return value
        print("Значение не должно быть пустым.")


def choose_record(config, client=None):
    client = client or ddns.HttpClient(config.timeout_seconds)
    token = ddns.authenticate(config, client)
    zone_id = ddns.find_zone(config, client, token)
    choices = []
    for rrset in ddns.listed(client, "/zones/" + zone_id + "/rrset", {"rrset_types": "A"}, token):
        if rrset.get("type") != "A":
            continue
        try:
            name = ddns.fqdn(rrset.get("name"))
            if name != config.zone and not name.endswith("." + config.zone):
                continue
            _, address, _ = ddns.validate_rrset(rrset)
        except ddns.DDNSError:
            continue
        choices.append((name, address))
    choices.sort()
    if not choices:
        raise InstallError("В зоне нет подходящей A-группы. Создайте в Selectel одну активную A-запись с одним IPv4 и повторите установку.")
    print("\nДоступные A-записи:")
    for index, (name, address) in enumerate(choices, 1):
        print("  %d. %s → %s" % (index, name, address))
    default = next((str(i) for i, item in enumerate(choices, 1) if item[0] == config.record), "1")
    while True:
        value = ask("Номер записи для обновления", default)
        if value.isascii() and value.isdigit() and 1 <= int(value) <= len(choices):
            return replace(config, record=choices[int(value) - 1][0])
        print("Введите номер из списка.")


def collect_config(existing=None):
    if existing and yes_no("Найдены настройки для %s. Использовать их?" % existing.record):
        return existing
    print("\nНужен сервисный пользователь Selectel с ролью member в проекте DNS-зоны.")
    account = ask("Номер аккаунта Selectel", existing.account_id if existing else "")
    project = ask("Имя проекта", existing.project_name if existing else "")
    username = ask("Имя сервисного пользователя", existing.username if existing else "")
    password = ask("Пароль сервисного пользователя", existing.password if existing else "", secret=True)
    while True:
        try:
            zone = ddns.fqdn(ask("Доменная зона, например example.com", existing.zone if existing else ""))
            break
        except ddns.ConfigError as error:
            print(error)
    record = existing.record if existing and existing.zone == zone else zone
    config = ddns.Config(account, project, username, password, zone, record)
    return choose_record(config)


def atomic_write(path, data, mode):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(config, source=SOURCE, root=Path("/"), runner=command):
    """Install atomically; root/runner injection allows offline transaction tests."""
    root = Path(root)
    destinations = {
        "selectel-ddns.py": root / "usr/local/lib/selectel-ddns/selectel-ddns.py",
        "selectel-ddns.service": root / "etc/systemd/system/selectel-ddns.service",
        "selectel-ddns.timer": root / "etc/systemd/system/selectel-ddns.timer",
        "config.json": root / "etc/selectel-ddns/config.json",
    }
    for path in destinations.values():
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
            raise InstallError("Путь установки содержит символическую ссылку: " + str(path))
        if path.exists() and not path.is_file():
            raise InstallError("Путь установки занят другим типом файла: " + str(path))

    # Prepare everything before stopping the existing timer.
    payloads = {name: (source / name).read_bytes() for name in destinations if name != "config.json"}
    payloads["config.json"] = (json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n").encode()
    enabled = runner(["systemctl", "is-enabled", "--quiet", "selectel-ddns.timer"], check=False).returncode == 0
    active = runner(["systemctl", "is-active", "--quiet", "selectel-ddns.timer"], check=False).returncode == 0
    snapshots = {name: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
                 for name, path in destinations.items()}
    backup = None
    if any(value is not None for value in snapshots.values()):
        backup_root = root / "var/lib/selectel-ddns/backups"
        if backup_root.is_symlink() or any(p.is_symlink() for p in backup_root.parents if p != root):
            raise InstallError("Каталог резервных копий содержит символическую ссылку.")
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_root.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        backup = Path(tempfile.mkdtemp(prefix=stamp, dir=backup_root))
        for name, snapshot in snapshots.items():
            if snapshot is not None:
                atomic_write(backup / name, snapshot[0], 0o600)

    for path in destinations.values():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    destinations["config.json"].parent.chmod(0o700)
    try:
        # stop of a nonexistent unit is harmless on a fresh install.
        for unit in ("selectel-ddns.timer", "selectel-ddns.service"):
            result = runner(["systemctl", "stop", unit], check=False)
            if result.returncode and (destinations[unit].exists() or (unit.endswith("timer") and active)):
                raise InstallError("Не удалось остановить " + unit)
        for name, path in destinations.items():
            mode = 0o600 if name == "config.json" else (0o755 if name.endswith(".py") else 0o644)
            atomic_write(path, payloads[name], mode)
        runner(["systemctl", "daemon-reload"])
        # Test the real unit, including DynamicUser and LoadCredential.
        runner(["systemctl", "start", "selectel-ddns.service"])
        runner(["systemctl", "enable", "--now", "selectel-ddns.timer"])
        runner(["systemctl", "is-active", "--quiet", "selectel-ddns.timer"])
        runner(["systemctl", "is-enabled", "--quiet", "selectel-ddns.timer"])
    except (Exception, KeyboardInterrupt) as error:
        print("Установка не завершена. Восстанавливаю предыдущие файлы и состояние таймера…")
        try:
            runner(["systemctl", "stop", "selectel-ddns.timer", "selectel-ddns.service"], check=False)
            runner(["systemctl", "disable", "selectel-ddns.timer"], check=False)
            for name, path in destinations.items():
                snapshot = snapshots[name]
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, snapshot[0], snapshot[1])
            runner(["systemctl", "daemon-reload"])
            if enabled:
                runner(["systemctl", "enable", "selectel-ddns.timer"])
            if active:
                runner(["systemctl", "start", "selectel-ddns.timer"])
        except Exception:
            raise InstallError("Автоматический откат не завершён. Проверьте systemctl и резервную копию: " + str(backup)) from None
        raise InstallError("Файлы и таймер восстановлены. DNS мог уже обновиться; проверьте запись. Причина: " + str(error)) from None
    return backup


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if os.geteuid() != 0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Запустите sudo bash install.sh в интерактивном терминале.", file=sys.stderr)
        return 2
    # Lock is held throughout prompting and installation; concurrent runs abort.
    lock = open("/run/selectel-ddns-install.lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Другой установщик уже запущен.", file=sys.stderr)
        lock.close()
        return 1
    try:
        print("Selectel DDNS 2.0 — интерактивная установка\n")
        path = Path("/etc/selectel-ddns/config.json")
        existing = None
        if path.exists():
            try:
                existing = ddns.Config.from_file(path)
            except ddns.ConfigError as error:
                print("Существующая конфигурация не прошла проверку:", error)
                if not yes_no("Создать новую конфигурацию? Предыдущий файл будет сохранён в резервной копии.", default=False):
                    return 1
        while True:
            try:
                config = collect_config(existing)
                print("\nПроверяю доступ к API, внешний IPv4 и выбранную A-запись…")
                ddns.run(config, check=True)
                break
            except (ddns.DDNSError, InstallError) as error:
                print("Проверка не прошла:", error)
                if not yes_no("Ввести настройки заново?"):
                    return 1
                existing = None
        print("\nЗона: %s\nA-запись: %s\nПроверка: каждые 2 минуты, с автозапуском после перезагрузки." % (config.zone, config.record))
        print("Первый запуск службы может обновить IP в DNS. Пароль сохранится локально с правами 0600.")
        if not yes_no("Установить и запустить?"):
            print("Установка службы отменена. Установленные зависимости сохранены.")
            return 0
        backup = install(config)
        print("\nГотово: служба проверена, таймер включён.")
        if backup:
            print("Предыдущие файлы:", backup)
        print("Журнал: journalctl -u selectel-ddns.service -n 20 --no-pager")
        print("Таймер: systemctl list-timers --all selectel-ddns.timer")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nУстановка отменена.")
        return 130
    except (ddns.DDNSError, InstallError) as error:
        print("Ошибка:", error, file=sys.stderr)
        return 1
    except Exception as error:
        print("Ошибка установки (%s). Подробности состояния службы: journalctl -u selectel-ddns.service" % type(error).__name__, file=sys.stderr)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
