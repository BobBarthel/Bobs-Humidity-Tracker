import json
import logging
import platform
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict

import webview

from bluetooth_service import DEFAULT_CONFIG, load_config, save_config, BluetoothService
from data_logging import DataLogger, Row


class AppAPI:
    def __init__(self, data_queue: queue.Queue, status_queue: queue.Queue, data_rows: list[Row]):
        self._data_queue = data_queue
        self._status_queue = status_queue
        self._data_rows = data_rows
        self._stopping = False
        self._window = None
        self._stop_ui = threading.Event()
        self._config = self._sanitize_config(load_config())
        self._sleep_inhibitor: subprocess.Popen | None = None
        self._logger = DataLogger(self._data_rows)
        self._ble = BluetoothService(self._data_queue, self._status_queue, self._config)
        self._ble.set_devices_callback(self._send_devices)
        self._ble.set_identity_callback(self._handle_identity)

    def set_window(self, window):
        self._window = window

    def toggle_connect(self):
        if self._ble.is_running() and not self._stopping:
            self.stop()
        else:
            self.start()
        return True

    def start(self):
        if self._ble.is_running():
            return
        self._stopping = False
        self._ble.start()

    def stop(self):
        self._stopping = True
        self._ble.stop()

    def reset_data(self):
        self._logger.clear()
        return True

    def get_config(self):
        return self._config

    def get_platform(self):
        return platform.system()

    def scan_devices(self, timeout: float | None = None):
        return self._ble.scan_devices(timeout=timeout)

    def start_scan(self):
        return self._ble.start_scan()

    def stop_scan(self):
        return self._ble.stop_scan()

    def verify_device(self, address: str):
        return self._ble.verify_device(address)

    def select_device(self, device: Dict[str, Any]):
        name = str(device.get("name") or "").strip()
        address = str(device.get("address") or "").strip()
        serial = str(device.get("serial") or "").strip()
        if not name:
            return {"ok": False, "message": "Missing device name."}
        self._update_selected_device(name, address, serial)
        return {"ok": True}

    def set_device_nickname(self, identifier: str, nickname: str):
        known_devices = self._get_known_devices()
        target = self._find_known_device(known_devices, identifier)
        if not target:
            return {"ok": False, "message": "Device not found."}
        target["nickname"] = nickname.strip()
        self._config["known_devices"] = known_devices
        save_config(self._config)
        return {"ok": True, "devices": self._get_known_devices()}

    def forget_device(self, identifier: str):
        known_devices = self._get_known_devices()
        updated = [device for device in known_devices if not self._is_identifier_match(device, identifier)]
        if len(updated) == len(known_devices):
            return {"ok": False, "message": "Device not found."}
        self._config["known_devices"] = updated
        if self._is_identifier_match(self._current_selection(), identifier):
            self._config["device_address"] = ""
            self._config["device_serial"] = ""
        save_config(self._config)
        return {"ok": True, "devices": self._get_known_devices()}

    def update_known_device(self, identifier: str, updates: Dict[str, Any]):
        known_devices = self._get_known_devices()
        target = self._find_known_device(known_devices, identifier)
        if not target:
            return {"ok": False, "message": "Device not found."}
        name = str(updates.get("name") or "").strip()
        nickname = str(updates.get("nickname") or "").strip()
        if name:
            target["name"] = name
        target["nickname"] = nickname
        self._config["known_devices"] = known_devices
        if self._is_identifier_match(self._current_selection(), identifier):
            if name:
                self._config["device_name"] = name
        save_config(self._config)
        return {"ok": True, "devices": self._get_known_devices()}

    def save_config(self, new_config: Dict[str, Any]):
        cleaned = {**self._config}
        for key in DEFAULT_CONFIG:
            if key in new_config:
                cleaned[key] = new_config[key]
        cleaned["scan_timeout"] = self._safe_float(cleaned["scan_timeout"], DEFAULT_CONFIG["scan_timeout"])
        cleaned["reconnect_delay"] = self._safe_float(cleaned["reconnect_delay"], DEFAULT_CONFIG["reconnect_delay"])
        cleaned["debounce_sec"] = self._safe_float(cleaned["debounce_sec"], DEFAULT_CONFIG["debounce_sec"])
        cleaned["table_max_rows"] = self._safe_int(cleaned["table_max_rows"], DEFAULT_CONFIG["table_max_rows"])
        if not str(cleaned.get("device_name", "")).strip():
            cleaned["device_name"] = DEFAULT_CONFIG["device_name"]
        cleaned = self._sanitize_config(cleaned)
        self._config = cleaned
        self._ble.update_config(self._config)
        save_config(self._config)
        return {"ok": True}

    def _safe_float(self, value: Any, default: float) -> float:
        try:
            result = float(value)
            if result != result:
                return default
            return result
        except Exception:
            return default

    def _safe_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _normalize_dialog_path(self, path):
        if isinstance(path, (list, tuple)):
            return path[0] if path else None
        return path

    def _sanitize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = {**DEFAULT_CONFIG, **(config or {})}
        cleaned["device_address"] = str(cleaned.get("device_address", "") or "").strip()
        cleaned["device_serial"] = str(cleaned.get("device_serial", "") or "").strip()
        known_devices = cleaned.get("known_devices")
        if not isinstance(known_devices, list):
            known_devices = []
        cleaned["known_devices"] = [
            self._normalize_known_device(device) for device in known_devices if isinstance(device, dict)
        ]
        return cleaned

    def _normalize_known_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": str(device.get("name") or "").strip(),
            "address": str(device.get("address") or "").strip(),
            "serial": str(device.get("serial") or "").strip(),
            "nickname": str(device.get("nickname") or "").strip(),
        }

    def _get_known_devices(self) -> list[Dict[str, Any]]:
        return [self._normalize_known_device(device) for device in self._config.get("known_devices", [])]

    def _current_selection(self) -> Dict[str, Any]:
        return {
            "name": self._config.get("device_name", ""),
            "address": self._config.get("device_address", ""),
            "serial": self._config.get("device_serial", ""),
        }

    def _is_identifier_match(self, device: Dict[str, Any], identifier: str) -> bool:
        if not device:
            return False
        identifier = str(identifier or "").strip()
        if not identifier:
            return False
        return identifier in {
            str(device.get("serial") or "").strip(),
            str(device.get("address") or "").strip(),
        }

    def _find_known_device(self, devices: list[Dict[str, Any]], identifier: str) -> Dict[str, Any] | None:
        for device in devices:
            if self._is_identifier_match(device, identifier):
                return device
        return None

    def _update_selected_device(self, name: str, address: str, serial: str) -> None:
        self._config["device_name"] = name or self._config.get("device_name", "")
        if address:
            self._config["device_address"] = address
        if serial:
            self._config["device_serial"] = serial
        self._upsert_known_device(name, address, serial)
        self._ble.update_config(self._config)
        save_config(self._config)

    def _upsert_known_device(self, name: str, address: str, serial: str) -> None:
        known_devices = self._get_known_devices()
        identifier = serial or address
        target = None
        if identifier:
            target = self._find_known_device(known_devices, identifier)
        elif name:
            for device in known_devices:
                if (
                    device.get("name") == name
                    and not device.get("address")
                    and not device.get("serial")
                ):
                    target = device
                    break
        if target:
            target["name"] = name or target.get("name", "")
            if address:
                target["address"] = address
            if serial:
                target["serial"] = serial
            if not target.get("nickname"):
                target["nickname"] = self._generate_nickname(name, known_devices)
        else:
            if not name:
                return
            known_devices.append(
                {
                    "name": name,
                    "address": address,
                    "serial": serial,
                    "nickname": self._generate_nickname(name, known_devices),
                }
            )
        self._config["known_devices"] = known_devices

    def _generate_nickname(self, name: str, known_devices: list[Dict[str, Any]]) -> str:
        base = (name or "").strip() or "Device"
        prefix = f"{base} #"
        used = set()
        for device in known_devices:
            nickname = str(device.get("nickname") or "").strip()
            if nickname.startswith(prefix):
                suffix = nickname[len(prefix):]
                if suffix.isdigit():
                    used.add(int(suffix))
        index = 1
        while index in used:
            index += 1
        return f"{prefix}{index}"

    def _handle_identity(self, identity: Dict[str, Any]) -> None:
        name = str(identity.get("name") or "").strip()
        address = str(identity.get("address") or "").strip()
        serial = str(identity.get("serial") or "").strip()
        if not name and not address and not serial:
            return
        self._update_selected_device(
            name or self._config.get("device_name", ""),
            address or self._config.get("device_address", ""),
            serial or self._config.get("device_serial", ""),
        )

    def save_csv(self):
        if not self._logger.has_data():
            return {"ok": False, "message": "No data to save yet."}
        if not self._window:
            return {"ok": False, "message": "Window not ready."}
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="sensor_log.csv",
            file_types=("CSV files (*.csv)",),
        )
        path = self._normalize_dialog_path(path)
        if not path:
            return {"ok": False, "message": "Save canceled."}
        ok, message = self._logger.save_csv(Path(path))
        return {"ok": ok, "message": message}

    def set_autosave(self, enabled: bool):
        if not enabled:
            self._logger.stop_autosave()
            self._prevent_sleep(False)
            return {"ok": True, "message": "Autosave off.", "path": ""}
        if not self._window:
            return {"ok": False, "message": "Window not ready."}
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="sensor_log.csv",
            file_types=("CSV files (*.csv)",),
        )
        path = self._normalize_dialog_path(path)
        if not path:
            return {"ok": False, "message": "Autosave canceled."}
        file_path = Path(path)
        ok, message = self._logger.start_autosave(file_path)
        if ok:
            self._prevent_sleep(True)
            return {"ok": True, "message": message, "path": str(file_path)}
        self._prevent_sleep(False)
        return {"ok": False, "message": message}

    def start_ui_pump(self):
        while not self._stop_ui.is_set():
            self._drain_queues()
            self._stop_ui.wait(0.1)

    def stop_ui_pump(self):
        self._stop_ui.set()
        if self._ble.is_running():
            self._ble.stop()
        self._prevent_sleep(False)
        self.stop_scan()

    def _prevent_sleep(self, enabled: bool):
        system = platform.system()
        if system == "Darwin":
            if enabled:
                if self._sleep_inhibitor and self._sleep_inhibitor.poll() is None:
                    return
                try:
                    self._sleep_inhibitor = subprocess.Popen(
                        ["caffeinate", "-dimsu"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    self._sleep_inhibitor = None
            else:
                if self._sleep_inhibitor and self._sleep_inhibitor.poll() is None:
                    try:
                        self._sleep_inhibitor.terminate()
                    except Exception:
                        pass
                self._sleep_inhibitor = None
            return

        if system == "Windows":
            try:
                import ctypes

                ES_CONTINUOUS = 0x80000000
                ES_SYSTEM_REQUIRED = 0x00000001
                ES_DISPLAY_REQUIRED = 0x00000002
                flags = ES_CONTINUOUS
                if enabled:
                    flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            except Exception:
                pass
            return

    def _send_devices(self, devices: list[Dict[str, Any]]):
        if not self._window:
            return
        known_devices = self._get_known_devices()
        known_by_serial = {d["serial"]: d for d in known_devices if d.get("serial")}
        known_found = []
        other_found = []
        for device in devices:
            address = str(device.get("address") or "").strip()
            name = str(device.get("name") or "").strip()
            serial = str(device.get("serial") or "").strip()
            compatible = bool(device.get("compatible"))
            match = known_by_serial.get(serial) if serial else None
            if match and address and match.get("address") != address:
                match["address"] = address
                self._config["known_devices"] = known_devices
                save_config(self._config)
            entry = {
                "name": name,
                "address": address,
                "serial": match.get("serial") if match else serial,
                "nickname": match.get("nickname") if match else "",
                "known": bool(match),
                "id": (match.get("serial") or match.get("address")) if match else (address or name),
                "compatible": compatible,
            }
            if match:
                known_found.append(entry)
            else:
                other_found.append(entry)
        payload = json.dumps({"known": known_found, "other": other_found})
        try:
            self._window.evaluate_js(f"updateDeviceList({payload});")
        except Exception:
            pass

    def _drain_queues(self):
        if not self._window:
            return

        def eval_js(func: str, *args):
            payload = json.dumps(args)
            self._window.evaluate_js(f"{func}.apply(null, {payload});")

        updated = False
        try:
            while True:
                row = self._data_queue.get_nowait()
                autosave_error = self._logger.append_row(row)
                if autosave_error:
                    self._prevent_sleep(False)
                    eval_js("setAutosaveState", False)
                    eval_js("setAutosavePath", "")
                    eval_js("setStatus", autosave_error)
                eval_js(
                    "updateReadings",
                    row.humidity,
                    row.temperature,
                    row.battery,
                )
                eval_js(
                    "addRow",
                    row.ts.strftime("%H:%M:%S"),
                    row.humidity,
                    row.temperature,
                )
                updated = True
        except queue.Empty:
            pass

        if updated:
            eval_js("setTableHint", "")

        try:
            while True:
                msg = self._status_queue.get_nowait()
                eval_js("setStatus", msg)
                if msg == "Stopped.":
                    eval_js("setConnectionState", "disconnected")
                    self._stopping = False
                elif not self._stopping:
                    if msg.startswith("Connected") or msg.startswith("Listening") or msg.startswith("Subscribed"):
                        eval_js("setConnectionState", "connected")
                    elif msg.startswith("Scanning") or msg.startswith("Found") or msg == "Connecting...":
                        eval_js("setConnectionState", "connecting")
                if msg == "Disconnecting...":
                    eval_js("setConnectionState", "connecting")
        except queue.Empty:
            pass


def load_html() -> str:
    html_path = Path(__file__).with_name("web_ui.html")
    return html_path.read_text(encoding="utf-8")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info("App starting.")
    data_queue = queue.Queue()
    status_queue = queue.Queue()
    data_rows: list[Row] = []

    api = AppAPI(data_queue, status_queue, data_rows)
    frameless = False
    window = webview.create_window(
        "Bob's Humidity Tracker",
        html=load_html(),
        width=900,
        height=620,
        min_size=(900, 600),
        frameless=frameless,
        resizable=True,
        js_api=api,
    )
    api.set_window(window)
    window.events.closed += api.stop_ui_pump
    window.events.loaded += lambda: window.evaluate_js("void 0")

    def start_ui(*_):
        api.start_ui_pump()

    webview.start(start_ui, window)


if __name__ == "__main__":
    main()
