"""Native Horizon RGBA/PCM -> FFmpeg -> bounded fragmented-MP4 browser broadcast."""
from collections import deque
from functools import lru_cache
import os
from pathlib import Path
import shutil
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .horizon_bot_lab import control
else:
    from horizon_bot_lab import control


def read_exact(stream, count):
    result = bytearray(count)
    view = memoryview(result)
    while view:
        received = stream.readinto(view)
        if not received:
            raise EOFError("media input ended")
        view = view[received:]
    return result


def write_all(fd, data, timeout=0.75):
    view = memoryview(data)
    deadline = time.monotonic() + timeout
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise RuntimeError("encoder input stalled; restart at a lower FPS")
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        view = view[written:]


def select_frame(frames, timestamp):
    if not frames or timestamp < frames[0][0] - 1e-6:
        raise RuntimeError("encoder outgrew its live frame queue; restart at a lower FPS")
    return next(frame for frame in reversed(frames) if frame[0] <= timestamp + 1e-6)


@lru_cache(maxsize=4)
def preferred_encoder(ffmpeg):
    if sys.platform == "darwin":
        try:
            probe = subprocess.run([
                ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=s=64x64:r=60",
                "-frames:v", "1", "-c:v", "h264_videotoolbox", "-allow_sw", "0", "-realtime", "1",
                "-f", "null", "-",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if probe.returncode == 0:
                return "h264_videotoolbox"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "libx264"


def codec_mime(initialization, audio):
    # The avcC decoder configuration is emitted by our FFmpeg muxer, not a browser guess.
    offset = initialization.find(b"avcC")
    if offset < 0 or len(initialization) < offset + 8 or initialization[offset + 4] != 1:
        raise ValueError("encoder did not provide an AVC decoder configuration")
    codec = bytes(initialization[offset + 5:offset + 8]).hex().upper()
    return f'video/mp4; codecs="avc1.{codec}' + (',mp4a.40.2"' if audio else '"')


class BotStream:
    def __init__(self, control_socket, directory, size, fps=60, audio=False, encoder=None):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is not installed; snapshots are still available")
        self.socket = control_socket
        self.size, self.fps, self.audio = size, fps, audio
        self.encoder_name = encoder or preferred_encoder(ffmpeg)
        if self.encoder_name not in ("h264_videotoolbox", "libx264"):
            raise ValueError("unsupported stream encoder")
        self.mime = None
        self.id = uuid.uuid4().hex[:12]
        self.cv = threading.Condition()
        self.running = threading.Event()
        self.running.set()
        self.clock_ready = threading.Event()
        self.base = None
        self.source_start = None
        self.frames = deque(maxlen=max(15, fps // 2))
        self.audio_data = bytearray()
        self.audio_start = 0
        self.init = b""
        self.fragments = deque(maxlen=16)
        self.sequence = 0
        self.received_frames = 0
        self.frame_times = deque(maxlen=120)
        self.fragment_times = deque(maxlen=60)
        self.written_frames = 0
        self.duplicated_frames = 0
        self.pipe_max_ms = 0.0
        self.audio_peak = 0.0
        self.viewers = 0
        self.last_viewer = time.monotonic()
        self.started = time.monotonic()
        self.error = None
        self.connection = None
        self.native_started = False
        self.stop_lock = threading.Lock()
        self.stopped = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        video_read, video_write = os.pipe()
        audio_read, audio_write = os.pipe()
        self.video_fd, self.audio_fd = video_write, audio_write
        os.set_blocking(video_write, False)
        if audio_write is not None:
            os.set_blocking(audio_write, False)
        self.log_path = Path(directory) / f"ffmpeg-{self.id}.log"
        self.log = self.log_path.open("wb")
        width, height = size
        args = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-thread_queue_size", "4", "-probesize", "32", "-analyzeduration", "0",
                "-f", "rawvideo", "-pixel_format", "rgba", "-video_size", f"{width}x{height}",
                "-framerate", str(fps), "-i", f"pipe:{video_read}"]
        args += ["-thread_queue_size", "16", "-probesize", "32", "-analyzeduration", "0",
                 "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", f"pipe:{audio_read}"]
        args += ["-map", "0:v:0", "-c:v", self.encoder_name]
        if self.encoder_name == "h264_videotoolbox":
            args += ["-realtime", "1", "-allow_sw", "0", "-b:v", "8M", "-pix_fmt", "nv12"]
        else:
            args += ["-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23", "-pix_fmt", "yuv420p",
                     "-keyint_min", str(max(1, fps // 5)), "-sc_threshold", "0"]
        level = "4.2" if width > 1280 or height > 720 else "3.2" if fps > 30 else "3.1"
        args += ["-profile:v", "baseline", "-level:v", level, "-bf", "0", "-g", str(max(1, fps // 5))]
        # Keep the track layout stable when sound changes; capture stays off while muted.
        args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"]
        # Every published fragment starts at an IDR, so late subscribers remain decodable.
        args += ["-avoid_negative_ts", "make_zero", "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
                 "-flush_packets", "1", "-f", "mp4", "pipe:1"]
        inherited = (video_read, audio_read)
        try:
            self.encoder = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=self.log, pass_fds=inherited)
        except BaseException:
            for fd in (video_read, video_write, audio_read, audio_write):
                if fd is not None:
                    os.close(fd)
            self.listener.close()
            self.log.close()
            raise
        os.close(video_read)
        if audio_read is not None:
            os.close(audio_read)
        workers = [self.receive_native, self.write_video, self.write_audio, self.read_fragments, self.monitor]
        for function in workers:
            threading.Thread(target=self.guarded, args=(function,), daemon=True).start()
        try:
            flag = " audio" if audio else ""
            control(self.socket, f"broadcast start 127.0.0.1:{self.listener.getsockname()[1]} {fps}{flag}")
            self.native_started = True
        except BaseException:
            self.stop()
            raise

    def guarded(self, function):
        try:
            function()
        except (OSError, EOFError, RuntimeError, ValueError, struct.error) as error:
            if self.running.is_set():
                self.error = str(error)
                self.running.clear()
                self.clock_ready.set()
                with self.cv:
                    self.cv.notify_all()

    def set_audio(self, enabled):
        with self.stop_lock:
            if not self.running.is_set():
                raise RuntimeError("stream is not active")
            if self.audio == enabled:
                return
            previous = self.audio
            with self.cv:
                self.audio = enabled
                self.audio_data.clear()
                self.audio_peak = 0.0
                self.audio_start = 0
            try:
                control(self.socket, "broadcast audio " + ("on" if enabled else "off"))
            except (OSError, RuntimeError):
                self.audio = previous
                raise

    def receive_native(self):
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(5)
        with self.connection.makefile("rb") as source:
            while self.running.is_set():
                kind, length = struct.unpack("<BI", read_exact(source, 5))
                if not 0 < length <= 64 * 1024 * 1024:
                    raise ValueError("invalid native media packet size")
                payload = read_exact(source, length)
                with self.cv:
                    if kind == 1:
                        width, height, micros = struct.unpack("<IIQ", payload[:16])
                        if [width, height] != list(self.size) or length != 16 + width * height * 4:
                            raise ValueError("capture size changed; restart the stream")
                        timestamp = micros / 1e6
                        self.frames.append((timestamp, memoryview(payload)[16:]))
                        self.received_frames += 1
                        self.frame_times.append(timestamp)
                        if self.base is None:
                            self.source_start = timestamp
                            self.base = time.monotonic() + 0.08
                            self.clock_ready.set()
                    elif kind == 2 and self.audio:
                        start = struct.unpack("<Q", payload[:8])[0]
                        samples = payload[8:]
                        if len(samples) % 8:
                            raise ValueError("invalid stereo audio packet")
                        end = self.audio_start + len(self.audio_data) // 8
                        if start > end:
                            gap = min(start - end, 48000)
                            self.audio_data.extend(bytes(gap * 8))
                            if start - end > gap:
                                self.audio_data.clear()
                                self.audio_start = start
                        elif start < end:
                            samples = samples[min(len(samples), (end - start) * 8):]
                        self.audio_data.extend(samples)
                        self.audio_peak = max((abs(v[0]) for v in struct.iter_unpack("<f", samples)), default=0.0)
                        if len(self.audio_data) > 48000 * 8 * 2:
                            drop = len(self.audio_data) - 48000 * 8 * 2
                            self.audio_start += drop // 8
                            del self.audio_data[:drop]
                    elif kind != 2:
                        raise ValueError("unknown native media packet")
                    self.cv.notify_all()

    def pace(self, seconds):
        delay = self.base + seconds - time.monotonic()
        if delay < -0.75:
            raise RuntimeError("encoder fell behind the live clock; restart at a lower FPS")
        if delay > 0:
            time.sleep(delay)

    def write_video(self):
        try:
            self.clock_ready.wait(5)
            if self.base is None or not self.running.is_set():
                raise RuntimeError("no rendered frames received")
            index = 0
            previous = None
            while self.running.is_set():
                self.pace(index / self.fps)
                with self.cv:
                    timestamp, pixels = select_frame(self.frames, self.source_start + index / self.fps)
                started = time.monotonic()
                write_all(self.video_fd, pixels)
                self.pipe_max_ms = max(self.pipe_max_ms, (time.monotonic() - started) * 1000)
                self.written_frames += 1
                self.duplicated_frames += int(timestamp == previous)
                previous = timestamp
                index += 1
        finally:
            os.close(self.video_fd)

    def write_audio(self):
        try:
            self.clock_ready.wait(5)
            if self.base is None or not self.running.is_set():
                raise RuntimeError("no capture clock available")
            index = 0
            while self.running.is_set():
                self.pace(index / 48000)
                samples = bytearray(960 * 8)
                source_index = round(self.source_start * 48000) + index
                with self.cv:
                    first = max(source_index, self.audio_start)
                    end = min(source_index + 960, self.audio_start + len(self.audio_data) // 8)
                    if self.audio and end > first:
                        samples[(first - source_index) * 8:(end - source_index) * 8] = self.audio_data[(first - self.audio_start) * 8:(end - self.audio_start) * 8]
                write_all(self.audio_fd, samples)
                index += 960
        finally:
            os.close(self.audio_fd)

    def read_fragments(self):
        initialization = bytearray()
        fragment = bytearray()
        while self.running.is_set():
            header = read_exact(self.encoder.stdout, 8)
            size, kind = struct.unpack(">I4s", header)
            if size == 1:
                extra = read_exact(self.encoder.stdout, 8)
                size = struct.unpack(">Q", extra)[0]
                header += extra
            if not len(header) <= size <= 16 * 1024 * 1024:
                raise ValueError("invalid encoder MP4 box")
            box = header + read_exact(self.encoder.stdout, size - len(header))
            with self.cv:
                if not self.init:
                    initialization.extend(box)
                    if kind == b"moov":
                        self.init = bytes(initialization)
                        self.mime = codec_mime(self.init, True)
                else:
                    fragment.extend(box)
                    if kind == b"mdat":
                        self.sequence += 1
                        self.fragments.append((self.sequence, bytes(fragment)))
                        self.fragment_times.append(time.monotonic())
                        fragment.clear()
                self.cv.notify_all()

    def subscribe(self):
        with self.cv:
            if self.viewers >= 4:
                raise RuntimeError("four viewers are already connected")
            self.viewers += 1
            self.last_viewer = time.monotonic()
        try:
            with self.cv:
                if not self.cv.wait_for(lambda: self.init or not self.running.is_set(), timeout=8) or not self.init:
                    raise RuntimeError(self.error or "encoder did not produce a stream")
                initialization = self.init
                cursor = self.fragments[-1][0] - 1 if self.fragments else 0
            yield initialization
            while self.running.is_set():
                with self.cv:
                    self.cv.wait_for(lambda: (self.fragments and self.fragments[-1][0] > cursor) or not self.running.is_set(), timeout=5)
                    chunks = [(seq, data) for seq, data in self.fragments if seq > cursor]
                for sequence, data in chunks:
                    if sequence != cursor + 1:
                        raise RuntimeError("viewer fell behind; reconnect to the live edge")
                    yield data
                    cursor = sequence
        finally:
            with self.cv:
                self.viewers -= 1
                self.last_viewer = time.monotonic()

    def monitor(self):
        while self.running.is_set():
            if self.encoder.poll() is not None:
                self.error = "FFmpeg exited; inspect " + str(self.log_path)
                break
            if not self.viewers and time.monotonic() - self.last_viewer > 12:
                break
            time.sleep(0.5)
        self.stop()

    def status(self):
        with self.cv:
            times = list(self.frame_times)
            span = times[-1] - times[0] if len(times) > 1 else 0
            measured = (len(times) - 1) / span if span > 0 else 0
            intervals = sorted((b - a) * 1000 for a, b in zip(times, times[1:]))
            gaps = [(b - a) * 1000 for a, b in zip(self.fragment_times, list(self.fragment_times)[1:])]
            return {"active": self.running.is_set(), "id": self.id, "audio": self.audio,
                    "fps": self.fps, "capture_fps": round(measured, 1), "size": self.size,
                    "viewers": self.viewers, "frames": self.received_frames, "audio_peak": self.audio_peak,
                    "encoder": self.encoder_name, "written_frames": self.written_frames,
                    "duplicated_frames": self.duplicated_frames, "pipe_max_ms": round(self.pipe_max_ms, 1),
                    "capture_p95_ms": round(intervals[int((len(intervals) - 1) * .95)], 1) if intervals else None,
                    "fragment_max_ms": round(max(gaps), 1) if gaps else None,
                    "mime": self.mime, "error": self.error, "ready": bool(self.init)}

    def stop(self):
        with self.stop_lock:
            if self.stopped:
                return
            self.stopped = True
            self.running.clear()
            self.clock_ready.set()
            with self.cv:
                self.frames.clear()
                self.audio_data.clear()
                self.fragments.clear()
                self.init = b""
                self.cv.notify_all()
            if self.native_started:
                try:
                    control(self.socket, "broadcast stop", timeout=3)
                except (OSError, RuntimeError):
                    pass
            self.listener.close()
            if self.connection:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
            if self.encoder.poll() is None:
                self.encoder.terminate()
                try:
                    self.encoder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.encoder.kill()
                    self.encoder.wait()
            self.encoder.stdout.close()
            self.log.close()
