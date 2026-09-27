"""The flow document's revision check — the fix for last-writer-wins saves."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


def doc(*names):
    return {"flows": [{"id": n, "name": n, "enabled": True, "nodes": [], "edges": []}
                      for n in names]}


class TestRevision(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = flows.FlowStore(os.path.join(self.dir.name, "flows.json"))

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_store_reads_rev_zero(self):
        self.assertEqual(self.store.load(), {"flows": [], "rev": 0})

    def test_each_save_bumps_the_revision(self):
        self.assertEqual(self.store.save(doc("a"))["rev"], 1)
        self.assertEqual(self.store.save(doc("a", "b"))["rev"], 2)
        self.assertEqual(self.store.load()["rev"], 2)

    def test_matching_revision_is_accepted(self):
        self.store.save(doc("a"))
        saved = self.store.save(doc("a", "b"), expect_rev=1)
        self.assertEqual(saved["rev"], 2)
        self.assertEqual(len(self.store.load()["flows"]), 2)

    def test_stale_revision_is_refused_and_changes_nothing(self):
        self.store.save(doc("a"))                       # rev 1
        self.store.save(doc("a", "b"), expect_rev=1)    # rev 2, another writer
        with self.assertRaises(flows.RevConflict) as caught:
            self.store.save(doc("a", "mine"), expect_rev=1)   # the stale tab
        # The refusal carries what is on disk, so the writer can be shown it.
        self.assertEqual(caught.exception.current["rev"], 2)
        self.assertEqual([f["id"] for f in caught.exception.current["flows"]], ["a", "b"])
        # and disk is untouched
        on_disk = self.store.load()
        self.assertEqual(on_disk["rev"], 2)
        self.assertEqual([f["id"] for f in on_disk["flows"]], ["a", "b"])

    def test_no_revision_is_trusted(self):
        """A restore from backups/ posts a file with no rev — it must still work."""
        self.store.save(doc("a"))
        self.store.save(doc("a", "b"), expect_rev=1)
        saved = self.store.save(doc("restored"))
        self.assertEqual(saved["rev"], 3)
        self.assertEqual([f["id"] for f in self.store.load()["flows"]], ["restored"])

    def test_a_backup_file_carries_no_rev_of_its_own_into_disk(self):
        """A document written with a rev field of its own is renumbered, not trusted."""
        self.store.save(doc("a"))
        stale = doc("b")
        stale["rev"] = 99
        self.assertEqual(self.store.save(stale)["rev"], 2)

    def test_corrupt_file_reads_as_empty_rev_zero(self):
        with open(self.store.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.store.load(), {"flows": [], "rev": 0})

    def test_non_integer_rev_on_disk_is_tolerated(self):
        with open(self.store.path, "w") as fh:
            json.dump({"flows": [], "rev": "nonsense"}, fh)
        self.assertEqual(self.store.load()["rev"], 0)

    def test_file_stays_private(self):
        self.store.save(doc("a"))
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
