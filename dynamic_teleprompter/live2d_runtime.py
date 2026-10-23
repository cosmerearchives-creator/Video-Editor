"""Experimental Cubism Core model mesh viewer for static poses.

Uses an SDK-installed official Cubism Core DLL. Meshes are drawn via Qt;
Cubism advanced clipping masks, shader effects, physics and motions are not
implemented. This is not a substitute for the official Cubism renderer.
"""
import ctypes as C
import os
import json
import math
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QRectF, Qt, QPointF
from PySide6.QtGui import QPainter, QPainterPath, QImage, QTransform
from live2d_idle import animation_values, microphone
from live2d_movement import perspective_scale
from live2d_worker import LatestPoseWorker
from collections import OrderedDict


class Vector2(C.Structure):
    _fields_ = [('X', C.c_float), ('Y', C.c_float)]


def _pointer(dll, name, result, *args):
    func = getattr(dll, name)
    func.restype = result
    func.argtypes = list(args)
    return func


class CubismMeshModel:
    def __init__(self, path, dll):
        self.path = Path(path).resolve()
        model_json = json.loads(self.path.read_text(encoding='utf-8-sig'))
        refs = model_json['FileReferences']
        self.textures = [QImage(str((self.path.parent / t).resolve())).convertToFormat(QImage.Format.Format_ARGB32)
                         for t in refs['Textures']]
        if any(t.isNull() for t in self.textures):
            raise ValueError('Could not load one or more model textures')
        blob = (self.path.parent / refs['Moc']).read_bytes()
        # Cubism Core requires 64-byte aligned, writable, long-lived moc memory.
        self._moc_bytes = C.create_string_buffer(len(blob) + 64)
        self._moc_address = (C.addressof(self._moc_bytes) + 63) & ~63
        C.memmove(self._moc_address, blob, len(blob))
        self._moc = dll.csmReviveMocInPlace(self._moc_address, len(blob))
        if not self._moc:
            raise ValueError('Cubism Core rejected this moc3 model')
        size = dll.csmGetSizeofModel(self._moc)
        self._model_bytes = C.create_string_buffer(size + 16)
        self._model_address = (C.addressof(self._model_bytes) + 15) & ~15
        self._model = dll.csmInitializeModelInPlace(self._moc, self._model_address, size)
        if not self._model:
            raise ValueError('Cubism Core could not initialize the model')
        dll.csmUpdateModel(self._model)
        self.dll = dll
        # Tracking must not prevent the previously working static renderer from loading.
        # Some Cubism Core builds/models may reject parameter introspection.
        self.parameter_indices = {}
        self.parameter_defaults = []
        self.parameter_min = None
        self.parameter_max = None
        self.parameter_values = None
        self.last_pose = None
        self.previous_controlled = set()
        self._next_idle_update = 0.0
        try:
            count_params = dll.csmGetParameterCount(self._model)
            ids = dll.csmGetParameterIds(self._model)
            self.parameter_indices = {ids[i].decode('utf-8', 'replace'): i for i in range(count_params)}
            defaults = dll.csmGetParameterDefaultValues(self._model)
            self.parameter_defaults = [defaults[i] for i in range(count_params)]
            self.parameter_min = dll.csmGetParameterMinimumValues(self._model)
            self.parameter_max = dll.csmGetParameterMaximumValues(self._model)
            self.parameter_values = dll.csmGetParameterValues(self._model)
        except Exception as exc:
            print(f'Live2D tracking unavailable; static renderer active: {exc}', flush=True)
        self.count = dll.csmGetDrawableCount(self._model)
        self.canvas = Vector2()
        self.origin = Vector2()
        self.ppu = C.c_float()
        dll.csmReadCanvasInfo(self._model, C.byref(self.canvas), C.byref(self.origin), C.byref(self.ppu))
        if self.canvas.X <= 0 or self.canvas.Y <= 0 or self.ppu.value <= 0:
            raise ValueError('Invalid Cubism canvas geometry')

    def set_tracking(self, values):
        """Update parameters on the rendering thread only; clamp to model limits."""
        if not self.parameter_indices or self.parameter_values is None:
            return False
        pose = tuple(sorted((str(k), round(float(v), 3)) for k, v in values.items() if k in self.parameter_indices))
        if pose == self.last_pose or (not pose and self.last_pose is None):
            return False
        controlled = {pid for pid, _ in pose}
        for pid in self.previous_controlled - controlled:
            index = self.parameter_indices[pid]
            self.parameter_values[index] = self.parameter_defaults[index]
        for pid, value in pose:
            index = self.parameter_indices.get(pid)
            if index is not None:
                self.parameter_values[index] = max(self.parameter_min[index], min(self.parameter_max[index], value))
        self.previous_controlled = controlled
        self.dll.csmUpdateModel(self._model)
        self.last_pose = pose
        return True

    def movement_bounds(self, width, height):
        """Conservative visible mesh bounds, normalized to the rendered image.

        Called only by the model's owning worker. Masking can shrink these
        bounds; including all visible vertices safely accounts for padding.
        """
        d, m = self.dll, self._model
        counts = d.csmGetDrawableVertexCounts(m)
        vertices = d.csmGetDrawableVertexPositions(m)
        opacity = d.csmGetDrawableOpacities(m)
        factor = min(width / self.canvas.X, height / self.canvas.Y)
        left, top, right, bottom = 1., 1., 0., 0.
        for i in range(self.count):
            if opacity[i] <= 0 or not vertices[i]:
                continue
            for j in range(counts[i]):
                v = vertices[i][j]
                x = .5 + (v.X * self.ppu.value + self.origin.X - self.canvas.X / 2) * factor / width
                y = .5 - (v.Y * self.ppu.value + self.origin.Y - self.canvas.Y / 2) * factor / height
                left, top = min(left, x), min(top, y)
                right, bottom = max(right, x), max(bottom, y)
        if right <= left or bottom <= top:
            return 0., 0., 1., 1.
        bounds = max(0., left), max(0., top), min(1., right), min(1., bottom)
        return bounds if bounds[0] < bounds[2] and bounds[1] < bounds[3] else (0., 0., 1., 1.)

    def draw(self, painter, canvas_width, canvas_height):
        """Render static Cubism geometry with basic clipping masks and blend modes.

        This is a Qt CPU fallback, not full compatibility with Cubism's GPU renderer.
        The finished composite is cached by draw_live2d between frames.
        """
        d, m, n = self.dll, self._model, self.count
        tex = d.csmGetDrawableTextureIndices(m)
        orders = d.csmGetRenderOrders(m)
        opacities = d.csmGetDrawableOpacities(m)
        vertex_counts = d.csmGetDrawableVertexCounts(m)
        vertices = d.csmGetDrawableVertexPositions(m)
        uvs = d.csmGetDrawableVertexUvs(m)
        index_counts = d.csmGetDrawableIndexCounts(m)
        indices = d.csmGetDrawableIndices(m)
        mask_counts = d.csmGetDrawableMaskCounts(m)
        masks = d.csmGetDrawableMasks(m)
        flags = d.csmGetDrawableConstantFlags(m)
        factor = min(canvas_width / self.canvas.X, canvas_height / self.canvas.Y)

        def draw_mesh(target, i, alpha=1.0):
            texture_index = tex[i]
            if texture_index < 0 or texture_index >= len(self.textures):
                return
            if vertex_counts[i] <= 0 or opacities[i] <= 0:
                return
            source = self.textures[texture_index]
            sw, sh = source.width(), source.height()
            count, v, uv, ind = vertex_counts[i], vertices[i], uvs[i], indices[i]
            if not v or not uv or not ind or sw == 0 or sh == 0:
                return
            def world(k):
                return QPointF(canvas_width/2 + (v[k].X * self.ppu.value + self.origin.X - self.canvas.X/2) * factor,
                               canvas_height/2 - (v[k].Y * self.ppu.value + self.origin.Y - self.canvas.Y/2) * factor)
            def image_pt(k):
                return QPointF(uv[k].X * sw, (1 - uv[k].Y) * sh)
            for t in range(0, index_counts[i] - 2, 3):
                a, b, c = int(ind[t]), int(ind[t+1]), int(ind[t+2])
                if min(a,b,c) < 0 or max(a,b,c) >= count:
                    continue
                p1,p2,p3 = world(a),world(b),world(c)
                s1,s2,s3 = image_pt(a),image_pt(b),image_pt(c)
                ux,uy = s2.x()-s1.x(),s2.y()-s1.y()
                vx,vy = s3.x()-s1.x(),s3.y()-s1.y()
                det = ux*vy - uy*vx
                if abs(det) < 0.00001: continue
                ax,ay = p2.x()-p1.x(),p2.y()-p1.y()
                bx,by = p3.x()-p1.x(),p3.y()-p1.y()
                m11=(ax*vy-bx*uy)/det
                m21=(bx*ux-ax*vx)/det
                m12=(ay*vy-by*uy)/det
                m22=(by*ux-ay*vx)/det
                dx=p1.x()-m11*s1.x()-m21*s1.y()
                dy=p1.y()-m12*s1.x()-m22*s1.y()
                target.save()
                target.setOpacity(max(0, min(1, alpha)))
                path=QPainterPath(p1)
                path.lineTo(p2);path.lineTo(p3);path.closeSubpath()
                target.setClipPath(path, Qt.ClipOperation.IntersectClip)
                target.setWorldTransform(QTransform(m11,m12,m21,m22,dx,dy), True)
                target.drawImage(QPointF(0,0), source)
                target.restore()

        def blank():
            image = QImage(int(canvas_width), int(canvas_height), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            return image

        mask_cache = {}
        # Build one alpha mask for every unique mask set, even if its source
        # drawable is behind or hidden in the normal render order.
        def mask_image(i):
            ids = tuple(int(masks[i][j]) for j in range(mask_counts[i]))
            if ids not in mask_cache:
                image = blank()
                mp = QPainter(image)
                try:
                    for drawable_index in ids:
                        if 0 <= drawable_index < n:
                            draw_mesh(mp, drawable_index, float(opacities[drawable_index]))
                finally:
                    mp.end()
                mask_cache[ids] = image
            return mask_cache[ids]

        for i in sorted(range(n), key=lambda j: orders[j]):
            if opacities[i] <= 0:
                continue
            has_mask = mask_counts[i] > 0
            if has_mask:
                piece = blank()
                pp = QPainter(piece)
                try:
                    draw_mesh(pp, i, float(opacities[i]))
                    pp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn
                                          if not (flags[i] & 8) else
                                          QPainter.CompositionMode.CompositionMode_DestinationOut)
                    pp.drawImage(QPointF(0,0), mask_image(i))
                finally:
                    pp.end()
            painter.save()
            try:
                if flags[i] & 1:
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                elif flags[i] & 2:
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                if has_mask:
                    painter.drawImage(QPointF(0,0), piece)
                else:
                    draw_mesh(painter, i, float(opacities[i]))
            finally:
                painter.restore()


