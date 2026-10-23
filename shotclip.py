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
"auto_paste": false in the config to only copy); Shift+Enter only copies.
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
                  "capture_text": True, "auto_paste": True}

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

THUMB_W, THUMB_H = 320, 200
SETTLE_MS = 400  # wait after a file event before reading the file
MAX_TEXT_CHARS = 100_000  # larger copies are not kept in the history
MAX_IMAGE_BYTES = 32 * 1024 * 1024  # larger copied images are not kept
# GNOME's screenshot tool both saves a file and copies the image; a copied
# image and a new screenshot file with the same size this close together are
# the same capture, so only one is kept (the file).
SCREENSHOT_MERGE_S = 10
IMAGE_EXT_FOR_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                      "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tiff"}
MIME_FOR_IMAGE_EXT = {ext: mime for mime, ext in IMAGE_EXT_FOR_MIME.items()} | {".jpeg": "image/jpeg"}


def log(*args):
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


def clipboard_provider(item):
    """Content provider for a history item (what gdk_clipboard_set_texture()
    and set_text() do in C; neither is exposed to Python).

    The GValue type must be exactly GdkTexture / gchararray: GTK looks up its
    serializers by that type, and PyGObject would otherwise type the value as
    the concrete subclass (GdkMemoryTexture), which offers no formats at all.
    """
    if item["kind"] == "text":
        value = GObject.Value(GObject.TYPE_STRING, item["text"])
    else:
        texture = Gdk.Texture.new_from_filename(item["file"])
        value = GObject.Value(Gdk.Texture, texture)
    return Gdk.ContentProvider.new_for_value(value)


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
    Text items:  kind="text", sha1, text, added, pinned.
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

    def add_text(self, text):
        """Store a copied text. Returns the item, or None if it is ignored."""
        if not text or not text.strip() or len(text) > MAX_TEXT_CHARS:
            return None
        sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
        item = self.get(sha)
        if item:
            item["added"] = time.time()
        else:
            item = {"kind": "text", "sha1": sha, "text": text,
                    "added": time.time(), "pinned": False}
            self.items.append(item)
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
.shotclip-card { padding: 6px; border-radius: 12px; }
.shotclip-thumb { border-radius: 8px; background: alpha(currentColor, 0.06); }
.shotclip-badge {
  min-width: 20px; padding: 0 6px; border-radius: 10px; margin: 6px;
  font-weight: bold; font-size: 0.85em;
  background: alpha(black, 0.6); color: white;
}
.shotclip-actions { margin: 4px; }
.shotclip-actions button { min-width: 28px; min-height: 28px; padding: 0;
  background: alpha(black, 0.55); color: white; }
