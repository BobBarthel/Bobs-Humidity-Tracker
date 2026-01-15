import asyncio
import json
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Callable

from bleak import BleakScanner, BleakClient

from data_logging import Row

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "device_name": "SHT40 Gadget",
    "device_address": "",
    "device_serial": "",
    "known_devices": [],
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

COMPATIBILITY_TIMEOUT_SEC = 60.0
SCAN_CHUNK_SEC = 0.5
SERIAL_CHAR_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
READ_TIMEOUT_SEC = 6.0
DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
SYSTEM_ID_CHAR_UUID = "00002a23-0000-1000-8000-00805f9b34fb"


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


def _decode_float_le(data: bytes) -> float:
    if len(data) < 4:
        return float("nan")
    import struct

    (val,) = struct.unpack("<f", data[:4])
    return val


def _decode_uint8(data: bytes) -> float:
    if len(data) < 1:
        return float("nan")
    return float(data[0])


def _normalize_serial(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if cleaned in {"00000000", "0000"}:
        return ""
    return cleaned


def _format_system_id(data: bytes) -> str:
    if not data or len(data) < 6:
        return ""
    return data[:6].hex()


class BLEWorker(threading.Thread):
    def __init__(
        self,
        data_queue: queue.Queue,
        status_queue: queue.Queue,
        config: Dict[str, Any],
        identity_callback: Callable[[Dict[str, Any]], None] | None = None,
    ):
        super().__init__(daemon=True)
        self._data_queue = data_queue
        self._status_queue = status_queue
        self._config = config
        self._identity_callback = identity_callback
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
            device_address = str(self._config.get("device_address", "") or "").strip()
            device_serial = str(self._config.get("device_serial", "") or "").strip()
            scan_timeout = float(self._config["scan_timeout"])
            reconnect_delay = float(self._config["reconnect_delay"])
            debounce_sec = float(self._config["debounce_sec"])

            target = None
            used_address = False
            if device_address:
                used_address = True
                target = device_address
                self._status(f"Connecting to {device_address}...")
                logger.info("Connecting using saved address %s", device_address)
            elif device_serial:
                self._status(f"Scanning for device id {device_serial}...")
                logger.info("Scanning for device id %s", device_serial)
                target = await self._find_device_by_serial(device_serial, scan_timeout)
            else:
                self._status("No device selected. Choose one in Settings.")
                logger.warning("No device address or id selected.")
                return

            if self._stop_event.is_set():
                break

            if not target:
                logger.warning("Device not found (serial=%s)", device_serial or "n/a")
                self._status(f"Not found. Retrying in {reconnect_delay}s...")
                try:
                    await asyncio.wait_for(self._async_stop.wait(), timeout=reconnect_delay)
                except asyncio.TimeoutError:
                    pass
                continue

            connected = False
            try:
                connected = await self._connect_and_listen(
                    target,
                    device_serial,
                    debounce_sec,
                )
            except Exception as e:
                self._status(f"Connection error: {e}")
            if not connected and used_address and not self._stop_event.is_set():
                self._status("Address failed. Scanning by device id...")
                logger.info("Falling back to serial scan after address failure.")
                try:
                    target = await self._find_device_by_serial(device_serial, scan_timeout)
                    if target and not self._stop_event.is_set():
                        await self._connect_and_listen(
                            target,
                            device_serial,
                            debounce_sec,
                        )
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

    async def _connect_and_listen(
        self,
        target,
        expected_serial: str,
        debounce_sec: float,
    ) -> bool:
        async with BleakClient(target) as client:
            address = getattr(target, "address", None) or str(target)
            name = getattr(target, "name", None) or ""
            self._status(f"Connected to {name} ({address}). Subscribing...")
            logger.info("Connected to %s (%s)", name, address)

            await self._log_device_info_service(client)
            serial_value = await self._read_identity(client)
            if serial_value:
                logger.info("Read serial %s from %s", serial_value, address)
            else:
                logger.warning("Serial unavailable for %s (using address fallback).", address)
            if expected_serial and serial_value and serial_value != expected_serial:
                self._status("Serial mismatch. Trying next device.")
                logger.warning(
                    "Serial mismatch for %s (expected=%s, actual=%s)",
                    address,
                    expected_serial,
                    serial_value,
                )
                return False

            if address and not self._config.get("device_address"):
                self._config["device_address"] = address
            if serial_value and not self._config.get("device_serial"):
                self._config["device_serial"] = serial_value

            if self._identity_callback:
                try:
                    self._identity_callback(
                        {
                            "name": name,
                            "address": address,
                            "serial": serial_value,
                        }
                    )
                except Exception:
                    pass

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
                last_hum["value"] = _decode_float_le(bytes(data))
                schedule_print()

            def temp_handler(sender: int, data: bytearray):
                last_temp["value"] = _decode_float_le(bytes(data))
                schedule_print()

            def batt_handler(sender: int, data: bytearray):
                last_batt["value"] = _decode_uint8(bytes(data))
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
                return True

            self._status("Listening for data. Press Disconnect to end.")
            while client.is_connected and not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._async_stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

            await self._stop_notify_safe(client, self._config["humidity_char_uuid"], hum_ok)
            await self._stop_notify_safe(client, self._config["temperature_char_uuid"], temp_ok)
            await self._stop_notify_safe(client, self._config["battery_char_uuid"], batt_ok)
            return True

    async def _find_device_by_serial(self, expected_serial: str, scan_timeout: float):
        if not expected_serial:
            return None
        devices = await BleakScanner.discover(timeout=scan_timeout)
        for device in devices or []:
            try:
                identity = await self._read_identity_for_device(device)
                if identity and identity == expected_serial:
                    logger.info("Matched device id %s for %s", identity, device.address)
                    return device
            except Exception:
                continue
        return None

    async def _read_identity(self, client: BleakClient) -> str:
        system_id = await self._read_system_id(client)
        if system_id:
            return system_id
        try:
            raw = await client.read_gatt_char(SERIAL_CHAR_UUID)
            if raw is None:
                return ""
            return _normalize_serial(bytes(raw).decode("utf-8", errors="ignore").strip())
        except Exception:
            return ""

    async def _read_system_id(self, client: BleakClient) -> str:
        try:
            raw = await client.read_gatt_char(SYSTEM_ID_CHAR_UUID)
            if raw is None:
                return ""
            return _format_system_id(bytes(raw))
        except Exception:
            return ""

    async def _log_device_info_service(self, client: BleakClient) -> None:
        services = getattr(client, "services", None)
        if services is None:
            logger.error("Device info: services not available on client")
            return
        service = None
        for candidate in services:
            if str(candidate.uuid).lower() == DEVICE_INFO_SERVICE_UUID:
                service = candidate
                break
        if not service:
            logger.info("Device info: service %s not found", DEVICE_INFO_SERVICE_UUID)
            return
        logger.info("Device info: service %s found with %d characteristics", service.uuid, len(service.characteristics))
        for char in service.characteristics:
            props = set(char.properties or [])
            if "read" not in props:
                logger.info("Device info: char %s not readable (props=%s)", char.uuid, ",".join(sorted(props)))
                continue
            try:
                raw = await asyncio.wait_for(client.read_gatt_char(char.uuid), timeout=READ_TIMEOUT_SEC)
                data = bytes(raw or b"")
                text = data.decode("utf-8", errors="ignore").strip()
                logger.info("Device info: char %s bytes=%s text=%s", char.uuid, data.hex(), text)
            except Exception:
                logger.exception("Device info: read failed for %s", char.uuid)

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
            hum["value"] = _decode_float_le(raw)
        except Exception:
            pass
        try:
            raw = await client.read_gatt_char(self._config["temperature_char_uuid"])
            temp["value"] = _decode_float_le(raw)
        except Exception:
            pass
        try:
            raw = await client.read_gatt_char(self._config["battery_char_uuid"])
            batt["value"] = _decode_uint8(raw)
        except Exception:
            pass

    def run(self):
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()


class BluetoothService:
    def __init__(
        self,
        data_queue: queue.Queue,
        status_queue: queue.Queue,
        config: Dict[str, Any],
    ):
        self._data_queue = data_queue
        self._status_queue = status_queue
        self._config = config
        self._worker: BLEWorker | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_stop = threading.Event()
        self._devices_callback: Callable[[list[Dict[str, Any]]], None] | None = None
        self._identity_callback: Callable[[Dict[str, Any]], None] | None = None

    def update_config(self, config: Dict[str, Any]) -> None:
        self._config = config

    def set_devices_callback(self, callback: Callable[[list[Dict[str, Any]]], None]) -> None:
        self._devices_callback = callback

    def set_identity_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._identity_callback = callback

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._worker = BLEWorker(
            self._data_queue,
            self._status_queue,
            self._config.copy(),
            identity_callback=self._identity_callback,
        )
        self._worker.start()
        self._status_queue.put("Connecting...")

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()
        self._status_queue.put("Disconnecting...")

    def scan_devices(self, timeout: float | None = None) -> dict:
        timeout_value = float(timeout) if timeout is not None else float(self._config.get("scan_timeout", 30.0))
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

    def start_scan(self) -> dict:
        if self._scan_thread and self._scan_thread.is_alive():
            return {"ok": False, "message": "Scan already running."}
        self._scan_stop.clear()
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        return {"ok": True}

    def stop_scan(self) -> dict:
        self._scan_stop.set()
        return {"ok": True}

    def verify_device(self, address: str) -> dict:
        if not address:
            return {"ok": False, "compatible": False, "message": "Missing device address."}

        async def _check():
            client = BleakClient(address)
            serial_value = ""
            humidity_ok = False
            try:
                logger.info("Compatibility check: connecting to %s", address)
                await asyncio.wait_for(client.connect(), timeout=COMPATIBILITY_TIMEOUT_SEC)
                try:
                    raw = await asyncio.wait_for(
                        client.read_gatt_char(SYSTEM_ID_CHAR_UUID),
                        timeout=READ_TIMEOUT_SEC,
                    )
                    serial_value = _format_system_id(bytes(raw))
                    if serial_value:
                        logger.info("Compatibility check: system id=%s", serial_value)
                    else:
                        logger.warning("Compatibility check: system id read empty for %s", address)
                except Exception:
                    logger.exception("Compatibility check: system id read failed for %s", address)
                    serial_value = ""
                if not serial_value:
                    try:
                        raw = await asyncio.wait_for(
                            client.read_gatt_char(SERIAL_CHAR_UUID),
                            timeout=READ_TIMEOUT_SEC,
                        )
                        serial_value = _normalize_serial(bytes(raw).decode("utf-8", errors="ignore").strip())
                        if serial_value:
                            logger.info("Compatibility check: serial=%s", serial_value)
                        else:
                            logger.warning("Compatibility check: serial read empty for %s", address)
                    except Exception:
                        logger.exception("Compatibility check: serial read failed for %s", address)
                        serial_value = ""
                try:
                    await asyncio.wait_for(
                        client.read_gatt_char(self._config["humidity_char_uuid"]),
                        timeout=READ_TIMEOUT_SEC,
                    )
                    humidity_ok = True
                    logger.info("Compatibility check: humidity char read ok for %s", address)
                except Exception:
                    logger.exception("Compatibility check: humidity read failed for %s", address)
                    humidity_ok = False
            finally:
                try:
                    logger.info("Compatibility check: disconnecting %s", address)
                    await asyncio.wait_for(client.disconnect(), timeout=READ_TIMEOUT_SEC)
                except Exception:
                    logger.exception("Compatibility check: disconnect failed for %s", address)
                    pass
            return {"serial": serial_value, "compatible": bool(serial_value or humidity_ok)}

        try:
            result = self._run_async(asyncio.wait_for(_check(), timeout=COMPATIBILITY_TIMEOUT_SEC))
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "compatible": False,
                "message": "Compatibility check timed out.",
            }
        except Exception as e:
            return {"ok": False, "compatible": False, "message": f"Check failed: {e}"}

        serial_value = result.get("serial", "") if isinstance(result, dict) else ""
        compatible = bool(result.get("compatible")) if isinstance(result, dict) else False
        if not compatible:
            return {
                "ok": False,
                "compatible": False,
                "message": "Unable to read device characteristics.",
                "serial": serial_value,
            }
        return {
            "ok": True,
            "compatible": True,
            "message": "Compatible device.",
            "serial": serial_value,
        }

    def _scan_loop(self) -> None:
        while not self._scan_stop.is_set():
            try:
                asyncio.run(self._scan_loop_async())
            except Exception:
                logger.exception("Scan loop failed")
                if self._scan_stop.wait(2.0):
                    break

    async def _scan_loop_async(self) -> None:
        seen: dict[str, Dict[str, Any]] = {}
        probing: set[str] = set()

        def handle_device(device, _advertisement_data):
            name = (device.name or "").strip()
            if not name:
                return
            address = getattr(device, "address", "")
            key = address or name
            if not key:
                return
            if key not in seen:
                seen[key] = {"name": name, "address": address, "serial": "", "compatible": False}
                self._send_devices(list(seen.values()))
            if key and key not in probing and self._should_probe_identity(name):
                probing.add(key)
                asyncio.create_task(self._probe_identity(device, key, seen, probing))

        scanner = BleakScanner(detection_callback=handle_device)
        await scanner.start()
        try:
            while not self._scan_stop.is_set():
                await asyncio.sleep(SCAN_CHUNK_SEC)
        finally:
            await scanner.stop()

    def _should_probe_identity(self, name: str) -> bool:
        return bool((name or "").strip())

    async def _probe_identity(
        self,
        device,
        key: str,
        seen: dict[str, Dict[str, Any]],
        probing: set[str],
    ) -> None:
        try:
            result = await self._read_identity_for_device(device)
            if key in seen:
                seen[key]["serial"] = result.get("serial", "")
                seen[key]["compatible"] = bool(result.get("compatible"))
                self._send_devices(list(seen.values()))
        finally:
            probing.discard(key)

    async def _read_identity_for_device(self, device) -> Dict[str, Any]:
        client = BleakClient(device)
        serial_value = ""
        compatible = False
        try:
            await asyncio.wait_for(client.connect(), timeout=READ_TIMEOUT_SEC)
            try:
                raw = await asyncio.wait_for(
                    client.read_gatt_char(SYSTEM_ID_CHAR_UUID),
                    timeout=READ_TIMEOUT_SEC,
                )
                system_id = _format_system_id(bytes(raw or b""))
            except Exception:
                system_id = ""
            if system_id:
                serial_value = system_id
            try:
                raw = await asyncio.wait_for(
                    client.read_gatt_char(SERIAL_CHAR_UUID),
                    timeout=READ_TIMEOUT_SEC,
                )
                if not serial_value:
                    serial_value = _normalize_serial(bytes(raw).decode("utf-8", errors="ignore").strip())
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    client.read_gatt_char(self._config["humidity_char_uuid"]),
                    timeout=READ_TIMEOUT_SEC,
                )
                compatible = True
            except Exception:
                compatible = False
        except Exception:
            serial_value = ""
            compatible = False
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=READ_TIMEOUT_SEC)
            except Exception:
                pass
        return {"serial": serial_value, "compatible": compatible}

    def _send_devices(self, devices: list[Dict[str, Any]]) -> None:
        if not self._devices_callback:
            return
        try:
            self._devices_callback(devices)
        except Exception:
            pass

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