def cubism_core_paths():
    """Search user-selected, shared user runtime, then legacy per-release paths."""
    candidates = []
    custom = os.environ.get('LIVE2D_CORE_PATH', '').strip().strip('"')
    if custom:
        selected = Path(custom).expanduser()
        candidates.append(selected / 'Live2DCubismCore.dll' if selected.is_dir() else selected)
    localappdata = os.environ.get('LOCALAPPDATA')
    if localappdata:
        candidates.append(Path(localappdata) / 'DynamicTeleprompter' / 'Runtime' / 'Live2D' / 'Live2DCubismCore.dll')
    candidates.append(Path(__file__).resolve().parent / 'live2d_sdk' / 'Live2DCubismCore.dll')
    candidates.append(Path(__file__).resolve().parent / 'Live2DCubismCore.dll')
    return candidates


def find_cubism_core():
    return next((p for p in cubism_core_paths() if p.is_file()), None)


class CubismCore:
    def __init__(self):
        dll_path = find_cubism_core()
        if dll_path is None:
            raise FileNotFoundError(
                'Cubism Core not found. Run setup_windows.bat and select your official Cubism SDK ZIP. '
                'Checked LIVE2D_CORE_PATH, the permanent user runtime directory, and the local live2d_sdk directory.'
            )
        d = C.CDLL(str(dll_path))
        p = C.c_void_p
        _pointer(d,'csmReviveMocInPlace',p,p,C.c_uint)
        _pointer(d,'csmGetSizeofModel',C.c_uint,p)
        _pointer(d,'csmInitializeModelInPlace',p,p,p,C.c_uint)
        _pointer(d,'csmUpdateModel',None,p)
        _pointer(d,'csmReadCanvasInfo',None,p,C.POINTER(Vector2),C.POINTER(Vector2),C.POINTER(C.c_float))
        _pointer(d,'csmGetDrawableCount',C.c_int,p)
        _pointer(d,'csmGetParameterCount',C.c_int,p)
        _pointer(d,'csmGetParameterIds',C.POINTER(C.c_char_p),p)
        for pname in ('csmGetParameterMinimumValues', 'csmGetParameterMaximumValues',
                      'csmGetParameterDefaultValues', 'csmGetParameterValues'):
            _pointer(d,pname,C.POINTER(C.c_float),p)
        for name,ctype in [('csmGetDrawableTextureIndices',C.c_int),('csmGetRenderOrders',C.c_int),
                           ('csmGetDrawableVertexCounts',C.c_int),('csmGetDrawableIndexCounts',C.c_int)]:
            _pointer(d,name,C.POINTER(ctype),p)
        _pointer(d,'csmGetDrawableOpacities',C.POINTER(C.c_float),p)
        _pointer(d,'csmGetDrawableVertexPositions',C.POINTER(C.POINTER(Vector2)),p)
        _pointer(d,'csmGetDrawableVertexUvs',C.POINTER(C.POINTER(Vector2)),p)
        _pointer(d,'csmGetDrawableIndices',C.POINTER(C.POINTER(C.c_ushort)),p)
        _pointer(d,'csmGetDrawableMaskCounts',C.POINTER(C.c_int),p)
        _pointer(d,'csmGetDrawableMasks',C.POINTER(C.POINTER(C.c_int)),p)
        _pointer(d,'csmGetDrawableConstantFlags',C.POINTER(C.c_ubyte),p)
        self.dll=d

    def load(self, path):
        return CubismMeshModel(path,self.dll)


