#!/usr/bin/env python3
"""adc-overlay: visual-only agent cursor overlay for Linux X11 (GNOME/Mutter tested).

Draws one or more fake "agent cursors" in a transparent, click-through,
override-redirect window. It never moves the real pointer, never takes focus,
and is only shown while the user is looking at the agent's workspace
(_NET_CURRENT_DESKTOP == --desktop). Controlled over a Unix socket with
newline-delimited JSON: one request line -> one reply line.

Ops:
  {"op":"ping"}
  {"op":"move","agent":"a","x":..,"y":..,"label":"..."}      -> {"ok":true,"ms":travel}
  {"op":"click","agent":"a","x":..,"y":..,"rect":[x,y,w,h]}  ripple + outline
  {"op":"type","agent":"a","rect":[x,y,w,h]}                 caret pulse + outline
  {"op":"label","agent":"a","label":"..."}
  {"op":"color","agent":"a","color":"#RRGGBB"}
  {"op":"desktop","n":2}
  {"op":"hide","agent":"a"}
  {"op":"quit"}
"""
import argparse
import json
import math
import os
import random
import socket
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
import cairo  # noqa: E402

PALETTE = ["#4299FF", "#FF6B4A", "#2BC48A", "#B36BFF", "#FFB020", "#FF4FA3", "#22C3E6", "#8BC34A"]


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def ease(t):
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


class Agent:
    def __init__(self, name, color, x, y):
        self.name = name
        self.color = color
        self.x, self.y = x, y
        self.path = None  # (p0, p1, p2, t0, dur)
        self.label = name
        self.effects = []  # dicts with kind, t0, dur, ...
        self.visible = True
        self.arrow = True
        self.born = time.monotonic()

    def bezier(self, t):
        (x0, y0), (cx, cy), (x1, y1) = self.path[:3]
        u = 1 - t
        return (u * u * x0 + 2 * u * t * cx + t * t * x1, u * u * y0 + 2 * u * t * cy + t * t * y1)


