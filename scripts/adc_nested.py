"""Nested mode for adc: the agent gets a private X server (Xephyr) on its own workspace.

Isolation:
  * Xephyr's host window is created with _NET_WM_DESKTOP preset and user time 0, so Mutter maps
    it straight onto the agent workspace without focus (no flash, no focus steal);
  * private X server  -> own pointer, keyboard focus, clipboard/selections;
  * private D-Bus session + private AT-SPI bus -> single-instance apps and notifications cannot
    leak onto the user's desktop; agent-desktop only sees the agent's apps;
  * physical input (xdotool, agent-desktop --headed) only touches the nested display.
"""
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
AGENT_ROOT = os.path.expanduser("~/.local/share/adc")
AGENT_HOME = os.path.join(AGENT_ROOT, "agent-home")   # isolated XDG config/data/state/cache


def xdg_env(env):
    """Apps in the nested display get their own settings, session state and recent files, so they
    never restore or modify the user's open documents. $HOME (real files) stays shared."""
    for k, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                   ("XDG_CACHE_HOME", "cache")):
        d = os.path.join(AGENT_HOME, sub)
        os.makedirs(d, mode=0o700, exist_ok=True)
        env[k] = d
    return env


# ------------------------------------------------------------------ helpers
def free_display():
    for n in range(7, 60):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    raise RuntimeError("no free X display number")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def nenv(st, real_dbus=False):
    """Environment for processes that live inside the nested display."""
    env = dict(os.environ)
    env["DISPLAY"] = st["display"]
    env.pop("WAYLAND_DISPLAY", None)
    env["GDK_BACKEND"] = "x11"
    env["QT_QPA_PLATFORM"] = "xcb"
    env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
    env["ACCESSIBILITY_ENABLED"] = "1"
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    env["GDK_DEBUG"] = "no-portals"   # GTK4: portals on the private bus would stall startup ~25 s
    env["GTK_USE_PORTAL"] = "0"
    env["GSK_RENDERER"] = "cairo"     # Xephyr has no DRI3; skip GL probing
    xdg_env(env)
    if not real_dbus:
        env["DBUS_SESSION_BUS_ADDRESS"] = st["dbus"]
    if st.get("a11y_bus"):
        env["AT_SPI_BUS_ADDRESS"] = st["a11y_bus"]
    return env


def xdo(st, *args):
    return subprocess.run(["xdotool", *map(str, args)], env=nenv(st), capture_output=True, text=True)


def spawn(cmd, env, log):
    return subprocess.Popen(cmd, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            start_new_session=True).pid


# ------------------------------------------------------------------ start / stop
DBUS_ALLOW = ("org.a11y.Bus.service", "ca.desrt.dconf.service", "org.gtk.vfs.Daemon.service")


