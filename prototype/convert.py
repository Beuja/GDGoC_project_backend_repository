"""Dubeogi 원본 → API 명세서 0.3 형식의 시드 JSON 변환 스크립트.

사용법 (backend 레포 폴더에서, Dubeogi·frontend는 backend 옆 폴더에 클론되어 있다고 가정):
    python -I prototype/convert.py ../Dubeogi prototype/data prototype/static

- 원본 코드는 읽기만 한다.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

HUB_WEIGHT = 100000

TYPE_MAP = {
    "평지": "FLAT",
    "계단위": "STAIRS_UP",
    "계단아래": "STAIRS_DOWN",
    "오르막": "UPHILL",
    "내리막": "DOWNHILL",
    "엘베": "ELEVATOR",
}

# 건물 이름 → 영문 이름 (이미지 파일 이름과 URL에 쓴다)
SLUGS = {
    "과학관": "science",
    "다향관": "dahyang",
    "대운동장": "stadium",
    "법학관_만해관": "law_manhae",
    "만해광장": "manhae_plaza",
    "명진관": "myungjin",
    "문화관": "culture",
    "본관": "main_hall",
    "사회과학관_경영관": "social_business",
    "상록원": "sangnok",
    "신공학관": "shingong",
    "원흥관": "wonheung",
    "정각원": "jeonggak",
    "정보문화관p": "jeongbo_p",
    "정보문화관q": "jeongbo_q",
    "중앙도서관": "library",
    "체육관": "gym",
    "학림관": "hakrim",
    "학생회관": "student_union",
    "학술관": "haksul",
    "혜화관": "hyehwa",
}

# 원본 내부 이름 → 화면에 보여 줄 이름
DISPLAY_NAMES = {
    "법학관_만해관": "법학관·만해관",
    "사회과학관_경영관": "사회과학관·경영관",
    "정보문화관p": "정보문화관 P",
    "정보문화관q": "정보문화관 Q",
}


def display(name: str) -> str:
    for src, shown in DISPLAY_NAMES.items():
        if name.startswith(src):
            return shown + name[len(src):]
    return name


# 학생들이 쓰는 줄임말 (팀이 보강할 데이터의 예시)
ALIASES = {
    "신공학관": ["신공"],
    "중앙도서관": ["중도", "도서관"],
    "정보문화관p": ["정보문화관", "정문관", "P동"],
    "정보문화관q": ["정보문화관", "Q동"],
    "사회과학관_경영관": ["사회과학관", "경영관", "사과관"],
    "법학관_만해관": ["법학관", "만해관"],
    "상록원": ["학식", "식당"],
    "학생회관": ["학관"],
}

PLACE_CATEGORY_OVERRIDES = {
    "중앙도서관": "LIBRARY",
    "상록원": "CAFETERIA",
    "대운동장": "LANDMARK",
    "만해광장": "LANDMARK",
}

FACILITY_ICON_MAP = {
    "local_drink": "VENDING",
    "print": "PRINTER",
    "monetization_on": "ATM",
    "store": "STORE",
    "coffee": "CAFE",
    "local_cafe": "CAFE",
    "table_bar": "LOUNGE",
    "chair": "LOUNGE",
    "weekend": "LOUNGE",
    "shower": "SHOWER",
}


def strip_comments(src: str) -> str:
    """Dart 주석을 앞에서부터 차례로 지운다.

    정규식으로 /* */를 먼저 지우면 `//*****` 같은 줄 주석을 블록 주석 시작으로 잘못 읽어
    수백 줄이 사라진다. 그래서 문자열·줄 주석·블록 주석을 순서대로 판단한다.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j == -1 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"PNG 아님: {path}")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        return raw.strip('"')
    if raw in ("true", "false"):
        return raw == "true"
    return float(raw) if "." in raw else int(raw)


