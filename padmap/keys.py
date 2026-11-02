"""Keys tab - bind buttons / hat directions / axes of any connected device
(HOTAS, wheel, pedals...) to keyboard keys, key combos or mouse movement.

Output: SendInput with hardware scan codes, so DirectInput / raw-input games see
        real key presses. The device keeps sending its own button too - nothing is
        hidden, both fire.

Add a binding, press the device input, then press the key (hold modifiers for a
combo, e.g. Ctrl+F). Double-click the Input or Key cell of a row to rebind it.
"""
import ctypes
import json
import os
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

from .devices import describe, snapshot, source_value
from .paths import PROFILES

PROFILE_DIR = os.path.join(PROFILES, "keys")
AUTOSAVE = os.path.join(PROFILE_DIR, "autosave.json")

DETECT = 0.5          # axis travel (raw units) that counts as "moved" when capturing
PRESS = 0.5           # default axis travel that holds a key down
CAPTURE_TIMEOUT = 8.0


# Keyboard output
# --------------------------------------------------------------------------
class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeybdInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
user32 = ctypes.windll.user32
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)

# keys whose scan code needs the E0 prefix
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
               0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
# Tk reports both sides of a modifier with the generic VK; the keysym tells them apart
SIDED_VK = {"Shift_L": 0xA0, "Shift_R": 0xA1, "Control_L": 0xA2, "Control_R": 0xA3,
            "Alt_L": 0xA4, "Alt_R": 0xA5, "Win_L": 0x5B, "Win_R": 0x5C}
MODIFIER_VK = {0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}
# keys polled while capturing: everything but mouse buttons and the side-less Shift/Ctrl/Alt
CAPTURE_VK = [vk for vk in range(0x08, 0xFF) if vk not in (0x10, 0x11, 0x12)]


def make_key(vk, keysym=""):
    vk = SIDED_VK.get(keysym, vk)
    scan = user32.MapVirtualKeyW(vk, 0)
    ext = vk in EXTENDED_VK
    buf = ctypes.create_unicode_buffer(64)
    n = user32.GetKeyNameTextW((scan << 16) | (ext << 24), buf, 64) if scan else 0
    return {"vk": vk, "scan": scan, "ext": ext,
            "name": buf.value if n else (keysym or f"VK {vk:#04x}")}


_SUBCLASSPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
comctl32 = ctypes.windll.comctl32
comctl32.DefSubclassProc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
comctl32.SetWindowSubclass.argtypes = (wintypes.HWND, _SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t)


def block_alt_menu(root):
    """Releasing Alt sends the window into Windows' modal menu loop, which freezes Tk
    until the next key - swallow that SC_KEYMENU so Alt can be captured and sent."""
    @_SUBCLASSPROC
    def proc(hwnd, msg, wparam, lparam, uid, data):
        if msg == 0x0112 and (wparam & 0xFFF0) == 0xF100:     # WM_SYSCOMMAND, SC_KEYMENU
            return 0
        return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
    root.update_idletasks()
    comctl32.SetWindowSubclass(int(root.wm_frame(), 16), proc, 1, 0)
    root._alt_menu_proc = proc          # keep the callback alive


def keys_down():
    """Virtual keys physically held right now (Tk never sees Alt reliably on Windows)."""
    return {vk for vk in CAPTURE_VK if user32.GetAsyncKeyState(vk) & 0x8000}


def key_id(k):
    return (k["scan"], k["ext"])


def send_key(k, down):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if k["ext"] else 0)
    if not down:
        flags |= KEYEVENTF_KEYUP
    inp = _Input(type=1)
    inp.u.ki = _KeybdInput(0, k["scan"], flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input))


MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x1, 0x800, 0x1000
MOUSE_AXES = {"x": "Mouse X", "y": "Mouse Y", "wheel": "Mouse Wheel"}
MOUSE_SPEED = 1500        # default px/s at full deflection (wheel: notches/s = speed / 100)
MOUSE_DEADZONE = 0.08


def send_mouse(dx, dy, wheel):
    inputs = []
    if dx or dy:
        inputs.append((MOUSEEVENTF_MOVE, dx, dy, 0))
    if wheel:
        inputs.append((MOUSEEVENTF_WHEEL, 0, 0, wheel))
    for flags, x, y, data in inputs:
        inp = _Input(type=0)
        inp.u.mi = _MouseInput(x, y, ctypes.c_uint32(data).value, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_Input))


def combo_text(keys):
    return " + ".join(k["name"] for k in keys)


