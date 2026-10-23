"""Bounded, latest-request-only background rendering, independent of Qt widgets.

The processor and all mutable model state belong exclusively to one daemon
thread. Published images must never be mutated after publication.
"""
from collections import OrderedDict
from dataclasses import dataclass
import threading
import time


@dataclass
class _Slot:
    signature: object
    payload: object
    pending: bool = True
    value: object = None
    error: str = ''
    ready_at: float = 0.
    elapsed_ms: float = 0.
    completed: int = 0


class LatestPoseWorker:
    def __init__(self, processor, capacity=5, interval=1 / 30):
        self._processor = processor
        self._capacity = capacity
        self._interval = interval
        self._condition = threading.Condition()
        self._slots = OrderedDict()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name='Live2D pose renderer', daemon=True)
        self._thread.start()

    def request(self, key, signature, payload):
        """Nonblocking: replace pending input and return the last completed value."""
        with self._condition:
            if self._closed:
                return None, ''
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot(signature, payload)
            elif slot.signature != signature:
                slot.signature, slot.payload, slot.pending = signature, payload, True
            self._slots.move_to_end(key)
            while len(self._slots) > self._capacity:
                self._slots.popitem(last=False)
            self._condition.notify()
            return slot.value, slot.error

    def stats(self, key):
        with self._condition:
            slot = self._slots.get(key)
            return ({'render_ms': slot.elapsed_ms, 'completed': slot.completed,
                     'pending': slot.pending} if slot else {})

    def close(self, timeout=0):
        # Never wait for CPU rasterization on the Qt shutdown path. The daemon
        # owns its DLL/model buffers until its in-flight operation returns.
        with self._condition:
            self._closed = True
            self._slots.clear()
            self._condition.notify_all()
        if timeout:
            self._thread.join(timeout)

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    now = time.monotonic()
                    pending = [(key, slot) for key, slot in self._slots.items() if slot.pending]
                    ready = [(key, slot) for key, slot in pending if slot.ready_at <= now]
                    if ready:
                        # Least recently completed first; rapid input on one
                        # character must not starve another character's image.
                        key, slot = min(ready, key=lambda item: item[1].ready_at)
                        signature, payload = slot.signature, slot.payload
                        slot.pending = False
                        break
                    delay = min(slot.ready_at - now for _, slot in pending) if pending else None
                    self._condition.wait(delay)
            started = time.perf_counter()
            try:
                value = self._processor(key, payload)
                error = ''
            except Exception as exc:
                value, error = None, str(exc)
            elapsed = (time.perf_counter() - started) * 1000
            with self._condition:
                if self._closed:
                    return
                if self._slots.get(key) is not slot:
                    continue
                # Publish in-flight completion even if a newer pose is waiting:
                # discarding every stale result would starve animated models.
                if value is not None:
                    slot.value = value
                slot.error = error
                slot.elapsed_ms = elapsed
                slot.completed += 1
                slot.ready_at = time.monotonic() + self._interval
                slot.pending = slot.signature != signature
