#!/usr/bin/env python3
"""Tests for tools/check.py. Run from the repo: python3 -m unittest discover -s tests

They need the headunit/ folder of the NBTApps repo, for its nbtpkg.py: where NBTAPPS_HEADUNIT says, or
../nbtapps-mono/headunit. Every package is a throwaway one in a throwaway repo, and nothing is fetched: the
archive's address and the source's commit are answered here.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import check  # noqa: E402

HEADUNIT = os.environ.get("NBTAPPS_HEADUNIT") or os.path.join(REPO, "..", "nbtapps-mono", "headunit")
if not os.path.isfile(os.path.join(HEADUNIT, "tools", "nbtpkg.py")):
    raise unittest.SkipTest("the NBTApps repo's headunit/ is not at %s: set NBTAPPS_HEADUNIT" % HEADUNIT)
check.load_nbtpkg(HEADUNIT)
nbtpkg = check.nbtpkg

AUTHOR, OTHER = bytes([7]) * 32, bytes([8]) * 32
COMMIT = "c0ffee" + "0" * 34


class CheckTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = work.name
        self.root = os.path.join(self.work, "repo")
        os.makedirs(self.root)
        self.served = {}            # address -> bytes
        self.commits = {("https://example.invalid/src", COMMIT)}
        for name, value in (("fetch", self.fetch), ("commit_exists", lambda repo, c: (repo, c) in self.commits),
                            ("official_ids", lambda: {"org.example.official"})):
            patch = mock.patch.object(check, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def fetch(self, url, limit):
        if url not in self.served:
            raise ValueError("it could not be fetched: Not Found")
        return self.served[url]

    def write(self, relative, value):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(value if isinstance(value, bytes) else
                    value.encode() if isinstance(value, str) else (json.dumps(value, indent=2) + "\n").encode())

    def archive(self, pkg_id, version, seed=AUTHOR, text="hello", logs=None):
        manifest = {"manifestVersion": 1, "id": pkg_id, "name": "Demo", "version": version,
                    "requires": {"agent": ">=1.2.0"},
                    "install": {"files": [{"src": "payload/", "dest": "${app}/", "mode": "0644"}]}}
        if logs is not None:
            manifest["logs"] = [{"path": path} for path in logs]
        with tempfile.TemporaryDirectory() as src:
            os.makedirs(os.path.join(src, "payload"))
            with open(os.path.join(src, "payload", "hello.txt"), "w") as f:
                f.write(text)
            with open(os.path.join(src, "manifest.json"), "w") as f:
                json.dump(manifest, f)
            return nbtpkg.build_package(src, seed)[1]

    def add(self, pkg_id, version, data=None, serve=True, **record):
        """A version as a pull request adds it, with the listing and author.pub of a new package, and the archive
        served at its address."""
        data = data if data is not None else self.archive(pkg_id, version)
        folder = "packages/%s" % pkg_id
        if not os.path.exists(os.path.join(self.root, folder)):
            self.write(folder + "/listing.json", {"summary": "A demo", "author": "Someone", "category": "utilities",
                                                  "description": "Does nothing."})
            self.write(folder + "/author.pub", "# someone\n%s\n" % nbtpkg.public_key(AUTHOR).hex())
        url = "https://example.invalid/dl/%s-%s.nbtpkg" % (pkg_id, version)
        value = {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "released": "2026-11-13",
                 "source": {"repo": "https://example.invalid/src", "commit": COMMIT}}
        value.update(record)
        self.write("%s/versions/%s.json" % (folder, version), {k: v for k, v in value.items() if v is not None})
        if serve:
            self.served[url] = data
        return data

    def check(self, base=None, offline=False):
        return check.check(self.root, base, offline, check.Report(io.StringIO()))

    def errors(self, **options):
        return ["%s: %s" % e for e in self.check(**options).errors]

    def git(self, *args):
        return subprocess.run(("git", "-C", self.root, "-c", "user.name=T", "-c", "user.email=t@example.invalid")
                              + args, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        return self.git("rev-parse", "HEAD")

    # ----

    def test_a_good_package_passes(self):
        self.add("com.example.app", "1.0.0", notes=["First"])
        self.write("packages/com.example.app/icon.png", check.PNG_MAGIC + b"rest")
        self.assertEqual([], self.errors())

    def test_the_archive_is_fetched_and_must_be_the_pinned_one_signed_by_author_pub(self):
        self.add("com.example.app", "1.0.0", serve=False)
        self.assertEqual(["packages/com.example.app/versions/1.0.0.json: the archive at \"url\": it could not be "
                          "fetched: Not Found"], self.errors())

        self.add("com.example.app", "1.0.0", sha256="ab" * 32)
        self.assertIn("is not the one \"sha256\" pins", self.errors()[0])

        self.add("com.example.app", "1.0.0", data=self.archive("com.example.app", "1.0.0", seed=OTHER))
        self.assertIn("nbtpkg.py verify --key author.pub refuses the archive (bad-signature)", self.errors()[0])

        self.add("com.example.app", "1.0.0", data=self.archive("com.example.other", "1.0.0"))
        self.assertIn("the archive is com.example.other 1.0.0", self.errors()[0])

    def test_the_logs_a_package_names_are_checked_with_its_manifest(self):
        logs = ["${data}/log/*.log", "${tmp}/crash.txt", "/tmp/demo.log"]
        self.add("com.example.app", "1.0.0", data=self.archive("com.example.app", "1.0.0", logs=logs))
        self.assertEqual([], self.errors())
        # The agent's own folder holds the pairing keys: no package names a log there. nbtpkg.py builds no such
        # package, so its manifest is put into the archive here, signed by its author.
        entries = nbtpkg.read_zip(self.archive("com.example.app", "1.0.0"))
        manifest = json.loads(entries[0]["data"])
        manifest["logs"] = [{"path": "/var/opt/nbtapp/phones.json"}]
        text = json.dumps(manifest).encode()
        data = nbtpkg.write_zip([nbtpkg.make_member(nbtpkg.MANIFEST, text),
                                 nbtpkg.make_member(nbtpkg.SIGNATURE, nbtpkg.sign(AUTHOR, text), store=True)]
                                + [nbtpkg.make_member(e["name"], e["data"]) for e in entries[2:] if not e["dir"]])
        self.add("com.example.app", "1.0.0", data=data)
        self.assertIn("nbtpkg.py verify --key author.pub refuses the archive (bad-manifest)", self.errors()[0])

    def test_the_source_commit_must_be_there(self):
        self.add("com.example.app", "1.0.0", source={"repo": "https://example.invalid/src", "commit": "1" * 40})
        self.assertEqual(["packages/com.example.app/versions/1.0.0.json: \"source\": %s is not a commit of "
                          "https://example.invalid/src that can be fetched" % ("1" * 40)], self.errors())

    def test_ids_that_are_not_a_community_packages_are_refused(self):
        self.add("org.nbtapp.thing", "1.0.0")
        self.add("org.example.official", "1.0.0")
        self.write("packages/Not_An_Id/listing.json", {})
        errors = self.errors()
        self.assertIn("packages/org.nbtapp.thing: ids under org.nbtapp. are the project's own", errors)
        self.assertIn("packages/org.example.official: the official catalogue already lists org.example.official",
                      errors)
        self.assertIn("packages/Not_An_Id: the folder's name is not a package id: two or more labels of a-z and "
                      "0-9, joined by dots", errors)

    def test_faults_in_the_files_are_all_reported(self):
        self.add("com.example.app", "1.0.0", url="https://example.invalid/dl/other.nbtpkg", notes=[""])
        self.add("com.example.app", "1.1.0", source=None, extra=1)
        self.add("com.example.app", "1.2.0", released="2026-02-30")
        self.write("packages/com.example.app/versions/2.0.json", {})
        self.write("packages/com.example.app/author.pub", "not a key\n")
        self.write("packages/com.example.app/listing.json", {"summary": "x" * 81, "author": "a", "category": "A",
                                                             "description": "d"})
        self.write("packages/com.example.app/icon.png", b"GIF89a")
        self.write("packages/com.example.app/notes.txt", "x")
        self.write("packages/com.example.empty/listing.json", {})
        errors = self.errors(offline=True)
        for expected in (
            "packages/com.example.app/notes.txt: is not a file a package has here",
            "packages/com.example.app/listing.json: .summary has a length that is not allowed",
            "packages/com.example.app/author.pub: is missing, or is not a key file as nbtpkg.py keygen writes one",
            "packages/com.example.app/icon.png: is not a PNG file",
            "packages/com.example.app/versions/1.0.0.json: .notes[0] has a length that is not allowed",
            "packages/com.example.app/versions/1.1.0.json: has no 'source'",
            "packages/com.example.app/versions/1.2.0.json: \"released\" is not a date",
            "packages/com.example.app/versions/2.0.json: the file's name is not <version>.json, where the version "
            "is the manifest's",
            "packages/com.example.empty: has no version",
        ):
            self.assertIn(expected, errors)

    def test_a_url_ends_in_the_archives_own_name(self):
        self.add("com.example.app", "1.0.0", url="https://example.invalid/dl/com.example.app-1.0.1.nbtpkg")
        self.assertEqual(["packages/com.example.app/versions/1.0.0.json: \"url\" does not end in "
                          "com.example.app-1.0.0.nbtpkg"], self.errors())

    def test_against_the_base_only_new_records_are_fetched_and_published_ones_never_change(self):
        self.git("init", "-q", "-b", "main")
        self.add("com.example.app", "1.0.0")
        base = self.commit()

        del self.served["https://example.invalid/dl/com.example.app-1.0.0.nbtpkg"]   # not fetched again
        self.add("com.example.app", "1.1.0")
        self.commit()
        self.assertEqual([], self.errors(base=base))

        self.add("com.example.app", "1.0.0", released="2026-11-14", serve=False)
        self.commit()
        self.assertEqual(["packages/com.example.app/versions/1.0.0.json: is a version that was already here, and "
                          "a published version never changes: a changed archive is a new version"],
                         self.errors(base=base))

    def test_a_pre_release_has_its_channel_and_a_version_with_a_pre_release_part_is_one(self):
        self.add("com.example.app", "1.0.0", channel="prerelease")
        self.add("com.example.app", "1.1.0-beta.1", channel="prerelease")
        self.assertEqual([], self.errors())

        self.add("com.example.app", "1.1.0-beta.2")
        self.add("com.example.app", "1.2.0", channel="beta")
        errors = self.errors(offline=True)
        self.assertIn("packages/com.example.app/versions/1.1.0-beta.2.json: 1.1.0-beta.2 has a pre-release part, so "
                      "it is a pre-release, and its record has \"channel\": \"prerelease\"", errors)
        self.assertIn("packages/com.example.app/versions/1.2.0.json: .channel must be one of: prerelease", errors)

    def test_a_pre_release_is_promoted_by_taking_its_channel_out_and_nothing_else(self):
        self.git("init", "-q", "-b", "main")
        self.add("com.example.app", "1.0.0")
        self.add("com.example.app", "1.1.0", channel="prerelease", notes=["Trying"])
        self.add("com.example.app", "1.2.0-rc.1", channel="prerelease")
        base = self.commit()
        self.served.clear()                                         # nothing is fetched again

        self.add("com.example.app", "1.1.0", notes=["Trying"])
        self.commit()
        report = self.check(base=base)
        self.assertEqual([], report.errors)
        self.assertEqual([("packages/com.example.app/versions/1.1.0.json",
                           "promotes this pre-release to stable, with the archive that is here already")], report.notices)

        # Anything else changed with it is a change, and so is a version with a pre-release part made stable.
        self.add("com.example.app", "1.1.0", notes=["Done"])
        self.add("com.example.app", "1.2.0-rc.1")
        self.commit()
        self.assertEqual(["packages/com.example.app/versions/1.1.0.json: is a version that was already here, and a "
                          "published version never changes: a changed archive is a new version",
                          "packages/com.example.app/versions/1.2.0-rc.1.json: 1.2.0-rc.1 has a pre-release part, so it "
                          "is a pre-release, and its record has \"channel\": \"prerelease\""],
                         sorted(self.errors(base=base)))

    def test_a_stable_version_is_never_made_a_pre_release(self):
        self.git("init", "-q", "-b", "main")
        self.add("com.example.app", "1.0.0")
        base = self.commit()
        self.add("com.example.app", "1.0.0", channel="prerelease")
        self.commit()
        self.assertEqual(["packages/com.example.app/versions/1.0.0.json: is a stable version that was already here, "
                          "and a stable version is never made a pre-release: take it out instead"],
                         self.errors(base=base))

    def test_what_the_reviewer_should_see_is_named_and_does_not_fail(self):
        self.git("init", "-q", "-b", "main")
        self.add("com.example.app", "1.0.0")
        base = self.commit()
        os.remove(os.path.join(self.root, "packages/com.example.app/versions/1.0.0.json"))
        self.add("com.example.app", "1.1.0")
        self.write("packages/com.example.app/author.pub", nbtpkg.public_key(AUTHOR).hex() + "\n")
        self.write("tools/other.py", "")
        self.commit()
        report = self.check(base=base)
        self.assertEqual([], report.errors)
        self.assertEqual([
            ("packages/com.example.app/author.pub",
             "changes: a new author.pub is a change of owner, and the pull request says why"),
            ("packages/com.example.app/versions/1.0.0.json", "takes this version out"),
            ("tools/other.py", "is not a package's file: this pull request changes the repo itself"),
        ], sorted(report.notices))

    def test_text_from_a_pull_request_cannot_start_a_line_of_its_own(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            report = check.Report(out)
        report.error("packages/a,b:c", "bad\n::add-mask::x\r%")
        self.assertEqual("::error file=packages/a%2Cb%3Ac::bad\\x0a::add-mask::x\\x0d%25\n", out.getvalue())


class RepoTest(unittest.TestCase):
    def test_this_repo_passes_without_the_network(self):
        report = check.check(REPO, None, True, check.Report(io.StringIO()))
        self.assertEqual([], report.errors)


if __name__ == "__main__":
    unittest.main()
