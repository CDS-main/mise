"""HX711 load cell interface.

Runs simulated on a laptop and real on the Pi — same API either way, so the
frontend never knows which it's talking to. That is deliberate: it means you
can build and test the whole UI before the hardware lands, and swapping in
the real cell changes nothing above this file.

Calibration lives in data/scale.json:
    {"offset": <raw reading with nothing on the pan>,
     "scale":  <raw counts per gram>}

To calibrate: POST /api/scale/tare with an empty pan, put a known mass on,
then POST /api/scale/calibrate {"grams": 500}.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from pathlib import Path

CAL_PATH = Path(__file__).resolve().parent.parent / "data" / "scale.json"

try:                                          # real hardware, Pi only
    from hx711 import HX711            # type: ignore
    import RPi.GPIO as GPIO            # type: ignore
    HAVE_HX711 = True
except Exception:
    HAVE_HX711 = False


class Scale:
    """One load cell. Thread-safe, non-blocking reads from a background sampler."""

    def __init__(self, dout: int = 5, sck: int = 6, sps: int = 10):
        self.dout, self.sck, self.sps = dout, sck, sps
        self.offset, self.scale = 0.0, 1.0
        self._raw = 0.0
        self._buf: list[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._hw = None
        self._sim_target = 0.0            # simulator: where the pan is heading
        self._load_cal()
        # Hardware init is allowed to fail. A missing or incompatible GPIO
        # library must never take the whole application down — the scale is
        # one feature, and everything else still works simulated.
        # NOTE: RPi.GPIO does NOT work on a Raspberry Pi 5 (RP1 southbridge;
        # its SOC base-address probe raises RuntimeError). Use rpi-lgpio,
        # which is a drop-in replacement, when the cell is actually wired.
        if HAVE_HX711:
            try:
                GPIO.setmode(GPIO.BCM)
                self._hw = HX711(dout_pin=dout, pd_sck_pin=sck)
            except Exception as e:
                print(f"[scale] hardware init failed ({type(e).__name__}: {e}) "
                      f"— falling back to the simulator")
                self._hw = None
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    # ── calibration ────────────────────────────────────────────────────────
    def _load_cal(self) -> None:
        try:
            d = json.loads(CAL_PATH.read_text())
            self.offset, self.scale = float(d["offset"]), float(d["scale"]) or 1.0
        except Exception:
            self.offset, self.scale = 0.0, 1.0

    def _save_cal(self) -> None:
        CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        CAL_PATH.write_text(json.dumps({"offset": self.offset, "scale": self.scale}, indent=2))

    def tare(self) -> float:
        with self._lock:
            self.offset = self._raw
        self._save_cal()
        return self.offset

    def calibrate(self, known_grams: float) -> float:
        """Call with a known mass sitting on the pan, after tare()."""
        if known_grams <= 0:
            raise ValueError("known mass must be positive")
        with self._lock:
            delta = self._raw - self.offset
        if abs(delta) < 1e-6:
            raise ValueError("no change in raw reading — is the mass actually on the pan?")
        self.scale = delta / known_grams
        self._save_cal()
        return self.scale

    # ── reads ──────────────────────────────────────────────────────────────
    def grams(self) -> float:
        with self._lock:
            return round((self._raw - self.offset) / (self.scale or 1.0), 2)

    def stable(self, window: int = 8, tol_g: float = 0.4) -> bool:
        """The settle detector. Same rule the UI uses: low spread over a window.

        This is the piece worth writing up — a bare threshold fires while the
        pan is still moving, and a fixed delay is slower than it needs to be.
        Standard deviation over a short window is neither.
        """
        with self._lock:
            buf = self._buf[-window:]
        if len(buf) < window:
            return False
        g = [(b - self.offset) / (self.scale or 1.0) for b in buf]
        return statistics.pstdev(g) < tol_g

    def status(self) -> dict:
        return {"grams": self.grams(), "stable": self.stable(),
                "simulated": self._hw is None, "sps": self.sps,
                "offset": self.offset, "scale": self.scale}

    # ── simulator control (no-op on real hardware) ─────────────────────────
    def sim_set(self, grams: float) -> None:
        self._sim_target = float(grams)

    # ── sampler ────────────────────────────────────────────────────────────
    def _read_raw(self) -> float:
        if self._hw is not None:
            try:
                self._hw.reset()
                vals = self._hw.get_raw_data(times=3)
                return float(sum(vals) / len(vals)) if vals else self._raw
            except Exception:
                return self._raw
        # simulator: first-order approach to target + a little noise
        drift = (self._sim_target * (self.scale or 1.0) + self.offset - self._raw) * 0.25
        noise = math.sin(time.time() * 7.3) * 1.5
        return self._raw + drift + noise

    def _loop(self) -> None:
        period = 1.0 / max(1, self.sps)
        while not self._stop.is_set():
            v = self._read_raw()
            with self._lock:
                self._raw = v
                self._buf.append(v)
                if len(self._buf) > 64:
                    del self._buf[:-64]
            time.sleep(period)

    def close(self) -> None:
        self._stop.set()


SCALE = Scale()
