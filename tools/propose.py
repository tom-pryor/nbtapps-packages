#!/usr/bin/env python3
"""Proposes a new version of a package that is already here, from a release of the package's own repo, as
.github/workflows/propose.yml does once an hour.

  tools/propose.py --nbtapps DIR [--repo OWNER/NAME] [--dry-run]

DIR is the headunit/ folder of the NBTApps repo, as for tools/check.py. OWNER/NAME is this repo on GitHub, from
GITHUB_REPOSITORY when that is set. The GitHub API is called with GITHUB_TOKEN when that is set; a pull request
needs it, --dry-run does not. It needs python3, git and the `cryptography` module.

For each package in packages/, the `source.repo` of its newest record is watched, if it is a repo on GitHub:
https://github.com/<owner>/<repo>, nothing more. A package is only ever added by hand, and its first pull request
is what has it watched.

The first page of the watched repo's releases is read, without its drafts. A release that holds an asset named
nbtpkg-record.json and one archive named <id>-<version>.nbtpkg offers that version. It offers it as a pre-release
if GitHub marks the release as one, or if the version has a pre-release part, such as 1.4.0-beta.1, and as a
stable version otherwise. Two versions are looked for, and the first of them that was not proposed before is
proposed:
  - the newest stable version offered that is above the newest stable record here
  - the newest pre-release offered that is above every record here, and above that stable version
A version that has a record here is never proposed, so the bot promotes nothing: a pre-release here whose release
is no longer one on GitHub is promoted by a pull request of the author's own, and the bot says so. A version is
proposed once: not while this repo has the branch auto/<id>-<version>, even if its pull request is closed, and
deleting the branch lets it be proposed again. Versions are ordered as semver 2.0.0 orders them, as the NBTApps
agent does.

The release's nbtpkg-record.json is {"id", "version", "record"} and nothing else, at most 64 KiB of JSON with no
key twice. `id` is the package's, `version` is the archive's, and `record` is a version's record that
schema/version.schema.json takes, whose `url` is the archive in that same release of the watched repo,
https://github.com/<owner>/<repo>/releases/download/<tag>/<id>-<version>.nbtpkg, and whose `source.repo` is the
watched repo. The record of a pre-release is given "channel": "prerelease" here, so the file need not have it;
a file that has it in a release that offers a stable version is declined.

The record is then written to packages/<id>/versions/<version>.json and committed, in a worktree of its own, and
tools/check.py checks it as the check on a pull request does: the archive is fetched and must be the file `sha256`
pins, `nbtpkg.py verify --key author.pub` must take it, and `source.commit` must be in `source.repo`. If it
passes, the commit is pushed to auto/<id>-<version> and a pull request is opened against main, with the check's
output in it. A release that fails any of this opens nothing: why is written as a warning and to the job's
summary, and the run passes. A failure of the bot itself, such as an error from the GitHub API, fails the run.

What is fetched is data: it is read, never run, and none of it goes into a command, a file's name, a branch or a
title. Those are made from the package's id and the version once both are checked. Nothing here but the new
record is ever written.

--dry-run does all of this but the push and the pull request, and prints them instead.
"""
import argparse
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402

REPO = check.REPO
THIS_REPO = "tom-pryor/nbtapps-packages"
API = "https://api.github.com"
BASE_BRANCH = "main"
BRANCH_PREFIX = "auto/"
RECORD_ASSET = "nbtpkg-record.json"
RECORD_FIELDS = ("url", "sha256", "released", "channel", "notes", "source")
MAX_API_BYTES = 8 << 20
MAX_RECORD_BYTES = 64 << 10
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")

_WATCHED = re.compile(r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})")
_THIS_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_COMMIT = re.compile(r"[0-9a-f]{40}")

# What happened to a package: kind is "proposed", "would propose" (--dry-run), "none" or "declined".
Outcome = namedtuple("Outcome", "pkg_id kind text")


class BotError(Exception):
    """A failure of the bot itself, such as an error from the GitHub API or git: the run fails."""


class NotFound(BotError):
    """The GitHub API answers 404."""


class Declined(Exception):
    """Why a release is not proposed."""


