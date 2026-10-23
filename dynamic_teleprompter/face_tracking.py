"""Experimental webcam head-position tracker. No webcam data leaves this computer.

OpenCV's built-in frontal-face detector is used for a basic head position estimate.
It is NOT full 3D landmark tracking and cannot detect blinks, mouth shapes or gaze.
"""
import threading
import math
import time

_lock = threading.Lock()
_values = {}
_status = 'Off'
_stop = threading.Event()
_thread = None
_preview_jpeg = None
_preview_camera = None

def current_tracking():
    with _lock:
        return dict(_values)

def tracking_active():
    """Whether a tracking worker is currently alive (including camera startup)."""
    with _lock:
        return _thread is not None and _thread.is_alive() and not _stop.is_set()


def directshow_camera_names():
    """Optional friendly names from DirectShow's enumeration order on Windows.

    Names are only used when scanning found the same camera through DirectShow.
    Non-DirectShow backends cannot safely be associated with these names.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


def camera_preview():
    """Return (JPEG bytes, camera index) from the active camera; never access OpenCV in the UI."""
    with _lock:
        return _preview_jpeg, _preview_camera


def inspect_camera(camera_index, backend_name=None):
    """Open a camera only for a diagnostic snapshot; returns JPEG bytes or raises.

    Call from a background worker so slow cameras cannot freeze the Qt interface.
    """
    import cv2
    choices = _camera_backends(cv2)
    if backend_name:
        choices = sorted(choices, key=lambda pair: pair[0] != backend_name)
    cap = None
    try:
        for name, backend in choices:
            candidate = cv2.VideoCapture(int(camera_index), backend)
            if candidate.isOpened():
                cap = candidate
                break
            candidate.release()
        if cap is None:
            raise RuntimeError(f'Camera {camera_index} could not be opened')
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f'Camera {camera_index} did not return a frame')
        width = frame.shape[1]
        if width > 640:
            frame = cv2.resize(frame, (640, round(frame.shape[0]*640/width)))
        ok, blob = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if not ok:
            raise RuntimeError('Failed to encode camera preview')
        return blob.tobytes()
    finally:
        if cap is not None:
            cap.release()


def tracking_status():
    with _lock:
        return _status

def _publish(values, status):
    global _values, _status
    with _lock:
        _values, _status = values, status

def stop_tracking():
    global _thread
    _stop.set()
    t = _thread
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=2)
    _thread = None
    _publish({}, 'Off')
    global _preview_jpeg, _preview_camera
    with _lock:
        _preview_jpeg = None
        _preview_camera = None

def probe_cameras(indices=range(8)):
    """Find usable local camera indexes; return (index, backend) tuples.

    Run this from a worker, never the GUI thread. OpenCV does not expose
    reliable human-readable Windows device names across backends.
    """
    try:
        import cv2
    except ImportError:
        raise RuntimeError('OpenCV missing: run setup_windows.bat and install webcam tracking')
    found = []
    for index in indices:
        for backend_name, backend in _camera_backends(cv2):
            cap = None
            try:
                cap = cv2.VideoCapture(int(index), backend)
                if cap is not None and cap.isOpened():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        found.append((int(index), backend_name))
                        break
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()
    return found


def _camera_backends(cv2):
    choices = []
    for name, attr in [('DirectShow', 'CAP_DSHOW'), ('Media Foundation', 'CAP_MSMF')]:
        if hasattr(cv2, attr):
            choices.append((name, getattr(cv2, attr)))
    choices.append(('Default', cv2.CAP_ANY))
    return choices


def open_camera(index):
    import cv2
    for name, backend in _camera_backends(cv2):
        cap = None
        try:
            cap = cv2.VideoCapture(int(index), backend)
            if cap.isOpened():
                return cap, name
        except Exception:
            pass
        if cap is not None:
            cap.release()
    return None, None


def start_tracking(camera_index=0):
    global _thread
    stop_tracking()
    _publish({}, f'Opening camera {int(camera_index)}…')
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(int(camera_index),), daemon=True, name='Live2D webcam tracker')
    _thread.start()

def _run(camera_index):
    try:
        import cv2
        classifier = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        if classifier.empty():
            _publish({}, 'OpenCV face detector unavailable')
            return
        cap, backend = open_camera(camera_index)
        if cap is None:
            _publish({}, f'Cannot open camera {camera_index}. Select another camera or check Windows camera permissions.')
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        _publish({}, f'Camera {camera_index} opened ({backend}); searching for face')
        eyes_classifier = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
        smoothed = None
        missed = 0
        last_preview = 0.0
        try:
            while not _stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    _publish({}, 'Camera stopped providing frames')
                    break
                global _preview_jpeg, _preview_camera
                now = time.monotonic()
                if now - last_preview >= 0.15:
                    last_preview = now
                    small = cv2.resize(frame, (320, round(frame.shape[0] * 320 / frame.shape[1])))
                    ok_jpg, preview = cv2.imencode('.jpg', small)
                    if ok_jpg:
                        with _lock:
                            _preview_jpeg = preview.tobytes()
                            _preview_camera = camera_index
                h,w = frame.shape[:2]
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(70,70))
                if len(faces):
                    x,y,fw,fh = max(faces, key=lambda f: f[2]*f[3])
                    # Inverse horizontal so model moves in the user's mirror direction.
                    nx = max(-1., min(1., 2*(x+fw/2)/w-1))
                    ny = max(-1., min(1., 2*(y+fh/2)/h-1))
                    # Eye-pair slope supplies an approximate head-roll angle. This
                    # is not true 3D pose estimation, but improves on a face-box-only tracker.
                    roll = 0.0
                    eye_found = False
                    if not eyes_classifier.empty():
                        roi = gray[y:y+int(fh*0.65), x:x+fw]
                        eyes = eyes_classifier.detectMultiScale(roi, scaleFactor=1.12,
                                                               minNeighbors=5,
                                                               minSize=(max(10, fw//12), max(10, fh//12)))
                        if len(eyes) >= 2:
                            # Prefer a horizontally separated pair in the upper face.
                            candidates = sorted(eyes, key=lambda e: e[2]*e[3], reverse=True)[:5]
                            pairs = [(a,b) for i,a in enumerate(candidates) for b in candidates[i+1:]
                                     if abs((a[0]+a[2]/2)-(b[0]+b[2]/2)) >= fw*0.2]
                            if pairs:
                                left,right = max(pairs, key=lambda ab: abs((ab[0][0]+ab[0][2]/2)-(ab[1][0]+ab[1][2]/2)))
                                if left[0] > right[0]: left,right = right,left
                                dx = (right[0]+right[2]/2)-(left[0]+left[2]/2)
                                dy = (right[1]+right[3]/2)-(left[1]+left[3]/2)
                                roll = max(-25., min(25., math.degrees(math.atan2(dy,dx))))
                                eye_found = True
                    target = (-nx*25.0, -ny*20.0, roll)
                    smoothed = target if smoothed is None else tuple(a*0.8+b*0.2 for a,b in zip(smoothed,target))
                    _publish({'ParamAngleX':smoothed[0], 'ParamAngleY':smoothed[1],
                              'ParamAngleZ':smoothed[2],
                              'ParamBodyAngleX':smoothed[0]*0.3},
                             f'Camera {camera_index}: Tracking face (position + {"eye roll" if eye_found else "position only"})')
                    missed = 0
                else:
                    missed += 1
                    if missed > 12:
                        smoothed = None
                        _publish({}, f'Camera {camera_index}: Searching for face')
                _stop.wait(0.03)
        finally:
            cap.release()
    except ImportError:
        _publish({}, 'OpenCV not installed — run install_face_tracking_windows.bat')
    except Exception as exc:
        _publish({}, f'Tracker error: {str(exc)[:120]}')
