"""GH가 출력한 buildable SHP 파일을 Rhino 없이 검증.

사용법:
  python3 scripts/verify_geom_shp.py <SHP 또는 디렉토리 경로>

검증 항목:
  - 필드 스키마 (pnu, floor, type, h_base_m, h_top_m, zone_cd, area_m2, seg_cnt)
  - .prj 좌표계 (EPSG:5179 Korea TM 확인)
  - feature 수, floor/type/zone 분포
  - 좌표 범위 (서울 BBOX 안에 들어가는지)
  - 폴리곤 closed 검사
  - 샘플 레코드 출력
"""

import os
import sys
from collections import Counter

import shapefile  # pip install pyshp


# 서울 대략 BBOX (EPSG:5179 Korea 2000 / Central Belt 2010)
# False_Easting=200000, False_Northing=600000 기준
SEOUL_X = (185000, 220000)
SEOUL_Y = (535000, 575000)

EXPECTED_FIELDS = [
    ("pnu", "C"),
    ("floor_from", "N"),
    ("floor_to", "N"),
    ("type", "N"),
    ("h_base_m", "N"),
    ("h_top_m", "N"),
    ("zone_cd", "C"),
    ("area_m2", "N"),
    ("seg_cnt", "N"),
]


def _print_header(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def verify_one(shp_path):
    base = os.path.splitext(shp_path)[0]
    name = os.path.basename(shp_path)
    _print_header(name)

    if not os.path.isfile(shp_path):
        print("  [FAIL] 파일 없음")
        return False
    for ext in (".shx", ".dbf"):
        if not os.path.isfile(base + ext):
            print("  [WARN] 동반 파일 없음:", ext)

    # .prj
    prj_path = base + ".prj"
    if os.path.isfile(prj_path):
        prj = open(prj_path).read()
        is_korea_tm = "Korea_2000" in prj or "Central_Belt" in prj or "127.0" in prj
        print("  prj 존재: ", "[OK] Korea TM 추정됨" if is_korea_tm else "[WARN] 좌표계 확인 필요")
    else:
        print("  [WARN] .prj 없음")

    reader = shapefile.Reader(shp_path, encoding="utf-8")
    try:
        shapes = reader.shapes()
        records = reader.records()
        fields = [(f[0], f[1]) for f in reader.fields[1:]]

        # 스키마
        field_names = [f[0] for f in fields]
        expected_names = [n for n, _ in EXPECTED_FIELDS]
        missing = [n for n in expected_names if n not in field_names]
        if missing:
            print("  [FAIL] 누락 필드:", missing)
        else:
            print("  [OK] 필드 스키마")
        print("  필드:", fields)

        n = len(shapes)
        print("  feature 수:", n)
        if n == 0:
            print("  [FAIL] feature 0개")
            return False

        # 분포
        types = Counter(r["type"] for r in records)
        zones = Counter(str(r["zone_cd"]) for r in records)
        floor_spans = Counter(
            (int(r["floor_from"]), int(r["floor_to"])) for r in records
        )
        print("  type 분포:", dict(sorted(types.items())))
        print("  zone 분포:", dict(zones.most_common()))

        # floor 범위 검증 (1~7 범위 안)
        bad_range = [
            (r["floor_from"], r["floor_to"]) for r in records
            if not (1 <= int(r["floor_from"]) <= int(r["floor_to"]) <= 7)
        ]
        if bad_range:
            print("  [WARN] floor 범위 이상 {}건 (샘플: {})".format(
                len(bad_range), bad_range[:3]
            ))
        else:
            print("  [OK] floor 범위 모두 정상 (1<=from<=to<=7)")

        # 상위 span 패턴
        print("  주요 floor span (from, to) -> count:")
        for span, cnt in floor_spans.most_common(8):
            spread = span[1] - span[0] + 1
            print("    {} (층 {}개) : {}".format(span, spread, cnt))

        # dedup 효과 (record 1개당 평균 몇 층을 커버하는지)
        total_floor_coverage = sum(
            (int(r["floor_to"]) - int(r["floor_from"]) + 1) for r in records
        )
        avg_floors_per_rec = total_floor_coverage / float(n) if n else 0
        print("  dedup 효율: record당 평균 {:.2f} 층 커버  ({} records로 {} 층 표현)".format(
            avg_floors_per_rec, n, total_floor_coverage
        ))
        # h_base/h_top 일관성
        bad_h = 0
        for r in records:
            exp_base = (int(r["floor_from"]) - 1) * 3.0
            exp_top = int(r["floor_to"]) * 3.0
            if abs(float(r["h_base_m"]) - exp_base) > 0.01 or \
               abs(float(r["h_top_m"]) - exp_top) > 0.01:
                bad_h += 1
        if bad_h:
            print("  [WARN] h_base/h_top 불일치 {}건".format(bad_h))
        else:
            print("  [OK] h_base/h_top 모두 일관 (= (floor-1)*3, floor*3)")

        # BBOX
        bbox = reader.bbox  # [xmin, ymin, xmax, ymax]
        print("  BBOX:", [round(v, 1) for v in bbox])
        x_in = SEOUL_X[0] <= bbox[0] and bbox[2] <= SEOUL_X[1]
        y_in = SEOUL_Y[0] <= bbox[1] and bbox[3] <= SEOUL_Y[1]
        if x_in and y_in:
            print("  [OK] 좌표 범위 서울 안")
        else:
            print("  [WARN] 좌표 범위가 서울 BBOX 밖 (좌표계 의심)")

        # 폴리곤 closed 검증 (앞 100개 샘플)
        sample = min(100, n)
        bad_rings = 0
        for s in shapes[:sample]:
            for part_idx in range(len(s.parts)):
                start = s.parts[part_idx]
                end = s.parts[part_idx + 1] if part_idx + 1 < len(s.parts) else len(s.points)
                ring = s.points[start:end]
                if len(ring) < 4 or ring[0] != ring[-1]:
                    bad_rings += 1
        if bad_rings == 0:
            print("  [OK] 샘플 {}개 폴리곤 모두 closed".format(sample))
        else:
            print("  [WARN] {}/{} 샘플에서 ring 닫힘 이상".format(bad_rings, sample))

        # 면적 sanity
        areas = [r["area_m2"] for r in records if r["area_m2"] > 0]
        if areas:
            print("  area_m2: min={:.2f}  median≈{:.2f}  max={:.2f}".format(
                min(areas), sorted(areas)[len(areas) // 2], max(areas)
            ))

        # 샘플 레코드
        print("  샘플 record[0]:", dict(zip(field_names, records[0])))

        return True
    finally:
        reader.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target = sys.argv[1]
    if os.path.isdir(target):
        shp_files = sorted(
            os.path.join(target, n) for n in os.listdir(target)
            if n.lower().endswith(".shp")
        )
        if not shp_files:
            print("[ERROR] 디렉토리에 .shp 파일 없음:", target)
            sys.exit(1)
        ok = 0
        for s in shp_files:
            if verify_one(s):
                ok += 1
        print()
        print("=" * 70)
        print("RESULT: {}/{} 파일 통과".format(ok, len(shp_files)))
    elif os.path.isfile(target) and target.lower().endswith(".shp"):
        verify_one(target)
    else:
        print("[ERROR] 유효한 .shp 또는 디렉토리가 아닙니다:", target)
        sys.exit(1)


if __name__ == "__main__":
    main()
