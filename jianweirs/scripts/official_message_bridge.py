"""Read Ovital WebSocket settings through its official Windows message API.

This is deliberately a getter: the published SDK has command 20850 but no
WebSocket-settings setter.  It does not enable the interface or edit Ovital's
configuration.  It never asks for custom-map definitions or prints PSKs.

Usage (same Windows session / integrity level as the running desktop app):
    python scripts/official_message_bridge.py
    python scripts/official_message_bridge.py --write-env .env

The optional export writes ONLY OMAP_WS_URL and OMAP_PSK to this project's local
environment file.  It is only available after an enabled WS/WSS service is read.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import struct
import time


WM_COPYDATA = 0x004A
OMAP_WIN_API_FLAG = 1001
REGISTER = 20000
GET_VERSION = 20006
GET_WEB_SOCKET_CONF = 20850
API_VERSION_V3 = 2


class BridgeError(RuntimeError):
    pass


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


class _RegisterAlignment(ctypes.Union):
    _fields_ = [("wsClass", ctypes.c_void_p), ("alignment", ctypes.c_double)]


class OmapRegister(ctypes.Structure):
    _fields_ = [("dwZero", wintypes.DWORD), ("iApiVer", ctypes.c_int),
                ("iRet", ctypes.c_int * 20), ("alignment", _RegisterAlignment)]


class OfficialMessageReader:
    """A hidden callback window implementing OMapApi.cpp's registration flow."""

    def __init__(self, timeout: float = 5) -> None:
        if os.name != "nt":
            raise BridgeError("This official desktop message API requires Windows.")
        self.timeout = timeout
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.class_name = f"OmapLocalReadOnly_{os.getpid()}"
        self.responses: dict[int, bytes] = {}
        self.callback_error = False
        self.hwnd = None
        self.atom = None
        self._prepare()

    def _prepare(self) -> None:
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        api = self.user32
        api.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        api.DefWindowProcW.restype = LRESULT
        api.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        api.FindWindowW.restype = wintypes.HWND
        api.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        api.RegisterClassW.restype = wintypes.ATOM
        api.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        api.CreateWindowExW.restype = wintypes.HWND
        api.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        api.SendMessageTimeoutW.restype = LRESULT
        api.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        api.PeekMessageW.restype = wintypes.BOOL
        api.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        api.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        api.DispatchMessageW.restype = LRESULT
        api.DestroyWindow.argtypes = [wintypes.HWND]
        api.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        @WNDPROC
        def callback(hwnd, message, wparam, lparam):
            try:
                if message == WM_COPYDATA and wparam == OMAP_WIN_API_FLAG and lparam:
                    incoming = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
                    command = incoming.dwData & 0xFFFF
                    # Only this explicitly requested configuration is copied. No
                    # custom maps, image data, user records, or unknown payloads.
                    if command == GET_WEB_SOCKET_CONF and incoming.lpData and 76 <= incoming.cbData <= 4096:
                        self.responses[command] = ctypes.string_at(incoming.lpData, incoming.cbData)
                        return 1
                    return 0
            except Exception:
                self.callback_error = True
                return 0
            return api.DefWindowProcW(hwnd, message, wparam, lparam)

        self._callback = callback  # ctypes callback must remain alive.
        self.instance = self.kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = callback
        wc.hInstance = self.instance
        wc.lpszClassName = self.class_name
        self.atom = api.RegisterClassW(ctypes.byref(wc))
        if not self.atom:
            raise BridgeError(f"Cannot register callback window (Windows error {ctypes.get_last_error()}).")
        # Never call ShowWindow: this API receiver has no visible user interface.
        self.hwnd = api.CreateWindowExW(0, self.class_name, self.class_name, 0,
                                      0, 0, 0, 0, None, None, self.instance, None)
        if not self.hwnd:
            self.close()
            raise BridgeError("Cannot create hidden callback window.")
        self.target = api.FindWindowW("omapWin", None)
        if not self.target:
            self.close()
            raise BridgeError("No running Ovital desktop window was found.")

    def _send(self, command: int, app_id: int = 0, payload: bytes | None = None) -> int:
        buffer = ctypes.create_string_buffer(payload) if payload else None
        outgoing = COPYDATASTRUCT()
        outgoing.dwData = (app_id << 16) | command
        outgoing.cbData = len(payload) if payload else 0
        outgoing.lpData = ctypes.cast(buffer, ctypes.c_void_p).value if buffer is not None else None
        result = ctypes.c_size_t()
        sent = self.user32.SendMessageTimeoutW(
            self.target, WM_COPYDATA, OMAP_WIN_API_FLAG,
            ctypes.addressof(outgoing), 0, int(self.timeout * 1000), ctypes.byref(result))
        if not sent:
            raise BridgeError(f"Official message call failed or timed out (Windows error {ctypes.get_last_error()}).")
        return ctypes.c_ssize_t(result.value).value

    def read(self) -> tuple[dict, str]:
        registration = OmapRegister()
        registration.iApiVer = API_VERSION_V3
        payload = bytes(registration) + self.class_name.encode("utf-16-le") + b"\0\0"
        app_id = self._send(REGISTER, payload=payload)
        if app_id == -10:
            raise BridgeError("Ovital's third-party message interface is disabled (official error -10). Enable it in desktop settings.")
        if app_id <= 0 or app_id > 0xFFFF:
            raise BridgeError(f"Official message registration was rejected (code {app_id}).")
        version = self._send(GET_VERSION, app_id)
        result = self._send(GET_WEB_SOCKET_CONF, app_id)
        if result < 0:
            raise BridgeError(f"WebSocket configuration getter was rejected (code {result}).")
        deadline = time.monotonic() + self.timeout
        message = wintypes.MSG()
        while GET_WEB_SOCKET_CONF not in self.responses:
            if self.callback_error:
                raise BridgeError("Could not parse the official configuration callback.")
            if time.monotonic() >= deadline:
                raise BridgeError("No WebSocket configuration callback arrived before the timeout.")
            while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.01)
        data = self.responses.pop(GET_WEB_SOCKET_CONF)
        flag, allowed_ip, port = struct.unpack_from("<iII", data)
        psk = data[12:76].split(b"\0", 1)[0].decode("utf-8", errors="strict")
        http_tiles = struct.unpack_from("<i", data, 844)[0] if len(data) >= 848 else None
        interface_type = struct.unpack_from("<I", data, 848)[0] if len(data) >= 852 else None
        # Only documented flag values are interpreted; raw certificate paths and
        # reserved bytes never leave the reader.
        if flag not in (0, 1, 2) or not 0 <= port <= 65535:
            raise BridgeError("The returned WebSocket configuration does not match the official structure.")
        config = {
            "version": version, "app_id": app_id, "service_flag": flag,
            "enabled": flag in (1, 2), "protocol": {0: None, 1: "ws", 2: "wss"}[flag],
            "port": port, "allowed_ip_raw": allowed_ip,
            # Some desktop builds redact strPsk even when a key is configured.
            # An empty getter field therefore does not establish an empty key.
            "psk_returned": bool(psk), "psk_present": True if psk else None,
            "http_tile_service": bool(http_tiles) if http_tiles is not None else None,
            "interface_type": interface_type,
            "websocket_interface_selected": interface_type in (3, 4) if interface_type is not None else None,
            "public_setter_available": False,
            "configuration_bytes": len(data),
        }
        return config, psk

    def close(self) -> None:
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.atom:
            self.user32.UnregisterClassW(self.class_name, self.instance)
            self.atom = None


