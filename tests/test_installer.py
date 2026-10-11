import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer_under_test", SOURCE / "installer.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class Runner:
    def __init__(self, active=False, enabled=False, fail=None):
        self.calls = []
        self.active = active
        self.enabled = enabled
        self.fail = fail

    def __call__(self, args, check=True):
        self.calls.append(args)
        if self.fail == args:
            self.fail = None
            if check:
                raise installer.InstallError("simulated systemd failure")
            return subprocess.CompletedProcess(args, 1, "simulated failure")
        if args[1] == "is-active":
            code = 0 if self.active else 3
        elif args[1] == "is-enabled":
            code = 0 if self.enabled else 1
        else:
            code = 0
            if "selectel-ddns.timer" in args:
                if args[1] == "stop":
                    self.active = False
                elif args[1] == "start":
                    self.active = True
                elif args[1] == "enable":
                    self.enabled = True
                    if "--now" in args:
                        self.active = True
                elif args[1] == "disable":
                    self.enabled = False
        if check and code:
            raise installer.InstallError("simulated command failure")
        return subprocess.CompletedProcess(args, code, "")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = installer.ddns.Config("123", "project", "service", 'secret"\\$value', "example.com.", "home.example.com.")
        self.config_path = self.root / "etc/selectel-ddns/config.json"
        self.script_path = self.root / "usr/local/lib/selectel-ddns/selectel-ddns.py"

    def seed(self):
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text('{"previous":true}')
        self.config_path.chmod(0o600)
        self.script_path.parent.mkdir(parents=True)
        self.script_path.write_text("old-script")
        self.script_path.chmod(0o755)

    def test_fresh_install_writes_private_json_and_starts_unit_before_timer(self):
        runner = Runner()
        backup = installer.install(self.config, root=self.root, runner=runner)
        self.assertIsNone(backup)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["password"], self.config.password)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.script_path.stat().st_mode & 0o777, 0o755)
        self.assertLess(runner.calls.index(["systemctl", "start", "selectel-ddns.service"]),
                        runner.calls.index(["systemctl", "enable", "--now", "selectel-ddns.timer"]))
        self.assertTrue(runner.active and runner.enabled)

    def test_upgrade_preserves_original_files_in_private_backup(self):
        self.seed()
        backup = installer.install(self.config, root=self.root, runner=Runner(True, True))
        self.assertEqual((backup / "config.json").read_text(), '{"previous":true}')
        self.assertEqual((backup / "selectel-ddns.py").read_text(), "old-script")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "config.json").stat().st_mode & 0o777, 0o600)

    def test_failed_service_restores_old_config_code_and_timer(self):
        self.seed()
        runner = Runner(True, True, fail=["systemctl", "start", "selectel-ddns.service"])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(installer.InstallError):
            installer.install(self.config, root=self.root, runner=runner)
        self.assertEqual(self.config_path.read_text(), '{"previous":true}')
        self.assertEqual(self.script_path.read_text(), "old-script")
        self.assertTrue(runner.active and runner.enabled)

    def test_failed_fresh_install_removes_partial_files_and_disables_timer(self):
        runner = Runner(fail=["systemctl", "enable", "--now", "selectel-ddns.timer"])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(installer.InstallError):
            installer.install(self.config, root=self.root, runner=runner)
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.script_path.exists())
        self.assertFalse(runner.active or runner.enabled)

    def test_atomic_write_failure_does_not_truncate_destination(self):
        path = self.root / "config.json"
        path.write_bytes(b"original")
        with mock.patch.object(installer.os, "replace", side_effect=OSError), self.assertRaises(OSError):
            installer.atomic_write(path, b"new", 0o600)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_symlink_destination_is_rejected_before_systemctl(self):
        self.config_path.parent.mkdir(parents=True)
        target = self.root / "target"
        target.write_bytes(b"unchanged")
        self.config_path.symlink_to(target)
        runner = Runner()
        with self.assertRaises(installer.InstallError):
            installer.install(self.config, root=self.root, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_secret_prompt_never_displays_existing_password(self):
        with mock.patch.object(installer.getpass, "getpass", return_value="") as prompt:
            self.assertEqual(installer.ask("Пароль", "private-existing", secret=True), "private-existing")
        self.assertNotIn("private-existing", prompt.call_args.args[0])

    def test_existing_configuration_can_be_reused_without_password_prompt(self):
        with mock.patch.object(installer, "yes_no", return_value=True), mock.patch.object(installer, "ask") as ask:
            self.assertIs(installer.collect_config(self.config), self.config)
        ask.assert_not_called()

    def test_record_selection_filters_unsafe_rrsets_and_reprompts(self):
        good = {"id": "497f6eca-6276-4993-bfeb-53cbbbba6f08", "name": "home.example.com.",
                "type": "A", "ttl": 60, "records": [{"content": "8.8.8.8", "disabled": False}]}
        multi = dict(good, name="multi.example.com.", records=good["records"] * 2)
        outside = dict(good, name="another.org.")
        with mock.patch.object(installer.ddns, "authenticate", return_value="token"), \
             mock.patch.object(installer.ddns, "find_zone", return_value="zone-id"), \
             mock.patch.object(installer.ddns, "listed", return_value=[multi, outside, good]), \
             mock.patch.object(installer, "ask", side_effect=["0", "100", "1"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            config = installer.choose_record(self.config, client=object())
        self.assertEqual(config.record, "home.example.com.")
        self.assertNotIn("multi.example.com", output.getvalue())
        self.assertNotIn("another.org", output.getvalue())


class BootstrapTest(unittest.TestCase):
    def dependencies(self, family):
        # Shell functions replace all package managers. No root or network needed.
        return subprocess.run(["bash", "-c", '''
source "$1"
apt-get() { printf 'APT:%s\\n' "$*"; }
dnf() { printf 'DNF:%s\\n' "$*"; }
install_dependencies "$2"
''', "test", str(SOURCE / "install.sh"), family], capture_output=True, text=True)

    def test_ubuntu_installs_required_packages(self):
        result = self.dependencies("ubuntu debian")
        self.assertEqual(result.returncode, 0)
        self.assertIn("APT:update", result.stdout)
        self.assertIn("install -y python3 ca-certificates dnsutils curl", result.stdout)

    def test_fedora_installs_required_packages(self):
        result = self.dependencies("fedora")
        self.assertEqual(result.returncode, 0)
        self.assertIn("DNF:install -y python3 ca-certificates bind-utils curl", result.stdout)

    def test_unknown_system_does_not_install_anything(self):
        result = self.dependencies("unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("APT:", result.stdout)
        self.assertNotIn("DNF:", result.stdout)

    def test_noninteractive_start_exits_before_mutations(self):
        result = subprocess.run(["bash", str(SOURCE / "install.sh")], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("терминал", result.stderr)


if __name__ == "__main__":
    unittest.main()
