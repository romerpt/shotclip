#!/usr/bin/env python3
"""ShotClip - screenshot and text clipboard history for GNOME on Wayland (Win+V style).

Wayland does not let background apps read the clipboard, so instead of polling
it ShotClip watches the GNOME Screenshots folder for images, and receives copied
text from its companion GNOME Shell extension over D-Bus.

Usage:
  shotclip              show the picker (starts the instance if needed)
  shotclip --daemon     start hidden and stay resident
  shotclip --quit       stop the running instance
  shotclip --copy-latest  (debug) copy the newest item to the clipboard

In the picker, Enter/click/1-9 copy and paste into the previous app (set
"auto_paste": false in the config to only copy); Shift+Enter only copies;
Ctrl+Enter pastes text with its formatting (HTML; only apps that accept HTML,
like LibreOffice, browsers and mail, can paste it). Copies made in apps listed in
"exclude_apps" are never recorded (the journal shows each copy's app ids).

Logs: journalctl --user -t shotclip
"""

import gettext
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "io.github.shotclip"
APP_NAME = "ShotClip"

# D-Bus object exported by the ShotClip GNOME Shell extension (extension/).
SHELL_BUS_NAME = "org.gnome.Shell"
SHELL_HELPER_PATH = "/io/github/shotclip/Shell"
SHELL_HELPER_IFACE = "io.github.shotclip.Shell"

DATA_DIR = os.path.join(GLib.get_user_data_dir(), "shotclip")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "shotclip")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {"max_items": 50, "watch_dirs": [], "import_existing": 20,
                  "capture_text": True, "auto_paste": True, "exclude_apps": [],
                  "layout": "list"}
PICKER_LAYOUTS = ("list", "compact", "tiles")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")  # screenshot files to pick up
# Known translations of GNOME Shell's "Screenshots" folder, used as fallbacks
# when the gettext lookup does not match what is on disk.
SCREENSHOT_DIR_FALLBACKS = (
    "Screenshots",
    "Capturas de ecrã",
    "Capturas de tela",
    "Capturas de pantalla",
    "Captures d’écran",
    "Bildschirmfotos",
    "Schermate",
    "Schermafbeeldingen",
    "Zrzuty ekranu",
)

THUMB_W, THUMB_H = 560, 360  # sharp enough for HiDPI list previews
SETTLE_MS = 400  # wait after a file event before reading the file
MAX_TEXT_CHARS = 100_000  # larger copies are not kept in the history
MAX_HTML_CHARS = 500_000  # formatting of larger copies is dropped (text kept)
MAX_IMAGE_BYTES = 32 * 1024 * 1024  # larger copied images are not kept
# GNOME's screenshot tool both saves a file and copies the image; a copied
# image and a new screenshot file with the same size this close together are
# the same capture, so only one is kept (the file).
SCREENSHOT_MERGE_S = 10
IMAGE_EXT_FOR_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                      "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tiff"}
MIME_FOR_IMAGE_EXT = {ext: mime for mime, ext in IMAGE_EXT_FOR_MIME.items()} | {".jpeg": "image/jpeg"}


def log(*args):
    if os.environ.get("JOURNAL_STREAM"):  # stderr goes to the journal
        print(*args, file=sys.stderr, flush=True)
    else:
        print(f"[shotclip {time.strftime('%H:%M:%S')}]", *args, file=sys.stderr, flush=True)


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha1_of_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_image_name(name):
    return name.lower().endswith(IMAGE_EXTS) and not name.startswith(".")


