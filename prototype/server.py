"""검증용 API 서버 (Python 기본 라이브러리만 사용, 설치 필요 없음).

실제 Dubeogi 데이터로 API 명세서 0.3 응답을 돌려준다. 프론트를 mock 없이 띄워 보는 용도다.
진짜 백엔드(FastAPI)가 준비되면 쓰지 않는다.

사용법 (backend 레포 폴더에서, Dubeogi·frontend는 backend 옆 폴더에 클론되어 있다고 가정):
    python -I prototype/convert.py ../Dubeogi prototype/data prototype/static   # 처음 한 번
    python -I prototype/server.py                                             # http://127.0.0.1:8000

프론트는 .env에 VITE_USE_MOCK=false 로 두고 npm run dev 하면 이 서버를 쓴다.
(vite.config.ts의 /api, /static 프록시가 8000번으로 넘겨 준다)
"""

from __future__ import annotations

import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import ApiError, Engine  # noqa: E402

ROOT = Path(__file__).resolve().parent
ENGINE = Engine(ROOT / "data")
STATIC = (ROOT / "static").resolve()
ALLOWED_ORIGIN = "http://localhost:5173"
PREFIX = "/api/v1"


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _api(self, fn) -> None:
        try:
            self._json(200, fn())
        except ApiError as e:
            self._json(e.status, e.body())
        except Exception as e:  # noqa: BLE001
            self.log_error("INTERNAL_ERROR: %r", e)
            self._json(500, {"error": {"code": "INTERNAL_ERROR", "message": "서버 오류"}})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}

        if path.startswith("/static/"):
            target = (STATIC / path[len("/static/"):]).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)

        def num(name, cast=int, default=None):
            if name not in q:
                if default is None:
                    raise ApiError(400, "VALIDATION_ERROR", f"{name}는 필수입니다.")
                return default
            try:
                return cast(q[name])
            except ValueError:
                raise ApiError(400, "VALIDATION_ERROR", f"{name} 형식이 잘못됐습니다.")

        routes = {
            f"{PREFIX}/map": lambda: ENGINE.get_map(),
            f"{PREFIX}/places": lambda: ENGINE.search_places(
                q.get("q", ""), q.get("category"),
                num("buildingId", int, -1) if "buildingId" in q else None, num("limit", int, 10)),
            f"{PREFIX}/places/nearest": lambda: ENGINE.nearest_place(
                num("x", float), num("y", float), num("floor", int, 0)),
            f"{PREFIX}/buildings": lambda: ENGINE.list_buildings(),
        }
        if path in routes:
            return self._api(routes[path])
        if path.startswith(f"{PREFIX}/buildings/"):
            raw = path.rsplit("/", 1)[-1]
            if not raw.isdigit():
                return self._json(400, {"error": {"code": "VALIDATION_ERROR", "message": "buildingId는 정수입니다."}})
            return self._api(lambda: ENGINE.building_detail(int(raw)))
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != f"{PREFIX}/routes":
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"code": "VALIDATION_ERROR", "message": "JSON 형식 오류"}})
        self._api(lambda: ENGINE.route(body))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("검증용 API 서버: http://127.0.0.1:8000/api/v1  (종료: Ctrl+C)")
    server.serve_forever()