def parse_graph(save_src: str):
    src = strip_comments(save_src)
    calls = re.findall(r"graph\.addEdge\((.*?)\);", src, flags=re.S)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    warnings: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    for call in calls:
        m = re.match(
            r'\s*"([^"]+)",\s*"([^"]+)",\s*([\d.]+),\s*([\d.]+),\s*"([^"]+)",\s*(true|false)',
            call,
        )
        if not m:
            warnings.append(f"파싱 실패: {call[:60]!r}")
            continue
        n1, n2, sec, kcal, typ, is_road = m.groups()
        named = {k: parse_value(v) for k, v in re.findall(r"(\w+)\s*:\s*(\"[^\"]*\"|[-\w.]+)", call[m.end():])}
        for idx, name in (("1", n1), ("2", n2)):
            if name in nodes:
                continue
            if f"node{idx}X" in named:
                nodes[name] = {
                    "name": name,
                    "x": float(named[f"node{idx}X"]),
                    "y": float(named[f"node{idx}Y"]),
                    "floor": int(named[f"isInside{idx}"]),
                    "building": None if named[f"building{idx}"] == "실외" else named[f"building{idx}"],
                    "showDetail": bool(named[f"showRoute{idx}"]),
                }
        if (n1, n2) in seen_pairs:
            warnings.append(f"중복 간선(처음 것만 사용): {n1} -> {n2}")
            continue
        seen_pairs.add((n1, n2))
        if typ not in TYPE_MAP:
            warnings.append(f"알 수 없는 간선 종류 {typ!r}: {n1} -> {n2}")
            continue
        edges.append({
            "from": n1,
            "to": n2,
            "seconds": float(sec),
            "kcal": float(kcal),
            "type": TYPE_MAP[typ],
            "isRoad": is_road == "true",
        })

    missing = sorted({e[k] for e in edges for k in ("from", "to")} - nodes.keys())
    for name in missing:
        warnings.append(f"좌표가 없는 노드: {name}")

    pair_set = {(e["from"], e["to"]) for e in edges}
    for a, b in sorted(pair_set):
        if (b, a) not in pair_set:
            warnings.append(f"반대 방향 간선 없음: {a} -> {b}")

    # 대표 노드(hub): 연결된 간선이 모두 100000인 노드
    incident: dict[str, list[float]] = defaultdict(list)
    for e in edges:
        incident[e["from"]].append(e["seconds"])
        incident[e["to"]].append(e["seconds"])
    hubs = {n for n, ws in incident.items() if all(w == HUB_WEIGHT for w in ws)}
    stray = [e for e in edges if e["seconds"] == HUB_WEIGHT and e["from"] not in hubs and e["to"] not in hubs]
    if stray:
        warnings.append(f"대표 노드가 아닌데 100000인 간선 {len(stray)}개 (확인 필요)")
    for e in edges:
        if e["from"] in hubs or e["to"] in hubs:
            e["seconds"] = 0.0
            e["kcal"] = 0.0
    for name, node in nodes.items():
        node["isHub"] = name in hubs

    return nodes, edges, warnings


def apply_fixes(nodes: dict, edges: list[dict], fixes: dict, warnings: list[str]) -> list[str]:
    """manual/edge_fixes.json의 보완 내용을 적용하고, 적용 기록을 돌려준다."""
    applied = []
    for f in fixes.get("nodeFloor", []):
        n = nodes.get(f["node"])
        if not n:
            raise SystemExit(f"[edge_fixes] 없는 노드: {f['node']}")
        applied.append(f"층 수정: {f['node']} {n['floor']} → {f['floor']}")
        n["floor"] = f["floor"]
    for f in fixes.get("removeEdges", []):
        before = len(edges)
        edges[:] = [e for e in edges if not (e["from"] == f["from"] and e["to"] == f["to"])]
        if len(edges) == before:
            raise SystemExit(f"[edge_fixes] 지울 간선이 없음: {f['from']} -> {f['to']}")
        applied.append(f"간선 삭제: {f['from']} -> {f['to']}")
    for f in fixes.get("addEdges", []):
        src_from, src_to = f["copyFrom"]
        template = next((e for e in edges if e["from"] == src_from and e["to"] == src_to), None)
        if not template:
            raise SystemExit(f"[edge_fixes] 복사할 간선이 없음: {src_from} -> {src_to}")
        if any(e["from"] == f["from"] and e["to"] == f["to"] for e in edges):
            raise SystemExit(f"[edge_fixes] 이미 있는 간선: {f['from']} -> {f['to']}")
        edges.append({**template, "from": f["from"], "to": f["to"]})
        applied.append(f"간선 추가: {f['from']} -> {f['to']} ({template['type']}, {template['seconds']:.1f}초, {src_from}->{src_to} 값 복사)")
    for v in fixes.get("toVerify", []):
        warnings.append(f"현장 확인 필요: {v['node']} (이름 {v['nameFloor']}층, 데이터 {v['dataFloor']}층)")
    return applied


def parse_string_map(src: str, var: str) -> dict[str, str]:
    m = re.search(rf"Map<String, String> {var} = \{{(.*?)\}};", src, flags=re.S)
    return dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", m.group(1)))


def parse_string_list(src: str, var: str) -> list[str]:
    m = re.search(rf"List<String> {var} = \[(.*?)\];", src, flags=re.S)
    return re.findall(r"'([^']+)'", m.group(1))