.shotclip-actions button:hover { background: alpha(black, 0.75); }
.shotclip-pinned { color: @accent_color; }
.shotclip-text { padding: 10px 12px; padding-right: 72px; font-size: 0.9em; }
"""


def format_time(ts):
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if dt.date() == today:
        return f"Today {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"Yesterday {dt:%H:%M}"
    return f"{dt:%d %b %Y %H:%M}" if dt.year != today.year else f"{dt:%d %b %H:%M}"


class PickerWindow(Adw.ApplicationWindow):
    def __init__(self, app, auto_copy=False):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.auto_copy = auto_copy
        self.history = app.history
        self.was_active = False
        self.closing = False
        self.set_default_size(760, 540)
        self.set_icon_name("edit-paste")

        self.flow = Gtk.FlowBox(
            valign=Gtk.Align.START, homogeneous=True,
            selection_mode=Gtk.SelectionMode.SINGLE,
            activate_on_single_click=True, max_children_per_line=6,
            min_children_per_line=2, row_spacing=6, column_spacing=6,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        self.flow.connect("child-activated", self._on_child_activated)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.flow)
        # Keep the keyboard selection scrolled into view.
        self.flow.set_vadjustment(scroller.get_vadjustment())

        self.empty = Adw.StatusPage(
            icon_name="edit-paste-symbolic", title="Nothing here yet",
            description="Take a screenshot (Print Screen) or copy some text.",
        )
        self.no_match = Adw.StatusPage(icon_name="system-search-symbolic",
                                       title="No matches")
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "grid")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.no_match, "no-match")

        header = Adw.HeaderBar()
        self.subtitle = Adw.WindowTitle(title="Clipboard history")
        header.set_title_widget(self.subtitle)
        search_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                         tooltip_text="Search (Ctrl+F)")
        header.pack_start(search_toggle)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic",
                           tooltip_text="Clear all unpinned (Ctrl+Shift+Del)")
        clear.connect("clicked", lambda *_: self._confirm_clear())
        header.pack_end(clear)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search text and file names",
                                            hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self.refresh())
        self.search_bar = Gtk.SearchBar(child=self.search_entry, show_close_button=True)
        self.search_bar.connect_entry(self.search_entry)
        search_toggle.bind_property(
            "active", self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        hint = Gtk.Label(
            label="Enter/click paste · Shift+Enter copy only · 1–9 quick paste · "
                  "P pin · Del remove · Ctrl+F search · Esc close",
            css_classes=["dim-label", "caption"], margin_top=6, margin_bottom=8,
            wrap=True, justify=Gtk.Justification.CENTER,
        )

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        view.add_top_bar(self.search_bar)
        view.add_bottom_bar(hint)
        self.set_content(view)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        self._listener = self.refresh
        self.history.listeners.append(self._listener)
        self.refresh()

    # -- building -----------------------------------------------------------

    def refresh(self):
        selected = self._selected_sha()
        self.flow.remove_all()
        everything = self.history.ordered()
        query = self.search_entry.get_text().strip().casefold()
        self.order = [i for i in everything if self._matches(i, query)]
        if query:
            self.subtitle.set_subtitle(f"{len(self.order)} of {len(everything)} item(s)")
        else:
            self.subtitle.set_subtitle(f"{len(everything)} item(s)")
        page = "grid" if self.order else ("no-match" if everything else "empty")
        self.stack.set_visible_child_name(page)
        target = None
        for idx, item in enumerate(self.order):
            child = self._make_card(item, idx)
            self.flow.append(child)
            if item["sha1"] == selected:
                target = child
        if self.order:
            target = target or self.flow.get_child_at_index(0)
            self.flow.select_child(target)
            if not self._search_has_focus():
                target.grab_focus()

    @staticmethod
    def _matches(item, query):
        if not query:
            return True
        if item["kind"] == "text":
            return query in item["text"].casefold()
        name = os.path.basename(item.get("source", ""))
        return query in f"{name} image screenshot".casefold()

    def _make_card(self, item, idx):
        sha = item["sha1"]
        if item["kind"] == "text":
            preview = Gtk.Label(
                label=item["text"][:400].strip(), wrap=True, lines=5,
                wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                xalign=0, yalign=0, max_width_chars=1, hexpand=True,
                css_classes=["shotclip-text"],
            )
            content = Gtk.Box(width_request=200, height_request=125,
                              css_classes=["shotclip-thumb"])
            content.append(preview)
        else:
            content = Gtk.Picture(
                content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                width_request=200, height_request=125, css_classes=["shotclip-thumb"],
            )
            self.app.thumbs.request(
                item, lambda tex, p=content: tex and p.set_paintable(tex))

        overlay = Gtk.Overlay(child=content)
        if idx < 9:
            overlay.add_overlay(Gtk.Label(
                label=str(idx + 1), halign=Gtk.Align.START, valign=Gtk.Align.START,
                css_classes=["shotclip-badge"],
            ))

        actions = Gtk.Box(spacing=4, halign=Gtk.Align.END, valign=Gtk.Align.START,
                          css_classes=["shotclip-actions"])
        pin = Gtk.Button(
            icon_name="view-pin-symbolic", focus_on_click=False, can_focus=False,
            tooltip_text="Unpin (P)" if item.get("pinned") else "Pin (P)",
            css_classes=["circular"] + (["shotclip-pinned"] if item.get("pinned") else []),
        )
        pin.connect("clicked", lambda *_: self.history.toggle_pin(sha))
        rm = Gtk.Button(icon_name="user-trash-symbolic", focus_on_click=False,
                        can_focus=False, tooltip_text="Remove (Del)", css_classes=["circular"])
        rm.connect("clicked", lambda *_: self.history.remove(sha))
        actions.append(pin)
        actions.append(rm)
        overlay.add_overlay(actions)

        label_text = format_time(item.get("added", 0))
        if item["kind"] == "text":
            label_text += f" · {len(item['text'])} chars"
        if item.get("pinned"):
            label_text = "📌 " + label_text
        caption = Gtk.Label(label=label_text, css_classes=["caption"], margin_top=4,
                            ellipsize=Pango.EllipsizeMode.END)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["shotclip-card"])
        box.append(overlay)
        box.append(caption)
        child = Gtk.FlowBoxChild(child=box)
        child.sha1 = sha
        if item["kind"] == "text":
            child.set_tooltip_text(item["text"][:500])
        else:
            source = item.get("source", "")
            origin = "copied image" if source == "clipboard" else os.path.basename(source)
            child.set_tooltip_text(
                f"{item.get('width', '?')}×{item.get('height', '?')}  {origin}")
        return child

    # -- helpers ------------------------------------------------------------

    def _selected_sha(self):
        sel = self.flow.get_selected_children() if hasattr(self, "order") else []
        return sel[0].sha1 if sel else None

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

    def _copy(self, sha, how, paste=True):
        item = self.history.get(sha)
        log(f"picker: copy via {how} (window active: {self.is_active()})")
        if not item:
            return
        method = self.app.copy_item(item)
        if method:
            self.close()
            # Pasting goes through the shell extension; the GTK fallback can't.
            if paste and method == "shell" and self.app.config.get("auto_paste", True):
                self.app.request_paste()

    def _columns(self):
        """Number of cards on the first row of the grid."""
        first_y, n = None, 0
        for i in range(len(self.order)):
            ok, rect = self.flow.get_child_at_index(i).compute_bounds(self.flow)
            if not ok:
                break
            if first_y is None:
                first_y = rect.origin.y
            elif abs(rect.origin.y - first_y) > 1:
                break
            n += 1
        return max(1, n)

    def _move(self, name, focus=True):
        if not self.order:
            return
        sel = self.flow.get_selected_children()
        idx = sel[0].get_index() if sel else 0
        cols = self._columns()
        step = {"Left": -1, "Right": 1, "Up": -cols, "Down": cols}
        if name == "Home":
            idx = 0
        elif name == "End":
            idx = len(self.order) - 1
        else:
            new = idx + step[name]
            if 0 <= new < len(self.order):
                idx = new
        child = self.flow.get_child_at_index(idx)
        self.flow.select_child(child)
        if focus:
            child.grab_focus()

    # -- events -------------------------------------------------------------

    def _on_child_activated(self, _flow, child):
        self._copy(child.sha1, "click/activate")

    def _on_key(self, _ctrl, keyval, _code, state):
        if self.get_visible_dialog():
            return False  # let the Clear dialog handle its own keys
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and name in ("f", "F"):
            self.search_bar.set_search_mode(not self.search_bar.get_search_mode())
            return True
        if ctrl and state & Gdk.ModifierType.SHIFT_MASK and name in ("Delete", "KP_Delete"):
            self._confirm_clear()
            return True
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        searching = self._search_has_focus()
        if name == "Escape":
            if self.search_bar.get_search_mode():
                self.search_bar.set_search_mode(False)  # also clears the query
                self._move("Home")
            else:
                self.close()
            return True
        if searching:
            # Typing goes to the search field; only these keys act on the grid.
            if name in ("Up", "Down", "KP_Up", "KP_Down", "Page_Up", "Page_Down"):
                self._move("Up" if "Up" in name else "Down", focus=False)
                return True
            if name not in ("Return", "KP_Enter", "ISO_Enter"):
                return False
        if name in ("Return", "KP_Enter", "ISO_Enter"):
            sha = self._selected_sha()
            if sha:
                # Shift+Enter: copy only, the user pastes with Ctrl+V.
                shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
                self._copy(sha, "Shift+Enter" if shift else "Enter", paste=not shift)
            return True
        name = name.removeprefix("KP_")
        if name in ("Left", "Right", "Up", "Down", "Home", "End"):
            self._move(name)
            return True
        digit = name[-1] if name.startswith("KP_") else name
        if len(digit) == 1 and digit in "123456789":
            idx = int(digit) - 1
            if idx < len(self.order):
                self._copy(self.order[idx]["sha1"], f"key {digit}")
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
        log(f"picker: window active = {self.is_active()}")
        if self.is_active():
            self.was_active = True
            if self.auto_copy and self.order:
                # --copy-latest: Mutter only accepts a selection from a client
                # with keyboard focus, so copy once the window is active.
                self.auto_copy = False
                latest = max(self.order, key=lambda i: i.get("added", 0))
                GLib.timeout_add(
                    150, lambda: self._copy(latest["sha1"], "--copy-latest", paste=False) and False)
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
        text_action = Gio.SimpleAction.new("add-text", GLib.VariantType.new("s"))
        text_action.connect("activate", self._on_add_text)
        self.add_action(text_action)
        image_action = Gio.SimpleAction.new("add-image", GLib.VariantType.new("(say)"))
        image_action.connect("activate", self._on_add_image)
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
            try:
                self._copy_via_shell(latest)
            except (GLib.Error, OSError) as e:
                # Without the extension a copy needs a focused window.
                log(f"shell copy unavailable ({getattr(e, 'message', e)}); using picker")
                self.show_picker(auto_copy=True)
                cmdline.print_literal("picker opened; newest item is copied once it has focus\n")
                return 0
            log(f"copied {latest['sha1'][:10]} to clipboard (via shell)")
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

    def _on_add_text(self, _action, param):
        if not self.running or not self.config.get("capture_text", True):
            return
        text = param.get_string()
        if self._is_echo(hashlib.sha1(text.encode("utf-8")).hexdigest()):
            return
        item = self.history.add_text(text)
        if item:
            log(f"added text ({len(item['text'])} chars) -> {item['sha1'][:10]}")

    def _on_add_image(self, _action, param):
        if not self.running:
            return
        mime = param.get_child_value(0).get_string()
        data = param.get_child_value(1).get_data_as_bytes().get_data()
        if self._is_echo(hashlib.sha1(data).hexdigest()):
            return
        item = self.history.add_image_bytes(data, mime)
        if item:
            log(f"added copied image {item['width']}x{item['height']} -> {item['sha1'][:10]}")
            self.thumbs.request(item, lambda _t: None)

    def _is_echo(self, sha):
        # While ShotClip replaces the clipboard, GNOME can briefly restore the
        # previous content; don't let that bump an old item to the top.
        if time.monotonic() < self.echo_guard_until and sha != self.last_copied_sha:
            log("ignored clipboard echo of previous content")
            return True
        return False

    def _on_history_changed(self):
        self.thumbs.prune({i["sha1"] for i in self.history.items})

    # -- clipboard / picker -------------------------------------------------

    def copy_item(self, item):
        self.last_copied_sha = item["sha1"]
        self.echo_guard_until = time.monotonic() + 1.0
        try:
            self._copy_via_shell(item)
            log(f"copied {item['sha1'][:10]} to clipboard (via shell)")
            return "shell"
        except (GLib.Error, OSError) as e:
            msg = getattr(e, "message", str(e))
            log(f"shell copy unavailable ({msg}); falling back to GTK clipboard")
        try:
            provider = clipboard_provider(item)
        except GLib.Error as e:
            log(f"copy failed: {e.message}")
            return False
        Gdk.Display.get_default().get_clipboard().set_content(provider)
        log(f"copied {item['sha1'][:10]} to clipboard (via GTK)")
        return "gtk"

    def request_paste(self):
        """Ask the shell extension to paste into the app that regains focus
        once the picker has closed."""
        def done(bus, res):
            try:
                bus.call_finish(res)
                log("paste requested")
            except GLib.Error as e:
                log(f"paste request failed: {e.message}")
        bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call(SHELL_BUS_NAME, SHELL_HELPER_PATH, SHELL_HELPER_IFACE, "Paste",
                 None, None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None, done)

    def _copy_via_shell(self, item):
        """Ask the ShotClip GNOME Shell extension to own the clipboard.

        A background client's selection on GNOME Wayland can end up serving
        stale data to apps; a selection owned by the shell itself (like
        Clipboard Indicator uses) pastes reliably and needs no focus.
        """
        if item["kind"] == "text":
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

    def show_picker(self, auto_copy=False):
        # Recreate the window every time: a freshly mapped window is much more
        # likely to receive focus on Wayland than a re-presented hidden one.
        if self.window:
            old = self.window
            old.detach()
            old.destroy()
        self.window = PickerWindow(self, auto_copy=auto_copy)
        log("picker: shown")
        self.window.present()


def main():
    Adw.init()
    app = ShotClipApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
