"""padmap_hotas - map any number of physical devices onto one virtual HOTAS
(7 axes, 16 buttons, 1 hat) modelled on the Thrustmaster T.Flight HOTAS Neo
Ace Combat 8 edition.

Input : pygame (SDL joystick API) + Windows joystick API for devices SDL skips.
Output: vJoy device 1 (DirectInput). It must be configured in vJoy's "Configure
        vJoy" tool with axes X Y Z Rx Ry Rz Slider1, 16 buttons, 1 continuous POV hat.

Click a control on the drawn HOTAS, then press/move the input you want on it.
Right-click a control to clear it. The stick and mini-stick bind X then Y.
"""
import ctypes
import json
import os
from ctypes import wintypes
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.path.join(HERE, "hotas.png")
AUTOSAVE = os.path.join(HERE, "profile_hotas.json")
VJOY_ID = 1

DEADZONE = 0.08           # default for stick axes; adjustable per binding in the UI
TRIGGER_DEADZONE = 0.0    # default for triggers
DETECT = 0.5          # axis travel (raw units) that counts as "moved"
CAPTURE_TIMEOUT = 8.0

# id, label, kind  (btn: on/off, trig: 0..1, axis: -1..1)
TARGETS = [
    ("sx", "Roll (stick X)", "axis"), ("sy", "Pitch (stick Y)", "axis"),
    ("twist", "Yaw (stick twist)", "axis"),
    ("thr", "Accelerate/Decelerate (throttle)", "trig"),
    ("lever", "Yaw Left/Right (throttle lever)", "axis"),
    ("msx", "Camera X (mini-stick)", "axis"), ("msy", "Camera Y (mini-stick)", "axis"),
    ("hup", "Move Cursor Up (hat)", "btn"), ("hright", "Move Cursor Right (hat)", "btn"),
    ("hdown", "Move Cursor Down (hat)", "btn"), ("hleft", "Move Cursor Left (hat)", "btn"),
    ("b1", "B1 Fire Machine Gun (trigger)", "btn"), ("b2", "B2 Change Weapon", "btn"),
    ("b3", "B3 Fire Missile/Weapon", "btn"), ("b4", "B4 Change Target", "btn"),
    ("b5", "B5 Toggle Radar Map (R2)", "btn"), ("b6", "B6 Gear Down/Up (L2)", "btn"),
    ("b7", "B7 High-G Mode (Square)", "btn"), ("b8", "B8 Autopilot (X)", "btn"),
    ("b9", "B9 Flare (Circle)", "btn"), ("b10", "B10 Change View (Triangle)", "btn"),
] + [(f"b{i}", f"B{i} (spare)", "btn") for i in range(11, 17)]
LABEL = {t: l for t, l, _ in TARGETS}
KIND = {t: k for t, _, k in TARGETS}


# --------------------------------------------------------------------------
# Source evaluation
# --------------------------------------------------------------------------
def source_value(src, state):
    """Normalised reading: buttons/hats 0|1, axes (raw-rest)/(peak-rest)."""
    dev = state.get(src["dev"])
    if dev is None:
        return 0.0
    try:
        if src["type"] == "button":
            return 1.0 if dev["buttons"][src["index"]] else 0.0
        if src["type"] == "hat":
            return 1.0 if dev["hats"][src["index"]][src["axis"]] == src["sign"] else 0.0
        raw = dev["axes"][src["index"]]
        span = src["peak"] - src["rest"]
        return (raw - src["rest"]) / span if span else 0.0
    except IndexError:
        return 0.0


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


def short(src):
    """Compact binding text for pasting over the picture."""
    dev = src["dev"].replace(" (winmm)", "")[:18]
    if src["type"] == "button":
        what = f'Btn {src["index"]}'
    elif src["type"] == "hat":
        what = f'Hat {src["index"]}' + {(0, -1): "<", (0, 1): ">", (1, 1): "^", (1, -1): "v"}[
            (src["axis"], src["sign"])]
    else:
        what = f'Axis {src["index"]}{"+" if src["peak"] > src["rest"] else "-"}'
    return f"{dev} {what}" + (" (inv)" if src.get("invert") else "")


