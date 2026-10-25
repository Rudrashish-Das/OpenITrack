import tkinter as tk

import pytest


@pytest.fixture
def tk_root(capfd):
    """A hidden Tk root window; skips the test where Tk can't open one."""
    # Tcl sets up its stdout/stderr channels when the window opens. With pytest's
    # output capture in place it then sometimes fails to load its own startup
    # scripts ("Can't find a usable tk.tcl"), so open it with capture paused.
    with capfd.disabled():
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"Tk can't open a window: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def shown_root(tk_root):
    """A root that counts as shown (Tk lays out only shown windows) but is see-through."""
    tk_root.attributes("-alpha", 0.0)
    tk_root.deiconify()
    return tk_root
