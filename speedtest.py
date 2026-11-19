#!/usr/bin/env python3
"""Измерение скорости скачивания: N последовательных HTTP-запросов к одному URL.

Пример:
    python3 speedtest.py https://example.com/big-image.jpg
"""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0
DEFAULT_CHUNK = 64 * 1024
USER_AGENT = "speedtest.py/1.3"

KIB = 1024
MIB = 1024 * 1024


class DownloadError(Exception):
    """Запрос не дал пригодного для замера результата."""


class UrlError(ValueError):
    """Некорректный адрес."""


@dataclass
class Attempt:
    """Результат одного запроса."""

    index: int
    seconds: float
    nbytes: int
    error: Optional[str] = None
    content_encoding: Optional[str] = None
    # Удалось ли вообще проверить полноту ответа: сервер сообщил Content-Length
    # или прислал chunked. Если нет, усечение ответа незаметно.
    verifiable: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def bits_per_second(self) -> float:
        if not self.ok or self.seconds <= 0:
            return 0.0
        return self.nbytes * 8 / self.seconds


def human_bytes(nbytes: int) -> str:
    if nbytes >= MIB:
        return f"{nbytes / MIB:.2f} MiB"
    if nbytes >= KIB:
        return f"{nbytes / KIB:.2f} KiB"
    return f"{nbytes} B"


def normalize_url(url: str) -> str:
    """Приводит адрес к виду, который можно отправить по сети.

    * проверяет схему, хост и порт;
    * хост в Unicode (например, кириллический домен) переводит в punycode;
    * не-ASCII символы и пробелы в пути и query кодирует процентами
      (уже закодированные %XX не трогает);
    * отбрасывает #fragment — на сервер он не отправляется.
    """
    url = url.strip()
    parts = urllib.parse.urlsplit(url)

    if parts.scheme not in ("http", "https"):
        raise UrlError("поддерживаются только схемы http:// и https://")
    if "@" in parts.netloc:
        raise UrlError("логин и пароль в URL не поддерживаются")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise UrlError("некорректный порт в URL") from None
    if not host:
        raise UrlError("в URL не указан хост")

    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlError(f"некорректное имя хоста: {host!r}") from None
    if ":" in host_ascii:  # IPv6-литерал
        host_ascii = f"[{host_ascii}]"

    netloc = host_ascii if port is None else f"{host_ascii}:{port}"
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/?%:@!$&'()*+,;=~-._")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (socket.timeout, TimeoutError))


