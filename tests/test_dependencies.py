import re
import tomllib
import unittest
from pathlib import Path


class DependencyTests(unittest.TestCase):
    def test_docker_android_export_matches_script_lock(self):
        root = Path(__file__).resolve().parent.parent
        lock = tomllib.loads((root / "myWhoosh2Garmin.py.lock").read_text())
        exported = (root / "android/requirements.txt").read_text()
        pins = dict(re.findall(r"^([a-z0-9-]+)==([^\s]+)", exported, re.MULTILINE))
        self.assertEqual(pins, {package["name"]: package["version"] for package in lock["package"]})
        for package in lock["package"]:
            with self.subTest(package=package["name"]):
                section = re.search(rf"^{package['name']}==.*?(?=^[a-z0-9-]+==|\Z)",
                                    exported, re.MULTILINE | re.DOTALL)[0]
                hashes = set(re.findall(r"--hash=(sha256:[a-f0-9]+)", section))
                artifacts = [package["sdist"], *package["wheels"]]
                self.assertEqual(hashes, {artifact["hash"] for artifact in artifacts})