def decode_html(data):
    """text/html as copied: UTF-8, or UTF-16 with a BOM (some browsers)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


# --------------------------------------------------------------------------
# Config / folders
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        atomic_write_json(CONFIG_FILE, cfg)
    except (OSError, ValueError) as e:
        log(f"config error ({e}); using defaults")
    try:
        cfg["max_items"] = max(1, int(cfg["max_items"]))
        cfg["import_existing"] = max(0, int(cfg["import_existing"]))
    except (TypeError, ValueError):
        cfg["max_items"] = DEFAULT_CONFIG["max_items"]
        cfg["import_existing"] = DEFAULT_CONFIG["import_existing"]
    if not isinstance(cfg["watch_dirs"], list):
        cfg["watch_dirs"] = []
    if not isinstance(cfg["exclude_apps"], list):
        cfg["exclude_apps"] = []
    cfg["exclude_apps"] = [str(a).casefold().removesuffix(".desktop") for a in cfg["exclude_apps"]]
    if cfg["layout"] not in PICKER_LAYOUTS:
        cfg["layout"] = DEFAULT_CONFIG["layout"]
    return cfg


def detect_screenshot_dirs(cfg):
    if cfg["watch_dirs"]:
        return [os.path.expanduser(d) for d in cfg["watch_dirs"]]

    pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    if not pictures:
        pictures = os.path.expanduser("~/Pictures")
    localized = gettext.dgettext("gnome-shell", "Screenshots")
    names = [localized] + [n for n in SCREENSHOT_DIR_FALLBACKS if n != localized]

    found = []
    for name in names:
        path = os.path.join(pictures, name)
        if os.path.isdir(path) and path not in found:
            found.append(path)
    if not found:
        # GNOME Shell creates exactly this folder on the first screenshot.
        path = os.path.join(pictures, localized)
        os.makedirs(path, exist_ok=True)
        found.append(path)
    return found


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

class History:
    """Index of stored items.

    Image items: kind="image", sha1, file, source, added, pinned, width, height.
    Text items:  kind="text", sha1, text, added, pinned, html (optional
                 formatting as copied).
    """

    def __init__(self, max_items):
        self.max_items = max_items
        self.items = []
        self.listeners = []
        self.lock = threading.Lock()
        os.makedirs(IMAGES_DIR, exist_ok=True)

    @property
    def exists_on_disk(self):
        return os.path.exists(HISTORY_FILE)

    def load(self):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", []) if isinstance(data, dict) else []
        except FileNotFoundError:
            items = []
        except (OSError, ValueError) as e:
            log(f"history.json unreadable ({e}); starting empty")
            items = []
        self.items = [i for i in items
                      if (i.get("kind") == "text" and isinstance(i.get("text"), str))
                      or os.path.exists(i.get("file", ""))]
        for i in self.items:
            i.setdefault("kind", "image")

    def save(self):
        atomic_write_json(HISTORY_FILE, {"version": 1, "items": self.items})

    def changed(self):
        self.save()
        for cb in list(self.listeners):
            cb()

    def ordered(self):
        pinned = [i for i in self.items if i.get("pinned")]
        rest = [i for i in self.items if not i.get("pinned")]
        key = lambda i: i.get("added", 0)  # noqa: E731
        return sorted(pinned, key=key, reverse=True) + sorted(rest, key=key, reverse=True)

    def get(self, sha):
        return next((i for i in self.items if i["sha1"] == sha), None)

    def _recent_twin(self, sha, width, height, source_kind):
        """An image of the same size, from the other kind of source (file vs
        clipboard), stored in the last SCREENSHOT_MERGE_S seconds."""
        now = time.time()
        for i in self.items:
            if (i["kind"] == "image" and i["sha1"] != sha
                    and (i.get("source") == "clipboard") == (source_kind == "clipboard")
                    and (i.get("width"), i.get("height")) == (width, height)
                    and now - i.get("added", 0) < SCREENSHOT_MERGE_S):
                return i
        return None

    def _store_image(self, sha, ext, width, height, source, added, write):
        dest = os.path.join(IMAGES_DIR, sha + ext)
        if not os.path.exists(dest):
            tmp = dest + ".tmp"
            write(tmp)
            os.replace(tmp, dest)
        item = self.get(sha)
        if item:
            item["added"] = max(item.get("added", 0), added)
            if source != "clipboard":
                item["source"] = source
        else:
            item = {
                "kind": "image", "sha1": sha, "file": dest, "source": source,
                "added": added, "pinned": False, "width": width, "height": height,
            }
            self.items.append(item)
        self.trim()
        return item

    def add_file(self, src, added=None, notify=True):
        """Copy src into the store. Returns the item, or None if not an image."""
        info = GdkPixbuf.Pixbuf.get_file_info(src)
        if info is None or info[0] is None:
            return None
        width, height = info[1], info[2]
        sha = sha1_of_file(src)
        if added is None:
            # The same screenshot may already be here from the clipboard.
            twin = self._recent_twin(sha, width, height, "clipboard")
            if twin and not twin.get("pinned"):
                self._drop(twin)
        ext = os.path.splitext(src)[1].lower() or ".png"
        item = self._store_image(sha, ext, width, height, src,
                                 added if added is not None else time.time(),
                                 lambda tmp: shutil.copyfile(src, tmp))
        if notify:
            self.changed()
        return item

    def add_image_bytes(self, data, mime):
        """Store an image copied in any app. Returns the item, or None if it
        is ignored (not decodable, too large, or a screenshot already kept)."""
        ext = IMAGE_EXT_FOR_MIME.get(mime)
        if not ext or not data or len(data) > MAX_IMAGE_BYTES:
            return None
        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(data)
            loader.close()
        except GLib.Error:
            return None
        pb = loader.get_pixbuf()
        if pb is None:
            return None
        sha = hashlib.sha1(data).hexdigest()
        if not self.get(sha) and self._recent_twin(sha, pb.get_width(), pb.get_height(), "file"):
            return None  # GNOME screenshot: the saved file is already in the history

        def write(tmp):
            with open(tmp, "wb") as f:
                f.write(data)
        item = self._store_image(sha, ext, pb.get_width(), pb.get_height(), "clipboard",
                                 time.time(), write)
        self.changed()
        return item

    def clear_unpinned(self):
        for item in [i for i in self.items if not i.get("pinned")]:
            self._drop(item)
        self.changed()

    def add_text(self, text, html=None):
        """Store a copied text (with its HTML formatting, if any). Returns the
        item, or None if it is ignored."""
        if not text or not text.strip() or len(text) > MAX_TEXT_CHARS:
            return None
        if html and len(html) > MAX_HTML_CHARS:
            html = None
        sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
        item = self.get(sha)
        if item:
            item["added"] = time.time()
        else:
            item = {"kind": "text", "sha1": sha, "text": text,
                    "added": time.time(), "pinned": False}
            self.items.append(item)
        if html:
            item["html"] = html
        self.trim()
        self.changed()
        return item

    def trim(self):
        rest = sorted((i for i in self.items if not i.get("pinned")),
                      key=lambda i: i.get("added", 0), reverse=True)
        for old in rest[self.max_items:]:
            self._drop(old)

    def _drop(self, item):
        self.items.remove(item)
        if item.get("file"):
            try:
                os.unlink(item["file"])
            except OSError:
                pass

    def remove(self, sha):
        item = self.get(sha)
        if item:
            self._drop(item)
            self.changed()

    def toggle_pin(self, sha):
        item = self.get(sha)
        if item:
            item["pinned"] = not item.get("pinned")
            self.trim()
            self.changed()


# --------------------------------------------------------------------------
# Thumbnails (decoded off the main thread, cached as textures)
# --------------------------------------------------------------------------

class ThumbCache:
    def __init__(self):
        self.textures = {}
        self.pending = {}  # sha -> list of callbacks

    @staticmethod
    def _texture_from_pixbuf(pb):
        fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        return Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt,
                                     pb.read_pixel_bytes(), pb.get_rowstride())

    def request(self, item, callback):
        sha = item["sha1"]
        if sha in self.textures:
            callback(self.textures[sha])
            return
        if sha in self.pending:
            self.pending[sha].append(callback)
            return
        self.pending[sha] = [callback]
        threading.Thread(target=self._load, args=(sha, item["file"]), daemon=True).start()

    def _load(self, sha, path):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, THUMB_W, THUMB_H, True)
        except GLib.Error as e:
            log(f"thumbnail failed for {path}: {e.message}")
            pb = None
        GLib.idle_add(self._done, sha, pb)

    def _done(self, sha, pb):
        tex = self._texture_from_pixbuf(pb) if pb else None
        if tex:
            self.textures[sha] = tex
        for cb in self.pending.pop(sha, []):
            cb(tex)
        return GLib.SOURCE_REMOVE

    def prune(self, keep_shas):
        for sha in list(self.textures):
            if sha not in keep_shas:
                del self.textures[sha]


# --------------------------------------------------------------------------
# Picker window
# --------------------------------------------------------------------------

CSS = """
/* shared */
.shotclip-list, .shotclip-compact, .shotclip-tiles { background: none; }
.shotclip-key {
  font-size: 0.75em; font-weight: 700; min-width: 18px; min-height: 18px;
  padding: 0 4px; border-radius: 5px;
  background: alpha(currentColor, 0.08);
  box-shadow: inset 0 -1px alpha(currentColor, 0.12);
}
row:selected .shotclip-key, flowboxchild:selected .shotclip-key {
  background: @accent_bg_color; color: @accent_fg_color; box-shadow: none;
}
.shotclip-meta { font-size: 0.8em; opacity: 0.65; }
.shotclip-kind { opacity: 0.55; }
.shotclip-link { color: @accent_color; }
.shotclip-thumb { border-radius: 8px; background: alpha(currentColor, 0.06); }
.shotclip-tag {
  font-size: 0.72em; font-weight: 600; padding: 1px 6px; border-radius: 6px;
  background: alpha(currentColor, 0.08); opacity: 0.75;
}
.shotclip-actions { opacity: 0; transition: opacity 120ms ease-out; }
row:hover .shotclip-actions, row:selected .shotclip-actions,
flowboxchild:hover .shotclip-actions, flowboxchild:selected .shotclip-actions { opacity: 1; }
.shotclip-actions button { min-width: 26px; min-height: 26px; padding: 0; }
.shotclip-actions button.shotclip-on { color: @accent_color; }
.shotclip-section { font-size: 0.82em; font-weight: 700; opacity: 0.55;
  margin: 10px 4px 6px 4px; }