def describe(src):
    if src["type"] == "button":
        text = f'{src["dev"]} - Button {src["index"]}'
    elif src["type"] == "hat":
        arrow = {(0, -1): "Left", (0, 1): "Right", (1, 1): "Up", (1, -1): "Down"}
        text = f'{src["dev"]} - Hat {src["index"]} {arrow[(src["axis"], src["sign"])]}'
    else:
        text = f'{src["dev"]} - Axis {src["index"]} ({"+" if src["peak"] > src["rest"] else "-"})'
    return text + (" [inverted]" if src.get("invert") else "")


# --------------------------------------------------------------------------
# winmm fallback: devices Windows exposes but SDL skips (e.g. Fanatec pedals)
# --------------------------------------------------------------------------
class _Caps(ctypes.Structure):
    _fields_ = [("mid", wintypes.WORD), ("pid", wintypes.WORD), ("name", wintypes.WCHAR * 32),
                ("lo", wintypes.UINT * 2), ("ymm", wintypes.UINT * 2), ("zmm", wintypes.UINT * 2),
                ("nb", wintypes.UINT), ("pmin", wintypes.UINT), ("pmax", wintypes.UINT),
                ("rmm", wintypes.UINT * 2), ("umm", wintypes.UINT * 2), ("vmm", wintypes.UINT * 2),
                ("caps", wintypes.UINT), ("maxaxes", wintypes.UINT), ("naxes", wintypes.UINT),
                ("maxb", wintypes.UINT), ("regkey", wintypes.WCHAR * 32),
                ("oem", wintypes.WCHAR * 260)]


class _Info(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in
                ("size", "flags", "x", "y", "z", "r", "u", "v", "buttons", "bnum", "pov",
                 "res1", "res2")]


VENDORS = {0x0EB7: "Fanatec", 0x231D: "VKB", 0x0483: "STM", 0x3434: "Keychron"}


def sdl_ids(sticks):
    """(vid, pid) of every SDL joystick, read from its GUID."""
    ids = set()
    for j in sticks.values():
        g = j.get_guid()
        try:
            ids.add((int(g[10:12] + g[8:10], 16), int(g[18:20] + g[16:18], 16)))
        except ValueError:
            pass
    return ids


class WinmmDevices:
    """Polls joystick-API devices whose vid/pid SDL did not enumerate."""

    def __init__(self):
        self.winmm = ctypes.windll.winmm
        self.slots = []         # (slot, name, [axis field names], [(min, max)])
        self.checked = 0.0
        self.known = None

    def refresh(self, skip):
        if self.known == skip and time.time() - self.checked < 2.0:
            return
        self.known, self.checked = set(skip), time.time()
        self.slots = []
        for slot in range(self.winmm.joyGetNumDevs()):
            c = _Caps()
            if self.winmm.joyGetDevCapsW(slot, ctypes.byref(c), ctypes.sizeof(c)) != 0:
                continue
            if (c.mid, c.pid) in skip:
                continue
            axes = [("x", c.lo), ("y", c.ymm)]
            for bit, field, rng in ((1, "z", c.zmm), (2, "r", c.rmm), (4, "u", c.umm), (8, "v", c.vmm)):
                if c.caps & bit:
                    axes.append((field, rng))
            name = f"{VENDORS.get(c.mid, 'Joystick')} {c.pid:04X} (winmm)"
            self.slots.append((slot, name, axes))

    def read(self):
        out = {}
        for slot, name, axes in self.slots:
            info = _Info()
            info.size, info.flags = ctypes.sizeof(_Info), 0xFF
            if self.winmm.joyGetPosEx(slot, ctypes.byref(info)) != 0:
                continue
            vals = []
            for field, rng in axes:
                lo, hi = rng[0], rng[1]
                vals.append(2.0 * (getattr(info, field) - lo) / (hi - lo) - 1.0 if hi > lo else 0.0)
            buttons = [bool(info.buttons >> i & 1) for i in range(32)]
            out[name] = {"axes": vals, "buttons": buttons, "hats": []}
        return out


