// ShotClip shell helper.
//  - On every clipboard change, read the text (or, for image-only copies, the
//    image) and hand it to the ShotClip daemon via its GApplication actions
//    "add-text" / "add-image" on D-Bus.
//  - Export io.github.shotclip.Shell so the daemon can set the clipboard from
//    inside the shell (a background client owning the clipboard on GNOME
//    Wayland is unreliable; a shell-owned selection always pastes), and paste
//    into the focused app with the compositor's own virtual keyboard (no
//    uinput/root needed), like Clipboard Indicator's paste-on-select.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
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
const MAX_IMAGE_BYTES = 32 * 1024 * 1024;
// Preferred order when a copy offers several image formats.
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff'];
const TEXT_TYPES = ['text/plain', 'text/plain;charset=utf-8', 'UTF8_STRING', 'STRING', 'TEXT'];
// Password managers (KeePassXC, ...) mark secrets with this type; never store them.
const SECRET_HINT = 'x-kde-passwordManagerHint';
// Files copied in Nautilus also offer text/plain paths; those are not text copies.
const COPIED_FILES = 'x-special/gnome-copied-files';

const SHELL_IFACE = `
<node>
  <interface name="io.github.shotclip.Shell">
    <method name="SetText">
      <arg type="s" direction="in" name="text"/>
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
        if (mimes.includes(SECRET_HINT) || mimes.includes(COPIED_FILES))
            return;
        if (mimes.some(m => TEXT_TYPES.includes(m))) {
            // Text wins when both are offered (e.g. a spreadsheet range also
            // offers a rendered image of itself).
            this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => this._sendText(text));
            return;
        }
        const imageType = IMAGE_TYPES.find(t => mimes.includes(t));
        if (imageType) {
            this._clipboard.get_content(St.ClipboardType.CLIPBOARD, imageType,
                (_cb, bytes) => this._sendImage(imageType, bytes));
        }
    }

    _sendText(text) {
        if (!this._clipboard || !text || !text.trim() || text.length > MAX_CHARS)
            return;
        this._activate('add-text', new GLib.Variant('s', text));
    }

    _sendImage(mimetype, bytes) {
        if (!this._clipboard || !bytes || bytes.get_size() === 0 || bytes.get_size() > MAX_IMAGE_BYTES)
            return;
        this._activate('add-image', new GLib.Variant('(say)', [mimetype, bytes.toArray()]));
    }

    _activate(action, parameter) {
        Gio.DBus.session.call(
            BUS_NAME, OBJECT_PATH, 'org.gtk.Actions', 'Activate',
            new GLib.Variant('(sava{sv})', [action, [parameter], {}]),
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
