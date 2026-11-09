# Publishing ShotClip

How to publish ShotClip on the two official channels, and how to ship an
update later. All commands run from the repository folder.

| Channel | What users get | Built by |
|---|---|---|
| [extensions.gnome.org](https://extensions.gnome.org) | The shell extension, installed from the browser or Extension Manager | `gnome-extensions pack` |
| Launchpad PPA `ppa:romerpt/shotclip` | The whole app with `apt`, with automatic updates | `make-ppa.sh` |
| GitHub Releases | The `.deb` and the single-file installer | `make-deb.sh`, `make-installer.sh` |

## 1. extensions.gnome.org

The extension only works together with the ShotClip app, so the listing says
so and links to this repository. Users who install the .deb or the PPA
package already have the extension; the listing is for people who find
ShotClip there first.

### First upload

1. Log in at <https://extensions.gnome.org> (create an account if needed).
2. Build the zip:
   ```bash
   gnome-extensions pack extension/ --out-dir dist --force
   ```
   This creates `dist/shotclip@shotclip.github.io.shell-extension.zip`.
3. Go to <https://extensions.gnome.org/upload/>, choose the zip, accept the
   license terms (GPL-2.0-or-later compatible; MIT is fine) and submit.
4. Wait for review, usually a few days. A reviewer may leave comments on the
   extension's page; answer them there, fix, and upload again.

### What reviewers check, and where this extension stands

| Rule | Status |
|---|---|
| Nothing created or connected until `enable()`; everything undone in `disable()` | Done: signal, virtual keyboard, D-Bus object and timers are all cleaned up |
| No work at import time | Done |
| No excessive logging | Done: the extension doesn't log |
| `metadata.json` has `uuid`, `name`, `description`, `shell-version`, `url` | Done |
| Description says what it does and what else it needs | Done: names the ShotClip app and explains the clipboard access |
| No bundled binaries or obfuscated code | Done |
| Clipboard access and simulated key presses are justified | Explained in the description: needed to paste on Wayland, data stays local, secrets are skipped |

Expect questions about two things: sending clipboard contents over D-Bus
(answer: only to the local ShotClip app, `NO_AUTO_START`, nothing leaves the
computer) and the virtual keyboard (answer: it presses Shift+Insert once,
only right after the user picks an item, like Clipboard Indicator does).

### Updates

Raise `version-name` in `extension/metadata.json`, pack again and upload the
new zip on the same page. extensions.gnome.org numbers versions itself; the
`version` field in the file is only used by `install.sh`.

## 2. Launchpad PPA

### One-time setup

1. **Tools**
   ```bash
   sudo apt install dpkg-dev devscripts dput gnupg
   ```
2. **Launchpad account**: sign in at <https://launchpad.net> with the same
   e-mail as `debian/changelog` (`vitor.aos@gmail.com`).
3. **GPG key**, used to sign uploads:
   ```bash
   gpg --full-generate-key            # RSA 4096, your name and vitor.aos@gmail.com
   gpg --list-secret-keys --keyid-format long
   gpg --keyserver keyserver.ubuntu.com --send-keys <KEY-ID>
   ```
   Then on Launchpad: your profile › **OpenPGP keys** › paste the
   fingerprint (`gpg --fingerprint`). Launchpad e-mails you an encrypted
   message; decrypt it (`gpg --decrypt`, paste the text) and open the link.
4. **Create the PPA**: your profile › **Create a new PPA**. Use the name
   `shotclip` and the display name "ShotClip".

### Upload

```bash
./make-ppa.sh --upload
```

This exports the last commit, builds and signs the source package
(`dist/ppa/`), and uploads it. Launchpad e-mails you when the upload is
accepted and again when the package is built (minutes to an hour). Follow
it at <https://launchpad.net/~romerpt/+archive/ubuntu/shotclip>.

To check that it builds without signing or uploading:
`./make-ppa.sh --unsigned`.

### Other Ubuntu releases

`debian/changelog` targets Ubuntu 26.04 (`resolute`). The package is the
same for every release, so for others (25.10 `questing`, and later ones)
don't upload again: on the PPA page, choose **View package details › Copy
packages**, select `shotclip`, pick the destination series and **Copy
existing binaries**.

ShotClip needs GNOME Shell 48 or later, so don't copy to releases older
than 25.04.

### Updates

1. Raise `VERSION` in `shotclip.py`.
2. Add a changelog entry with the same version:
   ```bash
   dch -v 1.1.0 "Describe the change"
   dch -r ""                          # marks it released for the series
   ```
3. Commit, then `./make-ppa.sh --upload`.

### After the first build: tell users

Add this to the top of the README's **Install** section:

````markdown
### From the PPA (Ubuntu, recommended)

```bash
sudo add-apt-repository ppa:romerpt/shotclip
sudo apt install shotclip
```

Updates arrive with your normal system updates.
````

## 3. GitHub release

```bash
./make-deb.sh && ./make-installer.sh
gh release create v<version> --title "ShotClip <version>" --generate-notes \
    dist/shotclip_<version>_all.deb dist/shotclip-installer.sh \
    dist/shotclip@shotclip.github.io.shell-extension.zip
```

## Release checklist

1. `VERSION` in `shotclip.py`, a new `debian/changelog` entry and
   `version-name` in `extension/metadata.json` all say the same version.
2. Commit and push.
3. `./make-ppa.sh --upload`, then copy to the other series once it's built.
4. Upload the extension zip to extensions.gnome.org (only if `extension/`
   changed).
5. Create the GitHub release.
