import asyncio
import csv
import json
import queue
import platform
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import webview
from bleak import BleakScanner, BleakClient

DEFAULT_CONFIG: Dict[str, Any] = {
    "device_name": "SHT40 Gadget",
    "humidity_service_uuid": "00001234-b38d-4985-720e-0f993a68ee41",
    "humidity_char_uuid": "00001235-b38d-4985-720e-0f993a68ee41",
    "temperature_service_uuid": "00002234-b38d-4985-720e-0f993a68ee41",
    "temperature_char_uuid": "00002235-b38d-4985-720e-0f993a68ee41",
    "battery_service_uuid": "0000180f-0000-1000-8000-00805f9b34fb",
    "battery_char_uuid": "00002a19-0000-1000-8000-00805f9b34fb",
    "scan_timeout": 30.0,
    "reconnect_delay": 3.0,
    "debounce_sec": 0.2,
    "table_max_rows": 6,
    "theme": "light",
}

COMPATIBILITY_TIMEOUT_SEC = 30.0
SCAN_CHUNK_SEC = 1.0


def config_path() -> Path:
    return Path(__file__).with_name("config.json")


def load_config() -> Dict[str, Any]:
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **data}
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def save_config(data: Dict[str, Any]) -> None:
    path = config_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def decode_float_le(data: bytes) -> float:
    if len(data) < 4:
        return float("nan")
    import struct
    (val,) = struct.unpack("<f", data[:4])
    return val


def decode_uint8(data: bytes) -> float:
    if len(data) < 1:
        return float("nan")
    return float(data[0])


@dataclass
class Row:
    ts: datetime
    humidity: float | None
    temperature: float | None
    battery: float | None


