"""Xbox tab - map any number of physical devices onto one virtual Xbox 360 pad.

Output: vgamepad (ViGEmBus) - one virtual Xbox 360 controller.

Click a control on the drawn pad, then press/move the input you want on it.
Right-click a control to clear it. Sticks bind two axes in a row (X, then Y).
"""
import json
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from .devices import describe, source_value
from .paths import ASSETS, PROFILES

IMAGE = os.path.join(ASSETS, "controller.png")
PROFILE_DIR = os.path.join(PROFILES, "xbox")
AUTOSAVE = os.path.join(PROFILE_DIR, "autosave.json")

DEADZONE = 0.08           # default for stick axes; adjustable per binding in the UI
TRIGGER_DEADZONE = 0.0    # default for triggers
DETECT = 0.5          # axis travel (raw units) that counts as "moved"
CAPTURE_TIMEOUT = 8.0

# id, label, kind  (btn: on/off, trig: 0..1, axis: -1..1)
TARGETS = [
    ("a", "A", "btn"), ("b", "B", "btn"), ("x", "X", "btn"), ("y", "Y", "btn"),
    ("lb", "Left Bumper", "btn"), ("rb", "Right Bumper", "btn"),
    ("lt", "Left Trigger", "trig"), ("rt", "Right Trigger", "trig"),
    ("back", "Back / View", "btn"), ("start", "Start / Menu", "btn"),
    ("guide", "Guide", "btn"),
    ("l3", "Left Stick Click", "btn"), ("r3", "Right Stick Click", "btn"),
    ("dup", "D-pad Up", "btn"), ("ddown", "D-pad Down", "btn"),
    ("dleft", "D-pad Left", "btn"), ("dright", "D-pad Right", "btn"),
    ("lsx", "Left Stick X", "axis"), ("lsy", "Left Stick Y", "axis"),
    ("rsx", "Right Stick X", "axis"), ("rsy", "Right Stick Y", "axis"),
]
LABEL = {t: l for t, l, _ in TARGETS}
KIND = {t: k for t, _, k in TARGETS}


def compute_outputs(mapping, state):
    out = {}
    for target, src in mapping.items():
        n = source_value(src, state)
        kind = KIND[target]
        invert = src.get("invert", False)
        if kind == "btn":
            out[target] = (1 - n if invert else n) > 0.5
        elif kind == "trig":
            dz = src.get("deadzone", TRIGGER_DEADZONE)
            n = min(1.0, max(0.0, 1 - n if invert else n))
            out[target] = 0.0 if n <= dz else (n - dz) / (1 - dz)
        else:
            dz = src.get("deadzone", DEADZONE)
            if src["type"] == "axis" and abs(src["rest"]) >= 0.9:
                n = n * 2 - 1          # lever resting at an end -> full stick range
            n = min(1.0, max(-1.0, n))
            if invert:
                n = -n
            if abs(n) <= dz:
                n = 0.0
            else:
                n = (abs(n) - dz) / (1 - dz) * (1 if n > 0 else -1)
            out[target] = n
    return out


