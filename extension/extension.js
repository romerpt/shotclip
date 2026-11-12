// ShotClip shell helper.
//  - On every clipboard change, read the copy (text, its HTML formatting if
//    any, an image for image-only copies, or video files copied in a file
//    manager) plus the app it came from, and hand it to the ShotClip daemon
//    via its GApplication action "add-clip".
//  - Export io.github.shotclip.Shell so the daemon can set the clipboard from
//    inside the shell (a background client owning the clipboard on GNOME
//    Wayland is unreliable; a shell-owned selection always pastes), and paste
//    into the focused app with the compositor's own virtual keyboard (no
//    uinput/root needed), like Clipboard Indicator's paste-on-select.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'io.github.shotclip';
const OBJECT_PATH = '/io/github/shotclip';
const SHELL_OBJECT_PATH = '/io/github/shotclip/Shell';
const PICKER_APP_ID = 'io.github.shotclip';
const FOCUS_WAIT_MS = 1000;  // give up pasting if the picker keeps focus this long
const SETTLE_MS = 60;        // let the input method report the new focus's purpose
const MAX_CHARS = 100000;
const MAX_HTML_BYTES = 2 * 1024 * 1024;
const MAX_IMAGE_BYTES = 32 * 1024 * 1024;
// Preferred order when a copy offers several image formats.
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff'];
const TEXT_TYPES = ['text/plain', 'text/plain;charset=utf-8', 'UTF8_STRING', 'STRING', 'TEXT'];
const HTML_TYPE = 'text/html';
// Password managers (KeePassXC, ...) mark secrets with this type; never store them.
const SECRET_HINT = 'x-kde-passwordManagerHint';
// Files copied in Nautilus also offer text/plain paths; those are not text
// copies. Only video files (screencasts) among them are recorded.
const COPIED_FILES = 'x-special/gnome-copied-files';
const VIDEO_EXTS = ['.webm', '.mp4', '.mkv', '.mov'];

const SHELL_IFACE = `
<node>
  <interface name="io.github.shotclip.Shell">
    <method name="SetText">
      <arg type="s" direction="in" name="text"/>
    </method>
    <method name="SetHtml">
      <arg type="ay" direction="in" name="html"/>
    </method>
    <method name="SetContent">
      <arg type="s" direction="in" name="mimetype"/>
      <arg type="ay" direction="in" name="data"/>
    </method>
    <method name="Paste"/>
  </interface>
</node>`;

export default class ShotClipExtension extends Extension {
    enable() {
        this._clipboard = St.Clipboard.get_default();
        this._selection = global.display.get_selection();
        this._ownerChangedId = this._selection.connect('owner-changed',
            (_sel, type, source) => this._onOwnerChanged(type, source));

        const seat = Clutter.get_default_backend().get_default_seat();
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._pasteSourceId = 0;

        this._dbus = Gio.DBusExportedObject.wrapJSObject(SHELL_IFACE, this);
        this._dbus.export(Gio.DBus.session, SHELL_OBJECT_PATH);
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
        this._cancelPaste();
        this._keyboard?.run_dispose();
        this._keyboard = null;
        if (this._ownerChangedId)
            this._selection.disconnect(this._ownerChangedId);
        this._ownerChangedId = 0;
        this._selection = null;
        this._clipboard = null;
    }

    // -- D-Bus methods called by the daemon --------------------------------

    SetText(text) {
        this._clipboard.set_text(St.ClipboardType.CLIPBOARD, text);
    }

    SetHtml(html) {
        // HTML only: a shell-owned clipboard offers a single format (GJS can't
        // implement a multi-format Meta.SelectionSource), so the daemon uses
        // this only when the user asks for formatting.
        this._clipboard.set_content(St.ClipboardType.CLIPBOARD, HTML_TYPE, new GLib.Bytes(html));
    }

    SetContent(mimetype, data) {
        this._clipboard.set_content(St.ClipboardType.CLIPBOARD, mimetype, new GLib.Bytes(data));
    }

