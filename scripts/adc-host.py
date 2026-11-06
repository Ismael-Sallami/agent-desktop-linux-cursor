#!/usr/bin/env python3
"""Host window for the agent's nested X server.

Creates an X11 top-level window with _NET_WM_DESKTOP preset (Mutter maps it directly on that
workspace), _NET_WM_USER_TIME=0 and WM_HINTS input=False (never activated on map), then runs
`Xephyr -parent <window>` inside it and lives as long as Xephyr does.
"""
import argparse
import ctypes as C
import signal
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--desktop", type=int, required=True)
ap.add_argument("--display", required=True)
ap.add_argument("--size", default="1600x900")
ap.add_argument("--title", default="adc agent")
a = ap.parse_args()
W, H = map(int, a.size.split("x"))

x = C.CDLL("libX11.so.6")
x.XOpenDisplay.restype = C.c_void_p
x.XOpenDisplay.argtypes = [C.c_char_p]
x.XDefaultRootWindow.restype = C.c_ulong
x.XDefaultRootWindow.argtypes = [C.c_void_p]
x.XCreateSimpleWindow.restype = C.c_ulong
x.XCreateSimpleWindow.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_uint,
                                  C.c_ulong, C.c_ulong]
x.XInternAtom.restype = C.c_ulong
x.XInternAtom.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
x.XChangeProperty.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong, C.c_ulong, C.c_int, C.c_int, C.c_void_p, C.c_int]
x.XStoreName.argtypes = [C.c_void_p, C.c_ulong, C.c_char_p]
x.XMapWindow.argtypes = [C.c_void_p, C.c_ulong]
x.XSync.argtypes = [C.c_void_p, C.c_int]
x.XDestroyWindow.argtypes = [C.c_void_p, C.c_ulong]
x.XCloseDisplay.argtypes = [C.c_void_p]

dpy = x.XOpenDisplay(None)
if not dpy:
    sys.exit("cannot open host display")
root = x.XDefaultRootWindow(dpy)
win = x.XCreateSimpleWindow(dpy, root, 40, 40, W, H, 0, 0, 0x2b2f3a)


def atom(n):
    return x.XInternAtom(dpy, n.encode(), 0)


def card(prop, *vals, typ="CARDINAL"):
    arr = (C.c_long * len(vals))(*vals)
    x.XChangeProperty(dpy, win, atom(prop), atom(typ), 32, 0, arr, len(vals))


x.XStoreName(dpy, win, a.title.encode())
name = a.title.encode()
x.XChangeProperty(dpy, win, atom("_NET_WM_NAME"), atom("UTF8_STRING"), 8, 0, name, len(name))
cls = b"adc-agent\0Adc-agent\0"
x.XChangeProperty(dpy, win, atom("WM_CLASS"), atom("STRING"), 8, 0, cls, len(cls))
card("_NET_WM_DESKTOP", a.desktop)
card("_NET_WM_USER_TIME", 0)
# WM_HINTS: flags=InputHint(1), input=False  -> WM never gives it focus on map
x.XChangeProperty(dpy, win, atom("WM_HINTS"), atom("WM_HINTS"), 32, 0,
                  (C.c_long * 9)(1, 0, 0, 0, 0, 0, 0, 0, 0), 9)
# fixed size hints so the WM keeps the nested screen 1:1
hints = (C.c_long * 18)(*([16 | 32] + [0] * 4 + [W, H, W, H] + [0] * 9))  # PMinSize|PMaxSize
x.XChangeProperty(dpy, win, atom("WM_NORMAL_HINTS"), atom("WM_SIZE_HINTS"), 32, 0, hints, 18)
x.XMapWindow(dpy, win)
x.XSync(dpy, 0)

xe = subprocess.Popen(["Xephyr", a.display, "-parent", str(win), "-screen", f"{W}x{H}", "-ac", "-br",
                       "-noreset", "-nolisten", "tcp"])


def bye(*_):
    xe.terminate()


signal.signal(signal.SIGTERM, bye)
signal.signal(signal.SIGINT, bye)
xe.wait()
x.XDestroyWindow(dpy, win)
x.XCloseDisplay(dpy)