def api(method, path, token=None, body=None):
    """The JSON the GitHub API answers. Raises NotFound for a 404 and BotError for any other failure."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "nbtapps-packages-propose"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read(MAX_API_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound("%s %s: 404" % (method, path))
        try:
            message = json.loads(e.read(4096).decode("utf-8"))["message"]
        except (ValueError, KeyError, TypeError, OSError):
            message = e.reason
        raise BotError("%s %s: HTTP %d: %s" % (method, path, e.code, message))
    except (urllib.error.URLError, OSError) as e:
        raise BotError("%s %s: %s" % (method, path, getattr(e, "reason", e)))
    if len(text) > MAX_API_BYTES:
        raise BotError("%s %s: the answer is larger than %d bytes" % (method, path, MAX_API_BYTES))
    try:
        return json.loads(text.decode("utf-8"))
    except ValueError as e:
        raise BotError("%s %s: the answer is not JSON: %s" % (method, path, e))


def git(*args, cwd, env=None):
    """git in `cwd`, with no configuration but the repo's own. Raises BotError."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               **(env or {}))
    try:
        result = subprocess.run(("git", "-C", cwd) + args, capture_output=True, text=True, env=env, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise BotError("git %s: %s" % (args[0], e))
    if result.returncode != 0:
        raise BotError("git %s: %s" % (args[0], result.stderr.strip()))
    return result.stdout.strip()


def branch_exists(repo, branch, token):
    try:
        api("GET", "/repos/%s/git/ref/heads/%s" % (repo, urllib.parse.quote(branch, safe="/")), token)
    except NotFound:
        return False
    return True


def push(tree, repo, branch, token):
    """Pushes the commit checked out in `tree` to a new branch of `repo` on GitHub. The token is given to git in
    its environment, so it is in no command line and in no file."""
    header = "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    git("push", "-q", "https://github.com/%s.git" % repo, "HEAD:refs/heads/" + branch, cwd=tree,
        env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
             "GIT_CONFIG_VALUE_0": header})


def open_pull(repo, token, title, branch, body):
    """Opens a pull request of `branch` against main. Returns its address."""
    answer = api("POST", "/repos/%s/pulls" % repo, token,
                 {"title": title, "head": branch, "base": BASE_BRANCH, "body": body})
    url = answer.get("html_url") if isinstance(answer, dict) else None
    return url if isinstance(url, str) else "(the API gave no address)"


def version_key(version):
    """A key that orders versions as semver 2.0.0 does, and as the NBTApps agent does: a pre-release sorts before
    its release, a numeric identifier before a word, and build metadata is left out."""
    match = check._VERSION.fullmatch(version)
    numbers = tuple(int(match.group(i)) for i in (1, 2, 3))
    if match.group(4) is None:
        return numbers + (1, ())
    return numbers + (0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                               for part in match.group(4).split(".")))