# Model loading, DLL access, pose updates and triangle drawing all belong to
# the single worker. Qt paint handlers only submit immutable control snapshots
# and composite the last completed QImage.
MAX_LIVE2D_RASTER_EDGE = 640
_worker = None
_last_error = {}
_errors = set()


class _PoseRenderer:
    def __init__(self):
        self.core = None
        self.models = OrderedDict()

    def __call__(self, key, payload):
        path, _ = key
        w, h, pose = payload
        if self.core is None:
            self.core = CubismCore()
        if key not in self.models:
            self.models[key] = (self.core.load(path), None, None)
        model, size, result = self.models[key]
        self.models.move_to_end(key)
        while len(self.models) > 5:
            self.models.popitem(last=False)
        changed = model.set_tracking(dict(pose))
        if changed or size != (w, h) or result is None:
            raster_scale = min(1., MAX_LIVE2D_RASTER_EDGE / max(w, h))
            rw, rh = max(1, round(w * raster_scale)), max(1, round(h * raster_scale))
            image = QImage(rw, rh, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                model.draw(painter, rw, rh)
            finally:
                painter.end()
            result = (image, model.movement_bounds(rw, rh))
            self.models[key] = (model, (w, h), result)
        return result


def shutdown_live2d():
    global _worker
    if _worker is not None:
        _worker.close()
        _worker = None


def _pose_worker():
    global _worker
    if _worker is None:
        _worker = LatestPoseWorker(_PoseRenderer())
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutdown_live2d)
    return _worker


