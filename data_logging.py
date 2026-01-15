import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Row:
    ts: datetime
    humidity: float | None
    temperature: float | None
    battery: float | None


class DataLogger:
    def __init__(self, data_rows: list[Row]):
        self._data_rows = data_rows
        self._autosave_path: Path | None = None

    def clear(self) -> None:
        self._data_rows.clear()

    def has_data(self) -> bool:
        return bool(self._data_rows)

    @property
    def autosave_path(self) -> Path | None:
        return self._autosave_path

    def save_csv(self, path: Path) -> tuple[bool, str]:
        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "humidity", "temperature"])
                for row in self._data_rows:
                    writer.writerow([
                        row.ts.isoformat(timespec="seconds"),
                        "" if row.humidity is None else f"{row.humidity:.2f}",
                        "" if row.temperature is None else f"{row.temperature:.2f}",
                    ])
            return True, f"Saved {len(self._data_rows)} rows."
        except Exception as e:
            return False, f"Failed to save: {e}"

    def start_autosave(self, path: Path) -> tuple[bool, str]:
        try:
            self._ensure_csv_header(path)
            self._autosave_path = path
            return True, f"Autosaving to {path.name}."
        except Exception as e:
            self._autosave_path = None
            return False, f"Failed to start autosave: {e}"

    def stop_autosave(self) -> None:
        self._autosave_path = None

    def append_row(self, row: Row) -> str | None:
        self._data_rows.append(row)
        if not self._autosave_path:
            return None
        try:
            with open(self._autosave_path, "a", newline="") as f:
                writer = csv.writer(f)
                self._maybe_write_header(writer, self._autosave_path)
                writer.writerow([
                    row.ts.isoformat(timespec="seconds"),
                    "" if row.humidity is None else f"{row.humidity:.2f}",
                    "" if row.temperature is None else f"{row.temperature:.2f}",
                ])
            return None
        except Exception as e:
            self._autosave_path = None
            return f"Autosave stopped: {e}"

    def _ensure_csv_header(self, path: Path) -> None:
        needs_header = not path.exists() or path.stat().st_size == 0
        if not needs_header:
            return
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "humidity", "temperature"])

    def _maybe_write_header(self, writer: csv.writer, path: Path) -> None:
        needs_header = not path.exists() or path.stat().st_size == 0
        if needs_header:
            writer.writerow(["time", "humidity", "temperature"])