.shotclip-hints { padding: 6px 12px 8px 12px; }
.shotclip-hints label { font-size: 0.78em; }
.shotclip-hints .shotclip-key { font-size: 0.8em; }
.shotclip-hints .dim-label { margin-right: 8px; }

/* list layout: one card per item */
.shotclip-list > row {
  padding: 0; margin: 0 0 6px 0; border-radius: 12px;
  background: none; outline: none; box-shadow: none;
}
.shotclip-list > row:hover, .shotclip-list > row:selected { background: none; }
.shotclip-card {
  padding: 10px 12px 8px 12px; border-radius: 12px;
  background: @card_bg_color;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.07), 0 1px 2px alpha(black, 0.05);
  transition: background 120ms ease-out, box-shadow 120ms ease-out;
}
.shotclip-list > row:hover .shotclip-card { background: mix(@card_bg_color, currentColor, 0.04); }
.shotclip-list > row:selected .shotclip-card {
  background: mix(@card_bg_color, @accent_bg_color, 0.08);
  box-shadow: 0 0 0 2px @accent_bg_color;
}
.shotclip-text { font-size: 0.95em; line-height: 1.3; }

/* compact layout: one line per item */
.shotclip-compact > row {
  min-height: 40px; padding: 0 8px; margin: 0 0 2px 0; border-radius: 8px;
  outline: none; background: none;
}
.shotclip-compact > row:hover { background: alpha(currentColor, 0.05); }
.shotclip-compact > row:selected {
  background: alpha(@accent_bg_color, 0.10);
  box-shadow: inset 0 0 0 2px @accent_bg_color;
}
.shotclip-compact .shotclip-thumb { border-radius: 4px; }
.shotclip-compact-icon { min-width: 40px; }

/* tiles layout: a grid */
.shotclip-tiles > flowboxchild {
  padding: 0; border-radius: 12px; background: none; outline: none;
}
.shotclip-tile {
  border-radius: 12px; background: @card_bg_color;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.08);
}
.shotclip-tiles > flowboxchild:hover .shotclip-tile {
  background: mix(@card_bg_color, currentColor, 0.03);
}
.shotclip-tiles > flowboxchild:selected .shotclip-tile {
  box-shadow: 0 0 0 2px @accent_bg_color;
}
.shotclip-tile .shotclip-thumb { border-radius: 12px 12px 0 0; }
.shotclip-tile-text { padding: 12px 14px; /* 24px of TILE_PREVIEW_H */ font-size: 0.95em; line-height: 1.35; }
.shotclip-tile-footer {
  padding: 4px 6px 4px 12px; min-height: 30px;
  border-top: 1px solid alpha(currentColor, 0.07);
}
"""

LAYOUTS = (("list", "List"), ("compact", "Compact rows"), ("tiles", "Tiles"))
PREVIEW_H = 112  # height of an image preview in the list layout
TILE_PREVIEW_H = 132
TILE_COLUMNS = 3
WINDOW_SIZES = {"list": (440, 600), "compact": (440, 600), "tiles": (760, 600)}


def format_time(ts, short=False):
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    delta = now - dt
    if timedelta(0) <= delta < timedelta(minutes=1):
        return "Now" if short else "Just now"
    if timedelta(0) <= delta < timedelta(hours=1):
        mins = int(delta.total_seconds() // 60)
        return f"{mins} min" if short else f"{mins} min ago"
    today = now.date()
    if dt.date() == today:
        return f"{dt:%H:%M}" if short else f"Today {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return "Yesterday" if short else f"Yesterday {dt:%H:%M}"
    if short:
        return f"{dt:%d %b}" if dt.year == today.year else f"{dt:%b %Y}"
    return f"{dt:%d %b %Y %H:%M}" if dt.year != today.year else f"{dt:%d %b %H:%M}"


def is_url(text):
    t = text.strip()
    return "\n" not in t and " " not in t and t.startswith(("http://", "https://", "www."))


def text_preview(text, max_lines=3, limit=400):
    """The first lines of a copied text, without blank lines and indentation."""
    lines = [ln.strip() for ln in text[:limit * 2].splitlines() if ln.strip()]
    preview = "\n".join(lines[:max_lines])[:limit]
    return preview + ("…" if len(lines) > max_lines else "")


def one_line(text, limit=200):
    return "  ".join(ln.strip() for ln in text[:limit * 2].splitlines() if ln.strip())[:limit]


class Thumbnail(Gtk.Picture):
    """A picture of a fixed height (and, optionally, width) that fills its
    box (cropped), instead of growing with the image's aspect ratio."""

    def __init__(self, height, width=0, **kwargs):
        super().__init__(content_fit=Gtk.ContentFit.COVER, can_shrink=True,
                         overflow=Gtk.Overflow.HIDDEN, css_classes=["shotclip-thumb"],
                         **kwargs)
        self.fixed_h, self.fixed_w = height, width

    def do_measure(self, orientation, _for_size):
        size = self.fixed_h if orientation == Gtk.Orientation.VERTICAL else self.fixed_w
        return size, size, -1, -1


class FixedLabel(Gtk.Label):
    """A label of a fixed height that never asks for more width than it gets
    (long copies would otherwise make every tile in the grid tall)."""

    def __init__(self, height, **kwargs):
        super().__init__(**kwargs)
        self.fixed_h = height

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return self.fixed_h, self.fixed_h, -1, -1
        return 0, 0, -1, -1


def keycap(text):
    return Gtk.Label(label=text, css_classes=["shotclip-key"],
                     valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)


class PickerWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.history = app.history
        self.layout = app.config.get("layout", "list")
        self.was_active = False
        self.closing = False
        self.order = []
        self.has_pinned = False
        self.filter = "all"
        self.set_default_size(*WINDOW_SIZES[self.layout])
        self.set_icon_name("edit-paste")

        if self.layout == "tiles":
            self.container = Gtk.FlowBox(
                valign=Gtk.Align.START, homogeneous=True,
                selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                min_children_per_line=TILE_COLUMNS, max_children_per_line=TILE_COLUMNS,
                row_spacing=12, column_spacing=12, css_classes=["shotclip-tiles"])
            self.container.connect("child-activated", self._on_activated)
            margins = dict(margin_top=4, margin_bottom=14, margin_start=14, margin_end=14)
        else:
            self.container = Gtk.ListBox(
                selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                valign=Gtk.Align.START,
                css_classes=["shotclip-compact" if self.layout == "compact" else "shotclip-list"])
            self.container.connect("row-activated", self._on_activated)
            self.container.set_header_func(self._section_header)
            margins = (dict(margin_top=0, margin_bottom=8, margin_start=8, margin_end=8)
                       if self.layout == "compact" else
                       dict(margin_top=4, margin_bottom=8, margin_start=12, margin_end=12))

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           vexpand=True)
        clamp = Adw.Clamp(maximum_size=1200 if self.layout == "tiles" else 720,
                          child=self.container, **margins)
        self.scroller.set_child(clamp)

        self.empty = Adw.StatusPage(
            icon_name="edit-paste-symbolic", title="Nothing copied yet",
            description="Copy some text or take a screenshot, and it shows up here.",
            css_classes=["compact"],
        )
        self.no_match = Adw.StatusPage(
            icon_name="system-search-symbolic", title="No matches",
            description="Try a different word, or press Esc to see everything.",
            css_classes=["compact"])
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=120)
        self.stack.add_named(self.scroller, "list")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.no_match, "no-match")

        header = Adw.HeaderBar(css_classes=["flat"])
        self.subtitle = Adw.WindowTitle(title="Clipboard")
        if self.layout == "tiles":
            header.set_title_widget(self._make_filters())
        else:
            header.set_title_widget(self.subtitle)
        search_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                         tooltip_text="Search (Ctrl+F)")
        header.pack_start(search_toggle)
        prefs = Gtk.Button(icon_name="emblem-system-symbolic",
                           tooltip_text="Preferences (Ctrl+,)")
        prefs.connect("clicked", lambda *_: self._show_preferences())
        header.pack_end(prefs)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic",
                           tooltip_text="Clear unpinned items (Ctrl+Shift+Del)")
        clear.connect("clicked", lambda *_: self._confirm_clear())
        header.pack_end(clear)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search text and file names",
                                            hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self.refresh())
        self.search_bar = Gtk.SearchBar(child=self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        search_toggle.bind_property(
            "active", self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        if self.layout == "compact":
            # Compact rows keep the search field in view; Ctrl+F focuses it.
            self.search_bar.set_search_mode(True)

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        view.add_top_bar(self.search_bar)
        view.add_bottom_bar(self._make_hints())
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        self._listener = self.refresh
        self.history.listeners.append(self._listener)
        self.refresh()

    # -- building -----------------------------------------------------------

    @staticmethod
    def _make_hints():
        box = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER, css_classes=["shotclip-hints"])
        for key, what in (("↵", "Paste"), ("⇧↵", "Copy"), ("1–9", "Quick paste"),
                          ("P", "Pin"), ("Del", "Remove")):
            box.append(keycap(key))
            box.append(Gtk.Label(label=what, css_classes=["dim-label"]))
        box.set_tooltip_text(
            "Enter or click: paste\nShift+Enter: copy only\n1–9: paste that item\n"
            "Ctrl+Enter: paste with formatting\nP: pin or unpin\nDel: remove\n"
            "Ctrl+F: search\nCtrl+,: preferences\nCtrl+Shift+Del: clear unpinned\nEsc: close")
        return box

    def _make_filters(self):
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        first = None
        for key, label in (("all", "All"), ("image", "Images"), ("text", "Text"),
                           ("pinned", "Pinned")):
            btn = Gtk.ToggleButton(label=label, can_focus=False, active=key == "all")
            if first:
                btn.set_group(first)
            else:
                first = btn
            btn.connect("toggled", self._on_filter_toggled, key)
            box.append(btn)
        return box

    def _on_filter_toggled(self, btn, key):
        if btn.get_active() and self.filter != key:
            self.filter = key
            self.refresh()

    def refresh(self):
        selected = self._selected_sha()
        self.container.remove_all()
        everything = self.history.ordered()
        query = self.search_entry.get_text().strip().casefold()
        self.order = [i for i in everything if self._matches(i, query)]
        n = len(everything)
        if query or self.filter != "all":
            self.subtitle.set_subtitle(f"{len(self.order)} of {n} items")
        else:
            self.subtitle.set_subtitle("1 item" if n == 1 else f"{n} items")
        page = "list" if self.order else ("no-match" if everything else "empty")
        self.stack.set_visible_child_name(page)
        self.has_pinned = any(i.get("pinned") for i in self.order)
        make = {"list": self._make_card, "compact": self._make_compact_row,
                "tiles": self._make_tile}[self.layout]
        target = None
        for idx, item in enumerate(self.order):
            child = make(item, idx)
            self.container.append(child)
            if item["sha1"] == selected:
                target = child
        if self.order:
            target = target or self._child(0)
            self._select(target)
            if not self._search_has_focus():
                target.grab_focus()

    def _section_header(self, row, before):
        if not self.has_pinned:
            row.set_header(None)
            return
        title = None
        if before is None:
            title = "Pinned" if row.pinned else "Recent"
        elif before.pinned and not row.pinned:
            title = "Recent"
        row.set_header(Gtk.Label(label=title, xalign=0, css_classes=["shotclip-section"])
                       if title else None)

    def _matches(self, item, query):
        if self.filter == "pinned" and not item.get("pinned"):
            return False
        if self.filter in ("image", "text") and item["kind"] != self.filter:
            return False
        if not query:
            return True
        if item["kind"] == "text":
            return query in item["text"].casefold()
        name = os.path.basename(item.get("source", ""))
        return query in f"{name} image screenshot".casefold()

    def _actions(self, item):
        sha = item["sha1"]
        pinned = bool(item.get("pinned"))
        actions = Gtk.Box(spacing=2, css_classes=["shotclip-actions"], valign=Gtk.Align.CENTER)
        pin = Gtk.Button(
            icon_name="view-pin-symbolic", focus_on_click=False, can_focus=False,
            tooltip_text="Unpin (P)" if pinned else "Pin (P)",
            css_classes=["flat", "circular"] + (["shotclip-on"] if pinned else []))
        pin.connect("clicked", lambda *_: self.history.toggle_pin(sha))
        rm = Gtk.Button(icon_name="user-trash-symbolic", focus_on_click=False,
                        can_focus=False, tooltip_text="Remove (Del)",
                        css_classes=["flat", "circular"])
        rm.connect("clicked", lambda *_: self.history.remove(sha))
        actions.append(pin)
        actions.append(rm)
        return actions

    @staticmethod
    def _kind_icon(item):
        if item["kind"] != "text":
            return "image-x-generic-symbolic"
        return "insert-link-symbolic" if is_url(item["text"]) else "text-x-generic-symbolic"

    def _finish(self, child, item, idx):
        child.sha1 = item["sha1"]
        child.pinned = bool(item.get("pinned"))
        if item["kind"] == "text":
            child.set_tooltip_text(item["text"][:500])
        else:
            source = item.get("source", "")
            origin = "Copied image" if source == "clipboard" else os.path.basename(source)
            child.set_tooltip_text(
                f"{origin}\n{item.get('width', '?')} × {item.get('height', '?')}")
        return child

    def _thumb(self, item, height, width=0, **kwargs):
        pic = Thumbnail(height, width, **kwargs)
        self.app.thumbs.request(item, lambda tex, p=pic: tex and p.set_paintable(tex))
        return pic

    def _make_card(self, item, idx):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       css_classes=["shotclip-card"])
        if item["kind"] == "text":
            link = is_url(item["text"])
            card.append(Gtk.Label(
                label=text_preview(item["text"]), wrap=True, lines=1 if link else 3,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                ellipsize=Pango.EllipsizeMode.MIDDLE if link else Pango.EllipsizeMode.END,
                xalign=0, yalign=0, max_width_chars=1, hexpand=True,
                css_classes=["shotclip-text"] + (["shotclip-link"] if link else []),
            ))
            meta = format_time(item.get("added", 0))
        else:
            card.append(self._thumb(item, PREVIEW_H, hexpand=True))
            meta = f"{format_time(item.get('added', 0))}   " \
                   f"{item.get('width', '?')} × {item.get('height', '?')}"

        footer = Gtk.Box(spacing=6)
        footer.append(Gtk.Image(icon_name=self._kind_icon(item), pixel_size=14,
                                css_classes=["shotclip-kind"]))
        footer.append(Gtk.Label(label=meta, xalign=0, hexpand=True,
                                ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["shotclip-meta"]))
        if item.get("html"):
            footer.append(Gtk.Label(label="Formatted", valign=Gtk.Align.CENTER,
                                    tooltip_text="Ctrl+Enter pastes it with formatting",
                                    css_classes=["shotclip-tag"]))
        footer.append(self._actions(item))
        if idx < 9:
            footer.append(keycap(str(idx + 1)))
        card.append(footer)
        return self._finish(Gtk.ListBoxRow(child=card), item, idx)

    def _make_compact_row(self, item, idx):
        box = Gtk.Box(spacing=10)
        if item["kind"] == "text":
            box.append(Gtk.Image(icon_name=self._kind_icon(item), pixel_size=16,
                                 css_classes=["shotclip-kind", "shotclip-compact-icon"]))
            link = is_url(item["text"])
            text = one_line(item["text"])
            classes = ["shotclip-link"] if link else []
        else:
            box.append(self._thumb(item, 26, 40, valign=Gtk.Align.CENTER))
            source = item.get("source", "")
            name = "Copied image" if source == "clipboard" else "Screenshot"
            text = f"{name}  {item.get('width', '?')} × {item.get('height', '?')}"
            link, classes = False, ["dim-label"]
        box.append(Gtk.Label(
            label=text, xalign=0, hexpand=True, max_width_chars=1, css_classes=classes,
            ellipsize=Pango.EllipsizeMode.MIDDLE if link else Pango.EllipsizeMode.END))
        box.append(self._actions(item))
        box.append(Gtk.Label(label=format_time(item.get("added", 0), short=True),
                             css_classes=["shotclip-meta"]))
        key = keycap(str(idx + 1) if idx < 9 else "")
        key.set_opacity(1 if idx < 9 else 0)
        box.append(key)
        return self._finish(Gtk.ListBoxRow(child=box), item, idx)

    def _make_tile(self, item, idx):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["shotclip-tile"],
                       overflow=Gtk.Overflow.HIDDEN)
        if item["kind"] == "text":
            link = is_url(item["text"])
            tile.append(FixedLabel(
                TILE_PREVIEW_H - 24, label=text_preview(item["text"], max_lines=6), wrap=True,
                lines=5, wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                xalign=0, yalign=0, hexpand=True,
                css_classes=["shotclip-tile-text"] + (["shotclip-link"] if link else [])))
        else:
            tile.append(self._thumb(item, TILE_PREVIEW_H, hexpand=True))
        footer = Gtk.Box(spacing=6, css_classes=["shotclip-tile-footer"])
        meta = "Pinned" if item.get("pinned") else format_time(item.get("added", 0))
        footer.append(Gtk.Label(label=meta, xalign=0, hexpand=True,
                                ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["shotclip-meta"]))
        footer.append(self._actions(item))
        if idx < 9:
            footer.append(keycap(str(idx + 1)))
        tile.append(footer)
        return self._finish(Gtk.FlowBoxChild(child=tile), item, idx)

    # -- container helpers (list box or flow box) ---------------------------

    def _child(self, idx):
        if self.layout == "tiles":
            return self.container.get_child_at_index(idx)
        return self.container.get_row_at_index(idx)

    def _select(self, child):
        if self.layout == "tiles":
            self.container.select_child(child)
        else:
            self.container.select_row(child)

    def _selected_child(self):
        if not hasattr(self, "container"):
            return None
        if self.layout == "tiles":
            sel = self.container.get_selected_children()
            return sel[0] if sel else None
        return self.container.get_selected_row()

    # -- helpers ------------------------------------------------------------

    def _selected_sha(self):
        child = self._selected_child()
        return child.sha1 if child else None

    def _search_has_focus(self):
        focus = self.get_focus()
        return focus is not None and (focus is self.search_entry
                                      or focus.is_ancestor(self.search_entry))

    def _confirm_clear(self):
        if not any(not i.get("pinned") for i in self.history.items):
            return
        dialog = Adw.AlertDialog(heading="Clear history?",
                                 body="All unpinned items are removed. Pinned items stay.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "clear" and self.history.clear_unpinned())
        dialog.present(self)

    def _show_preferences(self):
        PreferencesDialog(self.app).present(self)

    def _copy(self, sha, paste=True, formatted=False):
        item = self.history.get(sha)
        if not item:
            return
        if not self.app.copy_item(item, formatted=formatted):
            self.toasts.add_toast(Adw.Toast(
                title="Can't copy: the ShotClip shell extension isn't loaded or is "
                      "out of date. Log out and back in to finish installing.", timeout=6))
            return
        self.close()
        if paste and self.app.config.get("auto_paste", True):
            self.app.request_paste()

    def _scroll_to(self, child):
        ok, rect = child.compute_bounds(self.container)
        if not ok:
            return
        adj = self.scroller.get_vadjustment()
        top, bottom = rect.origin.y, rect.origin.y + rect.size.height
        if top < adj.get_value():
            adj.set_value(max(0, top - 8))
        elif bottom > adj.get_value() + adj.get_page_size():
            adj.set_value(bottom - adj.get_page_size() + 8)

    def _move(self, name, focus=True):
        if not self.order:
            return
        child = self._selected_child()
        idx = child.get_index() if child else 0
        last = len(self.order) - 1
        if self.layout == "tiles":
            cols, page = TILE_COLUMNS, TILE_COLUMNS * 3
            steps = {"Left": -1, "Right": 1, "Up": -cols, "Down": cols,
                     "Page_Up": -page, "Page_Down": page}
        else:
            steps = {"Left": -1, "Right": 1, "Up": -1, "Down": 1,
                     "Page_Up": -5, "Page_Down": 5}
        if name == "Home":
            idx = 0
        elif name == "End":
            idx = last
        elif self.layout == "tiles" and name in ("Left", "Right", "Up", "Down"):
            new = idx + steps[name]
            if 0 <= new <= last:
                idx = new
        else:
            idx = min(last, max(0, idx + steps[name]))
        child = self._child(idx)
        self._select(child)
        if focus:
            child.grab_focus()
        self._scroll_to(child)

    # -- events -------------------------------------------------------------

    def _on_activated(self, _container, child):
        self._copy(child.sha1)

    def _on_key(self, _ctrl, keyval, _code, state):
        if self.get_visible_dialog():
            return False  # let the Clear and Preferences dialogs handle their keys
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and name in ("f", "F"):
            if self.layout == "compact":
                self.search_entry.grab_focus()
            else:
                self.search_bar.set_search_mode(not self.search_bar.get_search_mode())
            return True
        if ctrl and name == "comma":
            self._show_preferences()
            return True
        if ctrl and state & Gdk.ModifierType.SHIFT_MASK and name in ("Delete", "KP_Delete"):
            self._confirm_clear()
            return True
        if ctrl and name in ("Return", "KP_Enter", "ISO_Enter"):
            sha = self._selected_sha()
            if sha:
                self._copy(sha, formatted=True)
            return True
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        searching = self._search_has_focus()
        if name == "Escape":
            if self.layout == "compact" and (searching or self.search_entry.get_text()):
                self.search_entry.set_text("")
                self._move("Home")
            elif self.layout != "compact" and self.search_bar.get_search_mode():
                self.search_bar.set_search_mode(False)  # also clears the query
                self._move("Home")
            else:
                self.close()
            return True
        if searching:
            # Typing goes to the search field; only these keys act on the items.
            if name in ("Up", "Down", "KP_Up", "KP_Down", "Page_Up", "Page_Down"):
                self._move(name.removeprefix("KP_"), focus=False)
                return True
            if name not in ("Return", "KP_Enter", "ISO_Enter"):
                return False
        if name in ("Return", "KP_Enter", "ISO_Enter"):
            sha = self._selected_sha()
            if sha:
                # Shift+Enter: copy only, the user pastes with Ctrl+V.
                shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
                self._copy(sha, paste=not shift)
            return True
        name = name.removeprefix("KP_")
        if name in ("Left", "Right", "Up", "Down", "Home", "End", "Page_Up", "Page_Down"):
            self._move(name)
            return True
        if len(name) == 1 and name in "123456789":
            idx = int(name) - 1
            if idx < len(self.order):
                self._copy(self.order[idx]["sha1"])
            return True
        if name in ("p", "P"):
            sha = self._selected_sha()
            if sha:
                self.history.toggle_pin(sha)
            return True
        if name in ("Delete", "KP_Delete"):
            sha = self._selected_sha()
            if sha:
                self.history.remove(sha)
            return True
        return False

    def _on_active_changed(self, *_):
        if self.is_active():
            self.was_active = True
        elif self.was_active and not self.closing:
            # Like Win+V: dismiss as soon as the user clicks elsewhere.
            self.close()

    def _on_close_request(self, *_):
        self.detach()
        return False

    def detach(self):
        self.closing = True
        if self._listener in self.history.listeners:
            self.history.listeners.remove(self._listener)
        if self.app.window is self:
            self.app.window = None


class PreferencesDialog(Adw.PreferencesDialog):
    """Edits config.json; the picker reopens in the new layout on close."""

    def __init__(self, app):
        super().__init__(title="Preferences", search_enabled=False)
        self.app = app
        self.cfg = dict(app.config)
        self.start_layout = self.cfg.get("layout", "list")
        page = Adw.PreferencesPage()
        self.add(page)

        look = Adw.PreferencesGroup(title="Appearance")
        names = Gtk.StringList.new([label for _key, label in LAYOUTS])
        self.layout_row = Adw.ComboRow(
            title="Layout", subtitle="How items are shown in the picker", model=names,
            selected=[k for k, _l in LAYOUTS].index(self.start_layout))
        self.layout_row.connect("notify::selected", self._on_layout)
        look.add(self.layout_row)
        page.add(look)

        hist = Adw.PreferencesGroup(title="History")
        keep = Adw.SpinRow.new_with_range(1, 1000, 1)
        keep.set_title("Items to keep")
        keep.set_subtitle("Pinned items don't count")
        keep.set_value(self.cfg["max_items"])
        keep.connect("notify::value",
                     lambda r, _p: self._set("max_items", int(r.get_value())))
        hist.add(keep)
        hist.add(self._switch("capture_text", "Keep copied text",
                              "Off: only screenshots and images are kept"))
        page.add(hist)

        pick = Adw.PreferencesGroup(title="Picking an item")
        pick.add(self._switch("auto_paste", "Paste right away",
                              "Off: the item is only copied, paste it with Ctrl+V"))
        page.add(pick)

        self.excl = Adw.PreferencesGroup(
            title="Never record copies from",
            description="App ids, as shown in the log (journalctl --user -t shotclip)")
        self.add_entry = Adw.EntryRow(title="Add an app id, then press Enter",
                                      show_apply_button=True)
        self.add_entry.connect("apply", self._on_add_app)
        self.excl.add(self.add_entry)
        self.app_rows = []
        self._fill_apps()
        page.add(self.excl)

        folders = Adw.PreferencesGroup(title="Screenshot folders")
        for d in getattr(app, "watch_dirs", []):
            folders.add(Adw.ActionRow(title=d.replace(os.path.expanduser("~"), "~", 1),
                                      subtitle="Set watch_dirs in config.json to change"))
        page.add(folders)

        self.connect("closed", self._on_closed)

    def _switch(self, key, title, subtitle):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.cfg[key]))
        row.connect("notify::active", lambda r, _p: self._set(key, r.get_active()))
        return row

    def _fill_apps(self):
        for row in self.app_rows:
            self.excl.remove(row)
        self.app_rows = []
        for app_id in self.cfg["exclude_apps"]:
            row = Adw.ActionRow(title=app_id)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=f"Stop excluding {app_id}", css_classes=["flat"])
            rm.connect("clicked", lambda _b, a=app_id: self._set(
                "exclude_apps", [x for x in self.cfg["exclude_apps"] if x != a]) or self._fill_apps())
            row.add_suffix(rm)
            self.excl.add(row)
            self.app_rows.append(row)

    def _on_add_app(self, entry):
        app_id = entry.get_text().strip().casefold().removesuffix(".desktop")
        entry.set_text("")
        if app_id and app_id not in self.cfg["exclude_apps"]:
            self._set("exclude_apps", self.cfg["exclude_apps"] + [app_id])
            self._fill_apps()

    def _on_layout(self, row, _pspec):
        self._set("layout", LAYOUTS[row.get_selected()][0])

    def _set(self, key, value):
        self.cfg[key] = value
        self.app.save_config(self.cfg)

    def _on_closed(self, *_):
        before = len(self.app.history.items)
        self.app.history.trim()
        if len(self.app.history.items) != before:
            self.app.history.changed()
        if self.app.config.get("layout") != self.start_layout and self.app.window:
            GLib.idle_add(lambda: self.app.show_picker() and False)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class ShotClipApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.running = False
        self.window = None
        self.monitors = []
        self.pending_files = {}
        self.history = None
        self.thumbs = ThumbCache()
        self.last_copied_sha = None
        self.echo_guard_until = 0.0

    # -- lifecycle ----------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        # Called over D-Bus (org.gtk.Actions.Activate) by the ShotClip GNOME
        # Shell extension, the only thing allowed to see clipboard changes.
        clip_action = Gio.SimpleAction.new("add-clip", GLib.VariantType.new("a{sv}"))
        clip_action.connect("activate", self._on_add_clip)
        self.add_action(clip_action)
        # Sent by extension version <= 4, still loaded until the next login.
        text_action = Gio.SimpleAction.new("add-text", GLib.VariantType.new("s"))
        text_action.connect("activate", lambda _a, p: self._add_clip(text=p.get_string()))
        self.add_action(text_action)
        image_action = Gio.SimpleAction.new("add-image", GLib.VariantType.new("(say)"))
        image_action.connect("activate", lambda _a, p: self._add_clip(
            mime=p.get_child_value(0).get_string(),
            image=p.get_child_value(1).get_data_as_bytes().get_data()))
        self.add_action(image_action)

    def ensure_running(self):
        """Start watching and keep the process alive (it must stay alive to
        keep owning the clipboard on Wayland)."""
        if self.running:
            return
        self.running = True
        self.hold()
        if os.environ.get("XDG_SESSION_TYPE") not in (None, "", "wayland"):
            log(f"warning: session type is {os.environ.get('XDG_SESSION_TYPE')}, "
                "ShotClip targets Wayland")
        self.config = load_config()
        self.config_mtime = self._config_mtime()
        self.history = History(self.config["max_items"])
        first_run = not self.history.exists_on_disk
        self.history.load()
        self.history.listeners.append(self._on_history_changed)
        self.watch_dirs = detect_screenshot_dirs(self.config)
        if first_run:
            self._import_existing(self.config["import_existing"])
        self.history.trim()
        self.history.save()
        for d in self.watch_dirs:
            self._watch(d)
        log(f"started (pid {os.getpid()}), {len(self.history.items)} item(s) in history")
        # Warm the thumbnail cache so the first picker opens instantly.
        for item in self.history.ordered():
            if item["kind"] == "image":
                self.thumbs.request(item, lambda _t: None)

    def do_command_line(self, cmdline):
        # An exception escaping this vfunc leaves the remote caller hanging.
        try:
            return self._handle_command_line(cmdline)
        except Exception as e:  # noqa: BLE001
            log(f"error handling command line: {e!r}")
            cmdline.printerr_literal(f"shotclip: error: {e}\n")
            return 1

    def _handle_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--help" in args or "-h" in args:
            cmdline.print_literal(__doc__.split("\n", 2)[2])
            return 0
        if "--quit" in args:
            log("quit requested")
            if self.window:
                self.window.close()
            self.quit()
            return 0
        unknown = [a for a in args if a not in ("--daemon", "--copy-latest")]
        if unknown:
            cmdline.printerr_literal(f"shotclip: unknown option(s): {' '.join(unknown)}\n")
            return 2

        self.ensure_running()
        if "--daemon" in args:
            return 0
        if "--copy-latest" in args:
            if not self.history.items:
                cmdline.printerr_literal("shotclip: history is empty\n")
                return 1
            latest = max(self.history.items, key=lambda i: i.get("added", 0))
            if not self.copy_item(latest):
                cmdline.printerr_literal("shotclip: copy failed; is the shell extension loaded?\n")
                return 1
            cmdline.print_literal(f"copied {latest['sha1'][:10]}\n")
            return 0
        self.show_picker()
        return 0

    # -- watching -----------------------------------------------------------

    def _import_existing(self, count):
        if count <= 0:
            return
        files = []
        for d in self.watch_dirs:
            try:
                for name in os.listdir(d):
                    path = os.path.join(d, name)
                    if is_image_name(name) and os.path.isfile(path):
                        files.append((os.path.getmtime(path), path))
            except OSError as e:
                log(f"cannot list {d}: {e}")
        files.sort(reverse=True)
        imported = 0
        for mtime, path in reversed(files[:count]):
            try:
                if self.history.add_file(path, added=mtime, notify=False):
                    imported += 1
            except OSError as e:
                log(f"import failed for {path}: {e}")
        log(f"first run: imported {imported} existing screenshot(s)")

    def _watch(self, path):
        gfile = Gio.File.new_for_path(path)
        try:
            mon = gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as e:
            log(f"cannot watch {path}: {e.message}")
            return
        mon.connect("changed", self._on_file_event)
        self.monitors.append(mon)
        log(f"watching {path}")

    def _on_file_event(self, _mon, file, other, event):
        E = Gio.FileMonitorEvent
        if event in (E.CHANGES_DONE_HINT, E.MOVED_IN):
            target = file
        elif event == E.RENAMED:
            target = other  # renamed within the folder, e.g. tmp -> final name
        else:
            return
        path = target.get_path() if target else None
        if not path or not is_image_name(os.path.basename(path)):
            return
        # Debounce: several events can arrive for one file.
        old = self.pending_files.pop(path, None)
        if old:
            GLib.source_remove(old)
        self.pending_files[path] = GLib.timeout_add(SETTLE_MS, self._ingest, path)

    def _ingest(self, path):
        self.pending_files.pop(path, None)
        if not os.path.isfile(path):
            return GLib.SOURCE_REMOVE
        try:
            item = self.history.add_file(path)
        except OSError as e:
            log(f"failed to add {path}: {e}")
            return GLib.SOURCE_REMOVE
        if item:
            log(f"added {os.path.basename(path)} -> {item['sha1'][:10]}")
            self.thumbs.request(item, lambda _t: None)
        else:
            log(f"ignored (not a readable image): {path}")
        return GLib.SOURCE_REMOVE

    def _on_add_clip(self, _action, param):
        clip = param.unpack()
        html = clip.get("html")
        self._add_clip(text=clip.get("text"), html=bytes(html) if html else None,
                       image=bytes(clip["image"]) if "image" in clip else None,
                       mime=clip.get("mime"), apps=clip.get("apps", []))

    def _add_clip(self, text=None, html=None, image=None, mime=None, apps=()):
        if not self.running:
            return
        self._reload_config_if_changed()
        excluded = set(self.config["exclude_apps"])
        if excluded & {a.casefold() for a in apps}:
            log(f"ignored copy from excluded app ({', '.join(apps)})")
            return
        source = f" from {', '.join(apps)}" if apps else ""
        if text is not None:
            if not self.config.get("capture_text", True):
                return
            if self._is_echo(hashlib.sha1(text.encode("utf-8")).hexdigest()):
                return
            item = self.history.add_text(text, decode_html(html) if html else None)
            if item:
                fmt = " with formatting" if item.get("html") else ""
                log(f"added text ({len(text)} chars{fmt}){source} -> {item['sha1'][:10]}")
        elif image is not None:
            if self._is_echo(hashlib.sha1(image).hexdigest()):
                return
            item = self.history.add_image_bytes(image, mime)
            if item:
                log(f"added copied image {item['width']}x{item['height']}{source}"
                    f" -> {item['sha1'][:10]}")
                self.thumbs.request(item, lambda _t: None)

    def _is_echo(self, sha):
        # While ShotClip replaces the clipboard, GNOME can briefly restore the
        # previous content; don't let that bump an old item to the top.
        return time.monotonic() < self.echo_guard_until and sha != self.last_copied_sha

    @staticmethod
    def _config_mtime():
        try:
            return os.stat(CONFIG_FILE).st_mtime
        except OSError:
            return None

    def _reload_config_if_changed(self):
        """Pick up edits to config.json (e.g. exclude_apps) without a restart."""
        mtime = self._config_mtime()
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            self.config = load_config()
            self.history.max_items = self.config["max_items"]
            log("config reloaded")

    def save_config(self, cfg):
        """Write cfg (from the Preferences dialog) and apply it right away."""
        atomic_write_json(CONFIG_FILE, {k: cfg[k] for k in DEFAULT_CONFIG})
        self.config_mtime = self._config_mtime()
        self.config = load_config()
        # Not trimmed yet: the Preferences dialog trims once it closes, so
        # stepping the number down doesn't delete items along the way.
        self.history.max_items = self.config["max_items"]

    def _on_history_changed(self):
        self.thumbs.prune({i["sha1"] for i in self.history.items})

    # -- clipboard / picker -------------------------------------------------

    def copy_item(self, item, formatted=False):
        """Put item on the clipboard through the shell extension. Returns
        False if the extension isn't available (e.g. not loaded yet)."""
        self.last_copied_sha = item["sha1"]
        self.echo_guard_until = time.monotonic() + 1.0
        try:
            self._copy_via_shell(item, formatted)
        except (GLib.Error, OSError) as e:
            log(f"copy failed: {getattr(e, 'message', e)}")
            return False
        log(f"copied {item['sha1'][:10]}{' with formatting' if formatted else ''}")
        return True

    def request_paste(self):
        """Ask the shell extension to paste into the app that regains focus
        once the picker has closed."""
        def done(bus, res):
            try:
                bus.call_finish(res)
            except GLib.Error as e:
                log(f"paste request failed: {e.message}")
        bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call(SHELL_BUS_NAME, SHELL_HELPER_PATH, SHELL_HELPER_IFACE, "Paste",
                 None, None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None, done)

    def _copy_via_shell(self, item, formatted=False):
        """Ask the ShotClip GNOME Shell extension to own the clipboard.

        A background client's selection on GNOME Wayland can end up serving
        stale data to apps; a selection owned by the shell itself (like
        Clipboard Indicator uses) pastes reliably and needs no focus.
        """
        if item["kind"] == "text" and item.get("html") and formatted:
            # HTML only (the shell can offer one format per copy), so this is
            # opt-in: apps without HTML support would paste nothing.
            method = "SetHtml"
            params = GLib.Variant("(ay)", (item["html"].encode("utf-8"),))
        elif item["kind"] == "text":
            method, params = "SetText", GLib.Variant("(s)", (item["text"],))
        else:
            with open(item["file"], "rb") as f:
                data = f.read()
            ext = os.path.splitext(item["file"])[1].lower()
            mime = MIME_FOR_IMAGE_EXT.get(ext, "image/png")
            params = GLib.Variant("(say)", (mime, data))
            method = "SetContent"
        bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call_sync(SHELL_BUS_NAME, SHELL_HELPER_PATH, SHELL_HELPER_IFACE, method,
                      params, None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None)

    def show_picker(self):
        # Recreate the window every time: a freshly mapped window is much more
        # likely to receive focus on Wayland than a re-presented hidden one.
        if self.window:
            old = self.window
            old.detach()
            old.destroy()
        self.window = PickerWindow(self)
        self.window.present()


def main():
    Adw.init()
    app = ShotClipApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