class XboxOutput:
    """Sink: turns device state into virtual-pad reports while running."""

    def __init__(self):
        self.mapping = {}
        self.outputs = {}
        self.running = False
        self.error = ""
        self.pad = None
        self.vg = None
        self.last = None

    def process(self, state, dt):
        self.outputs = outputs = compute_outputs(self.mapping, state)
        if self.running and self.pad is None:
            try:
                import vgamepad as vg
                self.vg, self.pad = vg, vg.VX360Gamepad()
                self.error = ""
                self.last = None
            except Exception as ex:
                self.error = f"Virtual pad failed: {ex}"
                self.running = False
        elif not self.running and self.pad is not None:
            self.shutdown()
        pad = self.pad
        if pad is None:
            return
        B = self.vg.XUSB_BUTTON
        bits = {
            "a": B.XUSB_GAMEPAD_A, "b": B.XUSB_GAMEPAD_B,
            "x": B.XUSB_GAMEPAD_X, "y": B.XUSB_GAMEPAD_Y,
            "lb": B.XUSB_GAMEPAD_LEFT_SHOULDER, "rb": B.XUSB_GAMEPAD_RIGHT_SHOULDER,
            "back": B.XUSB_GAMEPAD_BACK, "start": B.XUSB_GAMEPAD_START,
            "guide": B.XUSB_GAMEPAD_GUIDE,
            "l3": B.XUSB_GAMEPAD_LEFT_THUMB, "r3": B.XUSB_GAMEPAD_RIGHT_THUMB,
            "dup": B.XUSB_GAMEPAD_DPAD_UP, "ddown": B.XUSB_GAMEPAD_DPAD_DOWN,
            "dleft": B.XUSB_GAMEPAD_DPAD_LEFT, "dright": B.XUSB_GAMEPAD_DPAD_RIGHT,
        }
        mask = 0
        for t, bit in bits.items():
            if outputs.get(t):
                mask |= int(bit)
        frame = (mask, outputs.get("lt", 0.0), outputs.get("rt", 0.0),
                 outputs.get("lsx", 0.0), outputs.get("lsy", 0.0),
                 outputs.get("rsx", 0.0), outputs.get("rsy", 0.0))
        if frame != self.last:
            self.last = frame
            pad.report.wButtons = mask
            pad.left_trigger_float(value_float=frame[1])
            pad.right_trigger_float(value_float=frame[2])
            pad.left_joystick_float(x_value_float=frame[3], y_value_float=frame[4])
            pad.right_joystick_float(x_value_float=frame[5], y_value_float=frame[6])
            pad.update()

    def shutdown(self):
        if self.pad is not None:
            self.pad.reset()
            self.pad.update()
            self.pad = None


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
S = 0.6                 # image scale
OY = 70                 # top margin for shoulder chips
IMG_W = int(1200 * S)
CANVAS_H = int(1200 * S) + OY - 90

# circles: (cx, cy, r) / rects: (cx, cy, hw, hh), in 1200px image coordinates
HOT = {
    "y": ("c", 905, 332, 40), "a": ("c", 905, 487, 40),
    "x": ("c", 826, 410, 40), "b": ("c", 987, 414, 40),
    "back": ("c", 512, 408, 28), "start": ("c", 687, 408, 28), "guide": ("c", 599, 288, 36),
    "dup": ("r", 442, 552, 30, 28), "ddown": ("r", 442, 658, 30, 28),
    "dleft": ("r", 388, 605, 28, 30), "dright": ("r", 496, 605, 28, 30),
    "ls": ("c", 292, 410, 72), "rs": ("c", 757, 585, 72),
}
# chips live in canvas coordinates already: (cx, cy, hw, hh, text)
CHIPS = {
    "lt": (120, 35, 38, 15, "LT"), "lb": (215, 35, 38, 15, "LB"),
    "rb": (505, 35, 38, 15, "RB"), "rt": (600, 35, 38, 15, "RT"),
    "l3": (int(292 * S), int(500 * S) + OY, 25, 12, "L3"),
    "r3": (int(757 * S), int(690 * S) + OY, 25, 12, "R3"),
}
STICKS = {"ls": ("lsx", "lsy"), "rs": ("rsx", "rsy")}

BG = "#eef1f5"
C_UNBOUND, C_BOUND, C_LISTEN, C_ACTIVE = "#7d8799", "#2f9e44", "#f08c00", "#1c7ed6"