    Paste() {
        // The picker window is closing; wait until focus is back on the app
        // the user came from, then send the paste shortcut to it.
        this._cancelPaste();
        const start = GLib.get_monotonic_time();
        this._pasteSourceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 20, () => {
            const win = global.display.focus_window;
            if (!win || this._isPicker(win)) {
                if (GLib.get_monotonic_time() - start < FOCUS_WAIT_MS * 1000)
                    return GLib.SOURCE_CONTINUE;
                this._pasteSourceId = 0;
                return GLib.SOURCE_REMOVE;
            }
            this._pasteSourceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SETTLE_MS, () => {
                this._pasteSourceId = 0;
                this._sendPasteKeys();
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _isPicker(win) {
        return win.get_gtk_application_id() === PICKER_APP_ID ||
            win.get_wm_class() === PICKER_APP_ID;
    }

    _cancelPaste() {
        if (this._pasteSourceId)
            GLib.source_remove(this._pasteSourceId);
        this._pasteSourceId = 0;
    }

    _sendPasteKeys() {
        // Shift+Insert works in GTK, Qt, browsers and editors; terminals want
        // Ctrl+Shift+Insert (same keys as Clipboard Indicator).
        const keys = Main.inputMethod.content_purpose === Clutter.InputContentPurpose.TERMINAL
            ? [Clutter.KEY_Control_L, Clutter.KEY_Shift_L, Clutter.KEY_Insert]
            : [Clutter.KEY_Shift_L, Clutter.KEY_Insert];
        const time = GLib.get_monotonic_time();
        for (const key of keys)
            this._keyboard.notify_keyval(time, key, Clutter.KeyState.PRESSED);
        for (const key of keys.reverse())
            this._keyboard.notify_keyval(time, key, Clutter.KeyState.RELEASED);
    }

    // -- capture -----------------------------------------------------------

    _onOwnerChanged(type, source) {
        if (type !== Meta.SelectionType.SELECTION_CLIPBOARD || !source)
            return;
        const mimes = source.get_mimetypes();
        if (mimes.includes(SECRET_HINT))
            return;
        // Identify the app now: focus may move before the content arrives.
        const apps = this._focusedAppIds();
        if (mimes.includes(COPIED_FILES)) {
            this._clipboard.get_content(St.ClipboardType.CLIPBOARD, COPIED_FILES,
                (_cb, bytes) => this._sendVideoFiles(bytes, apps));
            return;
        }
        if (mimes.some(m => TEXT_TYPES.includes(m))) {
            // Text wins when both are offered (e.g. a spreadsheet range also
            // offers a rendered image of itself).
            this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => {
                if (!text || !text.trim() || text.length > MAX_CHARS)
                    return;
                if (!mimes.includes(HTML_TYPE)) {
                    this._sendClip({text}, apps);
                    return;
                }
                this._clipboard.get_content(St.ClipboardType.CLIPBOARD, HTML_TYPE,
                    (_cb2, html) => this._sendClip({text, html}, apps));
            });
            return;
        }
        const imageType = IMAGE_TYPES.find(t => mimes.includes(t));
        if (imageType) {
            this._clipboard.get_content(St.ClipboardType.CLIPBOARD, imageType,
                (_cb, image) => this._sendClip({image, mime: imageType}, apps));
        }
    }

    _sendVideoFiles(bytes, apps) {
        // "copy\nfile:///a.webm\nfile:///b.mp4" (or "cut\n...").
        if (!bytes || bytes.get_size() === 0 || bytes.get_size() > MAX_CHARS)
            return;
        const lines = new TextDecoder().decode(bytes.toArray()).split(/\r?\n/).slice(1);
        const files = [];
        for (const uri of lines) {
            if (!uri.startsWith('file://'))
                continue;
            const path = Gio.File.new_for_uri(uri).get_path();
            if (path && VIDEO_EXTS.some(ext => path.toLowerCase().endsWith(ext)))
                files.push(path);
        }
        if (files.length)
            this._call({apps: new GLib.Variant('as', apps), files: new GLib.Variant('as', files)});
    }

    _focusedAppIds() {
        const win = global.display.focus_window;
        if (!win)
            return [];
        const ids = [win.get_wm_class(), win.get_wm_class_instance(),
            win.get_gtk_application_id(), win.get_sandboxed_app_id()];
        const appId = Shell.WindowTracker.get_default().get_window_app(win)?.get_id();
        if (appId && !appId.startsWith('window:'))  // "window:N" = no .desktop file
            ids.push(appId.replace(/\.desktop$/, ''));
        return [...new Set(ids.filter(Boolean))];
    }

    _sendClip({text, html, image, mime}, apps) {
        if (!this._clipboard)
            return;
        const clip = {apps: new GLib.Variant('as', apps)};
        if (text)
            clip.text = new GLib.Variant('s', text);
        if (html && html.get_size() > 0 && html.get_size() <= MAX_HTML_BYTES)
            clip.html = new GLib.Variant('ay', html.toArray());
        if (image) {
            if (image.get_size() === 0 || image.get_size() > MAX_IMAGE_BYTES)
                return;
            clip.image = new GLib.Variant('ay', image.toArray());
            clip.mime = new GLib.Variant('s', mime);
        }
        this._call(clip);
    }

    _call(clip) {
        Gio.DBus.session.call(
            BUS_NAME, OBJECT_PATH, 'org.gtk.Actions', 'Activate',
            new GLib.Variant('(sava{sv})', ['add-clip', [new GLib.Variant('a{sv}', clip)], {}]),
            null, Gio.DBusCallFlags.NO_AUTO_START, -1, null,
            (conn, res) => {
                try {
                    conn.call_finish(res);
                } catch {
                    // ShotClip daemon not running: nothing to do.
                }
            });
    }
}
