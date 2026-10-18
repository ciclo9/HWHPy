# HWHPy | HWH

HWHPy is a remote control tool for Windows computers on the same local network. Run `host.py` on the computer being controlled and `receiver.py` on the computer connecting to it.

## Requirements

- A Windows computer with Python 3.10 or newer to run `host.py`.
- Another device on the same local network to run `receiver.py`. You can use a phone if it has a Python 3.10+ environment that can run scripts and access the local network.
- Install these Python dependencies on the host computer:

```powershell
python -m pip install psutil pyperclip imageio-ffmpeg Pillow
```

## Basic usage

On the host computer, run:

```powershell
python host.py
```

On the other device, run:

```powershell
python receiver.py
```

The client tries to discover the host automatically on the local network. If it cannot find the host, it asks for the connection code shown when `host.py` starts. Type `help` in the client to see the available commands.

## IP address and privacy

The creator's private IP address is **not hardcoded**. The host detects the local IP address of the computer running it and uses that address during the connection. This version does not add new entries to `host.log`; logs, screenshots, and recordings are excluded by `.gitignore`.

The connection code contains the local IP address and port encoded in Base64. Base64 is reversible: **the code is not a password and does not secure the connection**. This version does not authenticate clients or encrypt traffic. Use it only on a trusted network and with the consent of the person using the host computer. Do not expose the port to the Internet.
