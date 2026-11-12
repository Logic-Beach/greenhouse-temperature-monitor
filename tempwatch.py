#!/usr/bin/env python3
"""Read the temperature sensor on Raspberry Pi GPIO 4, log it, and switch two pins.

Cold pin (default GPIO 17, physical pin 11) turns on at 38 F or below.
Hot pin (default GPIO 27, physical pin 13) turns on at 110 F or above.
Outdoor temperature and humidity are pulled from Open-Meteo for the graph.

    python3 tempwatch.py

Then open http://<pi-ip>:8787 from a phone on the same network.
Edit the settings just below to change pins, limits, or weather location.
The web page only displays the log. It cannot change a pin.
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import math
import os
import re
import signal
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

# --- edit these -----------------------------------------------------------
# BCM numbers, not physical header numbers.
# GPIO 17 is physical pin 11. GPIO 27 is physical pin 13.
# GPIO 4 (physical pin 7) is the sensor. Do not use it as an output.
# Set a pin to None to leave that side unused.
COLD_PIN = 17
HOT_PIN = 27
COLD_ON_F = 38.0
HOT_ON_F = 110.0
# A pin turns off only after the temperature comes back by this many degrees,
# so a relay does not chatter when the reading sits on the limit.
HYSTERESIS_F = 10.0
# Relay boards that switch on when the input is grounded need True.
ACTIVE_LOW = False
SAMPLE_SECONDS = 60
PORT = 8787
# Outdoor weather from Open-Meteo (no API key). Set both to None to disable.
# Fetched on every sample (same cadence as SAMPLE_SECONDS).
WEATHER_LAT = 36.313107
WEATHER_LON = -86.865280
# Fixed offset for clocks on the page and in the console. CSV stays UTC.
DISPLAY_UTC_OFFSET_HOURS = -6
# --------------------------------------------------------------------------

SENSOR_PIN = 4
HOST = "0.0.0.0"

# BCM number -> physical pin on the 40-pin header.
BCM_PHYS = {
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21,
    10: 19, 11: 23, 12: 32, 13: 33, 14: 8, 15: 10, 16: 36,
    17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16,
    24: 18, 25: 22, 26: 37, 27: 13,
}

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "tempwatch.csv"
CSV_HEADER = (
    "time_utc,temp_f,temp_c,humidity_pct,cold_on,hot_on,"
    "out_temp_f,out_humidity_pct\n"
)
MAX_LOG_BYTES = 400_000_000
KEEP_LOG_BYTES = 399_000_000
GRAPH_KEEP = min(20_000, max(10, int(24 * 3600 / SAMPLE_SECONDS)))

_W1_ID = re.compile(r"^28-[0-9a-fA-F]{6,16}$")
_LABEL = re.compile(r"^[A-Za-z0-9:@._+-]{1,64}$")
_MILLI = re.compile(r"^-?\d{1,8}$")

watcher = None


class SensorError(Exception):
    """The probe did not return a usable reading."""


def c_to_f(temp_c):
    return temp_c * 9.0 / 5.0 + 32.0


def f_to_c(temp_f):
    return (temp_f - 32.0) * 5.0 / 9.0


def fmt_f(value):
    number = float(value)
    if number.is_integer():
        return f"{number:.0f}"
    return f"{number:.1f}"


def fmt_clock(ts=None, fmt="%H:%M:%S"):
    """Format a unix timestamp in DISPLAY_UTC_OFFSET_HOURS (default: now)."""
    when = time.time() if ts is None else float(ts)
    return time.strftime(fmt, time.gmtime(when + float(DISPLAY_UTC_OFFSET_HOURS) * 3600))


def one_line(exc):
    text = str(exc).strip().splitlines()
    line = text[0] if text else "GPIO error"
    return line[:180]


def pin_label(pin):
    if pin is None:
        return "not used"
    header = BCM_PHYS.get(pin)
    if header:
        return f"(GPIO {pin})"
    return f"GPIO {pin}"


def check_config():
    for name, pin in (("COLD_PIN", COLD_PIN), ("HOT_PIN", HOT_PIN)):
        if pin is None:
            continue
        if type(pin) is not int:
            raise SystemExit(f"{name} has to be a BCM number, or None.")
        if pin == SENSOR_PIN:
            raise SystemExit(f"{name} cannot be GPIO 4. That pin is the sensor (header pin 7).")
        if pin not in BCM_PHYS:
            raise SystemExit(f"{name}={pin} is not a BCM number on the 40-pin header.")
    if COLD_PIN is not None and COLD_PIN == HOT_PIN:
        raise SystemExit("COLD_PIN and HOT_PIN have to be different pins.")
    if type(ACTIVE_LOW) is not bool:
        raise SystemExit("ACTIVE_LOW has to be True or False.")
    if isinstance(SAMPLE_SECONDS, bool) or not isinstance(SAMPLE_SECONDS, (int, float)):
        raise SystemExit("SAMPLE_SECONDS has to be a number of seconds.")
    if SAMPLE_SECONDS < 2 or SAMPLE_SECONDS > 300:
        raise SystemExit("SAMPLE_SECONDS has to be between 2 and 300.")
    for name, value in (("COLD_ON_F", COLD_ON_F), ("HOT_ON_F", HOT_ON_F), ("HYSTERESIS_F", HYSTERESIS_F)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise SystemExit(f"{name} has to be a number.")
    if not -67 <= float(COLD_ON_F) <= 257 or not -67 <= float(HOT_ON_F) <= 257:
        raise SystemExit("Limits have to be between -67 F and 257 F.")
    if float(HYSTERESIS_F) <= 0:
        raise SystemExit("HYSTERESIS_F has to be greater than 0.")
    if float(COLD_ON_F) + float(HYSTERESIS_F) >= float(HOT_ON_F) - float(HYSTERESIS_F):
        raise SystemExit("The cold and hot limits are too close for that hysteresis.")
    weather_on = WEATHER_LAT is not None or WEATHER_LON is not None
    if weather_on:
        if WEATHER_LAT is None or WEATHER_LON is None:
            raise SystemExit("Set both WEATHER_LAT and WEATHER_LON, or set both to None.")
        for name, value in (("WEATHER_LAT", WEATHER_LAT), ("WEATHER_LON", WEATHER_LON)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SystemExit(f"{name} has to be a number.")
        if not -90 <= float(WEATHER_LAT) <= 90:
            raise SystemExit("WEATHER_LAT has to be between -90 and 90.")
        if not -180 <= float(WEATHER_LON) <= 180:
            raise SystemExit("WEATHER_LON has to be between -180 and 180.")
    if (
        isinstance(DISPLAY_UTC_OFFSET_HOURS, bool)
        or not isinstance(DISPLAY_UTC_OFFSET_HOURS, (int, float))
        or not math.isfinite(float(DISPLAY_UTC_OFFSET_HOURS))
        or not -12 <= float(DISPLAY_UTC_OFFSET_HOURS) <= 14
    ):
        raise SystemExit("DISPLAY_UTC_OFFSET_HOURS has to be between -12 and 14.")


def alarms(temp_f, cold_on, hot_on):
    """Turn on at the limit. Turn off only after the temperature comes back."""
    if temp_f <= COLD_ON_F:
        cold_on = True
    elif temp_f >= COLD_ON_F + HYSTERESIS_F:
        cold_on = False
    if temp_f >= HOT_ON_F:
        hot_on = True
    elif temp_f <= HOT_ON_F - HYSTERESIS_F:
        hot_on = False
    return cold_on, hot_on


def _read_text(path, limit=4096):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def _clean_label(text, fallback):
    cleaned = text.replace("\x00", "").strip()
    if _LABEL.fullmatch(cleaned):
        return cleaned
    return fallback


def parse_w1(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].endswith("YES"):
        raise SensorError("The DS18B20 checksum failed. Check the data wire and the 4.7k pull-up to 3.3V.")
    match = re.search(r"t=(-?\d{1,7})\s*$", lines[-1])
    if not match:
        raise SensorError("The DS18B20 reading had no temperature.")
    raw = int(match.group(1))
    if raw == 85000:
        raise SensorError("The DS18B20 returned its 85 C power-on value. Check power and the data wire.")
    temp_c = raw / 1000.0
    if temp_c < -55 or temp_c > 125:
        raise SensorError("The DS18B20 reading was outside its range.")
    return temp_c


def parse_milli(text):
    cleaned = text.strip()
    if not _MILLI.fullmatch(cleaned):
        raise SensorError("The sensor returned a reading that was not a number.")
    return int(cleaned) / 1000.0


class W1Sensor:
    def __init__(self, path, ident):
        self.path = path
        self.ident = ident

    def read(self):
        try:
            temp_c = parse_w1(_read_text(self.path))
        except OSError as exc:
            raise SensorError("The DS18B20 could not be read. Check that 1-Wire is enabled.") from exc
        return temp_c, None, "DS18B20 " + self.ident


class DhtSensor:
    def __init__(self, temp_path, humid_path, ident):
        self.temp_path = temp_path
        self.humid_path = humid_path
        self.ident = ident

    def read(self):
        try:
            temp_c = parse_milli(_read_text(self.temp_path))
            humidity = None
            if self.humid_path is not None and self.humid_path.is_file():
                humidity = parse_milli(_read_text(self.humid_path))
        except OSError as exc:
            raise SensorError("The DHT reading failed. It needs a couple of seconds between reads.") from exc
        if temp_c < -40 or temp_c > 125:
            raise SensorError("The DHT reading was outside its range.")
        if humidity is not None and (humidity < 0 or humidity > 100):
            humidity = None
        return temp_c, humidity, "DHT " + self.ident


def discover(root="/"):
    base = Path(root)
    w1_dir = base / "sys/bus/w1/devices"
    if w1_dir.is_dir():
        names = []
        try:
            entries = list(w1_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if _W1_ID.fullmatch(entry.name) and (entry / "w1_slave").is_file():
                names.append(entry.name)
        if names:
            names.sort()
            ident = _clean_label(names[0], "DS18B20")
            return W1Sensor(w1_dir / names[0] / "w1_slave", ident)

    iio_dir = base / "sys/bus/iio/devices"
    if not iio_dir.is_dir():
        return None
    try:
        devices = sorted(iio_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return None
    for device in devices:
        temp_path = device / "in_temp_input"
        name_path = device / "name"
        if not temp_path.is_file() or not name_path.is_file():
            continue
        try:
            name = _read_text(name_path).strip()
        except OSError:
            continue
        if "dht" not in name.lower():
            continue
        humid = device / "in_humidityrelative_input"
        return DhtSensor(temp_path, humid if humid.is_file() else None, _clean_label(name, "DHT"))
    return None


def on_pi(root="/"):
    path = Path(root) / "proc/device-tree/model"
    try:
        text = path.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace")
    except OSError:
        return False
    return "Raspberry Pi" in text


class Simulator:
    def read(self):
        now = time.time()
        temp_f = 71.5 + 1.8 * math.sin(now / 40.0) + 0.4 * math.sin(now / 9.0)
        return f_to_c(temp_f), None, "Simulated probe"


class OutdoorWeather:
    """Cached local outdoor temperature and humidity from Open-Meteo."""

    def __init__(self, lat, lon, refresh_seconds):
        self.lat = lat
        self.lon = lon
        self.refresh_seconds = float(refresh_seconds)
        self.lock = threading.Lock()
        self._fetched_at = 0.0
        self.temp_f = None
        self.humidity = None
        self.error = None
        self.when = None

    @property
    def enabled(self):
        return self.lat is not None and self.lon is not None

    def read(self):
        if not self.enabled:
            return None, None
        now = time.monotonic()
        with self.lock:
            age = now - self._fetched_at
            if self.temp_f is not None and age < self.refresh_seconds:
                return self.temp_f, self.humidity
            stale_temp = self.temp_f
            stale_humid = self.humidity
        try:
            temp_f, humidity = self._fetch()
        except Exception as exc:
            with self.lock:
                self.error = one_line(exc)
            return stale_temp, stale_humid
        with self.lock:
            self.temp_f = temp_f
            self.humidity = humidity
            self.when = time.time()
            self._fetched_at = now
            self.error = None
        return temp_f, humidity

    def _fetch(self):
        query = urlencode(
            {
                "latitude": f"{float(self.lat):.4f}",
                "longitude": f"{float(self.lon):.4f}",
                "current": "temperature_2m,relative_humidity_2m",
                "temperature_unit": "fahrenheit",
            }
        )
        url = "https://api.open-meteo.com/v1/forecast?" + query
        request = Request(url, headers={"User-Agent": "tempwatch/1.0", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=8) as response:
                payload = response.read(65536)
        except HTTPError as exc:
            raise SensorError(f"Weather request failed ({exc.code}).") from exc
        except URLError as exc:
            raise SensorError(f"Weather request failed ({one_line(exc)}).") from exc
        except TimeoutError as exc:
            raise SensorError("Weather request timed out.") from exc
        try:
            data = json.loads(payload.decode("utf-8"))
            current = data["current"]
            temp_f = float(current["temperature_2m"])
            humidity = float(current["relative_humidity_2m"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise SensorError("Weather response was not usable.") from exc
        if not math.isfinite(temp_f) or temp_f < -80 or temp_f > 140:
            raise SensorError("Outdoor temperature was outside its range.")
        if not math.isfinite(humidity) or humidity < 0 or humidity > 100:
            humidity = None
        return temp_f, humidity


class Outputs:
    """Drive the two alarm pins. When drive is false, the pins are left alone."""

    def __init__(self, drive):
        self.drive = bool(drive)
        self.backend = "off"
        self.error = None
        self._cold = None
        self._hot = None
        if self.drive:
            self._open()

    def _open(self):
        try:
            from gpiozero import DigitalOutputDevice
        except Exception:
            self.backend = "unavailable"
            self.error = "Install python3-gpiozero to drive the output pins."
            return
        cold = hot = None
        try:
            if COLD_PIN is not None:
                cold = DigitalOutputDevice(COLD_PIN, active_high=not ACTIVE_LOW, initial_value=False)
            if HOT_PIN is not None:
                hot = DigitalOutputDevice(HOT_PIN, active_high=not ACTIVE_LOW, initial_value=False)
        except Exception as exc:
            for device in (cold, hot):
                _close_device(device)
            self.backend = "unavailable"
            self.error = one_line(exc)
            return
        self._cold = cold
        self._hot = hot
        self.backend = "gpiozero"
        self.error = None

    def apply(self, cold_on, hot_on):
        if not self.drive or self.backend != "gpiozero":
            return
        self._set(self._cold, cold_on)
        self._set(self._hot, hot_on)

    def _set(self, device, on):
        if device is None:
            return
        try:
            if on:
                device.on()
            else:
                device.off()
        except Exception as exc:
            self.error = one_line(exc)

    def close(self):
        self.apply(False, False)
        _close_device(self._cold)
        _close_device(self._hot)
        self._cold = None
        self._hot = None


def _close_device(device):
    if device is None:
        return
    try:
        device.off()
    except Exception:
        pass
    try:
        device.close()
    except Exception:
        pass


def csv_line(ts, temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f=None, out_humidity=None):
    humid = "" if humidity is None else f"{humidity:.1f}"
    out_temp = "" if out_temp_f is None else f"{out_temp_f:.2f}"
    out_humid = "" if out_humidity is None else f"{out_humidity:.1f}"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return (
        f"{stamp},{temp_f:.2f},{temp_c:.2f},{humid},"
        f"{int(bool(cold_on))},{int(bool(hot_on))},{out_temp},{out_humid}\n"
    )


def tail_bytes(path, amount):
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > amount:
            handle.seek(size - amount)
            handle.readline()
        return handle.read()


def parse_log(text):
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("time_utc"):
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            ts = calendar.timegm(time.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ"))
            temp_f = float(parts[1])
            temp_c = float(parts[2])
            humidity = float(parts[3]) if parts[3] else None
            cold_on = parts[4] == "1"
            hot_on = parts[5] == "1"
            out_temp_f = float(parts[6]) if len(parts) > 6 and parts[6] else None
            out_humidity = float(parts[7]) if len(parts) > 7 and parts[7] else None
        except (ValueError, IndexError, OverflowError):
            continue
        if not math.isfinite(temp_f) or not math.isfinite(temp_c):
            continue
        if out_temp_f is not None and not math.isfinite(out_temp_f):
            out_temp_f = None
        if out_humidity is not None and (not math.isfinite(out_humidity) or out_humidity < 0 or out_humidity > 100):
            out_humidity = None
        rows.append((float(ts), temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f, out_humidity))
    return rows


class Watcher:
    def __init__(self, simulate, log_path=None):
        self.simulate = bool(simulate)
        self.log_path = Path(log_path) if log_path else LOG_PATH
        self.samples = deque(maxlen=GRAPH_KEEP)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.temp_f = None
        self.temp_c = None
        self.humidity = None
        self.out_temp_f = None
        self.out_humidity = None
        self.when = None
        self.cold_on = False
        self.hot_on = False
        self.error = None
        self.log_error = None
        self.weather_error = None
        self.label = "Simulated probe" if self.simulate else "GPIO 4"
        self.spec = None
        self.sim = Simulator()
        self.weather = OutdoorWeather(WEATHER_LAT, WEATHER_LON, SAMPLE_SECONDS)
        self.outputs = Outputs(drive=not self.simulate)
        self.gpio_error = self.outputs.error
        self._writes = 0
        self._last_error_at = 0.0
        self._header_checked = False

    def load(self):
        path = self.log_path
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            text = tail_bytes(path, KEEP_LOG_BYTES).decode("utf-8", "replace")
        except OSError as exc:
            print(f"Could not read {path.name} ({one_line(exc)}).", flush=True)
            return
        rows = parse_log(text)[-GRAPH_KEEP:]
        with self.lock:
            for row in rows:
                self.samples.append(row)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="tempwatch", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=SAMPLE_SECONDS + 2)
        try:
            self.outputs.close()
        except Exception:
            pass
        self.thread = None

    def _loop(self):
        while not self.stop.wait(SAMPLE_SECONDS):
            try:
                self.sample()
            except Exception:
                traceback.print_exc()
                self._fail("The sensor read failed.")

    def sample(self):
        if self.simulate:
            temp_c, humidity, label = self.sim.read()
        else:
            spec = self.spec or discover()
            if spec is None:
                self.spec = None
                self._fail("No DS18B20 or DHT sensor was found on GPIO 4.")
                return
            try:
                temp_c, humidity, label = spec.read()
            except SensorError as exc:
                self.spec = None
                self._fail(str(exc))
                return
            self.spec = spec
        if not math.isfinite(temp_c):
            self._fail("The sensor returned a reading that was not a number.")
            return
        if humidity is not None and (not math.isfinite(humidity) or humidity < 0 or humidity > 100):
            humidity = None
        out_temp_f, out_humidity = self.weather.read()
        temp_f = c_to_f(temp_c)
        with self.lock:
            cold_on, hot_on = alarms(temp_f, self.cold_on, self.hot_on)
            now = time.time()
            self.temp_f = temp_f
            self.temp_c = temp_c
            self.humidity = humidity
            self.out_temp_f = out_temp_f
            self.out_humidity = out_humidity
            self.when = now
            self.cold_on = cold_on
            self.hot_on = hot_on
            self.error = None
            self.weather_error = self.weather.error
            self.label = label
            self.samples.append(
                (now, temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f, out_humidity)
            )
        try:
            self.outputs.apply(cold_on, hot_on)
        except Exception as exc:
            self.outputs.error = one_line(exc)
        with self.lock:
            self.gpio_error = self.outputs.error
        self._append(now, temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f, out_humidity)
        out_bits = ""
        if out_temp_f is not None:
            out_bits = f"  out {out_temp_f:5.1f} F"
            if out_humidity is not None:
                out_bits += f" {out_humidity:3.0f}%"
        print(
            f"{fmt_clock()}  {temp_f:5.1f} F  "
            f"cold {'ON' if cold_on and COLD_PIN is not None else 'off'}  "
            f"hot {'ON' if hot_on and HOT_PIN is not None else 'off'}"
            f"{out_bits}",
            flush=True,
        )

    def _fail(self, message):
        with self.lock:
            self.error = message
        now = time.monotonic()
        if now - self._last_error_at < 60:
            return
        self._last_error_at = now
        print(f"{fmt_clock()}  no reading  {message}", flush=True)

    def _append(self, ts, temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f=None, out_humidity=None):
        line = csv_line(ts, temp_f, temp_c, humidity, cold_on, hot_on, out_temp_f, out_humidity)
        with self.lock:
            try:
                path = self.log_path
                path.parent.mkdir(parents=True, exist_ok=True)
                fresh = not path.exists() or path.stat().st_size == 0
                if not fresh and not self._header_checked:
                    self._upgrade_header(path)
                    self._header_checked = True
                with path.open("a", encoding="utf-8") as handle:
                    if fresh:
                        handle.write(CSV_HEADER)
                    handle.write(line)
                    handle.flush()
                self._writes += 1
                self.log_error = None
                if self._writes % 500 == 0:
                    self._trim()
            except OSError as exc:
                self.log_error = f"Could not write {self.log_path.name}: {one_line(exc)}"

    def _upgrade_header(self, path):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
                rest = handle.read()
        except OSError:
            return
        if not first.startswith("time_utc") or "out_temp_f" in first:
            return
        temporary = path.with_suffix(".csv.tmp")
        temporary.write_text(CSV_HEADER + rest, encoding="utf-8")
        os.replace(temporary, path)

    def _trim(self):
        path = self.log_path
        if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
            return
        data = tail_bytes(path, KEEP_LOG_BYTES)
        temporary = path.with_suffix(".csv.tmp")
        temporary.write_bytes(CSV_HEADER.encode("utf-8") + data)
        os.replace(temporary, path)

    def csv_bytes(self):
        with self.lock:
            try:
                if self.log_path.exists():
                    return self.log_path.read_bytes()
            except OSError as exc:
                message = f"Could not read the log: {one_line(exc)}\n".encode("utf-8")
                return CSV_HEADER.encode("utf-8") + message
        return CSV_HEADER.encode("utf-8")

    def snapshot(self):
        with self.lock:
            return {
                "simulate": self.simulate,
                "samples": list(self.samples),
                "temp_f": self.temp_f,
                "temp_c": self.temp_c,
                "humidity": self.humidity,
                "out_temp_f": self.out_temp_f,
                "out_humidity": self.out_humidity,
                "when": self.when,
                "cold_on": self.cold_on,
                "hot_on": self.hot_on,
                "error": self.error,
                "log_error": self.log_error,
                "weather_error": self.weather_error,
                "weather_enabled": self.weather.enabled,
                "label": self.label,
                "gpio_error": self.gpio_error,
                "backend": self.outputs.backend,
            }


def phrase_for(state):
    temp_f = state["temp_f"]
    if temp_f is None:
        return "Waiting for the sensor", "wait"
    cold = state["cold_on"] and COLD_PIN is not None
    hot = state["hot_on"] and HOT_PIN is not None
    if cold and hot:
        return "Both pins on", "hot"
    if cold:
        return "Cold pin on", "cold"
    if hot:
        return "Hot pin on", "hot"
    if temp_f <= COLD_ON_F:
        return "Below the cold limit", "cold"
    if temp_f >= HOT_ON_F:
        return "Above the hot limit", "hot"
    return "In range", "ok"


def side_text(kind, pin, on, on_at, off_at):
    if pin is None:
        return "off", f"{kind} output is unused. Set its pin at the top of the script to arm it."
    state = "on" if on else "off"
    word = "ON" if on else "off"
    text = (
        f"{pin_label(pin)} is {word}. "
        f"Turns on at {fmt_f(on_at)}°F, then off at {fmt_f(off_at)}°F."
    )
    return state, text


def nice_step(raw):
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = raw / (10 ** exponent)
    if base <= 1:
        nice = 1
    elif base <= 2:
        nice = 2
    elif base <= 5:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exponent)


def downsample(samples, buckets=360):
    """Average inside/outside temps and humidity into at most `buckets` points."""
    if len(samples) <= buckets:
        return list(samples)
    step = len(samples) / float(buckets)
    picked = []
    for index in range(buckets):
        start = int(index * step)
        end = int((index + 1) * step)
        chunk = samples[start:end]
        if not chunk:
            continue
        temp = sum(item[1] for item in chunk) / float(len(chunk))
        out_vals = [item[2] for item in chunk if item[2] is not None]
        out_temp = sum(out_vals) / float(len(out_vals)) if out_vals else None
        humid_vals = [item[3] for item in chunk if item[3] is not None]
        out_humid = sum(humid_vals) / float(len(humid_vals)) if humid_vals else None
        picked.append((chunk[-1][0], temp, out_temp, out_humid))
    return picked


def _series_points(ordered, index):
    return [(item[0], item[index]) for item in ordered if item[index] is not None]


def render_svg(samples):
    width, height = 720, 300
    left, right, top, bottom = 52, 44, 28, 28
    plot_w = width - left - right
    plot_h = height - top - bottom
    if len(samples) < 2:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 300" role="img" '
            'aria-label="Waiting for readings.">'
            '<text x="360" y="156" fill="#6a6558" font-size="18" text-anchor="middle">'
            "Waiting for readings.</text></svg>"
        )
    ordered = sorted(samples, key=lambda item: item[0])
    series = [
        (
            item[0],
            item[1],
            item[6] if len(item) > 6 else None,
            item[7] if len(item) > 7 else None,
        )
        for item in ordered
    ]
    points = downsample(series)
    temps = [item[1] for item in points]
    out_temps = [item[2] for item in points if item[2] is not None]
    out_humids = [item[3] for item in points if item[3] is not None]
    # Keep cold/hot trip points on-screen even when readings stay in the middle.
    range_vals = temps + out_temps + [float(COLD_ON_F), float(HOT_ON_F)]
    lo = min(range_vals)
    hi = max(range_vals)
    if hi - lo < 1:
        lo -= 0.5
        hi += 0.5
    pad = max(1.5, (hi - lo) * 0.06)
    y_lo = lo - pad
    y_hi = hi + pad
    t0 = points[0][0]
    t1 = points[-1][0]
    show_humid = len(out_humids) >= 2

    def x_of(ts):
        if t1 <= t0:
            return left + plot_w / 2.0
        return left + (ts - t0) / (t1 - t0) * plot_w

    def y_of(temp):
        return top + (y_hi - temp) / (y_hi - y_lo) * plot_h

    def y_humid(pct):
        return top + (100.0 - pct) / 100.0 * plot_h

    parts = []
    step = nice_step((y_hi - y_lo) / 5.0)
    tick = math.ceil((y_lo - 1e-9) / step) * step
    guard = 0
    while tick <= y_hi + step * 0.01 and guard < 12:
        y = y_of(tick)
        label = f"{tick:.0f}" if step >= 1 else f"{tick:.1f}"
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#d5d0c4"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" fill="#6a6558" font-size="12" text-anchor="end">'
            f"{html.escape(label)}</text>"
        )
        tick += step
        guard += 1

    if show_humid:
        for pct in (0, 25, 50, 75, 100):
            y = y_humid(pct)
            parts.append(
                f'<text x="{left + plot_w + 8}" y="{y + 4:.1f}" fill="#7a7568" font-size="11">'
                f"{pct}%</text>"
            )

    for limit, color in ((COLD_ON_F, "#2f5d9f"), (HOT_ON_F, "#b1432a")):
        y = y_of(limit)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-dasharray="4 4"/>'
        )
        parts.append(
            f'<text x="{left + 6}" y="{y - 6:.1f}" fill="{color}" font-size="12">'
            f"{html.escape(fmt_f(limit))}°</text>"
        )

    legend_x = left
    for color, label in (
        ("#2f6b3a", "Greenhouse °F"),
        ("#7a7568", "Outside °F"),
        ("#a39e90", "Outside RH"),
    ):
        if label.startswith("Outside °F") and not out_temps:
            continue
        if label.startswith("Outside RH") and not show_humid:
            continue
        parts.append(
            f'<rect x="{legend_x:.1f}" y="8" width="12" height="3" fill="{color}"/>'
            f'<text x="{legend_x + 16:.1f}" y="12" fill="#4a463c" font-size="11">'
            f"{html.escape(label)}</text>"
        )
        legend_x += 118

    greenhouse = [(item[0], item[1]) for item in points]
    coords = "".join(f"{x_of(ts):.1f},{y_of(temp):.1f} " for ts, temp in greenhouse)
    parts.append(
        f'<polyline points="{coords.strip()}" fill="none" stroke="#2f6b3a" stroke-width="2.2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    last_x = x_of(greenhouse[-1][0])
    last_y = y_of(greenhouse[-1][1])
    parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="#2f6b3a"/>')

    outside = _series_points(points, 2)
    if len(outside) >= 2:
        coords = "".join(f"{x_of(ts):.1f},{y_of(temp):.1f} " for ts, temp in outside)
        parts.append(
            f'<polyline points="{coords.strip()}" fill="none" stroke="#7a7568" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        parts.append(
            f'<circle cx="{x_of(outside[-1][0]):.1f}" cy="{y_of(outside[-1][1]):.1f}" '
            'r="3.5" fill="#7a7568"/>'
        )

    humid_pts = _series_points(points, 3)
    if len(humid_pts) >= 2:
        coords = "".join(f"{x_of(ts):.1f},{y_humid(pct):.1f} " for ts, pct in humid_pts)
        parts.append(
            f'<polyline points="{coords.strip()}" fill="none" stroke="#a39e90" stroke-width="1.5" '
            'stroke-dasharray="5 4" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        parts.append(
            f'<circle cx="{x_of(humid_pts[-1][0]):.1f}" cy="{y_humid(humid_pts[-1][1]):.1f}" '
            'r="3" fill="#a39e90"/>'
        )

    span = max(1.0, t1 - t0)
    for index, slot in enumerate((0.0, 0.5, 1.0)):
        ts = t0 + span * slot
        anchor = "start" if index == 0 else "end" if index == 2 else "middle"
        x = left if index == 0 else left + plot_w if index == 2 else left + plot_w * slot
        label = fmt_clock(ts, "%H:%M")
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8}" fill="#6a6558" font-size="12" text-anchor="{anchor}">'
            f"{html.escape(label)}</text>"
        )
    summary = (
        f"Greenhouse temperature. Low {min(temps):.1f} degrees F, "
        f"high {max(temps):.1f} degrees F."
    )
    if out_temps:
        summary += (
            f" Outside temperature. Low {min(out_temps):.1f} degrees F, "
            f"high {max(out_temps):.1f} degrees F."
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(summary, quote=True)}">{"".join(parts)}</svg>'
    )


def graph_note(samples):
    if len(samples) < 2:
        return "The line shows up after the next reading."
    temps = [item[1] for item in samples]
    lo = min(temps)
    hi = max(temps)
    note = (
        f"Greenhouse low {lo:.1f}°F, high {hi:.1f}°F. "
        f"Scale includes cold {fmt_f(COLD_ON_F)}°F and hot {fmt_f(HOT_ON_F)}°F."
    )
    out_temps = [item[6] for item in samples if len(item) > 6 and item[6] is not None]
    if out_temps:
        note += f" Outside low {min(out_temps):.1f}°F, high {max(out_temps):.1f}°F."
        note += " Humidity uses the right axis."
    return note


def render_rows(samples):
    if not samples:
        return '<tr><td colspan="8">No readings yet.</td></tr>'
    rows = []
    for row in reversed(samples[-12:]):
        ts, temp_f, temp_c, humidity, cold_on, hot_on = row[:6]
        out_temp_f = row[6] if len(row) > 6 else None
        out_humidity = row[7] if len(row) > 7 else None
        humid = "—" if humidity is None else f"{humidity:.0f}%"
        out_temp = "—" if out_temp_f is None else f"{out_temp_f:.1f}"
        out_humid = "—" if out_humidity is None else f"{out_humidity:.0f}%"
        clock = fmt_clock(ts, "%b %d %H:%M:%S")
        rows.append(
            "<tr>"
            f"<td>{html.escape(clock)}</td>"
            f"<td>{temp_f:.1f}</td>"
            f"<td>{temp_c:.1f}</td>"
            f"<td>{html.escape(humid)}</td>"
            f"<td>{html.escape(out_temp)}</td>"
            f"<td>{html.escape(out_humid)}</td>"
            f"<td>{'on' if cold_on else 'off'}</td>"
            f"<td>{'on' if hot_on else 'off'}</td>"
            "</tr>"
        )
    return "".join(rows)


SETUP_HTML = """
<section class="setup">
  <h2>The sensor is not showing up</h2>
  <p>Power it from 3.3V (physical pin 1) and ground. Put the data wire on GPIO 4 (physical pin 7). A 5V data line can damage the Pi. Add one of these lines, then reboot.</p>
  <p class="kicker">DS18B20</p>
  <code>dtoverlay=w1-gpio,gpiopin=4</code>
  <p class="kicker">DHT11 or DHT22</p>
  <code>dtoverlay=dht11,gpiopin=4</code>
  <p class="quiet">Use only the line that matches the sensor. Bookworm reads /boot/firmware/config.txt. Older images use /boot/config.txt. A bare DS18B20 needs a 4.7k resistor between data and 3.3V.</p>
