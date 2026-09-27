import os
import time
import hmac
import hashlib
import subprocess
import threading
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "").strip()

SUSPEND_DELAY_SECONDS = 10

_active_mux_requests = 0
_active_mux_lock = threading.Lock()
_suspend_timer = None

ALLOWED_HOSTS = {
    "i.pinimg.com",
    "v1.pinimg.com",
    "v2.pinimg.com",
    "v3.pinimg.com",
}

MAX_TTL = 3600
MAX_URL_LENGTH = 5000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def send_json(handler, status, data):
    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )
    handler.send_header(
        "Content-Length",
        str(len(body)),
    )
    handler.send_header(
        "Cache-Control",
        "no-store",
    )
    handler.end_headers()

    handler.wfile.write(body)


def is_allowed_hls_url(value):
    if not value or len(value) > MAX_URL_LENGTH:
        return False

    try:
        u = urlparse(value)

        if u.scheme != "https":
            return False

        if u.hostname not in ALLOWED_HOSTS:
            return False

        path = (u.path or "").lower()

        if not path.endswith(".m3u8"):
            return False

        return True

    except Exception:
        return False


def make_signature(source_url, exp):
    payload = f"{source_url}\n{exp}".encode("utf-8")

    return hmac.new(
        MUX_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(source_url, exp_text, sig):
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

    expected = make_signature(source_url, exp)

    return hmac.compare_digest(expected, sig)

def cancel_pending_suspend():
    global _suspend_timer

    with _active_mux_lock:
        if _suspend_timer is not None:
            try:
                _suspend_timer.cancel()
            except Exception:
                pass

            _suspend_timer = None


def schedule_suspend():
    global _suspend_timer

    with _active_mux_lock:
        if _active_mux_requests != 0:
            return

        if _suspend_timer is not None:
            try:
                _suspend_timer.cancel()
            except Exception:
                pass

        _suspend_timer = threading.Timer(
            SUSPEND_DELAY_SECONDS,
            suspend_render_service,
        )

        _suspend_timer.daemon = True
        _suspend_timer.start()

        print(
            f"[SUSPEND] Scheduled in "
            f"{SUSPEND_DELAY_SECONDS}s"
        )


def mux_request_started():
    global _active_mux_requests

    cancel_pending_suspend()

    with _active_mux_lock:
        _active_mux_requests += 1

        print(
            "[MUX] Active requests:",
            _active_mux_requests,
        )


def mux_request_finished():
    global _active_mux_requests

    with _active_mux_lock:
        _active_mux_requests = max(
            0,
            _active_mux_requests - 1,
        )

        print(
            "[MUX] Active requests:",
            _active_mux_requests,
        )

        if _active_mux_requests == 0:
            schedule_suspend()


def suspend_render_service():
    global _suspend_timer

    with _active_mux_lock:
        if _active_mux_requests != 0:
            print(
                "[SUSPEND] Cancelled: "
                "mux request still active"
            )
            _suspend_timer = None
            return

        _suspend_timer = None

    if not RENDER_API_KEY:
        print(
            "[SUSPEND] RENDER_API_KEY missing"
        )
        return

    if not RENDER_SERVICE_ID:
        print(
            "[SUSPEND] RENDER_SERVICE_ID missing"
        )
        return

    api_url = (
        "https://api.render.com/v1/services/"
        f"{RENDER_SERVICE_ID}/suspend"
    )

    req = urllib.request.Request(
        api_url,
        method="POST",
        headers={
            "Authorization": (
                f"Bearer {RENDER_API_KEY}"
            ),
            "Accept": "application/json",
        },
    )

    print("[SUSPEND] Requesting Render suspend...")

    try:
        with urllib.request.urlopen(
            req,
            timeout=15,
        ) as res:

            status = res.status

            print(
                "[SUSPEND] Render response:",
                status,
            )

    except urllib.error.HTTPError as e:

        body = ""

        try:
            body = e.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            pass

        print(
            "[SUSPEND] HTTP error:",
            e.code,
            body[:500],
        )

    except Exception as e:

        print(
            "[SUSPEND] Request failed:",
            repr(e),
        )
class MuxHandler(BaseHTTPRequestHandler):

    server_version = "PinterestMux/2.0"

    def log_message(self, fmt, *args):
        print(
            f"[HTTP] {self.address_string()} - {fmt % args}"
        )

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request(head_only=True)

    def handle_request(self, head_only=False):
    
        # هر درخواست جدید، suspend در انتظار را لغو می‌کند.
        cancel_pending_suspend()
    
        parsed = urlparse(self.path)

        # ─────────────────────────────────────────────
        # HEALTH
        # ─────────────────────────────────────────────

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

        # ─────────────────────────────────────────────
        # MUX
        # ─────────────────────────────────────────────

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

        source_url = query.get("source", [None])[0]
        exp = query.get("exp", [None])[0]
        sig = query.get("sig", [None])[0]

        if not source_url:
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "missing_source",
                },
            )
            return

        if not is_allowed_hls_url(source_url):
            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "invalid_hls_url",
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
        
        if not verify_signature(
            source_url,
            exp,
            sig,
        ):
            send_json(
                self,
                403,
                {
                    "ok": False,
                    "error": "invalid_or_expired_signature",
                },
            )
            return
        
        mux_request_started()
        
        try:
            self.stream_mux(
                source_url,
                head_only=head_only,
            )
        finally:
            mux_request_finished()        
            def stream_mux(self, source_url, head_only=False):

        headers = (
            f"User-Agent: {USER_AGENT}\r\n"
            "Accept: */*\r\n"
            "Referer: https://www.pinterest.com/\r\n"
            "Origin: https://www.pinterest.com\r\n"
        )

        cmd = [
            "ffmpeg",

            "-hide_banner",
            "-loglevel", "error",

            # HTTP timeout
            "-rw_timeout", "30000000",

            # HTTP headers for Pinterest
            "-headers", headers,

            # HLS master
            "-i", source_url,

            # Explicit streams
            "-map", "0:v:0",
            "-map", "0:a:0",

            # NO re-encoding
            "-c:v", "copy",
            "-c:a", "copy",

            # Fragmented MP4 for HTTP streaming
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",

            "-shortest",

            # stdout
            "-f", "mp4",
            "pipe:1",
        ]

        print("[MUX] Starting FFmpeg")
        print("[MUX] Source:", source_url)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            print(
                "[MUX] FFmpeg start failed:",
                repr(exc),
            )

            send_json(
                self,
                500,
                {
                    "ok": False,
                    "error": "ffmpeg_start_failed",
                },
            )
            return

        # stderr را جداگانه بخوان
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

        threading.Thread(
            target=drain_stderr,
            daemon=True,
        ).start()

        try:

            # اولین chunk
            first_chunk = process.stdout.read(
                64 * 1024
            )

            if not first_chunk:

                return_code = process.wait(
                    timeout=20
                )

                print(
                    "[MUX] No output. FFmpeg exit:",
                    return_code,
                )

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

            # HEAD فقط برای تست
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
                'inline; filename="pinterest.mp4"',
            )

            # حجم نهایی از قبل معلوم نیست
            # بنابراین Content-Length نمی‌فرستیم.

            self.end_headers()

            if head_only:
                try:
                    process.kill()
                except Exception:
                    pass

                try:
                    process.wait(timeout=5)
                except Exception:
                    pass

                return

            # اولین chunk
            self.wfile.write(first_chunk)
            self.wfile.flush()

            total = len(first_chunk)

            # stream مستقیم
            while True:

                chunk = process.stdout.read(
                    64 * 1024
                )

                if not chunk:
                    break

                self.wfile.write(chunk)
                self.wfile.flush()

                total += len(chunk)

            return_code = process.wait()

            print(
                f"[MUX] Finished: "
                f"{total} bytes, "
                f"exit={return_code}"
            )

        except (
            BrokenPipeError,
            ConnectionResetError,
        ):

            print(
                "[MUX] Client disconnected"
            )

            try:
                process.kill()
            except Exception:
                pass

            try:
                process.wait(timeout=5)
            except Exception:
                pass

        except Exception as exc:

            print(
                "[MUX] Stream error:",
                repr(exc),
            )

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

    print(
        f"[START] Listening on "
        f"0.0.0.0:{PORT}"
    )

    server.serve_forever()


if __name__ == "__main__":
    main()