def output_text(b):
    m = b.get("mouse")
    if not m:
        return combo_text(b["keys"])
    return (f'{MOUSE_AXES[m["axis"]]}  (speed {m.get("speed", MOUSE_SPEED)}'
            f'{", inverted" if m.get("invert") else ""})')


def pressed(src, state):
    threshold = src.get("deadzone", PRESS) if src["type"] == "axis" else PRESS
    value = source_value(src, state)
    return min(1.0, value) > threshold if src["type"] == "axis" else value > threshold


def signed_value(src, state, m):
    """-1..1 for a mouse-axis binding; a button / hat pushes +1 while held."""
    n = source_value(src, state)
    if src["type"] == "axis" and abs(src["rest"]) >= 0.9:
        n = n * 2 - 1              # lever resting at an end -> full range
    n = min(1.0, max(-1.0, n))
    dz = m.get("deadzone", MOUSE_DEADZONE)
    if abs(n) <= dz:
        return 0.0
    n = (abs(n) - dz) / (1 - dz) * (1 if n > 0 else -1)
    return -n if m.get("invert") else n




# --------------------------------------------------------------------------
# Output sink: holds keys down while their input is active
# --------------------------------------------------------------------------
class KeysOutput:
    def __init__(self):
        self.bindings = []          # [{"src": {...}, "keys": [{vk, scan, ext, name}, ...]}]
        self.active = []            # per binding: input currently pressed
        self.running = False
        self.paused = False         # set while the UI is capturing a binding
        self.held = {}              # key id -> key, currently down
        self.remainder = {"x": 0.0, "y": 0.0, "wheel": 0.0}

    def process(self, state, dt):
        bindings = self.bindings
        live = self.running and not self.paused
        active, move = [], {"x": 0.0, "y": 0.0, "wheel": 0.0}
        for b in bindings:
            m = b.get("mouse")
            if m:
                v = signed_value(b["src"], state, m)
                active.append(v != 0.0)
                scale = 120 / 100 if m["axis"] == "wheel" else 1   # wheel: 120 per notch
                move[m["axis"]] += v * m.get("speed", MOUSE_SPEED) * scale * dt
            else:
                active.append(pressed(b["src"], state))
        self.active = active
        if live:
            # carry the sub-pixel remainder so slow deflections still creep
            step = {}
            for a in move:
                self.remainder[a] += move[a]
                step[a] = int(self.remainder[a])
                self.remainder[a] -= step[a]
            # captured direction = right / up / wheel forward
            send_mouse(step["x"], -step["y"], step["wheel"])
        else:
            self.remainder = dict.fromkeys(self.remainder, 0.0)

        # keys wanted down, with their position in the combo (modifiers press first)
        held = self.held
        want = {}
        if live:
            for b, on in zip(bindings, active):
                if on and not b.get("mouse"):
                    for pos, k in enumerate(b["keys"]):
                        kid = key_id(k)
                        if kid not in want or pos < want[kid][0]:
                            want[kid] = (pos, k)
        for kid in sorted((k for k in held if k not in want),
                          key=lambda k: held[k]["vk"] in MODIFIER_VK):
            send_key(held.pop(kid), False)
        for kid, (pos, k) in sorted(want.items(), key=lambda kv: kv[1][0]):
            if kid not in held:
                send_key(k, True)
                held[kid] = k

    def shutdown(self):
        for k in self.held.values():
            send_key(k, False)
        self.held = {}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
C_LISTEN, C_OK, C_ERR = "#e8590c", "#2f9e44", "#c92a2a"


