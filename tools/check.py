#!/usr/bin/env python3
"""Checks the packages in this repo, as the check on a pull request does.

  tools/check.py --nbtapps DIR [--base REF] [--offline]

DIR is the headunit/ folder of a checkout of https://github.com/tom-pryor/nbtapps, whose tools/nbtpkg.py and
spec/ this uses. The check on a pull request checks it out at the commit .github/workflows/check.yml names.
It needs python3 and the `cryptography` module.

Every package is checked against the schemas and the rules that need no network: its id, the files it has,
listing.json, author.pub, the pictures, and each version's record. The ids the official catalogue lists are
read from https://catalogue.nbtapps.com/v1/index.json, and a package here may not have one.

A version's record that is new since REF (with no --base, every record) is then checked further:
  - its archive is fetched from `url`, and is the file `sha256` pins
  - `nbtpkg.py verify --key author.pub` takes it, and its manifest has the folder's id and the record's version
  - `source.commit` is a commit of `source.repo`, fetched over https

With --base, a record that was there at REF and is changed fails: a published version never changes. The one
change that does not fail is a promotion, which takes "channel": "prerelease" out of a record and changes
nothing else: the same archive is then a stable version. A record that was stable never gets a "channel". A
record that is taken out, a promotion, an author.pub that changes and a change outside packages/ are named for
the reviewer, and do not fail.

A version whose name has a pre-release part, such as 1.4.0-beta.1, is a pre-release: its record has
"channel": "prerelease", and it is never promoted.

--offline fetches nothing: no archive, no commit, and not the catalogue's ids.

What passes here is not yet accepted: a maintainer reviews the package and checks it again with the catalogue's
own tools before it is listed.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOGUE_INDEX = "https://catalogue.nbtapps.com/v1/index.json"
RESERVED = "org.nbtapp."
MAX_ARCHIVE_BYTES = 256 << 20
MAX_ICON_BYTES = 256 << 10
MAX_SCREENSHOT_BYTES = 2 << 20
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PACKAGE_FILES = ("listing.json", "author.pub", "icon.png", "screenshots", "versions")
PRERELEASE = "prerelease"   # the channel of a version that is not finished; a record with no "channel" is stable

_PACKAGE_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+")
_VERSION = re.compile(
    r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
_KEY = re.compile(r"[0-9a-f]{64}")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RECORD = re.compile(r"packages/([^/]+)/versions/([^/]+)\.json")

nbtpkg = None   # tools/nbtpkg.py of the NBTApps repo, once load_nbtpkg has found it


class Report:
    """What the check found. Text that a pull request wrote is never printed as it is: on GitHub a line of the
    output that starts with :: is a command to the runner, so every line break and control character in it is
    written out as an escape."""

    def __init__(self, out=sys.stdout):
        self.out, self.errors, self.notices = out, [], []
        self.github = os.environ.get("GITHUB_ACTIONS") == "true"

    @staticmethod
    def _clean(text):
        return "".join(c if 0x20 <= ord(c) != 0x7f else "\\x%02x" % ord(c) for c in str(text))

    def _say(self, kind, where, text):
        where, text = self._clean(where), self._clean(text)
        if self.github:
            print("::%s file=%s::%s" % (kind,
                                        where.replace("%", "%25").replace(",", "%2C").replace(":", "%3A"),
                                        text.replace("%", "%25")), file=self.out)
        else:
            print("%s: %s: %s" % (kind, where, text), file=self.out)

    def error(self, where, text):
        self.errors.append((where, text))
        self._say("error", where, text)

    def notice(self, where, text):
        self.notices.append((where, text))
        self._say("notice", where, text)


def load_nbtpkg(folder):
    global nbtpkg
    path = os.path.join(folder, "tools", "nbtpkg.py")
    if not os.path.isfile(path):
        sys.exit("check: %s is not there: --nbtapps is the headunit/ folder of the NBTApps repo" % path)
    sys.path.insert(0, os.path.dirname(path))
    import nbtpkg as module
    nbtpkg = module


def read_json(path, data=None):
    """(object, None) or (None, why not), of the file at `path` or of `data`, its bytes. A key twice is refused,
    as the catalogue refuses it."""
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("has the key %r twice" % key)
            obj[key] = value
        return obj

    try:
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except OSError as e:
        return None, "cannot be read: %s" % e.strerror
    except ValueError as e:
        return None, "is not JSON this check accepts: %s" % e
    if not isinstance(value, dict):
        return None, "is not a JSON object"
    return value, None


def load_schema(name):
    with open(os.path.join(REPO, "schema", name), "rb") as f:
        schema = json.load(f)
    # nbtpkg.py's validator takes the keywords the manifest schema uses; $id names the file and checks nothing.
    nbtpkg.check_schema_keywords({k: v for k, v in schema.items() if k != "$id"})
    return schema


def schema_problem(value, schema):
    try:
        nbtpkg.validate(value, schema, schema, "")
    except nbtpkg.SchemaError as e:
        return str(e).lstrip(": ")
    return None


def read_key(path):
    """The public key in a key file as nbtpkg.py keygen writes it, or None."""
    try:
        with open(path, "rb") as f:
            text = f.read(1000).decode("ascii")
    except (OSError, UnicodeDecodeError):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return bytes.fromhex(lines[0]) if len(lines) == 1 and _KEY.fullmatch(lines[0]) else None


def check_png(path, limit, where, report):
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if not data.startswith(PNG_MAGIC):
        report.error(where, "is not a PNG file")
    elif len(data) > limit:
        report.error(where, "is larger than %d bytes" % limit)


def is_date(value):
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def check_package(root, pkg_id, official, schemas, report):
    """The checks that need no network, of packages/<id>/. Returns {version: record} of the records that pass,
    and the author's key or None."""
    where = "packages/%s" % pkg_id
    folder = os.path.join(root, "packages", pkg_id)
    if not os.path.isdir(folder):
        report.error(where, "is not a folder")
        return {}, None
    if not _PACKAGE_ID.fullmatch(pkg_id) or len(pkg_id) > 128:
        report.error(where, "the folder's name is not a package id: two or more labels of a-z and 0-9, joined by dots")
        return {}, None
    if pkg_id.startswith(RESERVED):
        report.error(where, "ids under %s are the project's own" % RESERVED)
    if pkg_id in official:
        report.error(where, "the official catalogue already lists %s" % pkg_id)
    for name in sorted(os.listdir(folder)):
        if name not in PACKAGE_FILES:
            report.error("%s/%s" % (where, name), "is not a file a package has here")

    listing, why = read_json(os.path.join(folder, "listing.json"))
    if listing is None:
        report.error(where + "/listing.json", why)
    else:
        problem = schema_problem(listing, schemas["listing"])
        if problem:
            report.error(where + "/listing.json", problem)

    author = read_key(os.path.join(folder, "author.pub"))
    if author is None:
        report.error(where + "/author.pub", "is missing, or is not a key file as nbtpkg.py keygen writes one")

    if os.path.exists(os.path.join(folder, "icon.png")):
        check_png(os.path.join(folder, "icon.png"), MAX_ICON_BYTES, where + "/icon.png", report)
    shots = os.path.join(folder, "screenshots")
    for name in sorted(os.listdir(shots)) if os.path.isdir(shots) else []:
        at = "%s/screenshots/%s" % (where, name)
        if not _FILE_NAME.fullmatch(name) or not name.endswith(".png"):
            report.error(at, "a screenshot is a .png file with a plain name")
        else:
            check_png(os.path.join(shots, name), MAX_SCREENSHOT_BYTES, at, report)

    records = {}
    versions = os.path.join(folder, "versions")
    names = sorted(os.listdir(versions)) if os.path.isdir(versions) else []
    if not names:
        report.error(where, "has no version")
    for name in names:
        at = "%s/versions/%s" % (where, name)
        version = name[:-len(".json")]
        if not name.endswith(".json") or not _VERSION.fullmatch(version):
            report.error(at, "the file's name is not <version>.json, where the version is the manifest's")
            continue
        record, why = read_json(os.path.join(versions, name))
        if record is None:
            report.error(at, why)
            continue
        problem = schema_problem(record, schemas["version"])
        if problem:
            report.error(at, problem)
            continue
        if urllib.parse.urlsplit(record["url"]).path.rsplit("/", 1)[-1] != "%s-%s.nbtpkg" % (pkg_id, version):
            report.error(at, "\"url\" does not end in %s-%s.nbtpkg" % (pkg_id, version))
            continue
        if not is_date(record["released"]):
            report.error(at, "\"released\" is not a date")
            continue
        if _VERSION.fullmatch(version).group(4) is not None and record.get("channel") != PRERELEASE:
            report.error(at, "%s has a pre-release part, so it is a pre-release, and its record has \"channel\": "
                             "\"%s\"" % (version, PRERELEASE))
            continue
        records[version] = record
    return records, author


