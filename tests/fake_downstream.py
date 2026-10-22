"""测试用假下游：按脚本返回响应，并记录收到的每个请求。"""

import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"ok"
    delay_s: float = 0.0
    # 设置后，记录请求后阻塞直到事件被 set
    block: threading.Event | None = None


@dataclass
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    at: float


class FakeDownstream:
    def __init__(self, replies: list[Reply] | None = None):
        # 依次消费；用完后一直重复最后一个
        self.replies = list(replies or [Reply()])
        self.received: list[Received] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.received.append(
                        Received(self.command, self.path, {k: v for k, v in self.headers.items()}, body, time.monotonic())
                    )
                    reply = outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]
                if reply.block is not None:
                    reply.block.wait(30)
                if reply.delay_s:
                    time.sleep(reply.delay_s)
                try:
                    self.send_response(reply.status)
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(reply.body)))
                    self.end_headers()
                    self.wfile.write(reply.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def count(self) -> int:
        with self._lock:
            return len(self.received)
