"""API 명세서 0.3 응답을 만드는 검증용 엔진 (DB 없이 JSON을 메모리에 올린다).

백엔드_설계.md 6장 규칙을 그대로 따른다. 백엔드 팀이 FastAPI로 옮길 때 참고하는 용도다.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

ROAD_PENALTY = 3.0
STAIRS = {"STAIRS_UP", "STAIRS_DOWN"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class Engine:
    def __init__(self, data_dir: Path):
        load = lambda name: json.loads((data_dir / name).read_text(encoding="utf-8"))
        self.map_info = load("map.json")
        self.buildings = {b["id"]: b for b in load("buildings.json")}
        self.nodes = {n["id"]: n for n in load("nodes.json")}
        self.places = {p["id"]: p for p in load("places.json")}
        self.edges = [e for e in load("edges.json") if e["enabled"]]
        self.adj: dict[int, list[dict]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            self.adj[e["fromNodeId"]].append(e)

    # ---------- 공용 ----------
    def _place_summary(self, p: dict) -> dict:
        return {k: p[k] for k in ("id", "name", "category", "buildingId", "buildingName", "floor", "position")}

    def _floor_info(self, building_id: int | None, floor: int) -> tuple[str | None, dict | None]:
        """층 단면도 URL과 지도 위 위치·크기(floorImageBounds)."""
        if building_id is None or floor == 0:
            return None, None
        for f in self.buildings[building_id]["floors"]:
            if f["floor"] == floor:
                return f["floorImageUrl"], f["imageBounds"]
        return None, None

    # ---------- GET /map ----------
    def get_map(self) -> dict:
        return self.map_info

    # ---------- GET /places ----------
    def search_places(self, q: str, category: str | None = None, building_id: int | None = None, limit: int = 10) -> dict:
        q = (q or "").strip()
        if not q:
            raise ApiError(400, "VALIDATION_ERROR", "q는 1글자 이상이어야 합니다.")
        limit = max(1, min(30, limit))
        words = q.lower().split()
        scored = []
        for p in self.places.values():
            if category and p["category"] != category:
                continue
            if building_id is not None and p["buildingId"] != building_id:
                continue
            b = self.buildings.get(p["buildingId"]) if p["buildingId"] else None
            hay = " ".join([p["name"], *p["aliases"], *(b["aliases"] if b else []), b["name"] if b else ""]).lower()
            if not all(w in hay for w in words):
                continue
            name = p["name"].lower()
            rank = 0 if name == q.lower() else 1 if name.startswith(q.lower()) else 2
            scored.append((rank, p["name"], p))
        scored.sort(key=lambda t: (t[0], t[1]))
        return {"items": [self._place_summary(p) for _, _, p in scored[:limit]]}

    # ---------- GET /places/nearest ----------
    def nearest_place(self, x: float, y: float, floor: int = 0) -> dict:
        candidates = [p for p in self.places.values() if p["floor"] == floor]
        if not candidates:
            raise ApiError(404, "PLACE_NOT_FOUND", f"floor={floor}에 등록된 장소가 없습니다.")
        best = min(candidates, key=lambda p: math.dist((x, y), (p["position"]["x"], p["position"]["y"])))
        d = math.dist((x, y), (best["position"]["x"], best["position"]["y"]))
        return {"place": self._place_summary(best), "distancePx": round(d, 1)}

    # ---------- GET /buildings ----------
    def list_buildings(self) -> dict:
        return {"items": [
            {k: b[k] for k in ("id", "name", "aliases", "position", "thumbnailUrl")}
            for b in self.buildings.values()
        ]}

    def building_detail(self, building_id: int) -> dict:
        b = self.buildings.get(building_id)
        if not b:
            raise ApiError(404, "BUILDING_NOT_FOUND", f"존재하지 않는 건물입니다. (buildingId={building_id})")
        return {
            **{k: b[k] for k in ("id", "name", "aliases", "position", "photoUrl", "phone", "operatingHours")},
            "floors": [
                {"floor": f["floor"], "label": f["label"], "floorImageUrl": f["floorImageUrl"],
                 "floorImageBounds": f["imageBounds"]}
                for f in b["floors"]
            ],
            "facilities": b["facilities"],
        }

    # ---------- POST /routes ----------
    def _dijkstra(self, start: int, goal: int, mode: str, avoid_road: bool, avoid_stairs: bool, no_shortcut: bool):
        dist = {start: 0.0}
        prev: dict[int, dict] = {}
        pq = [(0.0, start)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == goal:
                break
            if d > dist.get(u, math.inf):
                continue
            for e in self.adj[u]:
                v = e["toNodeId"]
                if self.nodes[v]["isHub"] and v != goal:
                    continue  # 대표 노드는 출발·도착일 때만 지난다
                if avoid_stairs and e["type"] in STAIRS:
                    continue
                if no_shortcut and e["isShortcut"]:
                    continue
                w = e["seconds"] if mode == "FASTEST" else e["kcal"]
                if avoid_road and e["isRoad"]:
                    w *= ROAD_PENALTY
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v], prev[v] = nd, e
                    heapq.heappush(pq, (nd, v))
        if goal not in dist:
            return None
        path_edges = []
        cur = goal
        while cur != start:
            e = prev[cur]
            path_edges.append(e)
            cur = e["fromNodeId"]
        return list(reversed(path_edges))

    def route(self, body: dict) -> dict:
        try:
            from_id = int(body["from"]["placeId"])
            to_id = int(body["to"]["placeId"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "VALIDATION_ERROR", "from.placeId와 to.placeId는 필수 정수입니다.")
        opts = body.get("options") or {}
        mode = opts.get("mode", "FASTEST")
        if mode not in ("FASTEST", "EASIEST"):
            raise ApiError(400, "VALIDATION_ERROR", f"알 수 없는 mode: {mode}")
        avoid_road = bool(opts.get("avoidRoad", False))
        avoid_stairs = bool(opts.get("avoidStairs", False))
        compare = bool(opts.get("compare", False))
        if from_id == to_id:
            raise ApiError(400, "SAME_PLACE", "출발지와 도착지가 같습니다.")
        src, dst = self.places.get(from_id), self.places.get(to_id)
        if not src or not dst:
            raise ApiError(404, "PLACE_NOT_FOUND", f"존재하지 않는 장소입니다. (placeId={from_id if not src else to_id})")

        edges = self._dijkstra(src["nodeId"], dst["nodeId"], mode, avoid_road, avoid_stairs, False)
        if edges is None:
            raise ApiError(422, "NO_ROUTE", "현재 옵션으로는 경로가 없습니다.")
        total_seconds = round(sum(e["seconds"] for e in edges))

        saved = None
        if compare:
            base = self._dijkstra(src["nodeId"], dst["nodeId"], mode, avoid_road, avoid_stairs, True)
            if base is not None:
                saved = max(0, round(sum(e["seconds"] for e in base)) - total_seconds)

        # 대표 노드로 들고 나는 간선은 지도에 그리지 않는다 (건물 라벨 위치 → 입구 선이 생기므로)
        drawn = [e for e in edges if not (self.nodes[e["fromNodeId"]]["isHub"] or self.nodes[e["toNodeId"]]["isHub"])]
        segments = self._segments(drawn) if drawn else self._single_point_segment(dst)
        steps = self._steps(drawn, segments, src, dst)

        floors = [self.nodes[e["toNodeId"]]["floor"] for e in drawn]
        indoor_floors = [f for f in [self.nodes[drawn[0]["fromNodeId"]]["floor"], *floors] if f != 0] if drawn else []
        floor_changes = sum(1 for a, b in zip(indoor_floors, indoor_floors[1:]) if a != b)

        return {
            "summary": {
                "from": {"id": src["id"], "name": src["name"]},
                "to": {"id": dst["id"], "name": dst["name"]},
                "totalSeconds": total_seconds,
                "totalKcal": round(sum(e["kcal"] for e in edges), 1),
                "floorChanges": floor_changes,
                "usesRoad": any(e["isRoad"] for e in edges),
                "usesShortcut": any(e["isShortcut"] for e in edges),
                "savedSeconds": saved,
            },
            "segments": segments,
            "steps": steps,
        }

    def _point(self, nid: int) -> dict:
        n = self.nodes[nid]
        return {"nodeId": nid, "x": n["x"], "y": n["y"]}

    def _seg_key(self, e: dict):
        a, b = self.nodes[e["fromNodeId"]], self.nodes[e["toNodeId"]]
        # 간선의 도착 노드 기준으로 구간 성격을 정한다 (출입문 간선은 들어간 쪽에 붙는다)
        return (b["floor"] != 0, b["buildingId"] if b["floor"] != 0 else None, b["floor"], e["isShortcut"], e["name"])

    def _segments(self, edges: list[dict]) -> list[dict]:
        segments: list[dict] = []
        for e in edges:
            key = self._seg_key(e)
            if not segments or segments[-1]["_key"] != key:
                indoor, bid, floor, shortcut, name = key
                image_url, image_bounds = self._floor_info(bid, floor)
                segments.append({
                    "_key": key,
                    "index": len(segments),
                    "indoor": indoor,
                    "buildingId": bid,
                    "floor": floor,
                    "floorImageUrl": image_url,
                    "floorImageBounds": image_bounds,
                    "isShortcut": shortcut,
                    "name": name if shortcut else None,
                    "points": [self._point(e["fromNodeId"])],
                })
            segments[-1]["points"].append(self._point(e["toNodeId"]))
            e["_seg"] = segments[-1]["index"]
        for s in segments:
            del s["_key"]
        return segments

    def _single_point_segment(self, place: dict) -> list[dict]:
        n = self.nodes[place["nodeId"]]
        return [{
            "index": 0, "indoor": n["floor"] != 0, "buildingId": n["buildingId"], "floor": n["floor"],
            "floorImageUrl": None, "floorImageBounds": None, "isShortcut": False, "name": None,
            "points": [{"nodeId": n["id"], "x": n["x"], "y": n["y"]}],
        }]

    def _label(self, nid: int) -> str:
        n = self.nodes[nid]
        if n["buildingId"]:
            b = self.buildings[n["buildingId"]]["name"]
            return f"{b} {n['floor']}층" if n["floor"] > 0 else f"{b} B{-n['floor']}층" if n["floor"] < 0 else b
        return n["sourceName"]

    def _steps(self, edges: list[dict], segments: list[dict], src: dict, dst: dict) -> list[dict]:
        def step(action, text, nid, seg_index, seconds):
            n = self.nodes[nid]
            return {
                "index": 0, "action": action, "text": text,
                "position": {"x": n["x"], "y": n["y"]},
                "segmentIndex": seg_index, "floor": n["floor"], "seconds": seconds,
            }

        if not edges:
            nid = dst["nodeId"]
            out = [step("START", f"{src['name']}에서 출발합니다", nid, 0, None),
                   step("ARRIVE", dst["hint"] or f"도착: {dst['name']}", nid, 0, None)]
        else:
            out = [step("START", f"{src['name']}에서 출발합니다", edges[0]["fromNodeId"], 0, None)]
            groups: list[list[dict]] = []
            for e in edges:
                a, b = self.nodes[e["fromNodeId"]], self.nodes[e["toNodeId"]]
                if a["floor"] == 0 and b["floor"] != 0:
                    action = "ENTER_BUILDING"
                elif a["floor"] != 0 and b["floor"] == 0:
                    action = "EXIT_BUILDING"
                else:
                    action = {"FLAT": "WALK"}.get(e["type"], e["type"])
                if groups and groups[-1][0]["_action"] == action and action not in ("ENTER_BUILDING", "EXIT_BUILDING"):
                    groups[-1].append(e)
                else:
                    groups.append([e])
                e["_action"] = action
            for g in groups:
                action = g[0]["_action"]
                end = self.nodes[g[-1]["toNodeId"]]
                seconds = round(sum(e["seconds"] for e in g))
                target = next((self.nodes[e["toNodeId"]]["sourceName"] for e in reversed(g)
                               if self.nodes[e["toNodeId"]]["showDetail"]), self._label(end["id"]))
                if action == "ENTER_BUILDING":
                    text = f"{self._label(end['id'])}으로 들어가세요"
                elif action == "EXIT_BUILDING":
                    text = f"{self._label(g[0]['fromNodeId'])}에서 밖으로 나가세요"
                elif action == "ELEVATOR":
                    text = f"엘리베이터를 타고 {self._label(end['id'])}으로 이동"
                elif action in STAIRS:
                    verb = "올라가세요" if action == "STAIRS_UP" else "내려가세요"
                    text = f"계단으로 {self._label(end['id'])}까지 {verb}" if end["floor"] != 0 else f"계단을 {verb}"
                elif action == "UPHILL":
                    text = f"오르막길을 따라 {target}까지 이동"
                elif action == "DOWNHILL":
                    text = f"내리막길을 따라 {target}까지 이동"
                else:
                    text = f"{target}까지 이동 (약 {seconds}초)"
                out.append(step(action, text, g[0]["fromNodeId"], g[0]["_seg"], seconds))
            last = edges[-1]["toNodeId"]
            out.append(step("ARRIVE", dst["hint"] or f"도착: {dst['name']}", last, edges[-1]["_seg"], None))
            for e in edges:
                e.pop("_seg", None)
                e.pop("_action", None)
        for i, s in enumerate(out):
            s["index"] = i
        return out
