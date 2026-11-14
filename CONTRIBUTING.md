# Contributing

A pull request here adds a package to the NBTApps catalogue, adds a version of one, promotes a pre-release, or
changes a listing. Please read the agreement below first: opening a pull request means you accept it.

For a security problem, or a package that does something it should not, don't open a pull request or an issue:
report it privately as [SECURITY.md](https://github.com/tom-pryor/nbtapps/blob/main/SECURITY.md) says.

## Listing agreement (v1.0)

By opening a pull request on this repository you agree to the following for the package it names and everything
the pull request holds. By opening a package's first pull request you also agree to it for each later version of
that package that this repository's bot proposes from a release of the package's own repo, as README.md describes.

1. You give Tom Pryor a perpetual, worldwide, non-exclusive, royalty-free licence to publish what the pull request
   holds (the listing, its text, the icon and the screenshots) in the NBTApps catalogue, the NBTApps app and
   anything that shows the catalogue, and to change its form for that, such as to resize an image.
2. You allow the NBTApps catalogue to give the address of your archive and a signature over its manifest, and the
   NBTApps app and agent to download it from that address and install it on head units whose owners choose it.
3. You made the package, or you have the right to publish it and to give the above, including from anyone whose
   work is in it. Anything in it made by someone else is under a licence that allows this, and is named in your
   source.
4. The package does what its listing says and nothing it hides. You give it as is, without any warranty from the
   NBTApps project, which reviews it but does not make it.
5. Nothing obliges the project to list your package, or to keep listing it. A version or a package can be taken out
   of the catalogue at any time, and is when it is found to be unsafe.

You keep the copyright in your package and your listing. If you don't agree, please don't open a pull request.

## A new package

1. Make a key, once, and keep the private half safe and out of every repo:

   ```
   nbtpkg.py keygen you
   ```

2. Build and sign the package, and check it:

   ```
   nbtpkg.py build DIR --key you.key
   nbtpkg.py verify <id>-<version>.nbtpkg --key you.pub
   ```

3. Publish the archive at an https address that ends in `<id>-<version>.nbtpkg`, such as a release of the
   package's own repo, and do not change it afterwards.
4. Add `packages/<id>/` here:
   - `author.pub`: your `you.pub`, as it is
   - `listing.json`
   - `versions/<version>.json`, with the SHA-256 that `verify` printed
   - `icon.png` and `screenshots/`, if you have them
5. Run `python3 tools/check.py --nbtapps ../nbtapps/headunit` (see [What is checked](README.md#what-is-checked)),
   then open a pull request, and fill in its template. The same check runs on it.

[README.md](README.md) has the format of each file, and the rules.

## A new version

Publish the new archive at its own address, signed with the same key, and open a pull request that adds its
`versions/<version>.json`. Or let a bot open it from a release of the package's repo on GitHub, as
[A new version from your release](README.md#a-new-version-from-your-release) describes. A version that is listed
is never changed: a fix is a new version.

## A pre-release

A version for those who want to try what is not finished is published as any new version is, with one key more
in its record: `"channel": "prerelease"`. The app offers it only to those who ask for pre-releases. It is
reviewed and accepted as any version is, and a package may start with pre-releases alone. The bot proposes one
from a release that GitHub marks as a pre-release, and writes that key itself.

Choose its version for what comes next:

- a version with a pre-release part, such as `1.4.0-beta.1`, is always a pre-release. When it is done, the stable
  version is a new build with a new record, `1.4.0`;
- a plain version, such as `1.4.0`, can be promoted once it has been tried: the same archive becomes the stable
  version, and nothing is built again.

A pre-release that is not above the package's newest stable version is no longer listed, and its record may be
taken out.

## Promoting a pre-release

Open a pull request that takes the line `"channel": "prerelease",` out of the version's record and changes
nothing else, not even the notes. The check names it as a promotion and fetches nothing: the archive is the one
that was accepted, and it needs no second review. A stable version's dependencies are met by stable versions,
so promote what it depends on first. A stable version is never made a pre-release again; one that turns out bad
is taken out. The bot does not open this pull request: taking the pre-release mark off a release on GitHub
changes nothing here.

## Taking a version out

Open a pull request that deletes its record, and say why. Head units that have it keep it until they update.

## Review

A maintainer reviews every pull request. It may take time, and you may be asked questions or for changes. The
pull request is merged once the package passes and its versions are signed for the catalogue. They are listed
from the catalogue's next publish.
