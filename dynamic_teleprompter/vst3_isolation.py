"""Isolated VST3 host. Third-party plugin code never runs in the GUI process.

The host is a spawned Python child that loads Pedalboard and plugin binaries.
Audio runs over a local multiprocessing pipe (copies blocks, experimental latency).
"""
import multiprocessing as mp
import threading
import time
from audio_diagnostics import observe
from pathlib import Path

import numpy as np

_TIMEOUT = 0.35
_START_TIMEOUT = 8.0
_INSPECTION_TIMEOUT = 8.0
_thread_state = threading.local()


def _host(conn):
    # Imported only in child: crashes in a plugin must not terminate the UI.
    from pedalboard import load_plugin
    instances = {}
    try:
        while True:
            try:
                message = conn.recv()
            except EOFError:
                break
            op = message[0]
            if op == 'close':
                break
            try:
                _, path, plugin_name, *rest = message
                key = (path, plugin_name)
                if key not in instances:
                    kw = {'plugin_name': plugin_name} if plugin_name else {}
                    instances[key] = load_plugin(path, **kw)
                plugin = instances[key]
                if op == 'inspect':
                    details = {}
                    for name, param in plugin.parameters.items():
                        value = getattr(param, 'raw_value', None)
                        if isinstance(value, (str, int, float, bool)):
                            details[str(name)] = value
                    conn.send(('ok', details))
                elif op == 'process':
                    values, raw = rest
                    for name, value in (values or {}).items():
                        param = plugin.parameters.get(name)
                        if param is not None:
                            param.raw_value = value
                    samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
                    result = np.asarray(plugin(samples.reshape(1, -1), 48000, reset=False), dtype=np.float32)
                    if result.ndim == 2:
                        result = result.mean(axis=0)
                    result = np.clip(np.nan_to_num(result).reshape(-1), -1, 1)
                    if len(result) != len(samples):
                        raise RuntimeError('VST3 changed buffer length unexpectedly')
                    conn.send(('ok', np.clip(np.rint(result * 32767), -32768, 32767).astype('<i2').tobytes()))
            except Exception as exc:
                conn.send(('error', str(exc)))
    finally:
        conn.close()


class Host:
    def __init__(self):
        ctx = mp.get_context('spawn')
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=_host, args=(child,), daemon=True)
        self.process.start()
        child.close()
        self.first_request = True

    def request(self, msg, timeout):
        if not self.process.is_alive():
            raise RuntimeError('VST3 host process exited')
        try:
            self.conn.send(msg)
            if not self.conn.poll(max(timeout, _START_TIMEOUT) if self.first_request else timeout):
                raise TimeoutError('VST3 host timed out')
            status, result = self.conn.recv()
            self.first_request = False
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise RuntimeError('VST3 host connection lost') from exc
        if status != 'ok':
            raise RuntimeError(str(result))
        return result

    def close(self):
        if self.process.is_alive():
            try:
                self.conn.send(('close',))
                self.process.join(0.3)
            except (OSError, BrokenPipeError):
                pass
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(0.5)
        self.conn.close()


def _get_host():
    host = getattr(_thread_state, 'host', None)
    if host is None or not host.process.is_alive():
        if host is not None:
            host.close()
        host = Host()
        _thread_state.host = host
    return host


def process(path, plugin_name, parameters, data):
    path = str(Path(path))
    if not Path(path).exists():
        raise FileNotFoundError('VST3 not found: ' + path)
    # If a plugin fails, avoid restarting its host on every 10ms audio chunk.
    cooldown = getattr(_thread_state, 'cooldown', {})
    retry_time = cooldown.get((path, plugin_name), 0)
    if time.monotonic() < retry_time:
        raise RuntimeError('VST3 temporarily bypassed after host failure')
    try:
        started = time.perf_counter()
        result = _get_host().request(('process', path, plugin_name, parameters, data), _TIMEOUT)
        observe('vst_host_roundtrip', (time.perf_counter() - started) * 1000, len(data) / 2 / 48000 * 1000)
        cooldown.pop((path, plugin_name), None)
        return result
    except (RuntimeError, TimeoutError, OSError) as exc:
        cooldown[(path, plugin_name)] = time.monotonic() + 5
        _thread_state.cooldown = cooldown
        host = getattr(_thread_state, 'host', None)
        if host:
            host.close()
            _thread_state.host = None
        raise exc


def inspect(path, plugin_name=''):
    # Inspection uses a short-lived separate process, even when monitoring is active.
    host = Host()
    try:
        return host.request(('inspect', str(path), str(plugin_name)), _INSPECTION_TIMEOUT)
    finally:
        host.close()
