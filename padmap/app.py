"""padmap - one window, two tabs: devices -> virtual Xbox 360 pad, devices -> keyboard/mouse."""
import tkinter as tk
from tkinter import ttk

from .devices import DeviceReader
from .keys import KeysTab
from .xbox import XboxTab


def main():
    root = tk.Tk()
    root.title("padmap")
    root.minsize(900, 600)
    reader = DeviceReader()
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    tabs = [XboxTab(notebook, reader), KeysTab(notebook, reader)]
    notebook.add(tabs[0], text="Xbox controller")
    notebook.add(tabs[1], text="Keyboard / mouse")
    reader.start()

    def close():
        for tab in tabs:
            tab.shutdown()
        reader.stop()
        reader.join(timeout=1)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