def one_line(text: str, limit: int = 160) -> str:
    """Схлопывает текст в одну строку: сообщения об ошибках бывают многострочными
    (например, у redirect loop), а это ломает построчный формат отчёта."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit - 1] + "…"


def describe_error(exc: BaseException, timeout: float, received: int) -> str:
    """Короткое человекочитаемое описание сбоя, всегда в одну строку."""
    return one_line(_describe_error(exc, timeout, received))


def _describe_error(exc: BaseException, timeout: float, received: int) -> str:
    if is_timeout(exc):
        return f"таймаут {timeout:g} с"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        if is_timeout(exc.reason):
            return f"таймаут {timeout:g} с"
        return f"сеть: {exc.reason}"
    if isinstance(exc, http.client.IncompleteRead):
        return f"соединение оборвано, получено {received} байт"
    if isinstance(exc, http.client.HTTPException):
        return f"протокол HTTP: {exc.__class__.__name__}: {exc}"
    return f"{exc.__class__.__name__}: {exc}"


def cache_busted(url: str, index: int) -> str:
    parts = urllib.parse.urlsplit(url)
    extra = urllib.parse.urlencode({"_cb": f"{int(time.time() * 1000)}-{index}"})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urllib.parse.urlunsplit(parts._replace(query=query))


def read_some(response, chunk_size: int) -> bytes:
    """Читает не больше одного блока, не дожидаясь, пока он заполнится целиком.

    Обычный read(n) внутри буфера повторяет recv, пока не наберёт n байт, поэтому
    при медленной отдаче один вызов может висеть очень долго и общий дедлайн
    между чтениями не проверялся бы. read1() делает не больше одного recv.
    """
    reader = getattr(response, "read1", None)
    return reader(chunk_size) if reader else response.read(chunk_size)


def download_once(url: str, index: int, timeout: float, chunk_size: int) -> Attempt:
    """Один запрос: читаем тело целиком, данные не сохраняем.

    Ответ засчитывается успешным, только если он дочитан до конца:
    размер совпал с Content-Length (когда тело не chunked) и тело непустое.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Просим не отдавать ответ из промежуточных кэшей и не сжимать,
            # чтобы мерить реальный объём на проводе.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept-Encoding": "identity",
        },
    )

    started = time.perf_counter()
    deadline = started + timeout
    total = 0
    encoding: Optional[str] = None
    verifiable = True

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            encoding = response.getheader("Content-Encoding")
            # HTTPResponse.length: ожидаемый размер тела по Content-Length,
            # либо None, если сервер отдаёт chunked / закрывает соединение
            # по концу данных (заголовок при chunked по RFC игнорируется).
            expected = getattr(response, "length", None)
            # У chunked полноту гарантирует завершающий чанк: обрыв даст
            # IncompleteRead. Если нет ни того, ни другого, проверить нечем.
            verifiable = expected is not None or bool(getattr(response, "chunked", False))

            while True:
                chunk = read_some(response, chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if time.perf_counter() > deadline:
                    raise TimeoutError

            if expected is not None and total != expected:
                raise DownloadError(
                    f"неполный ответ: получено {total} из {expected} байт"
                )
            if total == 0:
                raise DownloadError("сервер вернул пустое тело (0 байт)")

    except DownloadError as exc:
        return Attempt(index, time.perf_counter() - started, total,
                       one_line(str(exc)), encoding, verifiable)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        return Attempt(index, time.perf_counter() - started, total,
                       describe_error(exc, timeout, total), encoding, verifiable)

    return Attempt(index, time.perf_counter() - started, total, None, encoding, verifiable)


def run(url: str, count: int, timeout: float, chunk_size: int,
        bust_cache: bool) -> Tuple[List[Attempt], bool]:
    """Последовательно выполняет запросы. Возвращает (замеры, прервано_ли)."""
    attempts: List[Attempt] = []

    for i in range(1, count + 1):
        target = cache_busted(url, i) if bust_cache else url
        try:
            attempt = download_once(target, i, timeout, chunk_size)
        except KeyboardInterrupt:
            print("\nПрервано пользователем — показываю, что успели замерить.",
                  file=sys.stderr, flush=True)
            return attempts, True

        attempts.append(attempt)

        if attempt.ok:
            print(
                f"  запрос {i:>2}/{count}: {attempt.seconds:7.3f} с  "
                f"{human_bytes(attempt.nbytes):>12}  "
                f"{attempt.bits_per_second / 1e6:8.2f} Mbit/s",
                flush=True,
            )
        else:
            print(f"  запрос {i:>2}/{count}: ОШИБКА — {attempt.error}", flush=True)

    return attempts, False


def warnings_for(ok: List[Attempt]) -> List[str]:
    notes: List[str] = []

    compressed = {a.content_encoding for a in ok if a.content_encoding} - {"identity"}
    if compressed:
        notes.append(
            f"сервер сжал ответ ({', '.join(sorted(compressed))}) вопреки Accept-Encoding: "
            "identity — объём и скорость посчитаны по сжатым байтам на проводе, "
            "распакованный файл больше"
        )

    sizes = {a.nbytes for a in ok}
    if len(sizes) > 1:
        notes.append(
            f"размер ответа менялся между запросами: "
            f"{human_bytes(min(sizes))} … {human_bytes(max(sizes))}"
        )

    if any(not a.verifiable for a in ok):
        notes.append(
            "сервер не сообщил размер ответа (ни Content-Length, ни chunked) — "
            "полноту такого ответа проверить нечем, при обрыве связи скорость "
            "может оказаться завышенной"
        )

    if len(ok) == 1:
        notes.append("успешный замер всего один — результат нерепрезентативен")

    return notes


def report(attempts: List[Attempt], interrupted: bool) -> int:
    ok = [a for a in attempts if a.ok]
    failed = len(attempts) - len(ok)

    print()
    if not ok:
        # Это часть отчёта, а не сбой самой утилиты, поэтому идёт в stdout:
        # при `speedtest.py ... > report.txt` файл должен объяснять сам себя.
        print("Ни один запрос не выполнен успешно — замерить скорость не удалось.",
              flush=True)
        return 1

    total_bytes = sum(a.nbytes for a in ok)
    total_seconds = sum(a.seconds for a in ok)
    avg_seconds = total_seconds / len(ok)
    speeds = sorted(a.bits_per_second for a in ok)

    # Основная метрика: суммарный объём / суммарное время запросов.
    overall_bps = total_bytes * 8 / total_seconds if total_seconds > 0 else 0.0

    print("Итог")
    print(f"  успешных запросов:    {len(ok)} из {len(attempts)}"
          + (f" (ошибок: {failed})" if failed else ""))
    print(f"  среднее время ответа: {avg_seconds:.3f} с")
    print(f"  общее время загрузки: {total_seconds:.3f} с")
    print(f"  скачано всего:        {human_bytes(total_bytes)} ({total_bytes} байт)")
    print(f"  средний размер:       {human_bytes(round(total_bytes / len(ok)))}")
    print()
    print(f"  СКОРОСТЬ СКАЧИВАНИЯ:  {overall_bps / 1e6:.2f} Mbit/s "
          f"({overall_bps / 8 / MIB:.2f} MiB/s)")
    print(f"  медиана по запросам:  {statistics.median(speeds) / 1e6:.2f} Mbit/s")
    print(f"  мин / макс:           {speeds[0] / 1e6:.2f} / {speeds[-1] / 1e6:.2f} Mbit/s")

    notes = warnings_for(ok)
    if interrupted:
        notes.insert(0, "прогон прерван до конца — часть запросов не выполнена")
    if notes:
        print()
        print("Предупреждения")
        for note in notes:
            print(f"  ! {note}")

    sys.stdout.flush()
    return 0 if failed == 0 and not interrupted else 1


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидалось целое число, получено {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("значение должно быть >= 1")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидалось число, получено {value!r}") from None
    if not (0 < number < float("inf")):  # отсекаем 0, отрицательные, nan и inf
        raise argparse.ArgumentTypeError("значение должно быть > 0 и конечным")
    return number


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Последовательно скачивает один и тот же URL N раз "
                    "и печатает среднее время ответа, объём и скорость.",
    )
    parser.add_argument("url", help="адрес, к которому стучаться (например, тяжёлая картинка)")
    parser.add_argument("-n", "--requests", type=positive_int, default=DEFAULT_REQUESTS,
                        help=f"сколько запросов сделать (по умолчанию {DEFAULT_REQUESTS})")
    parser.add_argument("-t", "--timeout", type=positive_float, default=DEFAULT_TIMEOUT,
                        help=f"таймаут одного запроса, с (по умолчанию {DEFAULT_TIMEOUT:g})")
    parser.add_argument("-c", "--chunk-size", type=positive_int, default=DEFAULT_CHUNK,
                        help=f"размер читаемого блока, байт (по умолчанию {DEFAULT_CHUNK})")
    parser.add_argument("--cache-bust", action="store_true",
                        help="добавлять уникальный query-параметр, чтобы обойти кэш CDN")

    args = parser.parse_args(argv)

    try:
        args.target = normalize_url(args.url)
    except UrlError as exc:
        parser.error(str(exc))
    return args


def make_output_safe() -> None:
    """Не падать на консолях, которые не умеют кириллицу: подставлять '?'."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    make_output_safe()
    args = parse_args(argv)

    print(f"URL: {args.url}")
    if args.target != args.url.strip():
        # адрес пришлось привести к сетевому виду (punycode, %-кодирование, срез #fragment)
        print(f"Запрашиваю: {args.target}")
    print(f"Запросов: {args.requests} (последовательно, каждый ответ читается целиком)")
    print(flush=True)

    attempts, interrupted = run(
        args.target, args.requests, args.timeout, args.chunk_size, args.cache_bust
    )

    code = report(attempts, interrupted)
    return 130 if interrupted else code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Получатель закрыл поток (например, `speedtest.py ... | head`).
        # Подменяем stdout на /dev/null, иначе интерпретатор при выходе
        # попытается дослать буфер и напечатает «Exception ignored».
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(141)  # 128 + SIGPIPE