def dbus_config(st_dir, sid):
    """Private session bus that can only auto-start accessibility, dconf and gvfs.
    No portals, notification daemons or shell services -> nothing stalls, nothing leaks out."""
    sdir = os.path.join(st_dir, f"{sid}-services")
    os.makedirs(sdir, exist_ok=True)
    for f in DBUS_ALLOW:
        src = os.path.join("/usr/share/dbus-1/services", f)
        dst = os.path.join(sdir, f)
        if os.path.exists(src) and not os.path.exists(dst):
            os.symlink(src, dst)
    conf = os.path.join(st_dir, f"{sid}-dbus.conf")
    with open(conf, "w") as fh:
        fh.write(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><keep_umask/><listen>unix:tmpdir={st_dir}</listen>
<auth>EXTERNAL</auth><standard_session_servicedirs/>
<servicedir>{sdir}</servicedir>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/>
<allow own="*"/></policy></busconfig>
""")
    # standard_session_servicedirs would re-add everything; drop it
    txt = open(conf).read().replace("<standard_session_servicedirs/>", "")
    open(conf, "w").write(txt)
    return conf


def start(st_dir, sid, desk, size, record, overlay_py, log):
    n = free_display()
    disp = f":{n}"
    w, h = map(int, size.lower().split("x"))
    st = {"mode": "nested", "display": disp, "size": [w, h], "pids": {}}
    # host window on the agent workspace + Xephyr inside it
    host = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "adc-host.py"), "--desktop", str(desk),
                             "--display", disp, "--size", f"{w}x{h}", "--title", f"adc agent {disp}"],
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    st["pids"]["host"] = host.pid
    for _ in range(100):
        if os.path.exists(f"/tmp/.X11-unix/X{n}"):
            break
        time.sleep(0.05)
    for _ in range(60):
        if subprocess.run(["xdpyinfo"], env=dict(os.environ, DISPLAY=disp), capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Xephyr did not come up (see overlay.log)")
    # private session bus
    out = subprocess.run(["dbus-daemon", f"--config-file={dbus_config(st_dir, sid)}", "--fork",
                          "--print-address=1", "--print-pid=1"], capture_output=True, text=True,
                         env=xdg_env(dict(os.environ))).stdout.split()  # dconf-service inherits isolated XDG
    st["dbus"], st["pids"]["dbus"] = out[0], int(out[1])
    env = dict(os.environ, DISPLAY=disp, DBUS_SESSION_BUS_ADDRESS=st["dbus"])
    env.pop("AT_SPI_BUS_ADDRESS", None)
    # private accessibility bus (auto-activated through the private session bus)
    a11y = ""
    for _ in range(30):
        r = subprocess.run(["dbus-send", "--session", "--print-reply=literal", "--dest=org.a11y.Bus",
                            "/org/a11y/bus", "org.a11y.Bus.GetAddress"], env=env, capture_output=True, text=True)
        a11y = r.stdout.strip()
        if a11y:
            break
        time.sleep(0.2)
    st["a11y_bus"] = a11y
    subprocess.run(["dbus-send", "--session", "--dest=org.a11y.Bus", "/org/a11y/bus",
                    "org.freedesktop.DBus.Properties.Set", "string:org.a11y.Status", "string:IsEnabled",
                    "variant:boolean:true"], env=env, capture_output=True)
    e = nenv(st)
    # window manager (optional) + compositor (for overlay alpha)
    for wm in ("openbox", "xfwm4", "metacity", "icewm", "fluxbox", "jwm", "twm"):
        if shutil.which(wm):
            st["pids"]["wm"] = spawn([wm], e, log)
            st["wm"] = wm
            break
    else:
        st["wm"] = None
    if shutil.which("picom"):
        st["pids"]["compositor"] = spawn(["picom", "--backend", "xrender", "--no-fading-openclose"], e, log)
        for _ in range(40):  # wait until picom owns the compositing selection
            if subprocess.run(["xprop", "-root", "_NET_WM_CM_S0"], env=e, capture_output=True).returncode == 0 and \
                    _cm_owned(e):
                break
            time.sleep(0.1)
    subprocess.run(["xsetroot", "-solid", "#2b2f3a"], env=e, capture_output=True)
    copy_keyboard_layout(e)
    time.sleep(0.6)
    sock = os.path.join(st_dir, f"{sid}.sock")
    st["socket"] = sock
    oe = dict(e, NO_AT_BRIDGE="1")  # the overlay must not show up in the agent's accessibility tree
    oe.pop("GTK_MODULES", None)
    st["pids"]["overlay"] = spawn([sys.executable, "-I", overlay_py, "--socket", sock, "--desktop", "0",
                                   "--no-arrow"], oe, log)
    xdo(st, "mousemove", w // 2, h // 2)
    if record:
        record_start(st, record, log)
    return st


def _cm_owned(env):
    code = ("import gi;gi.require_version('Gdk','3.0');from gi.repository import Gdk;"
            "print(Gdk.Screen.get_default().is_composited())")
    r = subprocess.run([sys.executable, "-I", "-c", code], env=dict(env, NO_AT_BRIDGE="1"), capture_output=True,
                       text=True)
    return r.stdout.strip() == "True"


def copy_keyboard_layout(nested_env):
    """Xephyr starts with a US keymap; give it the user's layout so the user can type (logins via
    ask-user) and the agent's keystrokes produce the same symbols."""
    q = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True).stdout
    opts = dict(re.findall(r"^(\w+):\s*(\S+)", q, re.M))
    args = ["setxkbmap"]
    for k in ("model", "layout", "variant", "options"):
        if opts.get(k):
            args += [f"-{'option' if k == 'options' else k}", opts[k]]
    if len(args) > 1:
        subprocess.run(args, env=nested_env, capture_output=True)


def stop(st):
    record_stop(st)
    for k in ("overlay", "compositor", "wm"):
        _kill(st["pids"].get(k))
    # graceful first: the main process alone gets SIGTERM so browsers flush cookies/sessions
    apps = [p for p in st.get("apps", []) if alive(p)]
    for pid in apps:
        _kill(pid)
    deadline = time.time() + 8
    while time.time() < deadline and any(alive(p) for p in apps):
        time.sleep(0.2)
    for pid in apps:
        _kill(pid, group=True)  # leftovers (helpers) only
    _kill(st["pids"].get("host"))
    time.sleep(0.3)
    _kill(st["pids"].get("dbus"))


