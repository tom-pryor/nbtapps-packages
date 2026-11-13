# NBTApps community packages

This repo is where developers add their own packages to the [NBTApps](https://github.com/tom-pryor/nbtapps)
catalogue, the list of packages the NBTApps app offers for BMW NBT Evo head units (iDrive 6). A package is added by
pull request. Once it is reviewed and merged, the official catalogue at `https://catalogue.nbtapps.com` lists it
as a **community** package, and the app installs it on a car.

The repo holds data only: a listing of each package and a record of each version. The archive itself, the
`.nbtpkg` file, stays where you publish it, such as a release of your own repo. The record pins its SHA-256, and
the app downloads it from your address.

## How a package gets in

1. You build the package with [`nbtpkg.py`](https://github.com/tom-pryor/nbtapps/blob/main/headunit/tools/nbtpkg.py)
   and sign it with a key of your own, whose public half is in this repo as `author.pub`.
2. You publish the archive at an https address and open a pull request that adds its record here.
3. A maintainer reviews the package: the source at the commit you name, the archive at your address, and what it
   does on a head unit. See [What is checked](#what-is-checked).
4. Once it passes, a maintainer signs the archive's `manifest.json` with the official package key and merges.
   That signature is published in the catalogue's index beside your version, not in your archive, which stays
   yours byte for byte.
5. The next publish of the catalogue lists the version.

The app and the head unit check the catalogue's signature over your manifest, and the manifest lists the SHA-256
of every file in the archive. So what installs is exactly the archive that was reviewed: a file changed at your
address is refused. Your own signature shows the review that the archive came from you.

A community package installs on a head unit whose NBTApps agent is version 1.2.0 or later. The app says when a
car's agent is too old, and offers the update.

## Layout

```
packages/<id>/listing.json               summary, author, category, description, homepage (optional)
packages/<id>/author.pub                 your Ed25519 public key, as `nbtpkg.py keygen` writes it
packages/<id>/icon.png                   optional, at most 256 KiB
packages/<id>/screenshots/*.png          optional, at most 2 MiB each
packages/<id>/versions/<version>.json    url, sha256, released, notes (optional), source
schema/                                  JSON Schemas for listing.json and a version's record
tools/check.py                           the check a pull request gets, which can be run before opening one
```

A package's name, size, `requires`, `depends`, risk flags and everything else about what it installs are not
written here. The catalogue reads them out of the archive, so they cannot differ from what installs.

### listing.json

```json
{
  "summary": "One line, at most 80 characters",
  "author": "Your name or handle",
  "category": "media",
  "description": "What it does and what it needs, at most 4000 characters. New lines are kept.",
  "homepage": "https://github.com/you/your-package"
}
```

`category` is a short lower-case word, such as `connectivity`, `media`, `navigation`, `system` or `utilities`.
[`schema/listing.schema.json`](schema/listing.schema.json) has every rule.

### versions/&lt;version&gt;.json

```json
{
  "url": "https://github.com/you/your-package/releases/download/v1.0.0/com.github.you.thing-1.0.0.nbtpkg",
  "sha256": "64 lower-case hex characters: the SHA-256 of the archive",
  "released": "2026-11-13",
  "notes": ["What is new, one line each"],
  "source": {
    "repo": "https://github.com/you/your-package",
    "commit": "the 40 hex characters of the commit the archive was built from"
  }
}
```

The file's name is the version, which is the manifest's `version`. The `url` ends in `<id>-<version>.nbtpkg`.
[`schema/version.schema.json`](schema/version.schema.json) has every rule.

## Rules

- **The id** is the folder's name and the manifest's `id`: two or more labels of `a-z` and `0-9` joined by dots,
  where a label may hold single hyphens. Use a reverse domain you control. On GitHub that can be
  `com.github.<your user>.<name>`. Ids under `org.nbtapp.` are the project's own and are not accepted here, and
  neither is an id the official catalogue already lists.
- **The archive** is a `.nbtpkg` that `nbtpkg.py verify --key author.pub` accepts, at an https address that serves
  it as it is, at most 256 MiB. Its manifest follows
  [`manifest.schema.json`](https://github.com/tom-pryor/nbtapps/blob/main/headunit/spec/manifest.schema.json).
- **A published version never changes.** A new archive needs a new version number and a new record. Deleting a
  record takes that version out of the catalogue.
- **The source** is a public repo, and `source.commit` is the commit the archive was built from.
- **Stock files.** A package does not replace a file of the head unit's own software.
- **iDrive plugins.** A package that installs into `${plugins}` asks for an HMI restart or a reboot, and its
  connector and focus ids are given to it by the catalogue, so that no two packages use the same one. The ids
  that are taken are listed at `https://catalogue.nbtapps.com/v1/hmi-ids.json`: choose others, and name the ids
  your plugin uses in the pull request.
- **Dependencies** are on packages the catalogue lists.
- **`author.pub`** stays the same from version to version. A pull request that changes it is treated as a change
  of owner: say why in it, and expect to be asked to show that you hold the old key.

## What is checked

Every pull request gets an automatic check, [`tools/check.py`](tools/check.py). It checks every package's files
against the schemas and the rules above, and for a version that the pull request adds, it fetches the archive
from `url`, wants the file `sha256` pins, runs `nbtpkg.py verify --key author.pub` on it, and looks for
`source.commit` in `source.repo`. A version that was already here and is changed fails. To run it first, with a
checkout of [NBTApps](https://github.com/tom-pryor/nbtapps) beside this repo:

```
python3 tools/check.py --nbtapps ../nbtapps/headunit
```

Passing the check is not being accepted. At review, the archive is fetched from your `url` and must be the file the record pins, signed by `author.pub`,
and must pass every check the catalogue's own packages pass. The source is read at `source.commit`, and the
package's install scripts and what it places on the head unit are looked at in particular. A package runs with
full access to a car's head unit. One that is not clear about what it does, or that a maintainer cannot follow,
is not accepted.

A version is only listed once it has passed. Merging is that decision, and it can be undone: a version or a
package found to be unsafe later is taken out of the catalogue.

## Building and signing

```
nbtpkg.py keygen you                 you.key (private: keep it safe and out of any repo) and you.pub
nbtpkg.py build DIR --key you.key    DIR -> <id>-<version>.nbtpkg, signed
nbtpkg.py verify <id>-<version>.nbtpkg --key you.pub
```

`verify` prints the archive's SHA-256 for the record. `nbtpkg.py --help` and the top of the file describe the
package format. It needs python3 and the `cryptography` module.

## Reporting a problem

A package that does something it should not, or a security problem in this repo, is reported privately, as
[SECURITY.md](https://github.com/tom-pryor/nbtapps/blob/main/SECURITY.md) says, not in an issue.

[CONTRIBUTING.md](CONTRIBUTING.md) has the agreement a pull request comes under and the steps, one by one.
