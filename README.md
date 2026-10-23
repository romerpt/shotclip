# ShotClip

Clipboard history for GNOME on Wayland, like Windows' Win+V. It keeps your
screenshots, the images and text you copy, and pastes the item you pick into
the app you were using.

## Requirements

- GNOME Shell 48–50 on Wayland (e.g. Ubuntu 26.04)
- `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0` (the installer
  offers to install missing ones with `sudo apt`)

## Install

```bash
bash shotclip-installer.sh
```

Then **log out and back in once**: GNOME only loads the ShotClip shell
extension at login on Wayland.

To remove it (your history is kept in `~/.local/share/shotclip`):

```bash
bash shotclip-installer.sh --uninstall
```

## Use

Press **Super+V**.

| Key | Action |
|---|---|
| Enter / click / 1–9 | Copy the item and paste it into the previous app |
| Shift+Enter | Copy only (paste yourself with Ctrl+V) |
| Ctrl+Enter | Paste with formatting (items marked "formatted"; for apps that accept HTML such as LibreOffice, browsers, mail) |
| Arrows, Home, End | Move the selection |
| P | Pin / unpin (pinned items are never removed) |
| Del | Remove the item |
| Ctrl+F | Search text and file names (↑/↓ and Enter work while typing) |
| Ctrl+Shift+Del | Clear all unpinned items (asks first) |
| Esc, or click elsewhere | Close (closes the search first if it is open) |

New screenshots (Print Screen) appear automatically, as do text and images
copied in any app. Text copied with formatting (web pages, LibreOffice, mail)
keeps it: Enter pastes the plain text (works everywhere), Ctrl+Enter pastes the
formatting into apps that accept HTML. When
a copy offers both text and an image (e.g. a spreadsheet range), the text is
kept. Copies that password managers mark as secret are skipped, as are copies
made in apps listed in `exclude_apps`.

## What the installer changes

- Installs `~/.local/bin/shotclip`, a desktop entry, an autostart entry, and
  the GNOME Shell extension `shotclip@shotclip.github.io`.
- Moves GNOME's "show notification list" shortcut from Super+V to **Super+M**
  only, and binds Super+V to ShotClip.
- Uninstalling reverts all of this.

## Settings

`~/.config/shotclip/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `max_items` | 50 | Unpinned items to keep |
| `watch_dirs` | `[]` | Screenshot folders; empty = auto-detect |
| `import_existing` | 20 | Screenshots imported on first run |
| `capture_text` | `true` | Record copied text |
| `auto_paste` | `true` | Paste after picking (otherwise copy only) |
| `exclude_apps` | `[]` | Apps whose copies are never recorded, e.g. `["org.keepassxc.KeePassXC", "1password"]` |

`exclude_apps` and the other settings apply as soon as the file is saved
(`max_items` on the next copy). To find an app's id, copy something in it and
look at the log: each entry says `from <ids>`; any of those ids works.

Logs: `journalctl --user -t shotclip`

## Privacy

History is stored unencrypted in `~/.local/share/shotclip/` (text inside
`history.json`, screenshots in `images/`). Pin nothing sensitive, or delete
items with Del.