def parse_building_info(src: str) -> dict[str, dict]:
    result = {}
    for block in re.split(r"BuildingInfoDetail\(", src)[1:]:
        name = re.search(r"name:\s*'([^']+)'", block)
        if not name:
            continue
        phone = re.search(r"teleNumber:\s*'([^']*)'", block)
        hours = re.search(r"operatingHours:\s*'([^']*)'", block)
        facilities = []
        for icon, fname, desc in re.findall(
            r"Amenity\(icon:\s*const Icon\(Icons\.(\w+)\),\s*name:\s*'([^']*)',\s*description:\s*'([^']*)'\)",
            block,
        ):
            base = re.sub(r"_(rounded|sharp|outlined)$", "", icon)
            facilities.append({
                "type": FACILITY_ICON_MAP.get(base, "ETC"),
                "name": fname,
                "floorDescription": desc,
            })
        result[name.group(1)] = {
            "phone": phone.group(1) if phone else None,
            "operatingHours": hours.group(1) if hours else None,
            "facilities": facilities,
        }
    return result


def floor_from_filename(fname: str, building_names: list[str]) -> tuple[str, int] | None:
    stem = fname[:-4]
    for b in sorted(building_names, key=len, reverse=True):
        if stem.startswith(b):
            rest = stem[len(b):].lstrip("_").rstrip("F")
            if rest.startswith("B") and rest[1:].isdigit():
                return b, -int(rest[1:])
            if rest.isdigit():
                return b, int(rest)
    return None


def floor_label(floor: int) -> str:
    return f"B{-floor}" if floor < 0 else f"{floor}F"