def export_env(path: Path, config: dict, psk: str) -> None:
    if not config["enabled"] or not config["port"]:
        raise BridgeError("The WebSocket service is disabled; no runtime environment was exported.")
    if not psk:
        raise BridgeError("The official getter returned no PSK; it may be redacted. Existing runtime credentials were not modified. Set the known key locally.")
    # Single quotes protect dollar signs and backslashes from dotenv interpolation.
    if any(ch in psk for ch in "\r\n'"):
        raise BridgeError("The PSK contains characters requiring manual environment-file setup.")
    values = {"OMAP_WS_URL": f"{config['protocol']}://127.0.0.1:{config['port']}", "OMAP_PSK": f"'{psk}'"}
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    found = set()
    output = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in values:
            if key not in found:
                output.append(f"{key}={values[key]}")
                found.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in values.items() if key not in found)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--write-env", type=Path, help="Export enabled WS endpoint and PSK locally, never to stdout.")
    args = parser.parse_args()
    reader = None
    try:
        if not 0 < args.timeout <= 30:
            raise BridgeError("timeout must be between 0 and 30 seconds.")
        reader = OfficialMessageReader(args.timeout)
        config, psk = reader.read()
        if args.write_env:
            export_env(args.write_env.resolve(), config, psk)
            config["environment_exported"] = True
        print(json.dumps(config, ensure_ascii=False))
        return 0
    except (BridgeError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if reader:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