</section>
"""

CSS = """
@import url("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Newsreader:opsz,wght@6..72,500;6..72,700&display=swap");
:root {
  color-scheme: light;
  --paper: #e8e2d4;
  --paper-2: #ddd5c4;
  --ink: #1f2418;
  --mute: #5c584c;
  --rule: #b7b09e;
  --leaf: #2f6b3a;
  --soil: #7a7568;
  --cold: #2f5d9f;
  --hot: #b1432a;
  --warn: #6b4a12;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  font: 16px/1.5 "IBM Plex Mono", "Cascadia Mono", monospace;
  background-color: var(--paper);
  background-image:
    linear-gradient(90deg, rgba(47, 107, 58, 0.035) 1px, transparent 1px),
    linear-gradient(rgba(47, 107, 58, 0.035) 1px, transparent 1px),
    radial-gradient(1200px 520px at 100% -10%, rgba(177, 67, 42, 0.08), transparent 50%),
    linear-gradient(165deg, #efe9db 0%, var(--paper) 42%, #d9d1bf 100%);
  background-size: 28px 28px, 28px 28px, auto, auto;
}
main {
  max-width: 860px;
  margin: 0 auto;
  padding: 36px 20px 64px;
}
h1, h2, p { margin: 0; }
h1 {
  font-family: "Newsreader", "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(2.4rem, 7vw, 3.6rem);
  font-weight: 700;
  line-height: 0.95;
  letter-spacing: -0.02em;
}
h2 {
  font-family: "Newsreader", "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 1.35rem;
  font-weight: 500;
}
.mast {
  display: grid;
  grid-template-columns: 1.4fr 0.8fr;
  gap: 18px 28px;
  align-items: end;
  margin-bottom: 28px;
  padding-bottom: 18px;
  border-bottom: 2px solid var(--ink);
}
.mast .tag {
  color: var(--mute);
  font-size: 0.78rem;
  max-width: 16rem;
  justify-self: end;
  text-align: right;
}
.banner {
  margin: 0 0 18px;
  padding: 10px 0;
  border-top: 1px solid var(--warn);
  border-bottom: 1px solid var(--warn);
  color: var(--warn);
}
.setup {
  margin: 0 0 22px;
  padding: 14px 0 4px;
  border-top: 1px dashed var(--rule);
}
.setup p { margin-top: 8px; }
.kicker {
  margin: 14px 0 4px;
  color: var(--mute);
  font-size: 0.72rem;
}
code {
  display: block;
  margin-top: 4px;
  padding: 8px 10px;
  background: rgba(31, 36, 24, 0.06);
  border-left: 3px solid var(--leaf);
  overflow-x: auto;
}
.readout {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
  gap: 10px 28px;
  margin: 8px 0 26px;
  padding: 8px 0 22px;
  border-bottom: 1px solid var(--rule);
}
.readout .temp {
  grid-row: span 4;
  align-self: start;
  font-family: "Newsreader", "Iowan Old Style", "Palatino Linotype", serif;
  font-size: clamp(5rem, 18vw, 7.5rem);
  font-weight: 700;
  line-height: 0.88;
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}
.readout.wait .temp { color: var(--mute); }
.readout.cold .temp { color: var(--cold); }
.readout.hot .temp { color: var(--hot); }
.readout.ok .temp { color: var(--leaf); }
.temp span {
  font-size: 0.28em;
  margin-left: 0.12em;
  color: var(--mute);
  font-family: "IBM Plex Mono", monospace;
  font-weight: 400;
  letter-spacing: 0;
  vertical-align: super;
}
.celsius, .humid, .outdoor, .status, .when {
  color: var(--mute);
  font-variant-numeric: tabular-nums;
}
.outdoor { color: var(--soil); }
.status {
  margin-top: 4px;
  color: var(--ink);
  font-weight: 600;
}
.readout.cold .status { color: var(--cold); }
.readout.hot .status { color: var(--hot); }
.readout.ok .status { color: var(--leaf); }
.when { margin-top: 2px; font-size: 0.85rem; }
.quiet, .note { color: var(--mute); }
.pins {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  margin: 0 0 10px;
  border-top: 1px solid var(--ink);
  border-bottom: 1px solid var(--ink);
}
.pin {
  margin: 0;
  padding: 14px 16px 16px 0;
}
.pin + .pin {
  padding-left: 16px;
  border-left: 1px solid var(--rule);
}
.pin strong {
  display: block;
  margin-bottom: 6px;
  font-family: "Newsreader", serif;
  font-size: 1.15rem;
  font-weight: 500;
}
.pin.cold strong { color: var(--cold); }
.pin.hot strong { color: var(--hot); }
.pin.on.cold { background: linear-gradient(90deg, rgba(47, 93, 159, 0.08), transparent 70%); }
.pin.on.hot { background: linear-gradient(90deg, rgba(177, 67, 42, 0.08), transparent 70%); }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin: 28px 0 12px;
}
.graph {
  padding: 8px 0 4px;
  background: rgba(255, 252, 245, 0.55);
  border: 1px solid var(--rule);
}
.graph svg { width: 100%; height: auto; display: block; }
.graph svg text { font-family: "IBM Plex Mono", monospace; }
.note { margin-top: 10px; font-size: 0.88rem; }
.csv {
  color: var(--ink);
  text-decoration: underline;
  text-underline-offset: 3px;
  font-weight: 600;
}
.csv:hover { color: var(--leaf); }
.csv:focus-visible, a:focus-visible {
  outline: 2px solid var(--leaf);
  outline-offset: 3px;
}
.table-wrap {
  overflow-x: auto;
  margin-top: 8px;
  border-top: 1px solid var(--ink);
}
table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  font-size: 0.92rem;
}
th, td {
  text-align: left;
  padding: 9px 14px 9px 0;
  white-space: nowrap;
}
th {
  color: var(--mute);
  font-size: 0.72rem;
  font-weight: 600;
}
td { border-top: 1px solid var(--rule); }
@media (max-width: 700px) {
  .mast, .readout, .pins { grid-template-columns: 1fr; }
  .mast .tag, .pin + .pin {
    justify-self: start;
    text-align: left;
    padding-left: 0;
    border-left: 0;
    border-top: 1px solid var(--rule);
  }
  .readout .temp { grid-row: auto; font-size: 4.6rem; }
  main { padding: 24px 14px 48px; }
}
"""


def render_page(state):
    phrase, kind = phrase_for(state)
    temp_f = state["temp_f"]
    temp_text = "—" if temp_f is None else f"{temp_f:.1f}"
    temp_c = state["temp_c"]
    celsius = "—" if temp_c is None else f"{temp_c:.1f}°C"
    if state["humidity"] is None:
        humid = ""
    else:
        humid = f'<p class="humid">Greenhouse humidity {state["humidity"]:.0f}%</p>'
    out_temp_f = state.get("out_temp_f")
    out_humidity = state.get("out_humidity")
    if out_temp_f is None:
        if state.get("weather_enabled") and state.get("weather_error"):
            outdoor = f'<p class="outdoor">Outside weather unavailable</p>'
        else:
            outdoor = ""
    else:
        out_bits = f"Outside {out_temp_f:.1f}°F"
        if out_humidity is not None:
            out_bits += f" · {out_humidity:.0f}% RH"
        outdoor = f'<p class="outdoor">{html.escape(out_bits)}</p>'
    if state["when"] is None:
        when = "No reading yet"
    else:
        when = "Sample " + fmt_clock(state["when"], "%b %d, %H:%M:%S")
    title = "Greenhouse Temperature" if temp_f is None else f"{temp_f:.1f}°F · Greenhouse Temperature"
    cold_class, cold_text = side_text(
        "Cold", COLD_PIN, state["cold_on"], COLD_ON_F, COLD_ON_F + HYSTERESIS_F
    )
    hot_class, hot_text = side_text(
        "Hot", HOT_PIN, state["hot_on"], HOT_ON_F, HOT_ON_F - HYSTERESIS_F
    )
    if state["simulate"]:
        banner = "This machine has no probe on GPIO 4, so the trace is simulated and the output pins stay off."
    elif state["error"] and temp_f is None:
        banner = ""
    elif state["error"]:
        banner = "Showing the last good reading. " + state["error"]
    else:
        banner = ""
    if state["log_error"]:
        banner = (banner + " " if banner else "") + state["log_error"]
    if state.get("weather_enabled") and state.get("weather_error") and out_temp_f is None:
        banner = (banner + " " if banner else "") + "Outdoor weather: " + state["weather_error"]
    banner_html = f'<p class="banner">{html.escape(banner)}</p>' if banner else ""
    show_setup = (not state["simulate"]) and temp_f is None
    setup = SETUP_HTML if show_setup else ""
    if state["simulate"]:
        pin_note = "Output pins are not driven while the probe is simulated."
    elif state["gpio_error"]:
        pin_note = state["gpio_error"]
    elif state["backend"] == "gpiozero":
        pin_note = ""
        if ACTIVE_LOW:
            pin_note += " An alarm pulls the pin low."
    else:
        pin_note = "Output pins are off."
    samples = state["samples"]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <meta http-equiv="refresh" content="{int(max(5, SAMPLE_SECONDS))}">
  <title>{html.escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
<main>
  <header class="mast">
    <h1>Greenhouse</h1>
    <p class="tag">Probe on GPIO 4. Relays follow greenhouse temperature only. Times are UTC−6.</p>
  </header>
  {banner_html}
  {setup}
  <section class="readout {kind}">
    <p class="temp">{html.escape(temp_text)}<span>°F</span></p>
    <p class="celsius">{html.escape(celsius)}</p>
    {humid}
    {outdoor}
    <p class="status">{html.escape(phrase)}</p>
    <p class="when">{html.escape(when)}</p>
  </section>
  <section class="pins">
    <p class="pin cold {cold_class}"><strong>Cold</strong> {html.escape(cold_text)}</p>
    <p class="pin hot {hot_class}"><strong>Hot</strong> {html.escape(hot_text)}</p>
  </section>
  <p class="note">{html.escape(pin_note)}</p>
  <section>
    <div class="section-head">
      <h2>History</h2>
      <a class="csv" href="/tempwatch.csv">Download CSV</a>
    </div>
    <div class="graph">{render_svg(samples)}</div>
    <p class="note">{html.escape(graph_note(samples))}</p>
  </section>
  <section>
    <div class="section-head">
      <h2>Recent readings</h2>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Greenhouse °F</th><th>Greenhouse °C</th><th>Greenhouse RH</th>
            <th>Outside °F</th><th>Outside RH</th><th>Cold</th><th>Hot</th>
          </tr>
        </thead>
        <tbody>{render_rows(samples)}</tbody>
      </table>
    </div>
  </section>
</main>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/":
                if watcher is None:
                    body = b"Tempwatch is not running.\n"
                    self._send(503, "text/plain; charset=utf-8", body)
                    return
                page = render_page(watcher.snapshot()).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", page)
                return
            if path == "/tempwatch.csv":
                payload = watcher.csv_bytes() if watcher is not None else CSV_HEADER.encode("utf-8")
                self._send(
                    200,
                    "text/csv; charset=utf-8",
                    payload,
                    (("Content-Disposition", 'attachment; filename="tempwatch.csv"'),),
                )
                return
            self._send(404, "text/plain; charset=utf-8", b"Not found\n")
        except Exception:
            traceback.print_exc()
            try:
                self._send(500, "text/plain; charset=utf-8", b"Something went wrong.\n")
            except Exception:
                pass

    def _send(self, status, content_type, body, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    check_config()
    parser = argparse.ArgumentParser(description="Log the GPIO 4 temperature and switch two output pins.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Do not read a probe and do not drive GPIO. A fake room temperature is logged instead.",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port has to be between 1 and 65535.")

    global watcher
    if args.simulate:
        simulate = True
    else:
        simulate = not (on_pi() or discover() is not None)
    watcher = Watcher(simulate)
    watcher.load()
    watcher.sample()
    watcher.start()

    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        watcher.close()
        raise SystemExit(f"Could not listen on {args.host}:{args.port}. {exc}") from exc

    def stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print("Tempwatch", flush=True)
    print("  Sensor data wire on GPIO 4 (physical pin 7).", flush=True)
    if COLD_PIN is None:
        print("  Cold output is disabled.", flush=True)
    else:
        print(
            f"  {pin_label(COLD_PIN)} turns on at {fmt_f(COLD_ON_F)} F "
            f"and off at {fmt_f(COLD_ON_F + HYSTERESIS_F)} F.",
            flush=True,
        )
    if HOT_PIN is None:
        print("  Hot output is disabled.", flush=True)
    else:
        print(
            f"  {pin_label(HOT_PIN)} turns on at {fmt_f(HOT_ON_F)} F "
            f"and off at {fmt_f(HOT_ON_F - HYSTERESIS_F)} F.",
            flush=True,
        )
    if simulate:
        print("  No probe on this machine. Simulating a room temperature. Pins are not driven.", flush=True)
    else:
        print("  Reading GPIO 4. Output pins stay off until a limit is crossed.", flush=True)
    if WEATHER_LAT is not None and WEATHER_LON is not None:
        print(
            f"  Outdoor weather from Open-Meteo at {float(WEATHER_LAT):.4f}, "
            f"{float(WEATHER_LON):.4f} (every {int(SAMPLE_SECONDS)}s).",
            flush=True,
        )
    print(f"  Open http://127.0.0.1:{args.port}/", flush=True)
    if args.host in ("0.0.0.0", "::"):
        print(f"  From another device, open http://<this-pi-ip>:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    finally:
        watcher.close()
        httpd.server_close()
        print("Stopped. Output pins are off.", flush=True)


if __name__ == "__main__":
    main()