class BLEWorker(threading.Thread):
    def __init__(self, data_queue: queue.Queue, status_queue: queue.Queue, config: Dict[str, Any]):
        super().__init__(daemon=True)
        self._data_queue = data_queue
        self._status_queue = status_queue
        self._config = config
        self._stop_event = threading.Event()
        self._loop = None
        self._async_stop = None

    def stop(self):
        self._stop_event.set()
        if self._loop and self._async_stop:
            self._loop.call_soon_threadsafe(self._async_stop.set)

    def _status(self, msg: str):
        self._status_queue.put(msg)

    async def _run(self):
        self._async_stop = asyncio.Event()
        while not self._stop_event.is_set():
            device_name = self._config["device_name"]
            scan_timeout = float(self._config["scan_timeout"])
            reconnect_delay = float(self._config["reconnect_delay"])
            debounce_sec = float(self._config["debounce_sec"])

            self._status(f"Scanning for '{device_name}'...")
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name == device_name) or (ad and ad.local_name == device_name),
                timeout=scan_timeout,
            )
            if self._stop_event.is_set():
                break
            if not device:
                self._status(f"Not found. Retrying in {reconnect_delay}s...")
                try:
                    await asyncio.wait_for(self._async_stop.wait(), timeout=reconnect_delay)
                except asyncio.TimeoutError:
                    pass
                continue

            self._status(f"Found {device.name} ({device.address})")

            try:
                async with BleakClient(device) as client:
                    self._status("Connected. Subscribing...")

                    last_hum = {"value": None}
                    last_temp = {"value": None}
                    last_batt = {"value": None}
                    print_handle = {"handle": None}

                    await self._read_initial_values(client, last_hum, last_temp, last_batt)
                    self._data_queue.put(
                        Row(datetime.now(), last_hum["value"], last_temp["value"], last_batt["value"])
                    )

                    def schedule_print():
                        handle = print_handle["handle"]
                        if handle is not None:
                            handle.cancel()
                        loop = asyncio.get_running_loop()
                        print_handle["handle"] = loop.call_later(
                            debounce_sec,
                            lambda: self._data_queue.put(
                                Row(
                                    datetime.now(),
                                    last_hum["value"],
                                    last_temp["value"],
                                    last_batt["value"],
                                )
                            ),
                        )

                    def hum_handler(sender: int, data: bytearray):
                        last_hum["value"] = decode_float_le(bytes(data))
                        schedule_print()

                    def temp_handler(sender: int, data: bytearray):
                        last_temp["value"] = decode_float_le(bytes(data))
                        schedule_print()

                    def batt_handler(sender: int, data: bytearray):
                        last_batt["value"] = decode_uint8(bytes(data))
                        schedule_print()

                    hum_ok = await self._start_notify(
                        client, self._config["humidity_char_uuid"], hum_handler, "humidity"
                    )
                    temp_ok = await self._start_notify(
                        client, self._config["temperature_char_uuid"], temp_handler, "temperature"
                    )
                    batt_ok = await self._start_notify(
                        client, self._config["battery_char_uuid"], batt_handler, "battery"
                    )
                    if not (hum_ok or temp_ok or batt_ok):
                        self._status("No notifiable characteristics found.")
                        return

                    self._status("Listening for data. Press Disconnect to end.")
                    while client.is_connected and not self._stop_event.is_set():
                        try:
                            await asyncio.wait_for(self._async_stop.wait(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass

                    await self._stop_notify_safe(client, self._config["humidity_char_uuid"], hum_ok)
                    await self._stop_notify_safe(client, self._config["temperature_char_uuid"], temp_ok)
                    await self._stop_notify_safe(client, self._config["battery_char_uuid"], batt_ok)
            except Exception as e:
                self._status(f"Connection error: {e}")

            if self._stop_event.is_set():
                break
            self._status(f"Disconnected. Reconnecting in {reconnect_delay}s...")
            try:
                await asyncio.wait_for(self._async_stop.wait(), timeout=reconnect_delay)
            except asyncio.TimeoutError:
                pass

        self._status("Stopped.")

    async def _start_notify(self, client: BleakClient, char_uuid: str, handler, label: str) -> bool:
        try:
            await client.start_notify(char_uuid, handler)
            self._status(f"Subscribed to {label}.")
            return True
        except Exception as e:
            self._status(f"Notify failed for {label}: {e}")
            return False

    async def _stop_notify_safe(self, client: BleakClient, char_uuid: str, enabled: bool):
        if not enabled:
            return
        try:
            await client.stop_notify(char_uuid)
        except Exception:
            pass

    async def _read_initial_values(self, client: BleakClient, hum, temp, batt):
        try:
            raw = await client.read_gatt_char(self._config["humidity_char_uuid"])
            hum["value"] = decode_float_le(raw)
        except Exception:
            pass
        try:
            raw = await client.read_gatt_char(self._config["temperature_char_uuid"])
            temp["value"] = decode_float_le(raw)
        except Exception:
            pass
        try:
            raw = await client.read_gatt_char(self._config["battery_char_uuid"])
            batt["value"] = decode_uint8(raw)
        except Exception:
            pass

    def run(self):
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()


class AppAPI:
    def __init__(self, data_queue: queue.Queue, status_queue: queue.Queue, data_rows: list[Row]):
        self._data_queue = data_queue
        self._status_queue = status_queue
        self._data_rows = data_rows
        self._worker: BLEWorker | None = None
        self._stopping = False
        self._window = None
        self._stop_ui = threading.Event()
        self._config = load_config()
        self._autosave_path: Path | None = None
        self._sleep_inhibitor: subprocess.Popen | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_stop = threading.Event()

    def set_window(self, window):
        self._window = window

    def toggle_connect(self):
        if self._worker and self._worker.is_alive() and not self._stopping:
            self.stop()
        else:
            self.start()
        return True

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._stopping = False
        self._worker = BLEWorker(self._data_queue, self._status_queue, self._config.copy())
        self._worker.start()
        self._status_queue.put("Connecting...")

    def stop(self):
        if self._worker:
            self._stopping = True
            self._worker.stop()
        self._status_queue.put("Disconnecting...")

    def reset_data(self):
        self._data_rows.clear()
        return True

    def get_config(self):
        return self._config

    def get_platform(self):
        return platform.system()

    def scan_devices(self, timeout: float | None = None):
        timeout_value = (
            float(timeout)
            if timeout is not None
            else float(self._config.get("scan_timeout", 30.0))
        )
        try:
            devices = self._run_async(BleakScanner.discover(timeout=timeout_value))
        except Exception as e:
            return {"ok": False, "message": f"Scan failed: {e}", "devices": []}
        results = []
        for device in devices or []:
            name = device.name or ""
            if not name.strip():
                continue
            results.append(
                {
                    "name": name,
                    "address": getattr(device, "address", ""),
                }
            )
        results.sort(key=lambda d: (d["name"].lower(), d["address"]))
        return {"ok": True, "devices": results}

    def start_scan(self):
        if self._scan_thread and self._scan_thread.is_alive():
            return {"ok": False, "message": "Scan already running."}
        self._scan_stop.clear()
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        return {"ok": True}

    def stop_scan(self):
        self._scan_stop.set()
        return {"ok": True}

    def _scan_loop(self):
        seen: dict[str, Dict[str, Any]] = {}
        while not self._scan_stop.is_set():
            try:
                result = self.scan_devices(timeout=SCAN_CHUNK_SEC)
                if result.get("ok"):
                    for device in result.get("devices", []):
                        key = device.get("address") or device.get("name")
                        if key and key not in seen:
                            seen[key] = device
                    self._send_devices(list(seen.values()))
            except Exception:
                pass

    def _send_devices(self, devices: list[Dict[str, Any]]):
        if not self._window:
            return
        payload = json.dumps(devices)
        try:
            self._window.evaluate_js(f"updateDeviceList({payload});")
        except Exception:
            pass

    def verify_device(self, address: str):
        if not address:
            return {"ok": False, "compatible": False, "message": "Missing device address."}

        async def _check():
            async with BleakClient(address) as client:
                await client.read_gatt_char(self._config["humidity_char_uuid"])
            return True

        try:
            self._run_async(asyncio.wait_for(_check(), timeout=COMPATIBILITY_TIMEOUT_SEC))
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "compatible": False,
                "message": "Compatibility check timed out.",
            }
        except Exception as e:
            return {"ok": False, "compatible": False, "message": f"Check failed: {e}"}

        return {
            "ok": True,
            "compatible": True,
            "message": "Compatible device.",
        }

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
        self._config = cleaned
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

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


    def save_csv(self):
        if not self._data_rows:
            return {"ok": False, "message": "No data to save yet."}
        if not self._window:
            return {"ok": False, "message": "Window not ready."}
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="sensor_log.csv",
            file_types=("CSV files (*.csv)",),
        )
        if isinstance(path, list):
            path = path[0] if path else None
        if not path:
            return {"ok": False, "message": "Save canceled."}
        try:
            file_path = Path(path)
            with open(file_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "humidity", "temperature"])
                for row in self._data_rows:
                    writer.writerow([
                        row.ts.isoformat(timespec="seconds"),
                        "" if row.humidity is None else f"{row.humidity:.2f}",
                        "" if row.temperature is None else f"{row.temperature:.2f}",
                    ])
            return {"ok": True, "message": f"Saved {len(self._data_rows)} rows."}
        except Exception as e:
            return {"ok": False, "message": f"Failed to save: {e}"}

    def set_autosave(self, enabled: bool):
        if not enabled:
            self._autosave_path = None
            self._prevent_sleep(False)
            return {"ok": True, "message": "Autosave off.", "path": ""}
        if not self._window:
            return {"ok": False, "message": "Window not ready."}
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="sensor_log.csv",
            file_types=("CSV files (*.csv)",),
        )
        if isinstance(path, list):
            path = path[0] if path else None
        if not path:
            return {"ok": False, "message": "Autosave canceled."}
        try:
            file_path = Path(path)
            needs_header = not file_path.exists() or file_path.stat().st_size == 0
            with open(file_path, "a", newline="") as f:
                writer = csv.writer(f)
                if needs_header:
                    writer.writerow(["time", "humidity", "temperature"])
            self._autosave_path = file_path
            self._prevent_sleep(True)
            return {
                "ok": True,
                "message": f"Autosaving to {file_path.name}.",
                "path": str(file_path),
            }
        except Exception as e:
            self._autosave_path = None
            self._prevent_sleep(False)
            return {"ok": False, "message": f"Failed to start autosave: {e}"}

    def start_ui_pump(self):
        while not self._stop_ui.is_set():
            self._drain_queues()
            self._stop_ui.wait(0.1)

    def stop_ui_pump(self):
        self._stop_ui.set()
        if self._worker and self._worker.is_alive():
            self._worker.stop()
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

    def _drain_queues(self):
        if not self._window:
            return

        def eval_js(func: str, *args):
            payload = json.dumps(args)
            self._window.evaluate_js(f"{func}.apply(null, {payload});")

        def append_autosave(row: Row):
            if not self._autosave_path:
                return
            file_path = self._autosave_path
            try:
                needs_header = not file_path.exists() or file_path.stat().st_size == 0
                with open(file_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    if needs_header:
                        writer.writerow(["time", "humidity", "temperature"])
                    writer.writerow([
                        row.ts.isoformat(timespec="seconds"),
                        "" if row.humidity is None else f"{row.humidity:.2f}",
                        "" if row.temperature is None else f"{row.temperature:.2f}",
                    ])
            except Exception as e:
                self._autosave_path = None
                self._prevent_sleep(False)
                eval_js("setAutosaveState", False)
                eval_js("setAutosavePath", "")
                eval_js("setStatus", f"Autosave stopped: {e}")

        updated = False
        try:
            while True:
                row = self._data_queue.get_nowait()
                self._data_rows.append(row)
                append_autosave(row)
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