def fetch(url, limit):
    """The body at an https address, up to `limit` bytes. Raises ValueError."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nbtapps-packages-check"})
        with urllib.request.urlopen(request, timeout=60) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ValueError("it redirects away from https")
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError) as e:
        raise ValueError("it could not be fetched: %s" % getattr(e, "reason", e))
    if len(data) > limit:
        raise ValueError("it is larger than %d bytes" % limit)
    return data


def official_ids():
    """The ids the official catalogue lists. Raises ValueError."""
    try:
        index = json.loads(fetch(CATALOGUE_INDEX, 16 << 20).decode("utf-8"))
        return {p["id"] for p in index["packages"]}
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("%s cannot be read: %s" % (CATALOGUE_INDEX, e))


def commit_exists(repo, commit):
    """True if `commit` is a commit of the repo at https address `repo`. Only https is fetched from, with no
    login, and of the commit's history nothing but the commit itself."""
    with tempfile.TemporaryDirectory() as work:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        git = ["git", "-C", work, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "credential.helper="]
        try:
            subprocess.run(git[:3] + ["init", "-q"], check=True, capture_output=True, env=env, timeout=30)
            subprocess.run(git + ["fetch", "-q", "--depth=1", "--filter=tree:0", "--", repo, commit],
                           check=True, capture_output=True, env=env, timeout=300)
            found = subprocess.run(git + ["rev-parse", "--verify", "-q", commit + "^{commit}"],
                                   capture_output=True, text=True, env=env, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
    return found == commit


def check_archive(pkg_id, version, record, author, where, report):
    """The checks of a new version that fetch: its archive, and the commit it was built from."""
    try:
        data = fetch(record["url"], MAX_ARCHIVE_BYTES)
    except ValueError as e:
        report.error(where, "the archive at \"url\": %s" % e)
        return
    found = hashlib.sha256(data).hexdigest()
    if found != record["sha256"]:
        report.error(where, "the archive at \"url\" is not the one \"sha256\" pins: its SHA-256 is %s" % found)
    elif author is not None:
        try:
            info = nbtpkg.verify_package(data, [author])
        except nbtpkg.PkgError as e:
            report.error(where, "nbtpkg.py verify --key author.pub refuses the archive (%s): %s" % (e.code, e))
        else:
            if (info["id"], info["version"]) != (pkg_id, version):
                report.error(where, "the archive is %s %s" % (info["id"], info["version"]))
    if not commit_exists(record["source"]["repo"], record["source"]["commit"]):
        report.error(where, "\"source\": %s is not a commit of %s that can be fetched"
                     % (record["source"]["commit"], record["source"]["repo"]))


def changes(root, base):
    """[(status, path)] of what changed from `base` to the commit checked out: A, M or D."""
    out = subprocess.run(["git", "-C", root, "diff", "--name-status", "--no-renames", "-z", base, "HEAD"],
                         check=True, capture_output=True).stdout.decode("utf-8", "replace")
    parts = out.split("\0")
    return [(parts[i][:1], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def changed_record(root, base, path):
    """What a change to a version's record that was there at `base` does: "promotes" when it takes
    "channel": "prerelease" out and changes nothing else, "demotes" when it gives a stable version a channel, and
    otherwise "changes"."""
    shown = subprocess.run(["git", "-C", root, "show", "%s:%s" % (base, path)], capture_output=True)
    before = read_json(path, shown.stdout)[0] if shown.returncode == 0 else None
    now = read_json(os.path.join(root, *path.split("/")))[0]
    if before is None or now is None:
        return "changes"
    if "channel" not in before and "channel" in now:
        return "demotes"
    if before.get("channel") == PRERELEASE and "channel" not in now \
            and {k: v for k, v in before.items() if k != "channel"} == now:
        return "promotes"
    return "changes"


def check(root, base=None, offline=False, report=None):
    report = report or Report()
    schemas = {name: load_schema("%s.schema.json" % name) for name in ("listing", "version")}
    official = set()
    if not offline:
        try:
            official = official_ids()
        except ValueError as e:
            report.error("packages", str(e))

    packages = os.path.join(root, "packages")
    checked = {}
    for pkg_id in sorted(os.listdir(packages)) if os.path.isdir(packages) else []:
        checked[pkg_id] = check_package(root, pkg_id, official, schemas, report)

    if base is None:
        new = {(pkg_id, version) for pkg_id, (records, _) in checked.items() for version in records}
    else:
        new = set()
        for status, path in changes(root, base):
            record = _RECORD.fullmatch(path)
            if record and status == "A":
                new.add(record.groups())
            elif record and status == "M":
                change = changed_record(root, base, path)
                if change == "promotes":
                    report.notice(path, "promotes this pre-release to stable, with the archive that is here already")
                elif change == "demotes":
                    report.error(path, "is a stable version that was already here, and a stable version is never "
                                       "made a pre-release: take it out instead")
                else:
                    report.error(path, "is a version that was already here, and a published version never changes: "
                                       "a changed archive is a new version")
            elif record and status == "D":
                report.notice(path, "takes this version out")
            elif re.fullmatch(r"packages/[^/]+/author\.pub", path) and status == "M":
                report.notice(path, "changes: a new author.pub is a change of owner, and the pull request says why")
            elif not path.startswith("packages/"):
                report.notice(path, "is not a package's file: this pull request changes the repo itself")

    for pkg_id, version in sorted(new):
        records, author = checked.get(pkg_id, ({}, None))
        if version not in records:
            continue   # the record itself has failed, and said why
        if offline:
            report.notice("packages/%s/versions/%s.json" % (pkg_id, version), "not fetched: --offline")
        else:
            check_archive(pkg_id, version, records[version], author,
                          "packages/%s/versions/%s.json" % (pkg_id, version), report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="check.py", description="Check the packages in this repo.")
    parser.add_argument("--nbtapps", required=True, help="the headunit/ folder of the NBTApps repo")
    parser.add_argument("--base", help="check further only the version records added since this commit")
    parser.add_argument("--offline", action="store_true", help="fetch nothing")
    args = parser.parse_args(argv)
    load_nbtpkg(os.path.abspath(args.nbtapps))
    report = check(REPO, args.base, args.offline)
    print("%d package(s): %d error(s), %d notice(s)" % (
        len(os.listdir(os.path.join(REPO, "packages"))) if os.path.isdir(os.path.join(REPO, "packages")) else 0,
        len(report.errors), len(report.notices)))
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
