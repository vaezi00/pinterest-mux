import os
import time
import hmac
import hashlib
import subprocess
import threading
import json
import urllib.request
import urllib.error
import tempfile

from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ═══════════════════════════════════════════════════════════════════
# LOG
# ═══════════════════════════════════════════════════════════════════

def log(*args):
    print("[MUX]", *args, flush=True)


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

PORT = int(
    os.environ.get(
        "PORT",
        "10000",
    )
)

MUX_SECRET = os.environ.get(
    "MUX_SECRET",
    "",
).strip()

RENDER_API_KEY = os.environ.get(
    "RENDER_API_KEY",
    "",
).strip()

RENDER_SERVICE_ID = os.environ.get(
    "RENDER_SERVICE_ID",
    "",
).strip()

# چند ثانیه بعد از پایان آخرین MUX، سرویس Suspend شود
SUSPEND_DELAY_SECONDS = 10


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


# ═══════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════

_active_mux_requests = 0
_active_mux_lock = threading.Lock()

_suspend_timer = None


# ═══════════════════════════════════════════════════════════════════
# JSON RESPONSE
# ═══════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════
# URL VALIDATION
# ═══════════════════════════════════════════════════════════════════

def is_allowed_hls_url(value):
    if not value:
        return False

    if len(value) > MAX_URL_LENGTH:
        return False

    try:
        u = urlparse(value)

        if u.scheme != "https":
            return False

        if u.hostname not in ALLOWED_HOSTS:
            return False

        if not (u.path or "").lower().endswith(".m3u8"):
            return False

        return True

    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# HMAC
# ═══════════════════════════════════════════════════════════════════

def make_signature(source_url, exp):
    payload = (
        f"{source_url}\n"
        f"{exp}"
    ).encode("utf-8")

    return hmac.new(
        MUX_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(
    source_url,
    exp_text,
    sig,
):
    if not MUX_SECRET:
        return False

    try:
        exp = int(exp_text)

    except (
        TypeError,
        ValueError,
    ):
        return False

    now = int(time.time())

    if exp <= now:
        return False

    if exp > now + MAX_TTL:
        return False

    expected = make_signature(
        source_url,
        exp,
    )

    return hmac.compare_digest(
        expected,
        sig,
    )


# ═══════════════════════════════════════════════════════════════════
# RENDER SUSPEND CONTROL
# ═══════════════════════════════════════════════════════════════════

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
            "[SUSPEND] Scheduled in "
            f"{SUSPEND_DELAY_SECONDS}s",
            flush=True,
        )


def mux_request_started():
    global _active_mux_requests

    cancel_pending_suspend()

    with _active_mux_lock:

        _active_mux_requests += 1

        print(
            "[MUX] Active requests:",
            _active_mux_requests,
            flush=True,
        )


def mux_request_finished():
    global _active_mux_requests

    with _active_mux_lock:

        _active_mux_requests = max(
            0,
            _active_mux_requests - 1,
        )

        active = _active_mux_requests

        print(
            "[MUX] Active requests:",
            active,
            flush=True,
        )

    if active == 0:
        schedule_suspend()