class XboxTab(ttk.Frame):
    """Maps any connected devices onto one virtual Xbox 360 pad."""

    def __init__(self, parent, reader):
        super().__init__(parent)
        root = self.root = self.winfo_toplevel()
        self.reader = reader
        self.engine = XboxOutput()
        reader.add_sink(self.engine)
        self.cap = None
        self.items = {}     # hotspot id -> canvas item
        self.dots = {}

        self.k = 1.0        # current zoom of the drawn pad (window resizing)
        self.chip_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.texts = {}

        left = tk.Frame(self)
        left.pack(side="left", fill="y", padx=8, pady=8)
        # status + buttons are packed first so they never get squeezed out
        self.status = tk.Label(left, anchor="w", text="Click a control, then press an input.",
                               fg="#444")
        self.status.pack(side="bottom", fill="x", pady=(6, 0))
        bar = tk.Frame(left)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self.run_btn = ttk.Button(bar, text="Start", command=self.toggle_run, width=10)
        self.run_btn.pack(side="left")
        ttk.Button(bar, text="Save...", command=self.save_as).pack(side="left", padx=4)
        ttk.Button(bar, text="Load...", command=self.load_from).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left", padx=4)

        self.canvas = tk.Canvas(left, width=IMG_W, height=CANVAS_H, bg=BG,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.img_src = Image.open(IMAGE).convert("RGBA")
        self.photo = self._render_image()
        self.canvas.create_image(0, OY, image=self.photo, anchor="nw", tags="img")
        self._build_hotspots()
        self.canvas.bind("<Configure>", self._on_resize)

        right = tk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)
        tk.Label(right, text="Connected devices", anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x")
        self.dev_label = tk.Label(right, anchor="w", justify="left", fg="#333")
        self.dev_label.pack(fill="x", pady=(0, 8))
        self.tree = ttk.Treeview(right, columns=("src",), height=len(TARGETS),
                                 selectmode="browse")
        self.tree.heading("#0", text="Control")
        self.tree.heading("src", text="Bound to")
        self.tree.column("#0", width=140)
        self.tree.column("src", width=260)
        self.tree.pack(fill="both", expand=True)
        for t, label, _ in TARGETS:
            self.tree.insert("", "end", iid=t, text=label, values=("-",))
        self.tree.bind("<Double-1>", self._row_bind)
        self.tree.bind("<Delete>", lambda e: self._clear_target(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sync_invert())
        self.inv_var = tk.BooleanVar()
        self.inv_chk = ttk.Checkbutton(right, text="Invert selected control",
                                       variable=self.inv_var, command=self._toggle_invert,
                                       state="disabled")
        self.inv_chk.pack(anchor="w", pady=(6, 0))
        self.steer_btn = ttk.Button(right, text="Bind lever steering", state="disabled",
                                    command=self.begin_steering)
        self.steer_btn.pack(anchor="w", pady=(6, 0))
        self.dz_scale = tk.Scale(right, from_=0, to=50, orient="horizontal", resolution=1,
                                 label="Deadzone % (sticks and triggers)", state="disabled",
                                 command=self._set_deadzone)
        self.dz_scale.bind("<ButtonRelease-1>", lambda e: self._autosave())
        self.dz_scale.pack(fill="x", pady=(4, 0))
        self.gain_scale = tk.Scale(right, from_=100, to=500, orient="horizontal", resolution=10,
                                   label="Lever steering sensitivity %", state="disabled",
                                   command=self._set_gain)
        self.gain_scale.bind("<ButtonRelease-1>", lambda e: self._autosave())
        self.gain_scale.pack(fill="x", pady=(4, 0))
        self._syncing = False
        tk.Label(right, anchor="w", fg="#777", justify="left",
                 text="Double-click a row to bind it, Delete to clear.\n"
                      "Clicking a control on the pad selects its row.\n"
                      "Right-click a control on the pad to clear it.\n"
                      "Esc cancels a pending bind.").pack(fill="x", pady=(6, 0))

        root.bind("<Escape>", lambda e: self.cancel() if self.cap else None, add="+")
        if os.path.exists(AUTOSAVE):
            self._load(AUTOSAVE, quiet=True)
        self._refresh_rows()
        self.tick()

    # -- drawing ----------------------------------------------------------
    def _render_image(self):
        size = (int(IMG_W * self.k), int(1200 * S * self.k))
        img = self.img_src.resize(size, Image.LANCZOS)
        bg = Image.new("RGBA", size, BG)
        bg.alpha_composite(img)
        return ImageTk.PhotoImage(bg)

    def _on_resize(self, event):
        k = max(0.4, min(event.width / IMG_W, event.height / CANVAS_H))
        if abs(k - self.k) < 0.005:
            return
        f = k / self.k
        self.canvas.scale("all", 0, 0, f, f)
        self.k = k
        self.photo = self._render_image()
        self.canvas.itemconfig("img", image=self.photo)
        self.canvas.coords("img", 0, OY * k)
        self.chip_font.config(size=max(6, round(9 * k)))

    def _build_hotspots(self):
        c = self.canvas
        for hid, g in HOT.items():
            if g[0] == "c":
                _, x, y, r = g
                x, y, r = x * S, y * S + OY, r * S
                item = c.create_oval(x - r, y - r, x + r, y + r, width=2, dash=(4, 3))
            else:
                _, x, y, hw, hh = g
                x, y, hw, hh = x * S, y * S + OY, hw * S, hh * S
                item = c.create_rectangle(x - hw, y - hh, x + hw, y + hh, width=2, dash=(4, 3))
            self.items[hid] = item
        for hid, (x, y, hw, hh, text) in CHIPS.items():
            item = c.create_rectangle(x - hw, y - hh, x + hw, y + hh, width=2, fill="#ffffff")
            self.texts[hid] = c.create_text(x, y, text=text, font=self.chip_font, fill="#333")
            self.items[hid] = item
        for hid in ("ls", "rs"):
            _, x, y, r = HOT[hid]
            self.dots[hid] = c.create_oval(0, 0, 0, 0, fill=C_ACTIVE, outline="")
        for hid, item in self.items.items():
            targets = list(STICKS.get(hid, (hid,)))
            # chip labels and stick dots sit on top of the hotspot, so they get the clicks too
            for it in (item, self.texts.get(hid), self.dots.get(hid)):
                if it is None:
                    continue
                c.tag_bind(it, "<Button-1>", lambda e, t=targets: self.begin(t))
                c.tag_bind(it, "<Button-3>",
                           lambda e, t=targets: [self._clear_target(x) for x in t])
                c.tag_bind(it, "<Enter>", lambda e: c.config(cursor="hand2"))
                c.tag_bind(it, "<Leave>", lambda e: c.config(cursor=""))

    def _targets_of(self, hid):
        return STICKS.get(hid, (hid,))

    # -- binding ----------------------------------------------------------
    def begin(self, targets, steer=False):
        self.tree.selection_set(targets[0])
        self.tree.see(targets[0])
        self.cap = {"queue": list(targets), "phase": "start", "t0": time.time(),
                    "prev": None, "base": None, "steer": [] if steer else None}
        self._cap_next(settle=False)

    def begin_steering(self):
        """Bind the selected stick axis to the difference between two levers."""
        sel = self.tree.selection()
        if sel:
            self.begin([sel[0], sel[0]], steer=True)

    def _cap_next(self, settle):
        cap = self.cap
        cap["target"] = cap["queue"].pop(0)
        cap["t0"] = time.time()
        cap["phase"] = "settle" if settle else "listen"
        if not settle:
            cap["base"] = self._snapshot()
        self._cap_status()

    def _cap_status(self):
        t = self.cap["target"]
        steer = self.cap["steer"]
        if steer is not None:
            if self.cap["phase"] == "settle":
                text = "Release the LEFT lever..."
            else:
                text = f'Push the {"RIGHT" if steer else "LEFT"} lever forward (Esc to cancel)'
            self.status.config(text=text, fg=C_LISTEN)
        elif self.cap["phase"] == "settle":
            self.status.config(text=f"Release the input, then it will listen for {LABEL[t]}...",
                               fg=C_LISTEN)
        else:
            hint = "move an axis" if KIND[t] == "axis" else "press a button / move an axis"
            self.status.config(text=f"Binding {LABEL[t]}: {hint} (Esc to cancel)", fg=C_LISTEN)

    def cancel(self, msg="Cancelled."):
        self.cap = None
        self.status.config(text=msg, fg="#444")

    def _snapshot(self):
        return {d: {"axes": list(v["axes"]), "buttons": list(v["buttons"]),
                    "hats": list(v["hats"])} for d, v in self.reader.state.items()}

    @staticmethod
    def _settled(base, now):
        for dev, b in base.items():
            n = now.get(dev)
            if n is None:
                continue
            if any(abs(x - y) > 0.2 for x, y in zip(n["axes"], b["axes"])):
                return False
            # compare to base: latching switches stay on and must not block the next bind
            if any(x and not y for x, y in zip(n["buttons"], b["buttons"])):
                return False
            if any(h != (0, 0) for h in n["hats"]):
                return False
        return True

    def _poll_capture(self):
        cap = self.cap
        now = self._snapshot()
        if cap["phase"] == "settle":
            if self._settled(cap["prev"], now):
                cap["base"] = now
                cap["phase"] = "listen"
                cap["t0"] = time.time()
                self._cap_status()
            elif time.time() - cap["t0"] > CAPTURE_TIMEOUT:
                self.cancel("Timed out waiting for the input to be released.")
            return
        if time.time() - cap["t0"] > CAPTURE_TIMEOUT:
            self.cancel(f"Timed out binding {LABEL[cap['target']]}.")
            return
        target, axis_only = cap["target"], KIND[cap["target"]] == "axis"
        for dev, cur in now.items():
            base = cap["base"].get(dev)
            if base is None:
                continue
            for i, v in enumerate(cur["axes"]):
                if i < len(base["axes"]) and abs(v - base["axes"][i]) > DETECT:
                    rest = base["axes"][i]
                    peak = 1.0 if v > rest else -1.0
                    return self._finish({"dev": dev, "type": "axis", "index": i,
                                         "rest": rest, "peak": peak})
            if axis_only:
                continue
            for i, down in enumerate(cur["buttons"]):
                if down and i < len(base["buttons"]) and not base["buttons"][i]:
                    return self._finish({"dev": dev, "type": "button", "index": i})
            for i, h in enumerate(cur["hats"]):
                if h != (0, 0) and i < len(base["hats"]) and base["hats"][i] == (0, 0):
                    ax = 0 if h[0] else 1
                    return self._finish({"dev": dev, "type": "hat", "index": i,
                                         "axis": ax, "sign": h[ax]})

    def _finish(self, src):
        cap = self.cap
        steer = cap["steer"]
        if steer is not None:
            steer.append(src)
            if len(steer) < 2:
                cap["prev"] = cap["base"]
                self._cap_next(settle=True)
                return
            src = {"type": "diff", "left": steer[0], "right": steer[1]}
        mapping = dict(self.engine.mapping)
        mapping[cap["target"]] = src
        self.engine.mapping = mapping
        self._refresh_rows()
        self._autosave()
        if cap["queue"]:
            cap["prev"] = cap["base"]
            self._cap_next(settle=True)
        else:
            self.cancel(f"Bound {LABEL[cap['target']]} to {describe(src)}.")
            self.status.config(fg="#2f9e44")

    def _sync_invert(self):
        sel = self.tree.selection()
        src = self.engine.mapping.get(sel[0]) if sel else None
        self.inv_var.set(bool(src and src.get("invert")))
        self.inv_chk.config(state="normal" if src else "disabled")
        kind = KIND[sel[0]] if sel else "btn"
        self.steer_btn.config(state="normal" if kind == "axis" else "disabled")
        self._syncing = True
        if src and kind != "btn":
            default = DEADZONE if kind == "axis" else TRIGGER_DEADZONE
            self.dz_scale.config(state="normal")
            self.dz_scale.set(round(src.get("deadzone", default) * 100))
        else:
            self.dz_scale.set(0)
            self.dz_scale.config(state="disabled")
        if src and src["type"] == "diff":
            self.gain_scale.config(state="normal")
            self.gain_scale.set(round(src.get("gain", 1.0) * 100))
        else:
            self.gain_scale.config(state="normal")      # a disabled Scale ignores set()
            self.gain_scale.set(100)
            self.gain_scale.config(state="disabled")
        self._syncing = False

    def _set_gain(self, value):
        sel = self.tree.selection()
        if self._syncing or not sel or self.engine.mapping.get(sel[0], {}).get("type") != "diff":
            return
        mapping = dict(self.engine.mapping)
        mapping[sel[0]] = {**mapping[sel[0]], "gain": int(float(value)) / 100}
        self.engine.mapping = mapping

    def _set_deadzone(self, value):
        sel = self.tree.selection()
        if self._syncing or not sel or sel[0] not in self.engine.mapping:
            return
        mapping = dict(self.engine.mapping)
        mapping[sel[0]] = {**mapping[sel[0]], "deadzone": int(float(value)) / 100}
        self.engine.mapping = mapping

    def _toggle_invert(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.engine.mapping:
            return
        mapping = dict(self.engine.mapping)
        mapping[sel[0]] = {**mapping[sel[0]], "invert": self.inv_var.get()}
        self.engine.mapping = mapping
        self._refresh_rows()
        self._autosave()

    def _clear_target(self, target):
        if not target:
            return
        mapping = dict(self.engine.mapping)
        if mapping.pop(target, None) is not None:
            self.engine.mapping = mapping
            self._refresh_rows()
            self._autosave()

    def _row_bind(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.begin([row])

    def clear_all(self):
        if self.engine.mapping and messagebox.askyesno("Clear all", "Remove every binding?"):
            self.engine.mapping = {}
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
            json.dump({"version": 1, "bindings": self.engine.mapping}, f, indent=2)

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
            self.engine.mapping = {t: s for t, s in data["bindings"].items() if t in KIND}
        except (OSError, ValueError, KeyError) as ex:
            if not quiet:
                messagebox.showerror("Load failed", str(ex))
            return
        self._refresh_rows()
        if not quiet:
            self.status.config(text=f"Loaded {os.path.basename(path)}.", fg="#444")

    # -- run / refresh ----------------------------------------------------
    def toggle_run(self):
        self.engine.running = not self.engine.running
        self.run_btn.config(text="Stop" if self.engine.running else "Start")

    def _refresh_rows(self):
        m = self.engine.mapping
        for t in KIND:
            self.tree.set(t, "src", describe(m[t]) if t in m else "-")
        self._sync_invert()

    def tick(self):
        eng = self.engine
        if self.cap:
            self._poll_capture()
        if eng.error:
            self.status.config(text=eng.error, fg="#c92a2a")
            eng.error = ""
            self.run_btn.config(text="Start")
        self.dev_label.config(text="\n".join(self.reader.devices) or "(none found)")
        out, m = eng.outputs, eng.mapping
        listening = set(self._targets_of_active())
        for hid, item in self.items.items():
            targets = self._targets_of(hid)
            if hid in listening:
                color, fill = C_LISTEN, ""
            elif any(isinstance(out.get(t), bool) and out[t] or
                     (KIND[t] == "trig" and out.get(t, 0) > 0.1) for t in targets):
                color, fill = C_ACTIVE, C_ACTIVE
            else:
                color = C_BOUND if any(t in m for t in targets) else C_UNBOUND
                fill = ""
            kw = {"outline": color, "width": 3 if hid in listening else 2}
            if hid in CHIPS:
                kw["fill"] = fill or "#ffffff"
            else:
                kw["fill"] = fill
                kw["stipple"] = "gray50" if fill else ""
            self.canvas.itemconfig(item, **kw)
        for hid, (tx, ty) in STICKS.items():
            _, x, y, r = HOT[hid]
            k = self.k
            cx = (x * S + out.get(tx, 0.0) * r * S * 0.7) * k
            cy = (y * S + OY - out.get(ty, 0.0) * r * S * 0.7) * k
            d = 5 * k
            self.canvas.coords(self.dots[hid], cx - d, cy - d, cx + d, cy + d)
        self.after(20, self.tick)

    def _targets_of_active(self):
        if not self.cap:
            return []
        t = self.cap["target"]
        return [hid for hid in self.items if t in self._targets_of(hid)]

    def shutdown(self):
        self.engine.running = False