def main() -> None:
    dubeogi, out_data, out_static = (Path(p) for p in sys.argv[1:4])
    save_src = (dubeogi / "lib/save/save.dart").read_text(encoding="utf-8")
    info_src = (dubeogi / "lib/save/building_info.dart").read_text(encoding="utf-8")
    img = dubeogi / "assets/images"

    nodes, edges, warnings = parse_graph(save_src)
    fixes_path = Path(__file__).resolve().parent / "manual/edge_fixes.json"
    applied_fixes = apply_fixes(nodes, edges, json.loads(fixes_path.read_text(encoding="utf-8")), warnings) if fixes_path.exists() else []
    plain_src = strip_comments(save_src)
    building_files = parse_string_map(plain_src, "originalData")
    floor_files = parse_string_list(plain_src, "allFileNames")
    selectable = parse_string_list(plain_src, "buildings")
    info = parse_building_info(info_src)

    for d in ("map", "building", "floor", "infophoto"):
        (out_static / d).mkdir(parents=True, exist_ok=True)
    out_data.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(img / "du.png", out_static / "map/campus.png")
    map_w, map_h = png_size(img / "du.png")

    # 건물
    buildings = []
    by_name: dict[str, dict] = {}
    for bid, (name, path) in enumerate(sorted(building_files.items()), start=1):
        src_png = dubeogi / path
        left, top = map(int, re.search(r"\((\d+),(\d+)\)", path).groups())
        w, h = png_size(src_png)
        slug = SLUGS[name]
        shutil.copyfile(src_png, out_static / f"building/{slug}.png")
        photo = img / f"infophoto/{name}.png"
        photo_url = None
        if photo.exists():
            shutil.copyfile(photo, out_static / f"infophoto/{slug}.png")
            photo_url = f"/static/infophoto/{slug}.png"
        else:
            warnings.append(f"건물 사진 없음: {name}")
        b = {
            "id": bid,
            "name": display(name),
            "sourceName": name,
            "slug": slug,
            "aliases": ALIASES.get(name, []),
            "position": {"x": left + w / 2, "y": top + h / 2},
            "imageBounds": {"x": left, "y": top, "width": w, "height": h},
            "thumbnailUrl": f"/static/building/{slug}.png",
            "photoUrl": photo_url,
            "phone": info.get(name, {}).get("phone"),
            "operatingHours": info.get(name, {}).get("operatingHours"),
            "facilities": info.get(name, {}).get("facilities", []),
            "floors": [],
        }
        buildings.append(b)
        by_name[name] = b

    # 층 + 층 단면도 (원본 앱은 건물 이미지 자리에 원래 크기로 겹쳐 그린다)
    for fname in floor_files:
        parsed = floor_from_filename(fname, list(by_name))
        if not parsed:
            warnings.append(f"층 단면도 이름 해석 실패: {fname}")
            continue
        bname, floor = parsed
        b = by_name[bname]
        w, h = png_size(img / f"floor/{fname}")
        tag = f"b{-floor}" if floor < 0 else str(floor)
        shutil.copyfile(img / f"floor/{fname}", out_static / f"floor/{b['slug']}_{tag}.png")
        b["floors"].append({
            "floor": floor,
            "label": floor_label(floor),
            "floorImageUrl": f"/static/floor/{b['slug']}_{tag}.png",
            "imageBounds": {"x": b["imageBounds"]["x"], "y": b["imageBounds"]["y"], "width": w, "height": h},
        })
    # 그래프에는 있지만 단면도가 없는 층도 층 목록에 넣는다
    for n in nodes.values():
        if n["building"] and n["floor"] != 0 and n["building"] in by_name:
            b = by_name[n["building"]]
            if all(f["floor"] != n["floor"] for f in b["floors"]):
                b["floors"].append({"floor": n["floor"], "label": floor_label(n["floor"]), "floorImageUrl": None, "imageBounds": None})
    for b in buildings:
        b["floors"].sort(key=lambda f: f["floor"])

    # 노드·간선에 id 부여
    node_list = []
    node_id = {}
    for nid, name in enumerate(sorted(nodes), start=1):
        n = nodes[name]
        node_id[name] = nid
        node_list.append({
            "id": nid,
            "sourceName": name,
            "x": n["x"],
            "y": n["y"],
            "floor": n["floor"],
            "buildingId": by_name[n["building"]]["id"] if n["building"] in by_name else None,
            "showDetail": n["showDetail"],
            "isHub": n["isHub"],
        })
    edge_list = [
        {
            "id": i,
            "fromNodeId": node_id[e["from"]],
            "toNodeId": node_id[e["to"]],
            "seconds": e["seconds"],
            "kcal": e["kcal"],
            "type": e["type"],
            "isRoad": e["isRoad"],
            "isShortcut": False,
            "name": None,
            "enabled": True,
        }
        for i, e in enumerate(edges, start=1)
        if e["from"] in node_id and e["to"] in node_id
    ]

    # 장소: 원본 앱에서 고를 수 있던 57곳
    places = []
    floor_place_pattern = re.compile(r"^(.*?) (B?\d+)층$")
    for pid, name in enumerate(selectable, start=1):
        n = nodes[name]
        building = by_name.get(name) or (by_name.get(n["building"]) if n["building"] else None)
        floor = n["floor"]
        m = floor_place_pattern.match(name)
        if name in PLACE_CATEGORY_OVERRIDES:
            category = PLACE_CATEGORY_OVERRIDES[name]
        elif name in by_name:
            category = "BUILDING"
        elif m:
            category = "BUILDING"  # 층 대표 장소. 명세서에 맞는 카테고리가 없어 BUILDING으로 둔다
            fl = m.group(2)
            floor = -int(fl[1:]) if fl.startswith("B") else int(fl)
            if building is None:
                prefix = m.group(1)
                building = next((b for b in buildings if b["sourceName"].startswith(prefix) or prefix in b["aliases"]), None)
        else:
            category = "ENTRANCE"
        if name in by_name:
            floor = 0
            pos = by_name[name]["position"]
        else:
            pos = {"x": n["x"], "y": n["y"]}
        places.append({
            "id": pid,
            "name": display(name),
            "sourceName": name,
            "category": category,
            "aliases": building["aliases"] if building and name == building["sourceName"] else [],
            "buildingId": building["id"] if building else None,
            "buildingName": building["name"] if building else None,
            "floor": floor,
            "position": pos,
            "nodeId": node_id[name],
            "hint": None,
        })

    map_info = {
        "imageUrl": "/static/map/campus.png",
        "width": map_w,
        "height": map_h,
        "defaultCenter": {"x": 1100, "y": 2600},
        "minZoom": -3,
        "maxZoom": 1,
    }

    for fname, payload in (
        ("map.json", map_info),
        ("buildings.json", buildings),
        ("nodes.json", node_list),
        ("edges.json", edge_list),
        ("places.json", places),
    ):
        (out_data / fname).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    report = {
        "nodes": len(node_list),
        "hubNodes": sum(n["isHub"] for n in node_list),
        "edges": len(edge_list),
        "edgeTypes": dict(Counter(e["type"] for e in edge_list)),
        "roadEdges": sum(e["isRoad"] for e in edge_list),
        "buildings": len(buildings),
        "floorImages": sum(1 for b in buildings for f in b["floors"] if f["floorImageUrl"]),
        "places": len(places),
        "placeCategories": dict(Counter(p["category"] for p in places)),
        "appliedFixes": applied_fixes,
        "warnings": warnings,
    }
    (out_data / "convert_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("warnings", "appliedFixes")}, ensure_ascii=False))
    print(f"보완 적용 {len(applied_fixes)}건")
    print(f"경고 {len(warnings)}건 → {out_data / 'convert_report.json'}")


if __name__ == "__main__":
    main()
