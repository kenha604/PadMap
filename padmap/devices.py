"""Shared device layer: input-source evaluation, winmm fallback and the one polling thread
that feeds every output (virtual Xbox pad, keyboard/mouse) with the same device state."""
import ctypes
import os
import threading
import time
from ctypes import wintypes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402


# --------------------------------------------------------------------------
# Source evaluation
# --------------------------------------------------------------------------
def lever_pos(src, state):
    """0 fully back .. 1 fully forward; ignores where the lever sat when it was bound."""
    raw = state[src["dev"]]["axes"][src["index"]]
    return (raw * (1 if src["peak"] > src["rest"] else -1) + 1) / 2


def source_value(src, state):
    """Normalised reading: buttons/hats 0|1, axes (raw-rest)/(peak-rest),
    twin-lever steering (left - right)."""
    if src["type"] == "diff":
        try:
            diff = lever_pos(src["left"], state) - lever_pos(src["right"], state)
            return diff * src.get("gain", 1.0)      # compute_outputs clamps to full lock
        except (KeyError, IndexError):
            return 0.0
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


def describe(src):
    if src["type"] == "diff":
        text = "Lever steering: " + " / ".join(
            f'{s["dev"]} axis {s["index"]}' for s in (src["left"], src["right"]))
    elif src["type"] == "button":
        text = f'{src["dev"]} - Button {src["index"]}'
    elif src["type"] == "hat":
        arrow = {(0, -1): "Left", (0, 1): "Right", (1, 1): "Up", (1, -1): "Down"}
        text = f'{src["dev"]} - Hat {src["index"]} {arrow[(src["axis"], src["sign"])]}'
    else:
        text = f'{src["dev"]} - Axis {src["index"]} ({"+" if src["peak"] > src["rest"] else "-"})'
    return text + (" [inverted]" if src.get("invert") else "")


def snapshot(state):
    return {d: {"axes": list(v["axes"]), "buttons": list(v["buttons"]),
                "hats": list(v["hats"])} for d, v in state.items()}


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
# Reader thread: owns pygame, polls every device once per tick and hands the
# state to each registered output ("sink")
# --------------------------------------------------------------------------
class DeviceReader(threading.Thread):
    """A sink is any object with process(state, dt) and shutdown()."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sinks = []
        self.state = {}
        self.devices = []
        self._quit = False

    def add_sink(self, sink):
        self.sinks = self.sinks + [sink]

    def run(self):
        try:
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
        last = time.perf_counter()

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
                n = sticks[iid].get_name()
                seen[n] = seen.get(n, 0) + 1
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
            if winmm is not None:
                skip = sdl_ids(sticks)
                extra = winmm.read()
                state.update(extra)
                names += list(extra)
            self.state, self.devices = state, names

            now = time.perf_counter()
            dt, last = min(0.05, now - last), now
            for sink in self.sinks:
                sink.process(state, dt)
            time.sleep(0.004)

        for sink in self.sinks:
            sink.shutdown()

    def stop(self):
        self._quit = True
