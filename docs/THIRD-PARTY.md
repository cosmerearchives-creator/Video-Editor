# Third-party components

The portable package includes dependency versions and copied upstream license
notices in this folder. Components retain their own copyrights and licenses.
Cubism Core, proprietary Live2D models and user VST plugins are not distributed.

FFmpeg 7.1 essentials is the unmodified Gyan Windows build distributed in
imageio-ffmpeg 0.6.0. It runs as a separate command-line process. Its build
configuration and license notice are captured in ffmpeg-build-and-license.txt.

- FFmpeg source: https://github.com/FFmpeg/FFmpeg/tree/n7.1
- Source archive: https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz
- Binary provider, build information and library/source links: https://www.gyan.dev/ffmpeg/builds/
- Python binary distribution: https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0
- FFmpeg license information: https://ffmpeg.org/legal.html

Other primary upstream projects:

- PySide6 / Qt: https://www.qt.io/qt-for-python
- NumPy: https://github.com/numpy/numpy
- PyTorch / torchaudio: https://github.com/pytorch/pytorch and https://github.com/pytorch/audio
- Whisper: https://github.com/openai/whisper
- Stable-ts: https://github.com/jianfch/stable-ts
- Vosk: https://github.com/alphacep/vosk-api
- Pedalboard: https://github.com/spotify/pedalboard
- OpenCV: https://github.com/opencv/opencv
- Sounddevice: https://github.com/spatialaudio/python-sounddevice
- PyInstaller: https://github.com/pyinstaller/pyinstaller

The accompanying dependency-versions.txt identifies the exact build environment.
Speech model weights are downloaded from Whisper's upstream model distribution
on first use and retained in the user's shared runtime cache, not in the ZIP.

RNNoise v0.1 (Xiph.Org/Mozilla/Jean-Marc Valin) is bundled for local speech
suppression under its BSD-style license, included as RNNoise-LICENSE.txt.
The checksum-pinned upstream source is fetched during the native build.
MSVC builds replace C99 variable-length arrays with equivalent stack allocation.
Source: https://github.com/xiph/rnnoise/tree/v0.1