def _kill(pid, group=False):
    if not pid:
        return
    try:
        if group:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ------------------------------------------------------------------ recording
def record_start(st, path, log):
    if alive(st.get("pids", {}).get("ffmpeg")):
        return {"ok": False, "error": "already recording"}
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w, h = st["size"]
    pid = spawn(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-draw_mouse", "1", "-framerate", "30",
                 "-video_size", f"{w}x{h}", "-i", st["display"], "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", path], dict(os.environ), log)
    st["pids"]["ffmpeg"] = pid
    st["recording"] = path
    return {"ok": True, "path": path}


def record_stop(st):
    pid = st.get("pids", {}).get("ffmpeg")
    if not alive(pid):
        return {"ok": False, "error": "not recording"}
    os.kill(pid, signal.SIGINT)
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.1)
    st["pids"].pop("ffmpeg", None)
    return {"ok": True, "path": st.pop("recording", None)}


# ------------------------------------------------------------------ human-like input
def pointer(st):
    o = xdo(st, "getmouselocation").stdout
    m = re.search(r"x:(\d+) y:(\d+)", o)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def glide(st, ov, x, y, label=None, agent="agent"):
    """Move the nested real pointer like a hand: eased, slight arc, label follows."""
    x0, y0 = pointer(st)
    dist = math.hypot(x - x0, y - y0)
    ms = int(max(120, min(650, 120 + dist * 0.45)))
    ov({"op": "move", "agent": agent, "x": x, "y": y, "ms": ms, "bow": 0, "label": label})
    if dist < 2:
        return
    n = max(6, ms // 16)
    nx, ny = -(y - y0) / dist, (x - x0) / dist
    bow = min(40, dist * 0.08) * (1 if (int(x0 + y0) % 2) else -1)
    chain = []
    for i in range(1, n + 1):
        t = i / n
        e = 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2
        arc = math.sin(math.pi * e) * bow
        chain += ["mousemove", int(x0 + (x - x0) * e + nx * arc), int(y0 + (y - y0) * e + ny * arc),
                  "sleep", f"{ms / n / 1000:.3f}"]
    chain += ["mousemove", int(x), int(y)]
    xdo(st, *chain)


def click(st, ov, x, y, button=1, count=1, label=None, agent="agent", mods=()):
    glide(st, ov, x, y, label, agent)
    for m in mods:
        xdo(st, "keydown", m)
    xdo(st, "click", "--repeat", count, "--delay", 90, button)
    for m in reversed(mods):
        xdo(st, "keyup", m)
    ov({"op": "click", "agent": agent, "x": x, "y": y})


def drag(st, ov, x1, y1, x2, y2, label=None, agent="agent"):
    glide(st, ov, x1, y1, label, agent)
    xdo(st, "mousedown", 1)
    ov({"op": "click", "agent": agent, "x": x1, "y": y1})
    dist = math.hypot(x2 - x1, y2 - y1)
    n = max(10, int(dist / 12))
    chain = []
    for i in range(1, n + 1):
        t = i / n
        chain += ["mousemove", int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t), "sleep", "0.012"]
    ov({"op": "move", "agent": agent, "x": x2, "y": y2, "ms": int(n * 14), "bow": 0})
    xdo(st, *chain)
    time.sleep(0.15)
    xdo(st, "mouseup", 1)


def focus(st, target=None):
    """Make sure keystrokes land where intended. target = window-name substring; otherwise the
    top-level window under the pointer gets focus if nothing (or something else) has it.
    Returns the focused window's name so the caller can verify before typing."""
    if target:
        ids = xdo(st, "search", "--onlyvisible", "--name", target).stdout.split()
        if not ids:
            raise RuntimeError(f"no window matching {target!r}")
        xdo(st, "windowactivate", "--sync", ids[-1])
    else:
        under = re.search(r"WINDOW=(\d+)", xdo(st, "getmouselocation", "--shell").stdout)
        active = xdo(st, "getactivewindow").stdout.strip()
        if under and (not active or active == "0"):
            xdo(st, "windowactivate", "--sync", under.group(1))
    return xdo(st, "getactivewindow", "getwindowname").stdout.strip()


