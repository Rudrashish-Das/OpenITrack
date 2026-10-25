"""A frame that scrolls only if its content turns out taller than the space it gets.

Every tab and setup page is laid out to fit the window (tests/test_window_fit.py),
so normally no scrollbar shows. This is the safety net for when something
doesn't fit anyway: a screen too small for the window, unexpected fonts, or
content longer than planned. Nothing is ever cut off unreachably.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

SCROLL_STEP = 20        # pixels per scroll unit
WHEEL_UNITS = 2         # units per mouse-wheel notch
# Widgets that scroll themselves: the wheel is theirs while the pointer is over them.
_SELF_SCROLLING = (tk.Text, ttk.Treeview, tk.Listbox)


class ScrollArea(ttk.Frame):
    """Put content in `.body` (pack or grid it as usual). The body is stretched to
    the area's full size, so expanding layouts behave as in a plain frame."""

    def __init__(self, parent, padding=0, **kw):
        # `padding` goes around the content; with 12+ on the right the scrollbar,
        # when shown, covers only padding.
        super().__init__(parent, **kw)
        bg = ttk.Style().lookup("TFrame", "background") or None
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, width=1, height=1,
                                yscrollincrement=SCROLL_STEP,
                                **({"background": bg} if bg else {}))
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._pending = False
        self.canvas.bind("<Configure>", lambda e: self._schedule())
        _install_handlers(self)

    @property
    def scrollable(self) -> bool:
        view_h = self.canvas.winfo_height()
        return view_h > 1 and self.body.winfo_reqheight() > view_h + 1  # 1 = not laid out yet

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)

    def _schedule(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._update)

    def _update(self) -> None:
        self._pending = False
        if not self.winfo_exists():
            return
        view_w, view_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if view_w <= 1 or view_h <= 1:
            return  # not laid out yet (e.g. a tab not shown so far): nothing to decide
        need_h = self.body.winfo_reqheight()
        self.canvas.itemconfigure(self._window, width=view_w, height=max(view_h, need_h))
        self.canvas.configure(scrollregion=(0, 0, view_w, max(view_h, need_h)))
        # The bar is laid over the right edge (the body's padding), not beside the
        # content: taking width would re-wrap text taller and keep it needed.
        if need_h > view_h + 1:
            if not self.bar.winfo_manager():
                self.bar.place(relx=1, rely=0, relheight=1, anchor="ne")
        elif self.bar.winfo_manager():
            self.bar.place_forget()
            self.canvas.yview_moveto(0)

    def wheel(self, delta: int) -> bool:
        """Scroll for a mouse-wheel delta (120 per notch). False if there's nothing to scroll."""
        if not self.scrollable:
            return False
        notches = delta / 120
        units = round(-notches * WHEEL_UNITS) or (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")
        return True


def _install_handlers(widget: tk.Misc) -> None:
    root = widget.winfo_toplevel()
    if getattr(root, "_scrollarea_handlers", False):
        return
    root._scrollarea_handlers = True
    root.bind_all("<MouseWheel>", _on_wheel, add="+")
    # The body has a fixed height while it's stretched, so it isn't told when its
    # content asks for more or less room. Every widget that changes size is, though:
    # any resize inside an area makes it check again (batched, once per idle moment).
    root.bind_all("<Configure>", _on_configure, add="+")
    # Dropdowns change their value on the wheel. Inside a scrolling page that turns
    # scrolling past one into an unnoticed settings change, so the wheel scrolls the
    # page instead (values still change by clicking or with the arrow keys).
    root.bind_class("TCombobox", "<MouseWheel>", lambda e: None)


def _on_configure(event) -> None:
    w = event.widget
    while w is not None and not isinstance(w, str):
        if isinstance(w, ScrollArea):
            w._schedule()
        w = getattr(w, "master", None)


def _on_wheel(event) -> str | None:
    w = event.widget
    if isinstance(w, str):  # Tk-internal window, e.g. an open dropdown's list
        return None
    while w is not None:
        if isinstance(w, _SELF_SCROLLING):
            return None
        if isinstance(w, ScrollArea) and w.wheel(event.delta):
            return "break"
        w = getattr(w, "master", None)
    return None