def shown(value):
    """A short, printable form of a value that was fetched."""
    text = check.Report._clean(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
    return text if len(text) <= 80 else text[:77] + "..."


def records_here(root, pkg_id):
    """{version: its record, or None if it cannot be read} of the package's records here."""
    folder = os.path.join(root, "packages", pkg_id, "versions")
    names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    versions = [name[:-len(".json")] for name in names
                if name.endswith(".json") and check._VERSION.fullmatch(name[:-len(".json")])]
    return {version: check.read_json(os.path.join(folder, version + ".json"))[0] for version in versions}


def newest_record(root, pkg_id):
    """(version, record) of the package's newest record here, or None."""
    here = records_here(root, pkg_id)
    if not here:
        return None
    version = max(here, key=version_key)
    return (version, here[version]) if here[version] is not None else None


def is_prerelease_here(record):
    """Whether a record here is a pre-release's. One that cannot be read counts as stable: nothing is proposed
    past it that a stable version would stop."""
    return isinstance(record, dict) and record.get("channel") == check.PRERELEASE


def parse_json(data):
    """The JSON in `data`, which is UTF-8 with no key twice, no NaN and no Infinity. Raises Declined."""
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("has the key %r twice" % key)
            obj[key] = value
        return obj

    def constant(name):
        raise ValueError("%s is not JSON" % name)

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as e:
        raise Declined("%s is not JSON this bot accepts: %s" % (RECORD_ASSET, shown(str(e))))


def offered(pkg_id, releases):
    """[(version, release, record asset, whether it is a pre-release)] of the releases that offer a version: not a
    draft, with an asset nbtpkg-record.json and one archive <id>-<version>.nbtpkg, both uploaded in full. A
    version is a pre-release if GitHub marks its release as one, or if it has a pre-release part."""
    if not isinstance(releases, list):
        raise BotError("the API's list of releases is not a list")
    found = []
    for release in releases:
        if (not isinstance(release, dict) or release.get("draft") is not False
                or not isinstance(release.get("prerelease"), bool)):
            continue
        if not isinstance(release.get("tag_name"), str) or not release["tag_name"]:
            continue
        assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        named = {a["name"]: a for a in assets
                 if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("state") == "uploaded"}
        prefix, suffix = pkg_id + "-", ".nbtpkg"
        versions = [name[len(prefix):-len(suffix)] for name in named
                    if name.startswith(prefix) and name.endswith(suffix)]
        versions = [v for v in versions if check._VERSION.fullmatch(v)]
        if RECORD_ASSET in named and len(versions) == 1:
            tagged = check._VERSION.fullmatch(versions[0]).group(4) is not None
            found.append((versions[0], release, named[RECORD_ASSET], release["prerelease"] or tagged))
    return found


def release_record(pkg_id, version, owner, repo, release, asset, schema, prerelease=False):
    """The record in a release's nbtpkg-record.json, once it keeps to the contract, and with "channel" if the
    release offers a `prerelease`. Raises Declined."""
    watched = "https://github.com/%s/%s" % (owner, repo)
    downloads = "/%s/%s/releases/download/" % (owner, repo)
    tag = release["tag_name"]

    url = asset.get("browser_download_url")
    where = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (where is None or where.scheme != "https" or where.netloc != "github.com" or where.query or where.fragment
            or not where.path.startswith(downloads) or not where.path.endswith("/" + RECORD_ASSET)):
        raise Declined("the release's %s is not served from %s" % (RECORD_ASSET, watched))
    if not isinstance(asset.get("size"), int) or asset["size"] > MAX_RECORD_BYTES:
        raise Declined("%s is larger than %d bytes" % (RECORD_ASSET, MAX_RECORD_BYTES))
    try:
        data = check.fetch(url, MAX_RECORD_BYTES)
    except ValueError as e:
        raise Declined("%s: %s" % (RECORD_ASSET, e))

    value = parse_json(data)
    if not isinstance(value, dict):
        raise Declined("%s is not a JSON object" % RECORD_ASSET)
    if set(value) != {"id", "version", "record"}:
        raise Declined("%s has the keys %s, not exactly id, version and record"
                       % (RECORD_ASSET, shown(sorted(value))))
    if value["id"] != pkg_id:
        raise Declined("%s is for %s, not %s" % (RECORD_ASSET, shown(value["id"]), pkg_id))
    if value["version"] != version:
        raise Declined("%s is for version %s, and the release's archive is %s-%s.nbtpkg"
                       % (RECORD_ASSET, shown(value["version"]), pkg_id, version))
    record = value["record"]
    if not isinstance(record, dict):
        raise Declined("\"record\" is not a JSON object")
    problem = check.schema_problem(record, schema)
    if problem:
        raise Declined("\"record\" is not a version's record: %s" % shown(problem))
    if not check.is_date(record["released"]):
        raise Declined("\"record\": \"released\" is not a date")

    archive = urllib.parse.urlsplit(record["url"])
    name = "/%s-%s.nbtpkg" % (pkg_id, version)
    if (archive.scheme != "https" or archive.netloc != "github.com" or archive.query or archive.fragment
            or not archive.path.startswith(downloads)):
        raise Declined("\"record\": \"url\" is not a release of %s" % watched)
    if not archive.path.endswith(name):
        raise Declined("\"record\": \"url\" does not end in %s" % name[1:])
    if urllib.parse.unquote(archive.path[len(downloads):-len(name)]) != tag:
        raise Declined("\"record\": \"url\" is not in the release the record is in")
    if record["source"]["repo"] != watched:
        raise Declined("\"record\": \"source.repo\" is %s, not %s" % (shown(record["source"]["repo"]), watched))
    if prerelease:
        # The release says so, whether or not the file does.
        return dict(record, channel=check.PRERELEASE)
    if "channel" in record:
        raise Declined("\"record\" has \"channel\": \"%s\", and the release is not marked as a pre-release on GitHub"
                       % check.PRERELEASE)
    return record


def format_record(record):
    """The record as a file here, laid out as README.md shows one."""
    def text(value):
        return json.dumps(value, ensure_ascii=False)

    fields = [key for key in RECORD_FIELDS if key in record]
    lines = ["{"]
    for i, key in enumerate(fields):
        comma = "," if i < len(fields) - 1 else ""
        if key == "source":
            lines += ['  "source": {',
                      '    "repo": %s,' % text(record["source"]["repo"]),
                      '    "commit": %s' % text(record["source"]["commit"]),
                      "  }" + comma]
        else:
            lines.append("  %s: %s%s" % (text(key), text(record[key]), comma))
    return "\n".join(lines + ["}"]) + "\n"


def fenced(text):
    fence = "`" * max([3] + [len(run) + 1 for run in re.findall(r"`+", text)])
    return "%s\n%s\n%s" % (fence, text.rstrip("\n"), fence)


def pull_body(pkg_id, version, owner, repo, tag, record, before, output):
    prerelease = record.get("channel") == check.PRERELEASE
    release = "https://github.com/%s/%s/releases/tag/%s" % (owner, repo, urllib.parse.quote(tag, safe=""))
    changes = ("https://github.com/%s/%s/compare/%s...%s" % (owner, repo, before, record["source"]["commit"])
               if before else "https://github.com/%s/%s/commit/%s" % (owner, repo, record["source"]["commit"]))
    return "\n".join([
        "<!-- Opened by .github/workflows/propose.yml with tools/propose.py, not by the package's author. -->",
        "",
        "**Package:** `%s`" % pkg_id,
        "**This adds:** version %s%s" % (version, ", a pre-release" if prerelease else ""),
        "",
        "A bot proposed this pull request from the author's own release, %s. The release holds the archive and "
        "`%s`, and the record in that file is added here as `packages/%s/versions/%s.json`. The author did not "
        "open this pull request: the package's first pull request accepted the listing agreement for the versions "
        "proposed from its releases, as CONTRIBUTING.md says." % (release, RECORD_ASSET, pkg_id, version),
        "",
    ] + ([
        "The release is a pre-release on GitHub, or its version has a pre-release part, so the record has "
        "`\"channel\": \"%s\"`: the app offers this version only to those who ask for pre-releases."
        % check.PRERELEASE,
        "",
    ] if prerelease else []) + [
        "### What it does on a head unit",
        "",
        "Not written by the bot. The source's changes since the newest version here: %s" % changes,
        "",
        "### iDrive plugin ids",
        "",
        "Not written by the bot: see the archive's manifest.",
        "",
        "### Tested on",
        "",
        "Not written by the bot.",
        "",
        "### The check",
        "",
        "`tools/check.py` passed on this record before the pull request was opened: the archive was fetched from "
        "`url` and is the file `sha256` pins, `nbtpkg.py verify --key author.pub` took it, and `source.commit` "
        "was found in `source.repo`. The check on this pull request itself waits for a maintainer to approve its "
        "run.",
        "",
        fenced(output),
        "",
        "### Checklist",
        "",
        "- [ ] The listing agreement: accepted by the package's first pull request, for versions proposed from the "
        "author's releases.",
        "- [x] `nbtpkg.py verify <archive> --key packages/%s/author.pub` passes, and `sha256` is the SHA-256 it "
        "printed." % pkg_id,
        "- [ ] The archive is at `url`, will not be changed there, and `url` ends in `<id>-<version>.nbtpkg`.",
        "- [ ] `source` names a public repo and the commit the archive was built from.",
        "- [x] `author.pub` is unchanged.",
        "",
    ])


@contextlib.contextmanager
def worktree(root, start):
    """A worktree of its own at `start`, removed afterwards with whatever is in it."""
    with tempfile.TemporaryDirectory(prefix="propose-") as work:
        tree = os.path.join(work, "tree")
        git("worktree", "add", "-q", "--detach", tree, start, cwd=root)
        try:
            yield tree
        finally:
            git("worktree", "remove", "--force", tree, cwd=root)


def check_record(tree, start, pkg_id, version, record):
    """Commits the record in `tree`, a worktree at `start`, and checks it with tools/check.py as the check on a
    pull request does. Returns the check's output and its errors."""
    relative = "packages/%s/versions/%s.json" % (pkg_id, version)
    with open(os.path.join(tree, *relative.split("/")), "x", encoding="utf-8") as f:   # never over a record
        f.write(format_record(record))
    git("add", "--", relative, cwd=tree)
    git("-c", "user.name=" + BOT[0], "-c", "user.email=" + BOT[1], "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "%s %s, proposed from the author's release" % (pkg_id, version),
        "-m", "The record is the release's %s, as tools/propose.py checked it." % RECORD_ASSET, cwd=tree)
    if check.changes(tree, start) != [("A", relative)]:
        raise BotError("the commit is not the one new record %s" % relative)

    report = check.Report(io.StringIO())
    report.github = False   # the output goes into the pull request, not to the runner
    check.check(tree, start, False, report)
    output = report.out.getvalue() + "%d package(s): %d error(s), %d notice(s)\n" % (
        len(os.listdir(os.path.join(tree, "packages"))), len(report.errors), len(report.notices))
    return output, report.errors


def candidates_of(offers, here):
    """What there is to propose of `offers`, as offered() gives them, to a package with the records `here`, as
    records_here() gives them: ([offer], version or None). The offers are at most two, the stable one first: the
    newest stable version above the newest stable record, and the newest pre-release above every record and above
    that stable version, which would otherwise be listed no longer once both are in. No offer is of a version
    that has a record. The version is the newest that is a pre-release here and that a release now offers as
    stable: one for its author to promote."""
    def newest(versions):
        return max(versions, key=version_key) if versions else None

    def above(version, floor):
        return floor is None or version_key(version) > version_key(floor)

    # Build metadata is no part of a version's order, so 1.0.0+build.2 is the version 1.0.0 that is here.
    taken = {version_key(version) for version in here}
    stable_here = newest([version for version, record in here.items() if not is_prerelease_here(record)])
    stable = [o for o in offers if not o[3] and above(o[0], stable_here) and version_key(o[0]) not in taken]
    stable = max(stable, key=lambda o: version_key(o[0])) if stable else None
    floor = newest(list(here) + ([stable[0]] if stable else []))
    pre = [o for o in offers if o[3] and above(o[0], floor)]
    pre = max(pre, key=lambda o: version_key(o[0])) if pre else None
    promotable = newest([o[0] for o in offers if not o[3] and is_prerelease_here(here.get(o[0]))])
    return [o for o in (stable, pre) if o is not None], promotable


def propose_package(root, start, this_repo, pkg_id, token, dry_run, schema, out):
    """What becomes of one package's newest release. Raises BotError."""
    newest = newest_record(root, pkg_id)
    if newest is None:
        return Outcome(pkg_id, "none", "has no record that can be read, to watch a repo from")
    here, before = newest
    source = before.get("source") if isinstance(before.get("source"), dict) else {}
    watched = _WATCHED.fullmatch(source["repo"]) if isinstance(source.get("repo"), str) else None
    if watched is None or watched.group(2) in (".", ".."):
        return Outcome(pkg_id, "none", "its source.repo is not a repo on GitHub, so it is not watched")
    owner, repo = watched.groups()
    before_commit = source.get("commit") if _COMMIT.fullmatch(str(source.get("commit"))) else None

    try:
        releases = api("GET", "/repos/%s/%s/releases?per_page=30" % (owner, repo), token)
    except NotFound:
        return Outcome(pkg_id, "declined", "%s cannot be found on GitHub" % source["repo"])
    candidates, promotable = candidates_of(offered(pkg_id, releases), records_here(root, pkg_id))
    waiting = []
    for candidate in candidates:
        branch = "%s%s-%s" % (BRANCH_PREFIX, pkg_id, candidate[0])
        if not branch_exists(this_repo, branch, token):
            break
        waiting.append("%s was proposed before: the branch %s is there" % (candidate[0], branch))
    else:
        if waiting:
            return Outcome(pkg_id, "none", "; ".join(waiting))
        if promotable:
            return Outcome(pkg_id, "none", "%s is a pre-release here, and its release is not one on GitHub any more: "
                                           "the bot promotes nothing, a pull request that takes \"channel\" out of "
                                           "its record does" % promotable)
        return Outcome(pkg_id, "none", "no release of %s offers a version above %s" % (source["repo"], here))
    version, release, asset, prerelease = candidate

    try:
        record = release_record(pkg_id, version, owner, repo, release, asset, schema, prerelease)
    except Declined as e:
        return Outcome(pkg_id, "declined", "%s: %s" % (version, e))

    with worktree(root, start) as tree:
        output, errors = check_record(tree, start, pkg_id, version, record)
        if errors:
            return Outcome(pkg_id, "declined", "%s: tools/check.py fails on the record:\n%s" % (version, output))
        title = "%s %s%s" % (pkg_id, version, " (pre-release)" if prerelease else "")
        body = pull_body(pkg_id, version, owner, repo, release["tag_name"], record, before_commit, output)
        if dry_run:
            print("--- would push %s and open the pull request \"%s\" against %s, which adds "
                  "packages/%s/versions/%s.json:\n%s--- with the body:\n%s---"
                  % (branch, title, BASE_BRANCH, pkg_id, version, format_record(record), body), file=out)
            return Outcome(pkg_id, "would propose", "%s, on %s" % (version, branch))
        push(tree, this_repo, branch, token)
    try:
        url = open_pull(this_repo, token, title, branch, body)
    except BotError as e:
        raise BotError("%s is pushed, but the pull request could not be opened (delete the branch to have it "
                       "proposed again): %s" % (branch, e))
    return Outcome(pkg_id, "proposed", "%s: %s" % (version, url))


def annotation(kind, text):
    """A workflow command, with the text escaped so that it cannot end the command or start another."""
    text = "".join(c if c == "\n" or 0x20 <= ord(c) != 0x7f else "\\x%02x" % ord(c) for c in text)
    return "::%s::%s" % (kind, text.replace("%", "%25").replace("\n", "%0A"))


def run(root, this_repo, token=None, dry_run=False, out=sys.stdout):
    """Proposes what there is to propose. Returns ([Outcome], [(package id, why the bot failed)])."""
    start = git("rev-parse", "--verify", "HEAD^{commit}", cwd=root)
    schema = check.load_schema("version.schema.json")
    packages = os.path.join(root, "packages")
    outcomes, failures = [], []
    for pkg_id in sorted(os.listdir(packages)) if os.path.isdir(packages) else []:
        if not check._PACKAGE_ID.fullmatch(pkg_id) or not os.path.isdir(os.path.join(packages, pkg_id)):
            continue   # not a package: tools/check.py says so
        try:
            outcomes.append(propose_package(root, start, this_repo, pkg_id, token, dry_run, schema, out))
        except BotError as e:
            failures.append((pkg_id, str(e)))
    return outcomes, failures


def summarise(outcomes, failures, out=sys.stdout):
    """Prints what happened, with a warning for each release declined and an error for each failure, and adds it
    to the job's summary on GitHub."""
    github = os.environ.get("GITHUB_ACTIONS") == "true"
    lines = ["## Proposals", ""]
    for o in outcomes:
        line = "%s: %s: %s" % (o.pkg_id, o.kind, o.text)
        print(check.Report._clean(line) if github else line, file=out)   # on GitHub, a line of its own is a command
        if o.kind == "declined" and github:
            print(annotation("warning", "%s: declined: %s" % (o.pkg_id, o.text)), file=out)
        lines.append("- `%s`: %s" % (o.pkg_id, o.kind))
        lines.append(fenced(o.text) if "\n" in o.text or o.kind == "declined" else "  " + o.text)
    for pkg_id, why in failures:
        print(annotation("error", "%s: the bot failed: %s" % (pkg_id, why)) if github
              else "%s: the bot failed: %s" % (pkg_id, why), file=out)
        lines += ["- `%s`: the bot failed" % pkg_id, fenced(why)]
    if not outcomes and not failures:
        print("no packages", file=out)
        lines.append("No packages.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github and summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="propose.py", description="Propose new versions from their releases.")
    parser.add_argument("--nbtapps", required=True, help="the headunit/ folder of the NBTApps repo")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or THIS_REPO,
                        help="this repo on GitHub, as owner/name")
    parser.add_argument("--dry-run", action="store_true", help="push nothing and open no pull request")
    args = parser.parse_args(argv)
    if not _THIS_REPO.fullmatch(args.repo):
        sys.exit("propose: --repo is owner/name")
    token = os.environ.get("GITHUB_TOKEN") or None
    if token is None and not args.dry_run:
        sys.exit("propose: GITHUB_TOKEN is not set: a pull request needs it, and --dry-run does not")
    check.load_nbtpkg(os.path.abspath(args.nbtapps))
    try:
        outcomes, failures = run(REPO, args.repo, token, args.dry_run)
    except BotError as e:
        outcomes, failures = [], [("-", str(e))]
    summarise(outcomes, failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