# --------------------------------------------------------------------------
# Output: vJoy
# --------------------------------------------------------------------------
class VJoySink:
    # target -> vJoy axis name; this is the axis layout games will see
    AXES = {"sx": "X", "sy": "Y", "thr": "Z", "msx": "RX", "msy": "RY",
            "twist": "RZ", "lever": "SL0"}
    HAT = {(1, 0, 0, 0): 0, (1, 1, 0, 0): 4500, (0, 1, 0, 0): 9000, (0, 1, 1, 0): 13500,
           (0, 0, 1, 0): 18000, (0, 0, 1, 1): 22500, (0, 0, 0, 1): 27000, (1, 0, 0, 1): 31500}
    BUTTONS = [f"b{i}" for i in range(1, 17)]
    DISC = {0: 0, 4500: 0, 9000: 1, 13500: 1, 18000: 2, 22500: 2, 27000: 3, 31500: 3}  # diagonals pick one

    def __init__(self):
        from pyvjoystick import vjoy
        try:
            self.dev = vjoy.VJoyDevice(VJOY_ID)
        except Exception as ex:
            raise RuntimeError(
                f"vJoy device {VJOY_ID} isn't available ({type(ex).__name__}). Open 'Configure "
                f"vJoy', enable device {VJOY_ID} with axes X Y Z Rx Ry Rz Slider1, 16 buttons "
                f"and 1 continuous POV hat, then Apply.") from None
        self.usage = {t: getattr(vjoy.HID_USAGE, n) for t, n in self.AXES.items()}
        missing = [self.AXES[t] for t, u in self.usage.items() if u not in self.dev.available_axis]
        if missing or self.dev.number_of_buttons < 16:
            raise RuntimeError(
                f"vJoy device {VJOY_ID} is missing "
                f"{('axes ' + ', '.join(missing)) if missing else ''}"
                f"{' and ' if missing and self.dev.number_of_buttons < 16 else ''}"
                f"{'buttons (has %d, needs 16)' % self.dev.number_of_buttons if self.dev.number_of_buttons < 16 else ''}"
                f". Fix it in 'Configure vJoy'.")
        pov = self.dev.axis_limits.get(vjoy.HID_USAGE.POV)
        self.discrete_hat = pov is not None and pov.maxValue <= 3   # device has a 4-way hat
        self.last = None

    def send(self, out):
        axes = []
        for t in self.AXES:
            v = out.get(t, 0.0)
            v = v if KIND[t] == "trig" else (v + 1) / 2
            axes.append(min(32768, max(0, int(round(v * 32768)))))
        buttons = [bool(out.get(t)) for t in self.BUTTONS]
        hat = [bool(out.get(t)) for t in ("hup", "hright", "hdown", "hleft")]
        for a, b in ((0, 2), (1, 3)):                 # opposite directions cancel
            if hat[a] and hat[b]:
                hat[a] = hat[b] = False
        hat = self.HAT.get(tuple(map(int, hat)), -1)
        frame = (tuple(axes), tuple(buttons), hat)
        if frame == self.last:
            return
        self.last = frame
        for (t, usage), value in zip(self.usage.items(), axes):
            self.dev.set_axis(usage, value)
        for i, down in enumerate(buttons, 1):
            self.dev.set_button(i, down)
        # every Set* call is applied to the driver immediately; a following update() would
        # push the device's stale cached struct over the top and freeze the axes
        if self.discrete_hat:
            self.dev.set_disc_pov(1, self.DISC.get(hat, -1))
        else:
            self.dev.set_cont_pov(1, hat)

    def close(self):
        try:
            self.dev.reset()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Engine thread: owns pygame + the virtual device
