#!/usr/bin/env python3
"""Локальный сервер для проверки краевых случаев speedtest.py.

Нужен только для проверки скрипта, в замерах не участвует.

    python3 testserver.py [порт]      # по умолчанию 127.0.0.1:8765

Эндпоинты:
    /ok                 нормальный ответ, 256 KiB
    /delay              то же, что /ok, но ответ приходит через 0.2 с
    /truncated          обещает 1 MiB в Content-Length, отдаёт 1 KiB и рвёт соединение
    /chunked            корректный chunked-ответ, 256 KiB
    /chunked-with-cl    chunked + заведомо неверный Content-Length (его надо игнорировать)
    /chunked-truncated  chunked, обрывается без завершающего чанка
    /empty              200 OK с пустым телом
    /slow               headers сразу, тело через 5 с (проверка таймаута)
    /trickle            тело по одному байту каждые 0.3 с (проверка общего дедлайна)
    /gzip               отдаёт gzip, игнорируя Accept-Encoding: identity
    /no-length          HTTP/1.0 без Content-Length: конец тела = закрытие соединения
    /redirect           302 на /ok
    /redirect-loop      302 сам на себя (бесконечная петля)
    /файл               кириллический путь, отдаёт то же, что /ok
    /error500           HTTP 500
    любой другой путь   404
"""

import gzip
import http.server
import sys
import threading
import time
import urllib.parse

PAYLOAD = b"x" * (256 * 1024)
DEFAULT_PORT = 8765
DELAY = 0.2  # задержка ответа на /delay, с

# Статистика для автотестов: какие пути запрашивали.
STATS_LOCK = threading.Lock()
STATS = {"paths": []}


def reset_stats():
    with STATS_LOCK:
        STATS["paths"] = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # тихо
        pass

    def _empty(self, code, **headers):
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_GET(self):
        with STATS_LOCK:
            STATS["paths"].append(self.path)
        path = urllib.parse.unquote(self.path.split("?")[0])

        try:
            if path in ("/ok", "/файл"):
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

            elif path == "/delay":
                time.sleep(DELAY)
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

            elif path == "/truncated":
                self.send_response(200)
                self.send_header("Content-Length", str(1024 * 1024))
                self.end_headers()
                self.wfile.write(b"y" * 1024)
                self.wfile.flush()
                self.close_connection = True

            elif path in ("/chunked", "/chunked-with-cl"):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                if path == "/chunked-with-cl":
                    self.send_header("Content-Length", "5")
                self.end_headers()
                for i in range(0, len(PAYLOAD), 32 * 1024):
                    self._send_chunk(PAYLOAD[i:i + 32 * 1024])
                self.wfile.write(b"0\r\n\r\n")

            elif path == "/chunked-truncated":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._send_chunk(b"z" * 1024)
                self.close_connection = True

            elif path == "/empty":
                self._empty(200)

            elif path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.flush()
                time.sleep(5)
                self.wfile.write(PAYLOAD)

            elif path == "/trickle":
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                for _ in range(200):
                    self.wfile.write(b"t")
                    self.wfile.flush()
                    time.sleep(0.3)

            elif path == "/gzip":
                body = gzip.compress(PAYLOAD)
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path == "/no-length":
                # Пишем ответ вручную: нужен HTTP/1.0 без Content-Length,
                # где длину тела задаёт только закрытие соединения.
                self.wfile.write(b"HTTP/1.0 200 OK\r\n"
                                 b"Content-Type: application/octet-stream\r\n\r\n")
                self.wfile.write(PAYLOAD)
                self.close_connection = True

            elif path == "/redirect":
                self._empty(302, Location="/ok")

            elif path == "/redirect-loop":
                self._empty(302, Location="/redirect-loop")

            elif path == "/error500":
                self._empty(500)

            else:
                self._empty(404)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def start(port=DEFAULT_PORT):
    """Запускает сервер в фоновом потоке. port=0 — свободный порт."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = start(port)
    print(f"слушаю http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C — выход)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
