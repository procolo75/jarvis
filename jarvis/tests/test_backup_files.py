"""
tests/test_backup_files.py
──────────────────────────
Which files the Google Drive backup carries.

    cd jarvis && python3 -m unittest discover tests

What is pinned down: every file the add-on keeps in /data is either backed up or
listed here with the reason it is not, so a new file cannot silently stay out.
"""

import re
import sys
import unittest
from pathlib import Path

_ADDON_ROOT = Path(__file__).resolve().parent.parent / "jarvis"
sys.path.insert(0, str(_ADDON_ROOT))

from backup.gdrive import _BACKUP_FILES

NOT_BACKED_UP = {
    # Expire within an hour: a restored copy is always discarded on startup.
    "/data/storm_front_state.json",
    "/data/rain_front_state.json",
    "/data/hail_front_state.json",
    # Legacy: nothing creates it and nothing reads it at runtime.
    "/data/agents.json",
    # Only named to be deleted by the lightning migration in main.py.
    "/data/lightning_state.json",
    "/data/lightning_rec",
}

_DATA_PATH = re.compile(r"(?<![\w./:-])/data/[\w.-]*\w")


def _paths_in_source() -> set[str]:
    found: set[str] = set()
    for source in _ADDON_ROOT.rglob("*.py"):
        found.update(_DATA_PATH.findall(source.read_text(encoding="utf-8")))
    return found


class BackupFilesTest(unittest.TestCase):
    def test_every_data_file_is_backed_up_or_excluded_on_purpose(self):
        backed_up = {str(p) for p in _BACKUP_FILES}
        self.assertEqual(_paths_in_source() - backed_up - NOT_BACKED_UP, set())

    def test_excluded_files_are_not_uploaded(self):
        self.assertEqual({str(p) for p in _BACKUP_FILES} & NOT_BACKED_UP, set())

    def test_assist_token_is_backed_up(self):
        self.assertIn(Path("/data/assist_token"), _BACKUP_FILES)


if __name__ == "__main__":
    unittest.main()