class KeysTab(ttk.Frame):
    """Binds device inputs to keyboard keys, combos and mouse movement."""

    def __init__(self, parent, reader):
        super().__init__(parent)
        root = self.root = self.winfo_toplevel()
        self.reader = reader
        self.engine = KeysOutput()
        reader.add_sink(self.engine)
        self.cap = None

        top = tk.Frame(self)
        top.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(top, text="Connected devices", anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x")
        self.dev_label = tk.Label(top, anchor="w", justify="left", fg="#333")
        self.dev_label.pack(fill="x", pady=(0, 8))

        self.tree = ttk.Treeview(top, columns=("src", "key"), show="headings", height=18,
                                 selectmode="browse")
        self.tree.heading("src", text="Input")
        self.tree.heading("key", text="Output")
        self.tree.column("src", width=340)
        self.tree.column("key", width=200)
        self.tree.tag_configure("on", background="#a5d8ff")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._row_bind)
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sync_settings())

        bar = tk.Frame(top)
        bar.pack(fill="x", pady=(6, 0))
        self.run_btn = ttk.Button(bar, text="Start", command=self.toggle_run, width=10)
        self.run_btn.pack(side="left")
        ttk.Button(bar, text="Add key", command=self.add).pack(side="left", padx=4)
        self.mouse_btn = ttk.Button(bar, text="Add mouse axis ▾",
                                    command=lambda: self._output_menu(None, self.mouse_btn))
        self.mouse_btn.pack(side="left")
        ttk.Button(bar, text="Remove", command=self.remove).pack(side="left", padx=4)
        ttk.Button(bar, text="Save...", command=self.save_as).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="Load...", command=self.load_from).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left", padx=4)

        # settings for the selected mouse-axis or axis-to-key row
        mbox = tk.Frame(top)
        mbox.pack(fill="x", pady=(6, 0))
        self.speed = tk.Scale(mbox, from_=100, to=6000, resolution=100, orient="horizontal",
                              label="Mouse speed (px/s at full deflection)", state="disabled",
                              command=lambda v: self._set_mouse("speed", int(float(v))))
        self.speed.pack(side="left", fill="x", expand=True)
        self.dz = tk.Scale(mbox, from_=0, to=100, resolution=1, orient="horizontal",
                           label="Deadzone %", state="disabled", length=140,
                           command=self._set_deadzone)
        self.dz.pack(side="left", padx=8)
        self.inv_var = tk.BooleanVar()
        self.inv_chk = ttk.Checkbutton(mbox, text="Invert", variable=self.inv_var,
                                       state="disabled",
                                       command=lambda: self._set_mouse("invert", self.inv_var.get()))
        self.inv_chk.pack(side="left", anchor="s", pady=(0, 6))
        for w in (self.speed, self.dz):
            w.bind("<ButtonRelease-1>", lambda e: (self._refresh_rows(), self._autosave()))
        self._syncing = False

        self.status = tk.Label(top, anchor="w", fg="#444",
                               text="Add key / Add mouse axis, then press or move a device input.")
        self.status.pack(fill="x", pady=(6, 0))
        tk.Label(top, anchor="w", fg="#777", justify="left",
                 text="Double-click a row's Input cell to rebind the input, its Output cell to\n"
                      "change the output (another key, or a mouse axis). Delete removes a row.\n"
                      "Hold modifiers for a key combo (e.g. Ctrl+F).\n"
                      "Esc cancels while waiting for a device input (it can be bound as a key).\n"
                      "Output is only sent while Started; it pauses during a bind.").pack(
            fill="x", pady=(6, 0))

        root.bind("<Escape>", self._escape, add="+")
        # while waiting for a key, swallow key events so Tk / Windows don't act on them
        # (the Alt menu, Treeview navigation); the key itself is read by polling
        root.bind_all("<Key>", lambda e: "break" if self._wants_key() else None)
        root.bind_all("<KeyRelease>", lambda e: "break" if self._wants_key() else None)
        block_alt_menu(root)
        if os.path.exists(AUTOSAVE):
            self._load(AUTOSAVE, quiet=True)
        self._refresh_rows()
        self.tick()

    # -- capture ----------------------------------------------------------
    def add(self):
        self._begin(None, ("src", "key"))

    def _row_bind(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.identify_column(event.x) == "#2":
            self._output_menu(int(row))
        else:
            self._begin(int(row), ("src",))

    def _output_menu(self, index, under=None):
        """Pick what a binding drives. index None = a new mouse-axis binding."""
        menu = tk.Menu(self.root, tearoff=0)
        if index is not None:
            menu.add_command(label="Keyboard key...", command=lambda: self._begin(index, ("key",)))
            menu.add_separator()
        for axis, label in MOUSE_AXES.items():
            menu.add_command(label=label, command=lambda a=axis: self._pick_mouse(index, a))
        if under is not None:
            menu.post(under.winfo_rootx(), under.winfo_rooty() + under.winfo_height())
        else:
            menu.post(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _pick_mouse(self, index, axis):
        if index is None:
            return self._begin(None, ("src",), mouse={"axis": axis})
        bindings = [dict(b) for b in self.engine.bindings]
        b = bindings[index]
        b["mouse"] = {**b.get("mouse", {}), "axis": axis}
        b.pop("keys", None)
        self.engine.bindings = bindings
        self.status.config(text=f"{describe(b['src'])} now drives {MOUSE_AXES[axis]}.", fg=C_OK)
        self._refresh_rows()
        self._autosave()

    def _begin(self, index, parts, mouse=None):
        self.cap = {"index": index, "parts": list(parts), "src": None, "mods": [],
                    "mouse": mouse}
        if index is not None and mouse is None:
            mouse = self.engine.bindings[index].get("mouse")
        self.cap["dir"] = mouse and {"x": "RIGHT", "y": "UP", "wheel": "UP"}[mouse["axis"]]
        self.engine.paused = True
        self._next_part()

    def _next_part(self):
        cap = self.cap
        if not cap["parts"]:
            return self._commit()
        cap["phase"] = cap["parts"].pop(0)
        cap["t0"] = time.time()
        cap["mods"] = []
        if cap["phase"] == "src":
            cap["base"] = self._snapshot()
            if cap["dir"]:
                text = (f"Move the device axis {cap['dir']} (or press a button to push that way)"
                        f"... (Esc cancels)")
            else:
                text = "Press a device button / hat, or move an axis... (Esc cancels)"
            self.status.config(text=text, fg=C_LISTEN)
        else:
            cap["stuck"] = keys_down()      # already held when this step began: ignored
            self.root.focus_force()
            self.status.config(text="Now press the keyboard key (hold modifiers for a combo)...",
                               fg=C_LISTEN)

    def _commit(self):
        cap = self.cap
        bindings = [dict(b) for b in self.engine.bindings]
        if cap["index"] is None:
            out = {"mouse": cap["mouse"]} if cap["mouse"] else {"keys": cap["keys"]}
            bindings.append({"src": cap["src"], **out})
            b = bindings[-1]
        else:
            b = bindings[cap["index"]]
            if cap["src"] is not None:
                b["src"] = cap["src"]
            if cap.get("keys"):
                b["keys"] = cap["keys"]
                b.pop("mouse", None)
        self.engine.bindings = bindings
        self._end(f"Bound {describe(b['src'])} to {output_text(b)}.", C_OK)
        self._refresh_rows()
        self._autosave()

    def _end(self, msg, color="#444"):
        self.cap = None
        self.engine.paused = False
        self.status.config(text=msg, fg=color)

    def _escape(self, event):
        if self.cap and self.cap["phase"] == "src":
            self._end("Cancelled.")

    def _snapshot(self):
        return snapshot(self.reader.state)

    def _poll_capture(self):
        cap = self.cap
        if time.time() - cap["t0"] > CAPTURE_TIMEOUT:
            return self._end("Timed out.")
        if cap["phase"] == "key":
            return self._poll_key()
        for dev, cur in self._snapshot().items():
            base = cap["base"].get(dev)
            if base is None:
                continue
            src = None
            for i, v in enumerate(cur["axes"]):
                if i < len(base["axes"]) and abs(v - base["axes"][i]) > DETECT:
                    rest = base["axes"][i]
                    src = {"dev": dev, "type": "axis", "index": i, "rest": rest,
                           "peak": 1.0 if v > rest else -1.0}
                    break
            for i, down in enumerate(cur["buttons"]):
                if src is None and down and i < len(base["buttons"]) and not base["buttons"][i]:
                    src = {"dev": dev, "type": "button", "index": i}
            for i, h in enumerate(cur["hats"]):
                if src is None and h != (0, 0) and i < len(base["hats"]) and base["hats"][i] == (0, 0):
                    ax = 0 if h[0] else 1
                    src = {"dev": dev, "type": "hat", "index": i, "axis": ax, "sign": h[ax]}
            if src is not None:
                cap["src"] = src
                return self._next_part()

    def _wants_key(self):
        return bool(self.cap) and self.cap["phase"] == "key"

    def _poll_key(self):
        cap = self.cap
        down = keys_down()
        cap["stuck"] &= down                # once released, a key counts again
        fresh = down - cap["stuck"]
        for vk in sorted(fresh & MODIFIER_VK):
            if vk not in [m["vk"] for m in cap["mods"]]:
                cap["mods"].append(make_key(vk))
        main = sorted(fresh - MODIFIER_VK)
        if main:
            cap["keys"] = cap["mods"] + [make_key(main[0])]
        elif cap["mods"] and not fresh & MODIFIER_VK:
            cap["keys"] = cap["mods"]       # modifier(s) pressed and released on their own
        else:
            return
        self._next_part()

    # -- edit -------------------------------------------------------------
    def _selected_mouse(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        i = int(sel[0])
        return i, self.engine.bindings[i].get("mouse")

    def _sync_settings(self):
        i, m = self._selected_mouse()
        src = self.engine.bindings[i]["src"] if i is not None else None
        key_axis = bool(src and src["type"] == "axis" and not m)
        self._syncing = True
        state = "normal" if m else "disabled"
        for w in (self.speed, self.dz):
            w.config(state="normal")         # a disabled Scale ignores set()
        self.speed.set(m.get("speed", MOUSE_SPEED) if m else MOUSE_SPEED)
        self.dz.set(round((m.get("deadzone", MOUSE_DEADZONE) if m else
                           src.get("deadzone", PRESS) if key_axis else 0) * 100))
        self.speed.config(state=state)
        self.dz.config(state="normal" if m or key_axis else "disabled")
        self.inv_chk.config(state=state)
        self.inv_var.set(bool(m and m.get("invert")))
        self._syncing = False

    def _set_deadzone(self, value):
        if self._syncing:
            return
        i, m = self._selected_mouse()
        if i is None:
            return
        dz = int(float(value)) / 100
        if m:
            self._set_mouse("deadzone", dz)
        elif self.engine.bindings[i]["src"]["type"] == "axis":
            bindings = list(self.engine.bindings)
            bindings[i] = {**bindings[i], "src": {**bindings[i]["src"], "deadzone": dz}}
            self.engine.bindings = bindings

    def _set_mouse(self, field, value):
        i, m = self._selected_mouse()
        if self._syncing or not m:
            return
        bindings = list(self.engine.bindings)
        bindings[i] = {**bindings[i], "mouse": {**m, field: value}}
        self.engine.bindings = bindings
        if field == "invert":
            self._refresh_rows()
            self._autosave()

    def remove(self):
        sel = self.tree.selection()
        if not sel or self.cap:
            return
        bindings = list(self.engine.bindings)
        del bindings[int(sel[0])]
        self.engine.bindings = bindings
        self._refresh_rows()
        self._autosave()

    def clear_all(self):
        if self.engine.bindings and messagebox.askyesno("Clear all", "Remove every binding?"):
            self.engine.bindings = []
            self._refresh_rows()
            self._autosave()

    # -- profiles ---------------------------------------------------------
    def _autosave(self):
        try:
            self._dump(AUTOSAVE)
        except OSError:
            pass

    def _dump(self, path):
        with open(path, "w") as f:
            json.dump({"version": 1, "keys": self.engine.bindings}, f, indent=2)

    def save_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialdir=PROFILE_DIR,
                                            filetypes=[("Profile", "*.json")])
        if path:
            self._dump(path)
            self.status.config(text=f"Saved {os.path.basename(path)}.", fg="#444")

    def load_from(self):
        path = filedialog.askopenfilename(initialdir=PROFILE_DIR, filetypes=[("Profile", "*.json")])
        if path:
            self._load(path)
            self._autosave()

    def _load(self, path, quiet=False):
        try:
            with open(path) as f:
                data = json.load(f)
            self.engine.bindings = [b for b in data["keys"]
                                    if b.get("src") and (b.get("keys") or b.get("mouse"))]
        except (OSError, ValueError, KeyError) as ex:
            if not quiet:
                messagebox.showerror("Load failed", f"Not a keys profile: {ex}")
            return
        self._refresh_rows()
        if not quiet:
            self.status.config(text=f"Loaded {os.path.basename(path)}.", fg="#444")

    # -- run / refresh ----------------------------------------------------
    def toggle_run(self):
        # A key emitted immediately after Start (notably Space) must not
        # activate the focused Start/Stop button a second time.
        self.root.focus_set()
        self.engine.running = not self.engine.running
        self.run_btn.config(text="Stop" if self.engine.running else "Start")

    def _refresh_rows(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, b in enumerate(self.engine.bindings):
            src = b["src"]
            input_text = describe(src)
            if src["type"] == "axis" and not b.get("mouse"):
                input_text += f' [deadzone {round(src.get("deadzone", PRESS) * 100)}%]'
            self.tree.insert("", "end", iid=str(i), values=(input_text, output_text(b)))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self._sync_settings()

    def tick(self):
        if self.cap:
            self._poll_capture()
        eng = self.engine
        self.dev_label.config(text="\n".join(self.reader.devices) or "(none found)")
        active = eng.active
        for i in range(len(eng.bindings)):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.item(iid, tags=("on",) if i < len(active) and active[i] else ())
        self.after(20, self.tick)

    def shutdown(self):
        self.engine.running = False