def key(st, ov, combo, label=None, agent="agent"):
    if label:
        ov({"op": "label", "agent": agent, "label": label})
    xdo(st, "key", "--clearmodifiers", combo)
    time.sleep(0.08)


def _paste(st, chunk):
    # private display -> private clipboard: the user's clipboard is never touched
    # xclip forks to keep serving the selection: don't hold its stdout/stderr pipes
    subprocess.run(["xclip", "-selection", "clipboard", "-i"], input=chunk.encode(), env=nenv(st),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    xdo(st, "key", "--clearmodifiers", "ctrl+v")
    time.sleep(0.05)


def write(st, ov, text, label=None, agent="agent", delay=45):
    """Type like a person. Xephyr's keymap cannot produce accents/ñ/emoji and xdotool drops
    newlines, so: ASCII runs are typed, '\n' becomes Return, other characters are pasted
    through the nested display's own clipboard."""
    if label:
        ov({"op": "label", "agent": agent, "label": label})
    run = ""

    def flush():
        nonlocal run
        if run:
            xdo(st, "type", "--delay", delay, run)
            run = ""
    for ch in text:
        if ch == "\n":
            flush()
            xdo(st, "key", "Return")
        elif ch == "\t":
            flush()
            xdo(st, "key", "Tab")
        elif 32 <= ord(ch) < 127:
            run += ch
        else:
            flush()
            _paste(st, ch)
    flush()


def scroll(st, ov, x, y, direction, amount, agent="agent"):
    glide(st, ov, x, y, None, agent)
    btn = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
    xdo(st, "click", "--repeat", amount, "--delay", 40, btn)


# ------------------------------------------------------------------ vision
def see(st, out, scale=0.5, grid=100, region=None):
    """Screenshot of the nested display, downscaled, with a labelled coordinate grid
    (labels are REAL nested coordinates, so read them and click those numbers)."""
    env = nenv(st)
    raw = out + ".raw.png"
    subprocess.run(["import", "-silent", "-window", "root", raw], env=env, check=True)
    w, h = st["size"]
    crop = []
    ox, oy = 0, 0
    if region:
        ox, oy, cw, ch = region
        crop = ["-crop", f"{cw}x{ch}+{ox}+{oy}", "+repage"]
        w, h = cw, ch
    draw = []
    if grid:
        for gx in range((ox // grid + 1) * grid if ox % grid else ox, ox + w, grid):
            X = (gx - ox) * scale
            draw += ["-stroke", "#ff00ff60", "-draw", f"line {X},0 {X},{h * scale}",
                     "-stroke", "none", "-fill", "#ff00ff", "-draw", f"text {X + 2},10 '{gx}'"]
        for gy in range((oy // grid + 1) * grid if oy % grid else oy, oy + h, grid):
            Y = (gy - oy) * scale
            draw += ["-stroke", "#ff00ff60", "-draw", f"line 0,{Y} {w * scale},{Y}",
                     "-stroke", "none", "-fill", "#ff00ff", "-draw", f"text 2,{Y - 2} '{gy}'"]
    subprocess.run(["convert", raw, *crop, "-resize", f"{int(scale * 100)}%", "-pointsize", "10", *draw, out],
                   check=True)
    os.unlink(raw)
    return {"path": out, "scale": scale, "origin": [ox, oy], "grid": grid,
            "note": "grid labels are nested-screen coordinates; pass them to click-xy"}


# ------------------------------------------------------------------ apps
def launch(st, cmd, log, real_dbus=False):
    pid = spawn(cmd, nenv(st, real_dbus=real_dbus), log)
    st.setdefault("apps", []).append(pid)
    return pid


CHROME_SKIP = {"Cache", "Code Cache", "GPUCache", "Service Worker", "DawnGraphiteCache", "DawnWebGPUCache",
               "GrShaderCache", "ShaderCache", "optimization_guide_model_store", "History Provider Cache"}


def browser(st, run_dir, url, profile=None, log=None, name=None):
    """Chrome inside the nested display. --profile copies a user profile (logins included) into
    a private user-data-dir; Chrome keeps using the real keyring through the real session bus,
    while its accessibility tree goes to the private a11y bus."""
    bin_ = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    # persistent agent profiles: log in once (adc ask-user), stay logged in across sessions
    tag = name or (("copy-" + profile) if profile else "default")
    udd = os.path.join(AGENT_ROOT, "browsers", re.sub(r"[^\w.-]", "_", tag))
    os.makedirs(os.path.dirname(udd), mode=0o700, exist_ok=True)
    src_root = os.path.expanduser("~/.config/google-chrome")
    if profile and not os.path.isdir(os.path.join(udd, "Default")):
        src = os.path.join(src_root, profile)
        if not os.path.isdir(src):
            raise RuntimeError(f"profile dir not found: {src}")
        os.makedirs(udd, mode=0o700, exist_ok=True)
        shutil.copy2(os.path.join(src_root, "Local State"), os.path.join(udd, "Local State"))
        shutil.copytree(src, os.path.join(udd, "Default"), symlinks=True,
                        ignore=lambda d, names: [n for n in names if n in CHROME_SKIP or n.startswith("Singleton")])
    w, h = st["size"]
    cmd = [bin_, f"--user-data-dir={udd}", "--no-first-run", "--no-default-browser-check",
           "--force-renderer-accessibility", "--disable-features=Translate", "--disable-gpu",
           "--password-store=basic", "--window-position=0,0", f"--window-size={w},{h}",
           "--new-window", url]
    # basic store: cookies are encrypted with a fixed key instead of the keyring/portal secret,
    # which changed between runs and made Chrome drop every login. The profile dir is 0700.
    os.chmod(udd, 0o700) if os.path.isdir(udd) else None
    return launch(st, cmd, log, real_dbus=False), udd


# ------------------------------------------------------------------ hand-off to the user
_STRINGS = {
    "en": {"app": "Agent (adc)", "title": "The agent needs your help", "go": "Go now", "later": "Not now",
           "turn": "Your turn", "done": "I'm done", "waiting": "Waiting for the user…", "thanks": "Thanks, resuming",
           "turn_msg": "When you are finished, press \u201cI'm done\u201d to return to where you were."},
    "es": {"app": "Agente (adc)", "title": "El agente necesita tu ayuda", "go": "Ir ahora", "later": "Ahora no",
           "turn": "Tu turno", "done": "He terminado", "waiting": "Esperando al usuario…", "thanks": "Gracias, continúo",
           "turn_msg": "Cuando acabes, pulsa \u00abHe terminado\u00bb y volverás a donde estabas."},
}


def _t(key):
    lang = (os.environ.get("ADC_LANG") or os.environ.get("LANG") or "en")[:2]
    return _STRINGS.get(lang, _STRINGS["en"])[key]


def _notify(msg, title, actions, timeout):
    args = ["notify-send", "-a", _t("app"), "-i", "dialog-password", "-u", "critical", title, msg]
    for k, v in actions:
        args += ["-A", f"{k}={v}"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or "closed"
    except subprocess.TimeoutExpired:
        return "timeout"


def _host_prop(name):
    o = subprocess.run(["xprop", "-root", name], capture_output=True, text=True).stdout
    m = re.search(r"(0x[0-9a-fA-F]+|\d+)\s*$", o.strip())
    return m.group(1) if m else None


def ask_user(st, ov, msg, title=None, timeout=900):
    """System notification on the USER's desktop (logins, captchas, 2FA, confirmations).
    'Ir ahora' takes the user to the agent workspace with the agent's window focused, so their own
    keyboard types into the agent display; 'He terminado' returns them where they were."""
    ov({"op": "label", "agent": "agent", "label": _t("waiting")})
    ans = _notify(msg, title or _t("title"), [("go", _t("go")), ("later", _t("later"))], timeout)
    if ans != "go":
        return {"answer": ans}
    prev_desk, prev_win = _host_prop("_NET_CURRENT_DESKTOP"), _host_prop("_NET_ACTIVE_WINDOW")
    host = subprocess.run(["xdotool", "search", "--class", "adc-agent"], capture_output=True, text=True).stdout.split()
    subprocess.run(["xdotool", "set_desktop", str(st["desktop"])], capture_output=True)
    if host:
        subprocess.run(["xdotool", "windowactivate", host[0]], capture_output=True)
    done = _notify(_t("turn_msg"), _t("turn"), [("done", _t("done"))], timeout)
    if _host_prop("_NET_CURRENT_DESKTOP") == str(st["desktop"]) and prev_desk is not None:
        subprocess.run(["xdotool", "set_desktop", prev_desk], capture_output=True)
        if prev_win and prev_win not in ("0x0", "0"):
            subprocess.run(["xdotool", "windowactivate", str(int(prev_win, 0))], capture_output=True)
    ov({"op": "label", "agent": "agent", "label": _t("thanks")})
    return {"answer": done}