def live2d_error(path):
    return _last_error.get(str(path), '')


def live2d_render_stats(layer):
    """Last background job duration/count, for performance diagnostics."""
    key = str(layer.get('path', '')), id(layer)
    return _worker.stats(key) if _worker is not None else {}


def draw_live2d(painter, layer, canvas_size):
    """Composite the latest worker image without waiting for model rendering."""
    path = str(layer.get('path', ''))
    try:
        w, h = canvas_size
        pose = {}
        if layer.get('tracking', False):
            from face_tracking import current_tracking
            pose.update(current_tracking())
        if layer.get('idle_enabled', False):
            pose.update(animation_values(layer, microphone.level()))
        movement = layer.get('movement', {})
        if movement.get('enabled', False):
            strength = float(movement.get('head_strength', 65)) / 100.
            pose['ParamAngleY'] = float(layer.get('_movement_turn', 0)) * 30. * strength
        # Snapshot mutable controls before crossing threads; workers never access
        # project dictionaries or QWidget/QPixmap objects.
        pose = tuple(sorted((str(k), round(float(v), 3)) for k, v in pose.items()))
        payload = (int(w), int(h), pose)
        # Separate instances of the same model can have independent head poses.
        key = path, id(layer)
        result, error = _pose_worker().request(key, payload, payload)
        if error:
            _last_error[path] = error
            if path not in _errors:
                print(f'Live2D layer unavailable ({path}): {error}', flush=True)
                _errors.add(path)
        else:
            _last_error.pop(path, None)
        if result is None:
            return  # Initial model loading is also asynchronous.
        image, bounds = result
        layer['_movement_bounds'] = bounds
        painter.save()
        try:
            painter.translate(w * float(layer.get('x', 0)) / 100,
                              h * float(layer.get('y', 0)) / 100)
            scale = (perspective_scale(layer, float(layer.get('y', 0)))
                     if movement.get('enabled', False) else max(.01, float(layer.get('scale', 1))))
            painter.translate(w / 2, h / 2)
            painter.scale(scale, scale)
            painter.translate(-w / 2, -h / 2)
            painter.setOpacity(max(0., min(1., float(layer.get('opacity', 100)) / 100)))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QRectF(0, 0, w, h), image)
        finally:
            painter.restore()
    except Exception as exc:
        _last_error[path] = str(exc)
        if path not in _errors:
            print(f'Live2D layer unavailable ({path}): {exc}', flush=True)
            _errors.add(path)
