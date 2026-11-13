<!-- One package per pull request. README.md has the format of each file and the rules. -->

**Package:** `<id>` <!-- e.g. com.github.you.thing -->
**This adds:** <!-- a new package / version X.Y.Z / a listing change / takes version X.Y.Z out -->

### What it does on a head unit

<!-- What it installs and where, what its install and removal scripts do, what runs and when, what it needs
     (other packages, coding, hardware). A reviewer reads the source too, but starts here. -->

### iDrive plugin ids

<!-- If the package installs into ${plugins}: the connector and focus ids the plugin uses. Otherwise: none. -->

### Tested on

<!-- Car or bench, head unit, iDrive software version, agent version. -->

### Checklist

- [ ] I have read [CONTRIBUTING.md](https://github.com/tom-pryor/nbtapps-packages/blob/main/CONTRIBUTING.md) and accept its listing agreement.
- [ ] `nbtpkg.py verify <archive> --key packages/<id>/author.pub` passes, and `sha256` is the SHA-256 it printed.
- [ ] The archive is at `url`, will not be changed there, and `url` ends in `<id>-<version>.nbtpkg`.
- [ ] `source` names a public repo and the commit the archive was built from.
- [ ] `author.pub` is unchanged, or this pull request says why it changes.