# --------------------------------------------------------------------------
class Engine(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.mapping = {}
        self.state = {}
        self.outputs = {}
        self.devices = []
        self.running = False
        self.error = ""
        self._quit = False

    def run(self):
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        pygame.display.init()
        pygame.joystick.init()
        sticks = {}
        try:
            winmm = WinmmDevices()
        except Exception:
            winmm = None
        skip = None                 # vid/pid SDL already reads, handed to the scanner thread

        def scan():
            # a winmm rescan blocks for ~250-550 ms on this PC; doing it on the polling thread
            # froze virtual-device output every 2 s, so a second instance scans and hands over its slots
            scanner = WinmmDevices()
            while not self._quit:
                if skip is not None:        # scanning before SDL's list is known duplicates it
                    scanner.refresh(skip)
                    winmm.slots = list(scanner.slots)
                time.sleep(0.5)

        if winmm is not None:
            threading.Thread(target=scan, daemon=True).start()
        sink = None

        def add(index):
            j = pygame.joystick.Joystick(index)
            sticks.setdefault(j.get_instance_id(), j)

        for i in range(pygame.joystick.get_count()):
            add(i)

        while not self._quit:
            for e in pygame.event.get():
                if e.type == pygame.JOYDEVICEADDED:
                    add(e.device_index)
                elif e.type == pygame.JOYDEVICEREMOVED:
                    sticks.pop(e.instance_id, None)

            names, seen, state = [], {}, {}
            for iid in sorted(sticks):
                j = sticks[iid]
                seen[j.get_name()] = seen.get(j.get_name(), 0) + 1
            count = {}
            for iid in sorted(sticks):
                j = sticks[iid]
                n = j.get_name()
                count[n] = count.get(n, 0) + 1
                key = n if seen[n] == 1 else f"{n} #{count[n]}"
                names.append(key)
                state[key] = {
                    "axes": [j.get_axis(i) for i in range(j.get_numaxes())],
                    "buttons": [j.get_button(i) for i in range(j.get_numbuttons())],
                    "hats": [j.get_hat(i) for i in range(j.get_numhats())],
                }
            extra = None
            if winmm is not None:
                skip = sdl_ids(sticks)
                extra = winmm.read()
                state.update(extra)
                names += list(extra)
            self.state, self.devices = state, names
            outputs = compute_outputs(self.mapping, state)
            self.outputs = outputs

            if self.running and sink is None:
                try:
                    sink = VJoySink()
                    self.error = ""
                except Exception as ex:
                    self.error = f"vJoy output failed: {ex}"
                    self.running = False
            elif not self.running and sink is not None:
                sink.close()
                sink = None

            if sink is not None:
                sink.send(outputs)
            time.sleep(0.004)

        if sink is not None:
            sink.close()

    def stop(self):
        self._quit = True


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
S = 0.6                 # image scale (the mapping picture is 1920x1080)
OY = 0
IMG_W = int(1920 * S)
IMG_H = int(1080 * S)
CANVAS_H = IMG_H + 60   # strip under the picture for the spare buttons

# Hotspots sit on the callout targets of Thrustmaster's mapping picture (the PS5 edition).
# circles: (cx, cy, r) / rects: (cx, cy, hw, hh), in 1920x1080 picture coordinates
HOT = {
    "trigger": ("c", 1345, 418, 22), "weapon": ("c", 1412, 358, 20),
    "missile": ("c", 1262, 350, 18), "target": ("c", 1318, 305, 16),
    "camera": ("c", 1293, 288, 18), "hat": ("c", 1252, 262, 16),
    "stick": ("c", 1245, 675, 40), "twist": ("r", 1250, 560, 100, 20),
    "r2": ("c", 448, 318, 14), "l2": ("c", 457, 343, 12),
    "lever": ("r", 545, 358, 40, 18), "thr": ("c", 520, 628, 22),
    "b7": ("c", 627, 662, 16), "b8": ("c", 645, 700, 16),
    "b9": ("c", 651, 733, 16), "b10": ("c", 582, 770, 16),
}
# which targets each hotspot binds (in order); anything else binds the target of its own name
GROUPS = {
    "trigger": ("b1",), "weapon": ("b2",), "missile": ("b3",), "target": ("b4",),
    "camera": ("msx", "msy"), "hat": ("hup", "hright", "hdown", "hleft"),
    "stick": ("sx", "sy"), "r2": ("b5",), "l2": ("b6",),
}
STICKS = {"stick": ("sx", "sy"), "camera": ("msx", "msy")}   # these also get a live dot
# where the binding text is pasted over the picture: (x, y, anchor), picture coordinates
LABELS = {
    "trigger": (1665, 460, "w"), "weapon": (1665, 380, "w"), "missile": (1055, 332, "e"),
    "target": (975, 128, "e"), "camera": (1100, 184, "e"), "hat": (905, 252, "e"),
    "stick": (1900, 650, "e"), "twist": (1590, 499, "w"),
    "r2": (380, 318, "w"), "l2": (380, 393, "w"), "lever": (650, 518, "w"),
    "thr": (380, 597, "w"), "b7": (380, 662, "w"), "b8": (380, 718, "w"),
    "b9": (380, 760, "w"), "b10": (380, 804, "w"),
}
# chips (canvas coordinates): the buttons that aren't on the picture
CHIPS = {f"b{i}": (300 + 90 * (i - 11), IMG_H + 32, 28, 14, f"B{i}") for i in range(11, 17)}

BG = "#1b2430"
C_UNBOUND, C_BOUND, C_LISTEN, C_ACTIVE = "#ffd43b", "#40c057", "#ff922b", "#4dabf7"


class App:
    def __init__(self, root):
        self.root = root
        root.title("padmap HOTAS - multi-device to virtual HOTAS (vJoy)")
        self.engine = Engine()
        self.engine.start()
        self.cap = None
        self.items = {}     # hotspot id -> canvas item
        self.dots = {}

        self.k = 1.0        # current zoom of the drawn pad (window resizing)
        self.chip_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.texts = {}

        left = tk.Frame(root)
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
        self.img_src = Image.open(IMAGE).convert("RGB")
        self.photo = self._render_image()
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw", tags="img")
        self._build_hotspots()
        self.canvas.bind("<Configure>", self._on_resize)

        right = tk.Frame(root)
        right.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)
        tk.Label(right, text="Connected devices", anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x")
        self.dev_label = tk.Label(right, anchor="w", justify="left", fg="#333")
        self.dev_label.pack(fill="x", pady=(0, 8))
        self.tree = ttk.Treeview(right, columns=("src",), height=22,
                                 selectmode="browse")
        self.tree.heading("#0", text="Control")
        self.tree.heading("src", text="Bound to")
        self.tree.column("#0", width=250)
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
        self.dz_scale = tk.Scale(right, from_=0, to=50, orient="horizontal", resolution=1,
                                 label="Deadzone % (sticks and triggers)", state="disabled",
                                 command=self._set_deadzone)
        self.dz_scale.bind("<ButtonRelease-1>", lambda e: self._autosave())
        self.dz_scale.pack(fill="x", pady=(4, 0))
        self._syncing = False
        tk.Label(right, anchor="w", fg="#777", justify="left",
                 text="Double-click a row to bind it, Delete to clear.\n"
                      "Clicking a control on the pad selects its row.\n"
                      "Right-click a control on the pad to clear it.\n"
                      "Esc cancels a pending bind.").pack(fill="x", pady=(6, 0))

        root.minsize(900, 560)
        root.bind("<Escape>", lambda e: self.cancel())
        root.protocol("WM_DELETE_WINDOW", self.close)
        if os.path.exists(AUTOSAVE):
            self._load(AUTOSAVE, quiet=True)
        self._refresh_rows()
        self.tick()

    # -- drawing ----------------------------------------------------------
    def _render_image(self):
        size = (int(IMG_W * self.k), int(IMG_H * self.k))
        return ImageTk.PhotoImage(self.img_src.resize(size, Image.LANCZOS))

    def _on_resize(self, event):
        k = max(0.4, min(event.width / IMG_W, event.height / CANVAS_H))
        if abs(k - self.k) < 0.005:
            return
        self.canvas.scale("all", 0, 0, k / self.k, k / self.k)
        self.k = k
        self.photo = self._render_image()
        self.canvas.itemconfig("img", image=self.photo)
        self.canvas.coords("img", 0, 0)
        self.chip_font.config(size=max(6, round(9 * k)))
        self.label_font.config(size=max(6, round(9 * k)))
        self._update_labels()

    def _build_hotspots(self):
        c = self.canvas
        self.label_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        c.create_text(20, IMG_H + 32, anchor="w", font=self.label_font, fill="#adb5bd",
                      text="Spare buttons (not on the picture):")
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
        for hid in STICKS:
            self.dots[hid] = c.create_oval(0, 0, 0, 0, fill=C_ACTIVE, outline="")
        self.labels = {}
        for hid, (lx, ly, anchor) in LABELS.items():
            box = c.create_rectangle(0, 0, 0, 0, fill="#fff3bf", outline="#e67700", state="hidden")
            txt = c.create_text(lx * S, ly * S, anchor=anchor, font=self.label_font,
                                fill="#1b1b1b", state="hidden")
            self.labels[hid] = (box, txt)
        for hid, item in self.items.items():
            targets = list(GROUPS.get(hid, (hid,)))
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
        return GROUPS.get(hid, (hid,))

    def _update_labels(self):
        """Paste each control's current binding over the picture."""
        c, m = self.canvas, self.engine.mapping
        for hid, (box, txt) in self.labels.items():
            parts = [short(m[t]) for t in self._targets_of(hid) if t in m]
            if not parts:
                c.itemconfig(box, state="hidden")
                c.itemconfig(txt, state="hidden")
                continue
            text = " / ".join(parts) if len(parts) <= 2 else f"{len(parts)} directions bound"
            c.itemconfig(txt, text=text, state="normal")
            x0, y0, x1, y1 = c.bbox(txt)
            c.coords(box, x0 - 4, y0 - 2, x1 + 4, y1 + 2)
            c.itemconfig(box, state="normal")
            c.tag_raise(box)
            c.tag_raise(txt)

    # -- binding ----------------------------------------------------------
    def begin(self, targets):
        self.tree.selection_set(targets[0])
        self.tree.see(targets[0])
        self.cap = {"queue": list(targets), "phase": "start", "t0": time.time(),
                    "prev": None, "base": None}
        self._cap_next(settle=False)

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
        if self.cap["phase"] == "settle":
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
                    "hats": list(v["hats"])} for d, v in self.engine.state.items()}

    @staticmethod
    def _settled(base, now):
        for dev, b in base.items():
            n = now.get(dev)
            if n is None:
                continue
            if any(abs(x - y) > 0.2 for x, y in zip(n["axes"], b["axes"])):
                return False
            if any(n["buttons"]) or any(h != (0, 0) for h in n["hats"]):
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
        self._syncing = True
        if src and kind != "btn":
            default = DEADZONE if kind == "axis" else TRIGGER_DEADZONE
            self.dz_scale.config(state="normal")
            self.dz_scale.set(round(src.get("deadzone", default) * 100))
        else:
            self.dz_scale.set(0)
            self.dz_scale.config(state="disabled")
        self._syncing = False

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
        path = filedialog.asksaveasfilename(defaultextension=".json", initialdir=HERE,
                                            filetypes=[("Profile", "*.json")])
        if path:
            self._dump(path)
            self.status.config(text=f"Saved {os.path.basename(path)}.", fg="#444")

    def load_from(self):
        path = filedialog.askopenfilename(initialdir=HERE, filetypes=[("Profile", "*.json")])
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
        self._update_labels()

    def tick(self):
        eng = self.engine
        if self.cap:
            self._poll_capture()
        if eng.error:
            self.status.config(text=eng.error, fg="#c92a2a")
            eng.error = ""
            self.run_btn.config(text="Start")
        self.dev_label.config(text="\n".join(eng.devices) or "(none found)")
        out, m = eng.outputs, eng.mapping
        listening = set(self._targets_of_active())
        for hid, item in self.items.items():
            targets = self._targets_of(hid)
            if hid in listening:
                color, fill = C_LISTEN, ""
            elif any(isinstance(out.get(t), bool) and out[t] or
                     (KIND[t] == "trig" and out.get(t, 0) > 0.1) or
                     (KIND[t] == "axis" and abs(out.get(t, 0)) > 0.15) for t in targets):
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
        self.root.after(20, self.tick)

    def _targets_of_active(self):
        if not self.cap:
            return []
        t = self.cap["target"]
        return [hid for hid in self.items if t in self._targets_of(hid)]

    def close(self):
        self.engine.running = False
        self.engine.stop()
        self.engine.join(timeout=1)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
