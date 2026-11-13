"""실제 데이터로 만든 API 응답이 프론트 타입(frontend/src/shared/types/api.ts)과 맞는지 검증한다.

사용법 (backend 레포 폴더에서, Dubeogi·frontend는 backend 옆 폴더에 클론되어 있다고 가정):
    python -I prototype/check_contract.py ../frontend/src/shared/types/api.ts prototype/data prototype/check_report.md

- 프론트 파일은 읽기만 한다.
- 응답에 없는 필드, 타입 오류, 허용되지 않은 enum 값, 프론트 타입에 없는 추가 필드를 모두 잡는다.
"""

from __future__ import annotations

import itertools
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import ApiError, Engine  # noqa: E402


# ---------------- api.ts 파서 ----------------
def strip_ts_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def split_top(s: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def parse_type(t: str):
    alts = split_top(t.strip(), "|")
    if len(alts) > 1:
        return ("union", [parse_type(a) for a in alts])
    t = alts[0]
    if t.endswith("[]"):
        return ("array", parse_type(t[:-2]))
    if t.startswith("{"):
        return ("object", parse_members(t[1:-1]))
    if t.startswith("'"):
        return ("literal", t.strip("'"))
    if t in ("number", "string", "boolean", "null"):
        return ("prim", t)
    return ("ref", t)


def parse_members(body: str) -> dict:
    members = {}
    for m in split_top(body.replace("\n", " "), ";"):
        mm = re.match(r"(\w+)(\?)?\s*:\s*(.+)$", m.strip().rstrip(","))
        if mm:
            members[mm.group(1)] = (parse_type(mm.group(3)), bool(mm.group(2)))
    return members


def parse_api_ts(src: str) -> dict:
    src = strip_ts_comments(src)
    types = {}
    for m in re.finditer(r"export type (\w+)\s*=\s*(.*?);", src, flags=re.S):
        types[m.group(1)] = parse_type(m.group(2).replace("\n", " "))
    for m in re.finditer(r"export interface (\w+)\s*\{", src):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        types[m.group(1)] = ("object", parse_members(src[m.end():i - 1]))
    return types


# ---------------- 검증 ----------------
def validate(value, t, types, path, errors):
    kind = t[0]
    if kind == "ref":
        return validate(value, types[t[1]], types, path, errors)
    if kind == "union":
        for alt in t[1]:
            sub: list[str] = []
            validate(value, alt, types, path, sub)
            if not sub:
                return
        errors.append(f"{path}: 허용되지 않은 값 {value!r}")
        return
    if kind == "literal":
        if value != t[1]:
            errors.append(f"{path}: {t[1]!r} 이어야 하는데 {value!r}")
        return
    if kind == "prim":
        ok = {
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }[t[1]]
        if not ok:
            errors.append(f"{path}: {t[1]} 이어야 하는데 {type(value).__name__} ({value!r})")
        return
    if kind == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: 배열이어야 함")
            return
        for i, v in enumerate(value):
            validate(v, t[1], types, f"{path}[{i}]", errors)
        return
    if kind == "object":
        if not isinstance(value, dict):
            errors.append(f"{path}: 객체여야 함")
            return
        for name, (mt, optional) in t[1].items():
            if name not in value:
                if not optional:
                    errors.append(f"{path}.{name}: 필수 필드 없음")
                continue
            validate(value[name], mt, types, f"{path}.{name}", errors)
        for extra in value.keys() - t[1].keys():
            errors.append(f"{path}.{extra}: 프론트 타입에 없는 필드")


def main() -> None:
    api_ts, data_dir, report_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    types = parse_api_ts(api_ts.read_text(encoding="utf-8"))
    eng = Engine(data_dir)
    errors: list[str] = []
    notes: list[str] = []
    counts = Counter()

    def check(value, type_name, label):
        before = len(errors)
        validate(value, ("ref", type_name), types, label, errors)
        counts["checked"] += 1
        if len(errors) > before:
            counts["failed"] += 1

    def check_error(err: ApiError, label):
        check(err.body(), "ApiErrorResponse", label)

    # 1. /map
    m = eng.get_map()
    check(m, "MapInfoResponse", "GET /map")

    # 2. /buildings
    check(eng.list_buildings(), "BuildingListResponse", "GET /buildings")
    for bid in eng.buildings:
        check(eng.building_detail(bid), "BuildingDetailResponse", f"GET /buildings/{bid}")
    try:
        eng.building_detail(9999)
    except ApiError as e:
        check_error(e, "GET /buildings/9999")

    # 3. /places
    for q in ["신공", "신공학관 4", "중도", "정문", "학식", "도서관", "없는장소xyz", "경영관 3"]:
        res = eng.search_places(q)
        check(res, "PlaceListResponse", f"GET /places?q={q}")
        counts[f"search:{q}"] = len(res["items"])
    for cat in ["BUILDING", "ENTRANCE", "LIBRARY"]:
        check(eng.search_places("관" if cat == "BUILDING" else "문" if cat == "ENTRANCE" else "도서관", category=cat), "PlaceListResponse", f"GET /places?category={cat}")

    # 4. /places/nearest
    for x, y, fl in [(1000, 2500, 0), (500, 2600, 4), (2400, 2700, 0)]:
        try:
            check(eng.nearest_place(x, y, fl), "NearestPlaceResponse", f"GET /places/nearest?x={x}&y={y}&floor={fl}")
        except ApiError as e:
            check_error(e, f"GET /places/nearest floor={fl}")
            notes.append(f"/places/nearest floor={fl}: {e.code} (그 층에 장소 없음)")

    # 5. /routes 전체 조합
    place_ids = sorted(eng.places)
    option_sets = {
        "기본(FASTEST, compare)": {"mode": "FASTEST", "compare": True},
        "EASIEST": {"mode": "EASIEST"},
        "avoidRoad": {"avoidRoad": True},
        "avoidStairs": {"avoidStairs": True},
    }
    no_route = Counter()
    geometry_issues = Counter()
    floor_img_segments = 0
    shown_examples = 0
    road_changed = 0
    for label, opts in option_sets.items():
        for a, b in itertools.permutations(place_ids, 2):
            body = {"from": {"placeId": a}, "to": {"placeId": b}, "options": opts}
            try:
                res = eng.route(body)
            except ApiError as e:
                check_error(e, f"POST /routes [{label}]")
                no_route[(label, e.code)] += 1
                continue
            check(res, "RouteResponse", f"POST /routes {a}->{b} [{label}]")
            segs, steps = res["segments"], res["steps"]
            for s1, s2 in zip(segs, segs[1:]):
                if s1["points"][-1] != s2["points"][0]:
                    geometry_issues["segment 경계 점 불일치"] += 1
            for s in segs:
                for p in s["points"]:
                    if not (0 <= p["x"] <= m["width"] and 0 <= p["y"] <= m["height"]):
                        geometry_issues["지도 밖 좌표"] += 1
                if s["floorImageUrl"]:
                    floor_img_segments += 1
            for st in steps:
                if not 0 <= st["segmentIndex"] < len(segs):
                    geometry_issues["step.segmentIndex 범위 밖"] += 1
            if steps[0]["action"] != "START" or steps[-1]["action"] != "ARRIVE":
                geometry_issues["START/ARRIVE 순서 오류"] += 1
            if label == "avoidRoad":
                base = eng.route({"from": {"placeId": a}, "to": {"placeId": b}})
                if base["summary"]["usesRoad"] and not res["summary"]["usesRoad"]:
                    road_changed += 1
            if label.startswith("기본") and shown_examples < 1 and len(segs) >= 3:
                shown_examples += 1
                example = res
    # 오류 응답 형식
    for body, lab in [({"from": {"placeId": 1}, "to": {"placeId": 1}}, "SAME_PLACE"),
                      ({"from": {"placeId": 1}, "to": {"placeId": 9999}}, "PLACE_NOT_FOUND"),
                      ({"from": {}}, "VALIDATION_ERROR")]:
        try:
            eng.route(body)
        except ApiError as e:
            check_error(e, f"POST /routes [{lab}]")
            if e.code != lab:
                errors.append(f"POST /routes [{lab}]: 기대한 코드와 다름 → {e.code}")

    # ---------------- 보고서 ----------------
    pairs = len(place_ids) * (len(place_ids) - 1)
    lines = [
        "# 프론트 타입 대조 결과 (자동 생성)",
        "",
        f"- 기준 타입: `{api_ts.as_posix()}` (읽기만 함)",
        f"- 검사한 응답: {counts['checked']}개, 타입 불일치 응답: {counts['failed']}개, 불일치 항목: {len(errors)}건",
        f"- 경로 조합: 장소 {len(place_ids)}곳 × {len(place_ids) - 1} = {pairs}쌍 × 옵션 {len(option_sets)}종",
        "",
        "## 경로 계산 결과",
        "",
        "| 옵션 | 경로 없음(NO_ROUTE) | 비율 |",
        "|---|---|---|",
    ]
    for label in option_sets:
        n = no_route[(label, "NO_ROUTE")]
        lines.append(f"| {label} | {n} | {n / pairs:.1%} |")
    lines += [
        "",
        f"- avoidRoad로 차도를 완전히 피하게 된 경로: {road_changed}쌍",
        f"- 층 단면도가 붙은 실내 구간 수(기본 옵션 포함 전체): {floor_img_segments}",
        f"- 도형 검사 문제: {dict(geometry_issues) or '없음'}",
        "",
        "## 장소 검색 결과 수",
        "",
    ]
    lines += [f"- `{k[7:]}` → {v}건" for k, v in counts.items() if k.startswith("search:")]
    if notes:
        lines += ["", "## 참고", ""] + [f"- {n}" for n in notes]
    lines += ["", "## 타입 불일치 상세", ""]
    lines += [f"- {e}" for e in errors[:200]] or ["- 없음"]
    if len(errors) > 200:
        lines.append(f"- ... 외 {len(errors) - 200}건")
    if shown_examples:
        import json
        lines += ["", "## 실제 응답 예시 (POST /routes)", "", "```json",
                  json.dumps(example, ensure_ascii=False, indent=1)[:6000], "```"]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"검사 {counts['checked']}개, 불일치 {len(errors)}건 → {report_path}")


if __name__ == "__main__":
    main()
