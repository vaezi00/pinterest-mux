import os
import time
import hmac
import hashlib
import subprocess
import threading
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PORT = int(os.environ.get("PORT", "10000"))
MUX_SECRET = os.environ.get("MUX_SECRET", "").strip()

ALLOWED_HOSTS = {
    "i.pinimg.com",
    "v1.pinimg.com",
    "v2.pinimg.com",
    "v3.pinimg.com",
}

MAX_TTL = 3600
MAX_URL_LENGTH = 5000


def error_json(message):
    body = (
        '{"ok":false,"error":'
        + json_escape(message)
        + "}"
    ).encode("utf-8")

    return body


def json_escape(value: str) -> str:
    import json
    return json.dumps(str(value), ensure_ascii=False)


def valid_cmfv_url(value: str) -> bool:
    if not value or len(value) > MAX_URL_LENGTH:
        return False

    try:
        u = urlparse(value)

        if u.scheme != "https":
            return False

        if u.hostname not in ALLOWED_HOSTS:
            return False

        path = (u.path or "").lower()

        if not path.endswith(".cmfv"):
            return False

        return True

    except Exception:
        return False


def make_signature(video_url: str, audio_url: str, exp: int) -> str:
    payload = f"{video_url}\n{audio_url}\n{exp}".encode("utf-8")

    return hmac.new(
        MUX_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(video_url: str, audio_url: str, exp_text: str, sig: str) -> bool:
    if not MUX_SECRET:
        return False

    try:
        exp = int(exp_text)
    except (TypeError, ValueError):
        return False

    now = int(time.time())

    if exp <= now:
        return False

    if exp > now + MAX_TTL:
        return False

    expected = make_signature(video_url, audio_url, exp)

    return hmac.compare_digest(expected, sig)


def send_json(handler, status: int, data: dict):
    import json

    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class MuxHandler(BaseHTTPRequestHandler):

    server_version = "PinterestMux/1.0"

    def log_message(self, fmt, *args):
        print(f"[HTTP] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def handle_request(self, send_body: bool):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "pinterest-mux",
                },
            )
            return

        if parsed.path != "/mux":
            send_json(
                self,
                404,
                {
                    "ok": False,
                    "error": "not_found",
                },
            )
            return

        query = parse_qs(parsed.query)

        video_url = query.get("video", [None])[0]
        audio_url = query.get("audio", [None])[0]
        exp = query.get("exp", [None])[0]
        sig = query.get("sig", [None])[0]

        if not video_url or not audio_url:
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "missing_video_or_audio",
                },
            )
            return

        if not valid_cmfv_url(video_url):
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "invalid_video_url",
                },
            )
            return

        if not valid_cmfv_url(audio_url):
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "invalid_audio_url",
                },
            )
            return

        if not exp or not sig:
            send_json(
                self,
                401,
                {
                    "ok": False,
                    "error": "missing_signature",
                },
            )
            return

        if not verify_signature(video_url, audio_url, exp, sig):
            send_json(
                self,
                403,
                {
                    "ok": False,
                    "error": "invalid_or_expired_signature",
                },
            )
            return

        self.stream_mux(
            video_url,
            audio_url,
            send_body=send_body,
        )

    def stream_mux(self, video_url: str, audio_url: str, send_body: bool):
        cmd = [
            "ffmpeg",

            "-hide_banner",
            "-loglevel", "error",

            # Remote HTTP inputs
            "-rw_timeout", "30000000",

            # Input 1 = video
            "-i", video_url,

            # Input 2 = audio
            "-i", audio_url,

            # Explicit mapping
            "-map", "0:v:0",
            "-map", "1:a:0",

            # NO re-encoding
            "-c:v", "copy",
            "-c:a", "copy",

            # Needed for fragmented MP4 over HTTP pipe
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",

            # Stop when the shortest input ends
            "-shortest",

            # Output directly to stdout
            "-f", "mp4",
            "pipe:1",
        ]

        print("[MUX] Starting FFmpeg")
        print("[MUX] video:", video_url)
        print("[MUX] audio:", audio_url)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            print("[MUX] Failed to start FFmpeg:", repr(exc))

            if not self.wfile.closed:
                send_json(
                    self,
                    500,
                    {
                        "ok": False,
                        "error": "ffmpeg_start_failed",
                    },
                )
            return

        # Drain stderr in a separate thread so FFmpeg cannot block
        # because its stderr pipe becomes full.
        def drain_stderr():
            try:
                while True:
                    line = process.stderr.readline()

                    if not line:
                        break

                    text = line.decode(
                        "utf-8",
                        errors="replace",
                    ).strip()

                    if text:
                        print("[FFmpeg]", text)

            except Exception:
                pass

        stderr_thread = threading.Thread(
            target=drain_stderr,
            daemon=True,
        )
        stderr_thread.start()

        try:
            # Read only a small first chunk.
            # We never buffer the whole output.
            first_chunk = process.stdout.read(64 * 1024)

            if not first_chunk:
                return_code = process.poll()

                if return_code is None:
                    return_code = process.wait(timeout=10)

                print(
                    "[MUX] FFmpeg produced no output, exit:",
                    return_code,
                )

                if not self.wfile.closed:
                    send_json(
                        self,
                        502,
                        {
                            "ok": False,
                            "error": "ffmpeg_no_output",
                            "exit_code": return_code,
                        },
                    )
                return

            # We deliberately do NOT send Content-Length.
            # Output size is unknown and is streamed progressively.
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "video/mp4",
            )
            self.send_header(
                "Cache-Control",
                "no-store",
            )
            self.send_header(
                "Content-Disposition",
                'inline; filename="video.mp4"',
            )
            self.send_header(
                "Accept-Ranges",
                "none",
            )
            self.end_headers()

            if not send_body:
                process.kill()
                process.wait()
                return

            # Send first chunk.
            self.wfile.write(first_chunk)
            self.wfile.flush()

            total = len(first_chunk)

            # Then continuously pipe FFmpeg stdout -> HTTP response.
            while True:
                chunk = process.stdout.read(64 * 1024)

                if not chunk:
                    break

                self.wfile.write(chunk)
                self.wfile.flush()

                total += len(chunk)

            return_code = process.wait()

            print(
                f"[MUX] Finished: {total} bytes, exit={return_code}"
            )

        except (BrokenPipeError, ConnectionResetError):
            print("[MUX] Client disconnected")

            try:
                process.kill()
            except Exception:
                pass

            try:
                process.wait(timeout=5)
            except Exception:
                pass

        except Exception as exc:
            print("[MUX] Streaming error:", repr(exc))

            try:
                process.kill()
            except Exception:
                pass

            try:
                process.wait(timeout=5)
            except Exception:
                pass


def main():
    if not MUX_SECRET:
        raise RuntimeError(
            "MUX_SECRET environment variable is required"
        )

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        MuxHandler,
    )

    print(f"[START] Pinterest mux listening on 0.0.0.0:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