def suspend_render_service():
    global _suspend_timer

    with _active_mux_lock:

        if _active_mux_requests != 0:

            print(
                "[SUSPEND] Cancelled: "
                "mux request still active",
                flush=True,
            )

            _suspend_timer = None
            return

        _suspend_timer = None

    if not RENDER_API_KEY:

        print(
            "[SUSPEND] RENDER_API_KEY missing",
            flush=True,
        )

        return

    if not RENDER_SERVICE_ID:

        print(
            "[SUSPEND] RENDER_SERVICE_ID missing",
            flush=True,
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

    print(
        "[SUSPEND] Requesting Render suspend...",
        flush=True,
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=15,
        ) as res:

            print(
                "[SUSPEND] Render response:",
                res.status,
                flush=True,
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
            flush=True,
        )

    except Exception as e:

        print(
            "[SUSPEND] Request failed:",
            repr(e),
            flush=True,
        )


# ═══════════════════════════════════════════════════════════════════
# HTTP HANDLER
# ═══════════════════════════════════════════════════════════════════

class MuxHandler(BaseHTTPRequestHandler):

    server_version = "PinterestMux/2.0"

    def log_message(self, fmt, *args):
        print(
            "[HTTP] "
            f"{self.address_string()} - "
            f"{fmt % args}",
            flush=True,
        )

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request(
            head_only=True,
        )

    # ═══════════════════════════════════════════════════════════════
    # ROUTER
    # ═══════════════════════════════════════════════════════════════

    def handle_request(
        self,
        head_only=False,
    ):
        parsed = urlparse(
            self.path
        )

        # فقط درخواست واقعی /mux
        # تایمر Suspend را لغو می‌کند.
        #
        # Health check نباید سرویس را بیدار/فعال نگه دارد.
        if parsed.path == "/mux":
            cancel_pending_suspend()

        # ───────────────────────────────────────────────────────────
        # HEALTH
        # ───────────────────────────────────────────────────────────

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

        # ───────────────────────────────────────────────────────────
        # ONLY /mux
        # ───────────────────────────────────────────────────────────

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

        query = parse_qs(
            parsed.query,
        )

        source_url = query.get(
            "source",
            [None],
        )[0]

        exp = query.get(
            "exp",
            [None],
        )[0]

        sig = query.get(
            "sig",
            [None],
        )[0]

        # ───────────────────────────────────────────────────────────
        # VALIDATE SOURCE
        # ───────────────────────────────────────────────────────────

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

        if not is_allowed_hls_url(
            source_url
        ):

            send_json(
                self,
                400,
                {
                    "ok": False,
                    "error": "invalid_hls_url",
                },
            )

            return

        # ───────────────────────────────────────────────────────────
        # VALIDATE SIGNATURE
        # ───────────────────────────────────────────────────────────

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

        # ───────────────────────────────────────────────────────────
        # MUX
        # ───────────────────────────────────────────────────────────

        mux_request_started()

        try:

            self.stream_mux(
                source_url,
                head_only=head_only,
            )

        finally:

            mux_request_finished()

    # ═══════════════════════════════════════════════════════════════
    # FFMPEG + COMPLETE MP4
    # ═══════════════════════════════════════════════════════════════

    def stream_mux(
        self,
        source_hls,
        head_only=False,
    ):
        temp_path = None
        response_started = False

        try:

            # ───────────────────────────────────────────────────────
            # فایل موقت روی خود Render
            #
            # روی PC یا Cloudflare ذخیره نمی‌شود.
            # ───────────────────────────────────────────────────────

            tmp = tempfile.NamedTemporaryFile(
                prefix="mux_",
                suffix=".mp4",
                dir="/tmp",
                delete=False,
            )

            temp_path = tmp.name

            tmp.close()

            log(
                "FFmpeg output file:",
                temp_path,
            )

            # ───────────────────────────────────────────────────────
            # FFMPEG
            #
            # خروجی کامل MP4 روی فایل ساخته می‌شود.
            #
            # +faststart باعث می‌شود moov در ابتدای فایل قرار گیرد.
            # ───────────────────────────────────────────────────────

            cmd = [
                "ffmpeg",

                "-hide_banner",

                "-loglevel",
                "error",

                "-rw_timeout",
                "30000000",

                "-headers",
                (
                    "User-Agent: "
                    f"{USER_AGENT}\r\n"
                    "Referer: "
                    "https://www.pinterest.com/\r\n"
                    "Origin: "
                    "https://www.pinterest.com\r\n"
                ),

                "-i",
                source_hls,

                "-map",
                "0:v:0",

                "-map",
                "0:a:0?",

                "-c:v",
                "copy",

                "-c:a",
                "copy",

                "-movflags",
                "+faststart",

                "-shortest",

                "-f",
                "mp4",

                temp_path,
            ]

            log(
                "Starting FFmpeg..."
            )

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            # ───────────────────────────────────────────────────────
            # FFMPEG ERROR
            # ───────────────────────────────────────────────────────

            if result.returncode != 0:

                error_text = (
                    result.stderr.strip()
                    if result.stderr
                    else "ffmpeg_failed"
                )

                log(
                    "FFmpeg failed:",
                    error_text[:2000],
                )

                send_json(
                    self,
                    502,
                    {
                        "ok": False,
                        "error": "ffmpeg_failed",
                        "details": error_text[:1000],
                    },
                )

                response_started = True

                return

            # ───────────────────────────────────────────────────────
            # FFPROBE
            #
            # برای فهمیدن duration واقعی فایل.
            # ───────────────────────────────────────────────────────

            probe = subprocess.run(
                [
                    "ffprobe",

                    "-v",
                    "error",

                    "-show_entries",
                    "format=duration,size",

                    "-show_entries",
                    (
                        "stream="
                        "index,"
                        "codec_type,"
                        "duration,"
                        "time_base,"
                        "start_time"
                    ),

                    "-of",
                    "json",

                    temp_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            log(
                "FFPROBE:",
                probe.stdout[:5000],
            )

            if probe.returncode != 0:

                log(
                    "FFPROBE ERROR:",
                    probe.stderr[:2000],
                )

            # ───────────────────────────────────────────────────────
            # FINAL FILE SIZE
            # ───────────────────────────────────────────────────────

            try:

                file_size = os.path.getsize(
                    temp_path
                )

            except Exception:

                file_size = 0

            if file_size <= 0:

                raise RuntimeError(
                    "mux_output_empty"
                )

            log(
                "FFmpeg completed:",
                file_size,
                "bytes",
            )

            # ───────────────────────────────────────────────────────
            # HTTP RESPONSE
            #
            # فایل کامل است و Content-Length واقعی دارد.
            # ───────────────────────────────────────────────────────

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "video/mp4",
            )

            self.send_header(
                "Content-Length",
                str(file_size),
            )

            self.send_header(
                "Content-Disposition",
                'inline; filename="video.mp4"',
            )

            self.send_header(
                "Cache-Control",
                "no-store",
            )

            self.end_headers()

            response_started = True

            # ───────────────────────────────────────────────────────
            # HEAD
            # ───────────────────────────────────────────────────────

            if head_only:

                log(
                    "HEAD request completed:",
                    file_size,
                    "bytes",
                )

                return

            # ───────────────────────────────────────────────────────
            # STREAM COMPLETE FILE
            # ───────────────────────────────────────────────────────

            total_sent = 0

            with open(
                temp_path,
                "rb",
            ) as f:

                while True:

                    chunk = f.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    try:

                        self.wfile.write(
                            chunk
                        )

                        self.wfile.flush()

                        total_sent += len(
                            chunk
                        )

                    except (
                        BrokenPipeError,
                        ConnectionResetError,
                    ):

                        log(
                            "Client disconnected "
                            "during mux download"
                        )

                        break

            log(
                "HTTP stream completed:",
                total_sent,
                "bytes",
            )

        # ───────────────────────────────────────────────────────────
        # CLIENT DISCONNECTED
        # ───────────────────────────────────────────────────────────

        except (
            BrokenPipeError,
            ConnectionResetError,
        ):

            log(
                "Client disconnected"
            )

        # ───────────────────────────────────────────────────────────
        # OTHER ERROR
        # ───────────────────────────────────────────────────────────

        except Exception as e:

            log(
                "stream_mux failed:",
                repr(e),
            )

            if not response_started:

                try:

                    send_json(
                        self,
                        500,
                        {
                            "ok": False,
                            "error": str(e),
                        },
                    )

                except Exception:
                    pass

        finally:

            # ───────────────────────────────────────────────────────
            # حذف فایل موقت
            # ───────────────────────────────────────────────────────

            if temp_path:

                try:

                    os.remove(
                        temp_path
                    )

                    log(
                        "Temp mux file removed:",
                        temp_path,
                    )

                except FileNotFoundError:
                    pass

                except Exception as e:

                    log(
                        "Temp file cleanup failed:",
                        repr(e),
                    )


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():

    if not MUX_SECRET:

        raise RuntimeError(
            "MUX_SECRET environment variable is required"
        )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        MuxHandler,
    )

    print(
        "[START] Listening on "
        f"0.0.0.0:{PORT}",
        flush=True,
    )

    server.serve_forever()


if __name__ == "__main__":
    main()
