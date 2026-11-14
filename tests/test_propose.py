#!/usr/bin/env python3
"""Tests for tools/propose.py. Run from the repo: python3 -m unittest discover -s tests

They need the headunit/ folder of the NBTApps repo, as tests/test_check.py does. GitHub is a fake one here, with
invented repos, releases and records, in front of a throwaway repo: nothing is fetched, pushed or opened.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import check  # noqa: E402
import propose  # noqa: E402

HEADUNIT = os.environ.get("NBTAPPS_HEADUNIT") or os.path.join(REPO, "..", "nbtapps-mono", "headunit")
if not os.path.isfile(os.path.join(HEADUNIT, "tools", "nbtpkg.py")):
    raise unittest.SkipTest("the NBTApps repo's headunit/ is not at %s: set NBTAPPS_HEADUNIT" % HEADUNIT)
check.load_nbtpkg(HEADUNIT)
nbtpkg = check.nbtpkg

AUTHOR, OTHER = bytes([7]) * 32, bytes([8]) * 32
THIS = "example-owner/example-packages"
APP, WATCHED = "com.example.app", "https://github.com/example-dev/app"
COMMIT, COMMIT2 = "c0ffee" + "0" * 34, "badc0de" + "1" * 33


class FakeGitHub:
    """The GitHub API as the bot uses it, and the push."""

    def __init__(self):
        self.releases = {}      # "owner/repo" -> [release]
        self.branches = set()   # this repo's branches
        self.pulls = []         # the bodies of the pull requests opened
        self.pushed = {}        # branch -> {path: text} of what the pushed commit adds
        self.failing = set()    # "owner/repo" whose releases the API fails on

    def api(self, method, path, token=None, body=None):
        listing = re.fullmatch(r"/repos/([^/]+/[^/]+)/releases\?per_page=30", path)
        if method == "GET" and listing:
            if listing.group(1) in self.failing:
                raise propose.BotError("GET %s: HTTP 502: Bad Gateway" % path)
            if listing.group(1) not in self.releases:
                raise propose.NotFound("GET %s: 404" % path)
            return json.loads(json.dumps(self.releases[listing.group(1)]))
        ref = re.fullmatch(r"/repos/%s/git/ref/heads/(.+)" % THIS, path)
        if method == "GET" and ref:
            if urllib.parse.unquote(ref.group(1)) not in self.branches:
                raise propose.NotFound("GET %s: 404" % path)
            return {"ref": "refs/heads/" + urllib.parse.unquote(ref.group(1))}
        if method == "POST" and path == "/repos/%s/pulls" % THIS:
            assert body["head"] in self.branches and body["base"] == "main"
            self.pulls.append(body)
            return {"html_url": "https://github.com/%s/pull/%d" % (THIS, len(self.pulls))}
        raise AssertionError("not an API call the bot makes: %s %s" % (method, path))

    def push(self, tree, repo, branch, token):
        assert repo == THIS and token == "t0ken" and branch not in self.branches
        def git(*args):
            return subprocess.run(("git", "-C", tree) + args, check=True, capture_output=True, text=True).stdout
        added = git("diff", "--name-only", "HEAD~1", "HEAD").split()
        self.pushed[branch] = {path: git("show", "HEAD:" + path) for path in added}
        self.pushed[branch][":author"] = git("log", "-1", "--format=%an <%ae>")
        self.branches.add(branch)


class ProposeTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = os.path.join(work.name, "repo")
        os.makedirs(self.root)
        self.github = FakeGitHub()
        self.served = {}            # address -> bytes
        self.commits = {(WATCHED, COMMIT), (WATCHED, COMMIT2)}
        for target, name, value in ((check, "fetch", self.fetch),
                                    (check, "commit_exists", lambda repo, c: (repo, c) in self.commits),
                                    (check, "official_ids", lambda: {"org.example.official"}),
                                    (propose, "api", self.github.api), (propose, "push", self.github.push)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.git("init", "-q", "-b", "main")
        self.add_package(APP, WATCHED)
        self.head = self.commit()

    def fetch(self, url, limit):
        if url not in self.served:
            raise ValueError("it could not be fetched: Not Found")
        if len(self.served[url]) > limit:
            raise ValueError("it is larger than %d bytes" % limit)
        return self.served[url]

    def git(self, *args):
        return subprocess.run(("git", "-C", self.root, "-c", "user.name=T", "-c", "user.email=t@example.invalid")
                              + args, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def write(self, relative, value):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")

    def archive(self, pkg_id, version, seed=AUTHOR):
        manifest = {"manifestVersion": 1, "id": pkg_id, "name": "Demo", "version": version,
                    "requires": {"agent": ">=1.2.0"},
                    "install": {"files": [{"src": "payload/", "dest": "${app}/", "mode": "0644"}]}}
        with tempfile.TemporaryDirectory() as src:
            os.makedirs(os.path.join(src, "payload"))
            with open(os.path.join(src, "payload", "hello.txt"), "w") as f:
                f.write("hello")
            with open(os.path.join(src, "manifest.json"), "w") as f:
                json.dump(manifest, f)
            return nbtpkg.build_package(src, seed)[1]

    def add_package(self, pkg_id, repo, version="1.0.0"):
        """A package as its first pull request adds it. Its archive is not served: it is not fetched again."""
        folder = "packages/%s" % pkg_id
        self.write(folder + "/listing.json", {"summary": "A demo", "author": "Someone", "category": "utilities",
                                              "description": "Does nothing."})
        self.write(folder + "/author.pub", "# someone\n%s\n" % nbtpkg.public_key(AUTHOR).hex())
        self.write("%s/versions/%s.json" % (folder, version), {
            "url": "%s/releases/download/v%s/%s-%s.nbtpkg" % (repo, version, pkg_id, version),
            "sha256": "ab" * 32, "released": "2026-11-01", "source": {"repo": repo, "commit": COMMIT}})

    def release(self, version, pkg_id=APP, repo=WATCHED, tag=None, draft=False, prerelease=False, data=None,
                change=None, raw=None, record_asset=True, archive_asset=True):
        """A release of `repo` with its archive and nbtpkg-record.json, as the author's release workflow makes
        one. `change` changes the file's JSON, `raw` stands for the file's bytes."""
        tag = tag or "v" + version
        data = data if data is not None else self.archive(pkg_id, version)
        base = "%s/releases/download/%s/" % (repo, tag)
        value = {"id": pkg_id, "version": version, "record": {
            "url": base + "%s-%s.nbtpkg" % (pkg_id, version), "sha256": hashlib.sha256(data).hexdigest(),
            "released": "2026-11-13", "notes": ["What is new"], "source": {"repo": repo, "commit": COMMIT2}}}
        if change:
            change(value)
        raw = raw if raw is not None else json.dumps(value).encode()
        assets = []
        if archive_asset:
            assets.append({"name": "%s-%s.nbtpkg" % (pkg_id, version), "size": len(data), "state": "uploaded",
                           "browser_download_url": base + "%s-%s.nbtpkg" % (pkg_id, version)})
            self.served[base + "%s-%s.nbtpkg" % (pkg_id, version)] = data
        if record_asset:
            assets.append({"name": "nbtpkg-record.json", "size": len(raw), "state": "uploaded",
                           "browser_download_url": base + "nbtpkg-record.json"})
            self.served[base + "nbtpkg-record.json"] = raw
        self.github.releases.setdefault(repo[len("https://github.com/"):], []).insert(0, {
            "tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets})
        return value

    def run_bot(self, dry_run=False):
        out = io.StringIO()
        outcomes, failures = propose.run(self.root, THIS, "t0ken", dry_run, out)
        self.out = out.getvalue()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))   # the checkout is left as it was
        self.assertEqual("", self.git("status", "--porcelain"))
        self.assertEqual(1, self.git("worktree", "list", "--porcelain").count("worktree "))
        return [tuple(o) for o in outcomes], failures

    def declined(self, why, *pieces):
        outcomes, failures = self.run_bot()
        self.assertEqual([], failures)
        self.assertEqual(1, len(outcomes))
        self.assertEqual((APP, "declined"), outcomes[0][:2], why)
        for piece in pieces:
            self.assertIn(piece, outcomes[0][2], why)
        self.assertEqual(([], {}), (self.github.pulls, self.github.pushed), why)

    # ----

    def test_nothing_to_do(self):
        self.github.releases["example-dev/app"] = []
        self.assertEqual(([(APP, "none", "no release of %s offers a version above 1.0.0" % WATCHED)], []),
                         self.run_bot())
        self.assertEqual([], self.github.pulls)

    def test_a_new_version_is_proposed(self):
        self.release("1.1.0")
        outcomes, failures = self.run_bot()
        self.assertEqual(([(APP, "proposed", "1.1.0: https://github.com/%s/pull/1" % THIS)], []),
                         (outcomes, failures))
        record = "packages/com.example.app/versions/1.1.0.json"
        pushed = self.github.pushed["auto/com.example.app-1.1.0"]
        self.assertEqual({record, ":author"}, set(pushed))
        self.assertEqual("github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
                         pushed[":author"].strip())
        self.assertEqual(json.loads(self.served["%s/releases/download/v1.1.0/nbtpkg-record.json" % WATCHED])["record"],
                         json.loads(pushed[record]))
        self.assertTrue(pushed[record].startswith('{\n  "url": "https://github.com/example-dev/app/'))
        self.assertIn('\n  "notes": ["What is new"],\n  "source": {\n    "repo": ', pushed[record])

        pull = self.github.pulls[0]
        self.assertEqual(("com.example.app 1.1.0", "auto/com.example.app-1.1.0", "main"),
                         (pull["title"], pull["head"], pull["base"]))
        for piece in ("**Package:** `com.example.app`", "**This adds:** version 1.1.0",
                      "A bot proposed this pull request from the author's own release, "
                      "https://github.com/example-dev/app/releases/tag/v1.1.0.",
                      "%s/compare/%s...%s" % (WATCHED, COMMIT, COMMIT2), "1 package(s): 0 error(s), 0 notice(s)"):
            self.assertIn(piece, pull["body"])
        self.assertNotIn("@", pull["body"].replace("41898282+github-actions[bot]@", ""))

        # Proposed once: its branch is there now.
        self.assertEqual([(APP, "none", "1.1.0 was proposed before: the branch auto/com.example.app-1.1.0 is "
                                        "there")], self.run_bot()[0])
        self.assertEqual(1, len(self.github.pulls))

    def test_a_dry_run_pushes_nothing_and_opens_nothing(self):
        self.release("1.1.0")
        self.assertEqual(([(APP, "would propose", "1.1.0, on auto/com.example.app-1.1.0")], []),
                         self.run_bot(dry_run=True))
        self.assertEqual(([], {}), (self.github.pulls, self.github.pushed))
        self.assertIn("--- would push auto/com.example.app-1.1.0 and open the pull request \"com.example.app "
                      "1.1.0\" against main", self.out)

    def test_drafts_and_pre_releases_are_skipped(self):
        self.release("1.1.0", draft=True)
        self.release("1.2.0", prerelease=True)
        self.assertEqual("none", self.run_bot()[0][0][1])
        self.release("1.0.1")
        self.assertEqual([(APP, "proposed", "1.0.1: https://github.com/%s/pull/1" % THIS)], self.run_bot()[0])

    def test_a_release_that_is_not_for_the_bot_is_skipped(self):
        self.release("1.1.0", record_asset=False)
        self.release("1.2.0", archive_asset=False)
        self.release("1.3.0")
        self.github.releases["example-dev/app"][0]["assets"][1]["state"] = "starter"   # still uploading
        self.assertEqual("none", self.run_bot()[0][0][1])

    def test_an_older_or_equal_version_is_skipped(self):
        self.release("0.9.0")
        self.release("1.0.0")
        self.release("1.0.0+build.2")
        self.release("1.0.0-rc.1")
        self.assertEqual([(APP, "none", "no release of %s offers a version above 1.0.0" % WATCHED)],
                         self.run_bot()[0])

    def test_only_the_newest_version_above_is_proposed(self):
        self.release("1.10.0")
        self.release("1.2.0")
        self.release("1.9.0")
        self.assertEqual([(APP, "proposed", "1.10.0: https://github.com/%s/pull/1" % THIS)], self.run_bot()[0])
        self.assertEqual(["auto/com.example.app-1.10.0"], list(self.github.pushed))

    def test_a_branch_that_is_there_already_stops_the_version(self):
        self.github.branches.add("auto/com.example.app-1.1.0")
        self.release("1.1.0")
        self.assertEqual("none", self.run_bot()[0][0][1])
        self.assertEqual([], self.github.pulls)

    def test_versions_order_as_semver(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                 "1.0.0-rc.1", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "10.0.0"]
        self.assertEqual(order, sorted(reversed(order), key=propose.version_key))
        self.assertEqual(propose.version_key("1.0.0"), propose.version_key("1.0.0+build.5"))

    def test_the_record_must_be_for_the_package(self):
        self.release("1.1.0", change=lambda v: v.update(id="com.example.other"))
        self.declined("another id", "nbtpkg-record.json is for com.example.other, not com.example.app")

    def test_the_record_must_be_for_the_archives_version(self):
        self.release("1.1.0", change=lambda v: v.update(version="1.1.1"))
        self.declined("another version", "is for version 1.1.1, and the release's archive is "
                                         "com.example.app-1.1.0.nbtpkg")

    def test_the_url_must_be_on_the_watched_repo(self):
        def elsewhere(value):
            value["record"]["url"] = "https://github.com/someone-else/app/releases/download/v1.1.0/" \
                                     "com.example.app-1.1.0.nbtpkg"
        self.release("1.1.0", change=elsewhere)
        self.declined("another repo", "\"url\" is not a release of %s" % WATCHED)

        for url in ("https://example.invalid/example-dev/app/releases/download/v1.1.0/com.example.app-1.1.0.nbtpkg",
                    "https://github.com@example.invalid/example-dev/app/releases/download/v1.1.0/"
                    "com.example.app-1.1.0.nbtpkg",
                    "https://github.com/example-dev/app/releases/download/v1.1.0/com.example.app-1.1.0.nbtpkg"
                    "?x=.nbtpkg"):
            self.github.releases.clear()
            self.release("1.1.0", change=lambda v: v["record"].update(url=url))
            self.declined(url, "\"url\" is not a release of %s" % WATCHED)

    def test_the_url_must_end_in_the_archives_name_in_the_same_release(self):
        self.release("1.1.0", change=lambda v: v["record"].update(
            url="%s/releases/download/v1.1.0/com.example.app-1.0.0.nbtpkg" % WATCHED))
        self.declined("another name", "\"url\" does not end in com.example.app-1.1.0.nbtpkg")
        self.github.releases.clear()
        self.release("1.1.0", change=lambda v: v["record"].update(
            url="%s/releases/download/v1.0.0/com.example.app-1.1.0.nbtpkg" % WATCHED))
        self.declined("another release", "\"url\" is not in the release the record is in")

    def test_source_repo_must_be_the_watched_repo(self):
        self.release("1.1.0", change=lambda v: v["record"]["source"].update(
            repo="https://github.com/example-dev/fork"))
        self.declined("another source", "\"source.repo\" is https://github.com/example-dev/fork, not %s" % WATCHED)

    def test_the_file_has_exactly_its_three_keys(self):
        self.release("1.1.0", change=lambda v: v.update(extra=1))
        self.declined("extra key", "has the keys [\"extra\", \"id\", \"record\", \"version\"]")
        self.github.releases.clear()
        self.release("1.1.0", change=lambda v: v.pop("record"))
        self.declined("missing key", "has the keys [\"id\", \"version\"]")

    def test_the_record_must_pass_the_schema(self):
        for why, change, piece in (
                ("upper case", lambda v: v["record"].update(sha256="AB" * 32), ".sha256"),
                ("extra field", lambda v: v["record"].update(signature="x"), "'signature'"),
                ("no source", lambda v: v["record"].pop("source"), "has no 'source'"),
                ("not a date", lambda v: v["record"].update(released="2026-02-30"), "\"released\" is not a date"),
                ("not an object", lambda v: v.update(record=[]), "\"record\" is not a JSON object")):
            self.github.releases.clear()
            self.release("1.1.0", change=change)
            self.declined(why, piece)

    def test_the_file_must_be_small_strict_json(self):
        for why, raw, piece in (
                ("malformed", b"{\"id\": ", "is not JSON this bot accepts"),
                ("twice", b"{\"id\": \"a\", \"id\": \"b\"}", "has the key 'id' twice"),
                ("NaN", b"{\"id\": NaN}", "NaN is not JSON"),
                ("not UTF-8", b"{\"id\": \"\xff\"}", "is not JSON this bot accepts"),
                ("deep", b"[" * 60000, "is not JSON this bot accepts"),
                ("an array", b"[]", "nbtpkg-record.json is not a JSON object"),
                ("oversized", b" " * (64 << 10) + b"{}", "nbtpkg-record.json is larger than 65536 bytes")):
            self.github.releases.clear()
            self.release("1.1.0", raw=raw)
            self.declined(why, piece)

    def test_a_file_bigger_than_the_release_says_is_cut_off(self):
        self.release("1.1.0")
        self.github.releases["example-dev/app"][0]["assets"][1]["size"] = 10
        self.served["%s/releases/download/v1.1.0/nbtpkg-record.json" % WATCHED] = b" " * (64 << 10) + b"{}"
        self.declined("oversized", "nbtpkg-record.json: it is larger than 65536 bytes")

    def test_a_failing_check_opens_nothing(self):
        self.release("1.1.0", change=lambda v: v["record"].update(sha256="ab" * 32))
        self.declined("wrong sha256", "tools/check.py fails on the record:", "is not the one \"sha256\" pins")

        self.github.releases.clear()
        self.release("1.1.0", data=self.archive(APP, "1.1.0", seed=OTHER))
        self.declined("another key", "nbtpkg.py verify --key author.pub refuses the archive (bad-signature)")

        self.github.releases.clear()
        self.release("1.1.0", change=lambda v: v["record"]["source"].update(commit="1" * 40))
        self.declined("no such commit", "is not a commit of %s that can be fetched" % WATCHED)

    def test_a_repo_that_is_not_on_github_is_not_watched(self):
        self.add_package("com.example.elsewhere", "https://example.invalid/src")
        self.add_package("com.example.slash", "https://github.com/example-dev/app/")
        self.head = self.commit()
        self.github.releases["example-dev/app"] = []
        outcomes = dict((o[0], o[1:]) for o in self.run_bot()[0])
        self.assertEqual(("none", "its source.repo is not a repo on GitHub, so it is not watched"),
                         outcomes["com.example.elsewhere"])
        self.assertEqual("none", outcomes["com.example.slash"][0])

    def test_several_packages(self):
        self.add_package("com.example.gone", "https://github.com/example-dev/gone")
        self.add_package("com.example.quiet", "https://github.com/example-dev/quiet", version="2.0.0")
        self.add_package("com.example.second", "https://github.com/example-dev/second")
        self.add_package("com.example.broken", "https://github.com/example-dev/broken")
        self.add_package("com.example.down", "https://github.com/example-dev/down")
        self.head = self.commit()
        self.commits.add(("https://github.com/example-dev/second", COMMIT2))
        self.release("1.1.0")
        self.release("2.0.0", pkg_id="com.example.quiet", repo="https://github.com/example-dev/quiet")
        self.release("1.0.1", pkg_id="com.example.second", repo="https://github.com/example-dev/second")
        self.release("1.0.1", pkg_id="com.example.broken", repo="https://github.com/example-dev/broken",
                     change=lambda v: v.update(extra=1))
        self.github.failing.add("example-dev/down")
        outcomes, failures = self.run_bot()
        self.assertEqual([
            (APP, "proposed", "1.1.0: https://github.com/%s/pull/1" % THIS),
            ("com.example.broken", "declined", "1.0.1: nbtpkg-record.json has the keys [\"extra\", \"id\", "
                                               "\"record\", \"version\"], not exactly id, version and record"),
            ("com.example.gone", "declined", "https://github.com/example-dev/gone cannot be found on GitHub"),
            ("com.example.quiet", "none", "no release of https://github.com/example-dev/quiet offers a version "
                                          "above 2.0.0"),
            ("com.example.second", "proposed", "1.0.1: https://github.com/%s/pull/2" % THIS),
        ], outcomes)
        self.assertEqual([("com.example.down", "GET /repos/example-dev/down/releases?per_page=30: HTTP 502: "
                                               "Bad Gateway")], failures)
        self.assertEqual({"auto/com.example.app-1.1.0", "auto/com.example.second-1.0.1"}, set(self.github.pushed))
        # Each pull request adds its own record and nothing else.
        self.assertEqual({"packages/com.example.second/versions/1.0.1.json", ":author"},
                         set(self.github.pushed["auto/com.example.second-1.0.1"]))

    def test_what_is_printed_on_github_cannot_start_a_command(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": ""}):
            propose.summarise([propose.Outcome(APP, "declined", "1.1.0: bad\n::add-mask::x\r%")],
                              [(APP, "it\nfailed")], out)
        self.assertEqual(["com.example.app: declined: 1.1.0: bad\\x0a::add-mask::x\\x0d%",
                          "::warning::com.example.app: declined: 1.1.0: bad%0A::add-mask::x\\x0d%25",
                          "::error::com.example.app: the bot failed: it%0Afailed"],
                         out.getvalue().split("\n")[:3])

    def test_a_record_is_written_as_readme_shows_one(self):
        record = {"source": {"commit": COMMIT, "repo": WATCHED}, "notes": ["One", "Zwei ü"], "released": "2026-11-13",
                  "sha256": "ab" * 32, "url": WATCHED + "/releases/download/v1/com.example.app-1.0.0.nbtpkg"}
        text = propose.format_record(record)
        self.assertEqual(record, json.loads(text))
        self.assertEqual(['{', '  "url": "%s/releases/download/v1/com.example.app-1.0.0.nbtpkg",' % WATCHED,
                          '  "sha256": "%s",' % ("ab" * 32), '  "released": "2026-11-13",',
                          '  "notes": ["One", "Zwei ü"],', '  "source": {', '    "repo": "%s",' % WATCHED,
                          '    "commit": "%s"' % COMMIT, '  }', '}', ''], text.split("\n"))


if __name__ == "__main__":
    unittest.main()
