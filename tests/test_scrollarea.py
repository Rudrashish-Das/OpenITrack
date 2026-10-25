"""ScrollArea: scrolls only when its content is taller than its space."""

import tkinter as tk
from tkinter import ttk

import pytest

from openitrack.scrollarea import ScrollArea


@pytest.fixture
def area(shown_root):
    shown_root.geometry("400x300")
    a = ScrollArea(shown_root, padding=12)
    a.pack(fill="both", expand=True)
    return a


def fill(area, height):
    block = ttk.Frame(area.body, height=height, width=50)
    block.pack(fill="x")
    area.winfo_toplevel().update()
    return block


def wheel(widget, notches):
    widget.event_generate("<MouseWheel>", delta=int(120 * notches))
    widget.winfo_toplevel().update()


def test_no_scrollbar_when_the_content_fits(area):
    fill(area, 100)
    assert not area.scrollable and not area.bar.winfo_manager()
    # The content is stretched to the whole area, so expanding layouts still work.
    assert area.body.winfo_height() == area.canvas.winfo_height()
    assert area.body.winfo_width() == area.canvas.winfo_width()


def test_scrollbar_appears_and_goes_with_the_content(area):
    block = fill(area, 900)
    assert area.scrollable and area.bar.winfo_manager() == "place"
    assert area.body.winfo_width() == area.canvas.winfo_width()  # the bar takes no width
    block.configure(height=50)
    area.winfo_toplevel().update()
    assert not area.scrollable and not area.bar.winfo_manager()


def test_the_wheel_scrolls_only_what_can_scroll(area):
    fill(area, 100)
    wheel(area.body, -1)
    assert area.canvas.yview()[0] == 0
    block = fill(area, 900)
    wheel(block, -3)  # down, over a plain widget in the page
    assert area.canvas.yview()[0] > 0
    wheel(block, 10)  # back up, past the top
    assert area.canvas.yview()[0] == 0


def test_widgets_that_scroll_themselves_keep_the_wheel(area):
    fill(area, 900)
    text = tk.Text(area.body, height=4)
    text.insert("end", "line\n" * 200)
    text.pack()
    area.winfo_toplevel().update()
    wheel(text, -3)
    assert area.canvas.yview()[0] == 0  # the page stayed put


def test_the_wheel_over_a_dropdown_scrolls_the_page_not_the_value(area):
    fill(area, 900)
    box = ttk.Combobox(area.body, values=["a", "b", "c"], state="readonly")
    box.current(1)
    box.pack()
    area.winfo_toplevel().update()
    wheel(box, -2)
    assert box.get() == "b"
    assert area.canvas.yview()[0] > 0


def test_an_area_not_laid_out_yet_shows_no_scrollbar(tk_root):
    a = ScrollArea(tk_root)  # never packed: 1x1
    ttk.Frame(a.body, height=900).pack()
    tk_root.update()
    assert not a.scrollable and not a.bar.winfo_manager()