class Overlay:
    def __init__(self, desktop, sock_path):
        self.desktop = desktop
        self.sock_path = sock_path
        self.agents = {}
        self.busy_until = 0.0
        self.guard_reverts = 0
        self.user_win = None
        self.arrow = True
        self.lock = threading.Lock()
        self.current_desktop = None
        self.anim_running = False

        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_app_paintable(True)
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        self.win.set_title("adc-overlay")
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None:
            print("adc-overlay: no RGBA visual; overlay would be opaque", flush=True)
        else:
            # use alpha even if the compositor is not up yet; it becomes transparent once it is
            self.win.set_visual(visual)
        self.sw, self.sh = screen.get_width(), screen.get_height()
        self.win.move(0, 0)
        self.win.resize(self.sw, self.sh)
        self.win.connect("draw", self.on_draw)
        self.win.connect("realize", self.on_realize)
        self.win.realize()

    # ---------- window plumbing ----------
    def on_realize(self, w):
        # Empty input shape -> every click passes through to the windows below.
        w.input_shape_combine_region(cairo.Region())

    def set_shown(self):
        want = self.current_desktop is None or self.current_desktop == self.desktop
        if want and not self.win.get_visible():
            self.win.show_all()
            self.win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
        elif not want and self.win.get_visible():
            self.win.hide()
        return False

    def watch_desktop(self):
        # Event-driven: xprop -spy prints a line whenever the property changes.
        while True:
            try:
                p = subprocess.Popen(["xprop", "-root", "-spy", "_NET_CURRENT_DESKTOP"],
                                     stdout=subprocess.PIPE, text=True)
                for line in p.stdout:
                    try:
                        self.current_desktop = int(line.strip().rsplit("=", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    GLib.idle_add(self.set_shown)
            except OSError:
                pass
            time.sleep(1)

    # ---------- focus guard ----------
    @staticmethod
    def _xprop(args):
        try:
            return subprocess.run(["xprop"] + args, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def win_desktop(self, xid):
        import re
        m = re.search(r"=\s*(\d+)", self._xprop(["-id", hex(xid), "_NET_WM_DESKTOP"]))
        return int(m.group(1)) if m else None

    def guard(self):
        """Undo any focus/workspace change the agent's apps cause on the user's side.

        Rules (user = whoever is not on the agent workspace):
        * a window on the agent workspace becomes active while the user looks at another
          workspace -> re-activate the user's last window (no workspace switch happens);
        * the visible workspace jumps to the agent's within `busy` time of an agent action
          (launch, click...) -> jump back to the user's workspace and window.
        A switch the user makes on their own (outside agent activity) is respected, so they
        can go and watch the agent.
        """
        import re
        self.user_win = None
        self.user_desk = self.current_desktop
        last_desk = self.current_desktop
        p = subprocess.Popen(["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW", "_NET_CURRENT_DESKTOP"],
                             stdout=subprocess.PIPE, text=True)
        for line in p.stdout:
            now = time.monotonic()
            busy = now < self.busy_until
            if line.startswith("_NET_CURRENT_DESKTOP"):
                try:
                    d = int(line.rsplit("=", 1)[1])
                except (ValueError, IndexError):
                    continue
                if d == self.desktop and last_desk is not None and last_desk != self.desktop and busy:
                    self.restore(last_desk)
                    self.log(f"guard: reverted workspace jump {last_desk}->{d}")
                    continue
                last_desk = d
                if d != self.desktop:
                    self.user_desk = d
            elif line.startswith("_NET_ACTIVE_WINDOW"):
                m = re.search(r"0x[0-9a-fA-F]+", line)
                if not m:
                    continue
                xid = int(m.group(0), 16)
                if xid == 0:
                    continue
                wd = self.win_desktop(xid)
                cur = self.current_desktop
                if wd == self.desktop and cur is not None and cur != self.desktop:
                    self.restore(None)
                    self.log(f"guard: agent window {hex(xid)} took focus; restored {self.user_win}")
                elif wd is not None and wd != self.desktop:
                    self.user_win = xid

    def restore(self, desk):
        if desk is not None:
            subprocess.run(["xdotool", "set_desktop", str(desk)], capture_output=True)
        if self.user_win:
            subprocess.run(["xdotool", "windowactivate", str(self.user_win)], capture_output=True)
        self.guard_reverts += 1

    def log(self, msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    # ---------- agents ----------
    def agent(self, name):
        a = self.agents.get(name)
        if a is None:
            color = PALETTE[len(self.agents) % len(PALETTE)]
            a = Agent(name, color, self.sw / 2 + 40 * len(self.agents), self.sh / 2)
            a.arrow = self.arrow
            self.agents[name] = a
        return a

    def start_anim(self):
        if not self.anim_running:
            self.anim_running = True
            GLib.timeout_add(16, self.tick)

    def tick(self):
        now = time.monotonic()
        busy = False
        with self.lock:
            for a in self.agents.values():
                if a.path:
                    p0, c, p1, t0, dur = a.path
                    t = min(1.0, (now - t0) / dur)
                    a.x, a.y = a.bezier(ease(t))
                    if t >= 1.0:
                        a.x, a.y = p1
                        a.path = None
                    else:
                        busy = True
                a.effects = [e for e in a.effects if now - e["t0"] < e["dur"]]
                if a.effects:
                    busy = True
                if now - a.born < 1.2:
                    busy = True
        self.win.queue_draw()
        if not busy:
            self.anim_running = False
        return busy

    def handle(self, req):
        op = req.get("op")
        name = str(req.get("agent") or "agent")[:64]
        now = time.monotonic()
        with self.lock:
            if op == "ping":
                return {"ok": True, "desktop": self.desktop, "current_desktop": self.current_desktop,
                        "agents": list(self.agents), "guard_reverts": self.guard_reverts,
                        "user_window": hex(self.user_win) if self.user_win else None}
            if op == "busy":
                self.busy_until = max(self.busy_until, now + float(req.get("ms", 3000)) / 1000)
                return {"ok": True}
            if op == "quit":
                GLib.idle_add(Gtk.main_quit)
                return {"ok": True}
            if op == "desktop":
                self.desktop = int(req["n"])
                GLib.idle_add(self.set_shown)
                return {"ok": True}
            a = self.agent(name)
            a.visible = True
            if "label" in req and req["label"] is not None:
                a.label = str(req["label"])[:80]
            if op == "color":
                a.color = req.get("color", a.color)
            elif op == "label":
                pass
            elif op == "warp":
                a.x, a.y, a.path = float(req["x"]), float(req["y"]), None
                GLib.idle_add(self.win.queue_draw)
            elif op == "think":
                # scanning ring while the agent reads the UI (snapshot/find/get)
                a.effects.append({"kind": "think", "t0": now, "dur": float(req.get("ms", 900)) / 1000})
            elif op == "hide":
                a.visible = False
            elif op == "move":
                x1, y1 = float(req["x"]), float(req["y"])
                dx, dy = x1 - a.x, y1 - a.y
                dist = math.hypot(dx, dy)
                dur = max(0.12, min(0.7, 0.12 + dist * 0.00045))
                if req.get("ms"):
                    dur = max(0.01, float(req["ms"]) / 1000)
                # Curved path: control point offset perpendicular to the travel.
                bow = random.uniform(-0.18, 0.18) * dist * float(req.get("bow", 1))
                nx, ny = (-dy / dist, dx / dist) if dist > 1 else (0, 0)
                cx, cy = (a.x + x1) / 2 + nx * bow, (a.y + y1) / 2 + ny * bow
                a.path = ((a.x, a.y), (cx, cy), (x1, y1), now, dur)
                GLib.idle_add(self.start_anim)
                return {"ok": True, "ms": int(dur * 1000)}
            elif op in ("click", "type"):
                if "x" in req and "y" in req:
                    a.x, a.y, a.path = float(req["x"]), float(req["y"]), None
                if op == "click":
                    a.effects.append({"kind": "ripple", "t0": now, "dur": 0.55, "x": a.x, "y": a.y})
                else:
                    a.effects.append({"kind": "caret", "t0": now, "dur": 1.4, "rect": req.get("rect")})
                if req.get("rect"):
                    a.effects.append({"kind": "outline", "t0": now, "dur": 1.0, "rect": req["rect"]})
                GLib.idle_add(self.start_anim)
            else:
                return {"ok": False, "error": f"unknown op {op!r}"}
            GLib.idle_add(self.start_anim)
            return {"ok": True}

    # ---------- socket ----------
    def serve(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        srv.listen(16)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()

    def client(self, conn):
        with conn, conn.makefile("rw") as f:
            for line in f:
                try:
                    resp = self.handle(json.loads(line))
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": str(e)}
                f.write(json.dumps(resp) + "\n")
                f.flush()

    # ---------- drawing ----------
    def on_draw(self, w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        now = time.monotonic()
        with self.lock:
            agents = list(self.agents.values())
            for a in agents:
                if not a.visible:
                    continue
                r, g, b = hex_rgb(a.color)
                for e in a.effects:
                    t = (now - e["t0"]) / e["dur"]
                    if e["kind"] == "ripple":
                        for k in (0, 0.25):
                            tt = t - k
                            if 0 <= tt <= 1:
                                cr.set_source_rgba(r, g, b, 0.85 * (1 - tt))
                                cr.set_line_width(3)
                                cr.arc(e["x"], e["y"], 6 + 30 * ease(tt), 0, 2 * math.pi)
                                cr.stroke()
                    elif e["kind"] == "outline" and e.get("rect"):
                        x, y, ww, hh = e["rect"]
                        alpha = 1 - max(0, (t - 0.6) / 0.4)
                        self.rounded(cr, x - 3, y - 3, ww + 6, hh + 6, 6)
                        cr.set_source_rgba(r, g, b, 0.18 * alpha)
                        cr.fill_preserve()
                        cr.set_source_rgba(r, g, b, 0.95 * alpha)
                        cr.set_line_width(2.5)
                        cr.stroke()
                    elif e["kind"] == "think":
                        ang = (now - e["t0"]) * 7
                        cr.set_source_rgba(r, g, b, 0.9 * (1 - t * 0.5))
                        cr.set_line_width(2.5)
                        cr.arc(a.x + 6, a.y + 8, 15, ang, ang + 4.2)
                        cr.stroke()
                    elif e["kind"] == "caret" and e.get("rect"):
                        x, y, ww, hh = e["rect"]
                        if int((now - e["t0"]) * 4) % 2 == 0:
                            cr.set_source_rgba(r, g, b, 1)
                            cr.rectangle(x + min(ww - 6, 8), y + hh * 0.2, 2.5, hh * 0.6)
                            cr.fill()
                self.draw_cursor(cr, a, now)

    @staticmethod
    def rounded(cr, x, y, w, h, rad):
        cr.new_sub_path()
        cr.arc(x + w - rad, y + rad, rad, -math.pi / 2, 0)
        cr.arc(x + w - rad, y + h - rad, rad, 0, math.pi / 2)
        cr.arc(x + rad, y + h - rad, rad, math.pi / 2, math.pi)
        cr.arc(x + rad, y + rad, rad, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def draw_cursor(self, cr, a, now):
        r, g, b = hex_rgb(a.color)
        fade = min(1.0, (now - a.born) / 0.4)
        cr.save()
        cr.translate(a.x, a.y)
        if not a.arrow:  # real pointer is drawn by X; only the label pill
            self._pill(cr, a, r, g, b, fade)
            cr.restore()
            return
        s = 1.25
        pts = [(0, 0), (0, 17), (4.2, 13.2), (7.2, 20), (10, 18.8), (7.1, 12.2), (12.6, 12.2)]
        # shadow
        cr.save()
        cr.translate(1.5, 2)
        cr.move_to(*[v * s for v in pts[0]])
        for p in pts[1:]:
            cr.line_to(p[0] * s, p[1] * s)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.35 * fade)
        cr.fill()
        cr.restore()
        cr.move_to(*[v * s for v in pts[0]])
        for p in pts[1:]:
            cr.line_to(p[0] * s, p[1] * s)
        cr.close_path()
        cr.set_source_rgba(r, g, b, fade)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, fade)
        cr.set_line_width(1.6)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        self._pill(cr, a, r, g, b, fade)
        cr.restore()

    def _pill(self, cr, a, r, g, b, fade):
        if a.label:
            cr.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(a.label)
            px, py = 18, 24
            pw, ph = ext.x_advance + 16, 22
            self.rounded(cr, px, py, pw, ph, 11)
            cr.set_source_rgba(r, g, b, 0.95 * fade)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, fade)
            cr.move_to(px + 8, py + 15)
            cr.show_text(a.label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--desktop", type=int, required=True)
    ap.add_argument("--no-arrow", action="store_true", help="real pointer visible; draw only label/effects")
    args = ap.parse_args()
    ov = Overlay(args.desktop, args.socket)
    ov.arrow = not args.no_arrow
    try:
        out = subprocess.run(["xprop", "-root", "_NET_CURRENT_DESKTOP"], capture_output=True, text=True).stdout
        ov.current_desktop = int(out.strip().rsplit("=", 1)[1])
    except Exception:  # noqa: BLE001
        ov.current_desktop = None
    ov.set_shown()
    threading.Thread(target=ov.watch_desktop, daemon=True).start()
    threading.Thread(target=ov.serve, daemon=True).start()
    threading.Thread(target=ov.guard, daemon=True).start()

    def keep_on_top():
        # without a WM (nested Xephyr) keep_above is ignored; restack ourselves
        if ov.win.get_visible() and ov.win.get_window():
            ov.win.get_window().raise_()
        return True
    GLib.timeout_add(500, keep_on_top)
    try:
        Gtk.main()
    finally:
        try:
            os.unlink(args.socket)
        except OSError:
            pass


if __name__ == "__main__":
    main()
