"""Batch processor: SHP directory -> NorthSky allowable area CSV.

Inputs (GH globals or CLI arg):
- shp_dir         : SHP 파일이 들어있는 디렉토리 경로
- target_lot_limit: 계산할 대상 필지 수 제한(옵션)
- setback_type    : 실행할 단일 시나리오 타입 int (1=A, 2=B, 3=C, 4=D, 5=E, 6=F)
                    지정하지 않으면 all_types 값에 따라 결정
- all_types       : True이면 지원하는 모든 타입(1~6)을 순서대로 실행

CLI 사용 예:
  python shp_northsky_batch.py <shp_dir>              # 전체 시나리오 실행
  python shp_northsky_batch.py <shp_dir> 3            # 시나리오 C만 실행
  python shp_northsky_batch.py <shp_dir> all          # 전체 시나리오 실행

동작:
1) 디렉토리에서 .shp 파일 탐색
2) 필지를 읽어 Lot/Road 분류
3) 제1/2/3종 일반주거지역(A13: 13,14,15) Lot만 대상
4) 1~7층(층고 3m) 기준으로 각 층 허용 바운더리 면적 계산
5) result 폴더에 층별 면적 CSV, 요약 CSV, 비교 CSV 저장
"""

import csv
import datetime
import importlib
import math
import os
import sys
from collections import Counter, defaultdict

# GH 환경: .gh 파일 위치 기준으로 setback_sim 경로를 sys.path에 추가
try:
    _ghenv = globals().get("ghenv")
    if _ghenv is not None:
        _gh_file = _ghenv.Component.OnPingDocument().FilePath
        if _gh_file:
            _d = os.path.dirname(os.path.abspath(_gh_file))
            if os.path.isdir(_d) and _d not in sys.path:
                sys.path.insert(0, _d)
except Exception:
    pass

import Rhino.Geometry as geo  # type: ignore

try:
    from . import constants, northsky, utils, shp_to_lot  # type: ignore
except Exception:
    import constants  # type: ignore
    import northsky  # type: ignore
    import utils  # type: ignore
    import shp_to_lot  # type: ignore

importlib.reload(constants)
importlib.reload(utils)
importlib.reload(northsky)
importlib.reload(shp_to_lot)


FLOOR_HEIGHT_M = 3.0
MAX_FLOOR = 7
SUPPORTED_SETBACK_TYPES = (1, 2, 3, 4, 5, 6)


def _resolve_shp_path(shp_dir):
    """입력 디렉토리(또는 shp 파일 경로)에서 대상 shp 파일 1개를 결정한다."""
    if not shp_dir:
        raise ValueError("shp_dir 입력이 필요합니다.")

    candidate = os.path.abspath(shp_dir)
    if os.path.isfile(candidate) and candidate.lower().endswith(".shp"):
        return candidate

    if not os.path.isdir(candidate):
        raise FileNotFoundError("유효한 SHP 디렉토리가 아닙니다: {}".format(shp_dir))

    shp_files = sorted(
        [
            os.path.join(candidate, name)
            for name in os.listdir(candidate)
            if name.lower().endswith(".shp")
        ]
    )
    if not shp_files:
        raise FileNotFoundError(
            "디렉토리에서 .shp 파일을 찾지 못했습니다: {}".format(candidate)
        )

    return shp_files[0]


def _compute_allowable_rows(
    shp_path, include_counter=False, target_lot_limit=None, setback_type=None
):
    """대상 SHP에 대해 층별 허용면적 테이블(row dict 리스트) 생성."""
    repo = shp_to_lot.LotRepository(shp_path)
    lots, roads = repo.lots, repo.roads

    landuse_counter = Counter(
        utils.normalize_landuse_code(getattr(lot, "landuse_code", "")) for lot in lots
    )

    target_lots = [
        lot
        for lot in lots
        if utils.normalize_landuse_code(getattr(lot, "landuse_code", ""))
        in constants.RESIDENTIAL_GENERAL_CODES
    ]

    if target_lot_limit is not None:
        if target_lot_limit > 0:
            target_lots = target_lots[:target_lot_limit]

    rows = []
    total_height = FLOOR_HEIGHT_M * MAX_FLOOR
    warning_pnus = set()
    road20m_rows = []
    centerline_rows = []

    for lot in target_lots:
        other_lots = repo.get_other_lots(lot)

        try:
            calc = northsky.create_calculator(
                target_lot=lot,
                neighbor_lots=other_lots,
                max_distance=total_height,
                height=FLOOR_HEIGHT_M,
                ratio=constants.DEFAULT_RATIO,
            )
        except Exception:
            warning_pnus.add(str(getattr(lot, "pnu", "")))
            continue

        if getattr(calc, "qa_has_road20m_exclusion", False):
            owner_pnus = sorted(getattr(calc, "qa_road20m_exclusion_owner_pnus", set()))
            if owner_pnus:
                for owner_pnu in owner_pnus:
                    road20m_rows.append(
                        {
                            "target_pnu": str(getattr(lot, "pnu", "")),
                            "owner_pnu": owner_pnu,
                        }
                    )
            else:
                road20m_rows.append(
                    {
                        "target_pnu": str(getattr(lot, "pnu", "")),
                        "owner_pnu": "",
                    }
                )

        if getattr(calc, "qa_has_apartment_centerline", False):
            owner_pnus = sorted(
                getattr(calc, "qa_apartment_centerline_owner_pnus", set())
            )
            if owner_pnus:
                for owner_pnu in owner_pnus:
                    centerline_rows.append(
                        {
                            "target_pnu": str(getattr(lot, "pnu", "")),
                            "owner_pnu": owner_pnu,
                        }
                    )
            else:
                centerline_rows.append(
                    {
                        "target_pnu": str(getattr(lot, "pnu", "")),
                        "owner_pnu": "",
                    }
                )

        lot_region_inward = calc.lot_region_inward
        lot_region_inward_area = (
            0.0 if lot_region_inward is None else utils.get_area(lot_region_inward)
        )
        if lot_region_inward is None:
            warning_pnus.add(str(getattr(lot, "pnu", "")))
            lot_region_inward = None

            rows.append(
                {
                    "pnu": lot.pnu,
                    "jimok": lot.jimok,
                    "landuse_code": getattr(lot, "landuse_code", ""),
                    "landuse": getattr(lot, "landuse", constants.LANDUSE_UNKNOWN),
                    "lot_area_m2": lot.area,
                    "lot_area_inward_1m_m2": lot_region_inward_area,
                    "floor": 0,
                    "height_m": 0.0,
                    "allowed_area_m2": 0.0,
                    "base_segment_count": 0,
                }
            )
            continue

        for floor in range(1, MAX_FLOOR + 1):
            height_m = FLOOR_HEIGHT_M * floor
            try:
                calc.compute(
                    height=height_m,
                    type=setback_type,
                )
                buildable = calc.buildable_boundary
                allowed_area = 0.0 if not buildable else utils.get_area(buildable)
            except Exception:
                warning_pnus.add(str(getattr(lot, "pnu", "")))
                # 실패 층부터 마지막 층까지 0으로 채워 parcel_count 일관성 유지
                for fill_floor in range(floor, MAX_FLOOR + 1):
                    rows.append(
                        {
                            "pnu": lot.pnu,
                            "jimok": lot.jimok,
                            "landuse_code": getattr(lot, "landuse_code", ""),
                            "landuse": getattr(lot, "landuse", constants.LANDUSE_UNKNOWN),
                            "lot_area_m2": lot.area,
                            "lot_area_inward_1m_m2": lot_region_inward_area,
                            "floor": fill_floor,
                            "height_m": FLOOR_HEIGHT_M * fill_floor,
                            "allowed_area_m2": 0.0,
                            "base_segment_count": len(calc.base_segments or []),
                        }
                    )
                break

            rows.append(
                {
                    "pnu": lot.pnu,
                    "jimok": lot.jimok,
                    "landuse_code": getattr(lot, "landuse_code", ""),
                    "landuse": getattr(lot, "landuse", constants.LANDUSE_UNKNOWN),
                    "lot_area_m2": lot.area,
                    "lot_area_inward_1m_m2": lot_region_inward_area,
                    "floor": floor,
                    "height_m": height_m,
                    "allowed_area_m2": allowed_area,
                    "base_segment_count": len(calc.base_segments or []),
                }
            )

    qa_data = {
        "warning_pnus": sorted([p for p in warning_pnus if p]),
        "road20m_rows": road20m_rows,
        "centerline_rows": centerline_rows,
    }

    if include_counter:
        return rows, len(lots), len(roads), len(target_lots), landuse_counter, qa_data
    return rows, len(lots), len(roads), len(target_lots), qa_data


def _compute_summary(rows):
    """층별 용도지역별 요약 통계(평균/표준편차/중앙값) 계산."""
    groups = defaultdict(list)
    pnu_per_landuse = defaultdict(set)

    for row in rows:
        if row["floor"] == 0:
            continue
        landuse = row["landuse"]
        floor = row["floor"]
        groups[(landuse, floor)].append(float(row["allowed_area_m2"]))
        pnu_per_landuse[landuse].add(row["pnu"])

    summary_rows = []
    for (landuse, floor) in sorted(groups.keys()):
        areas = groups[(landuse, floor)]
        n = len(areas)
        if n == 0:
            continue
        mean = sum(areas) / n
        variance = sum((x - mean) ** 2 for x in areas) / n
        std = math.sqrt(variance) if variance > 0 else 0.0
        sorted_areas = sorted(areas)
        if n % 2 == 0:
            median = (sorted_areas[n // 2 - 1] + sorted_areas[n // 2]) / 2.0
        else:
            median = sorted_areas[n // 2]

        summary_rows.append(
            {
                "landuse": landuse,
                "parcel_count": len(pnu_per_landuse[landuse]),
                "floor": floor,
                "mean_area_m2": round(mean, 4),
                "std_area_m2": round(std, 4),
                "median_area_m2": round(median, 4),
            }
        )

    return summary_rows


def _save_qa_csv(path, headers, rows):
    """QA 결과를 지정 컬럼으로 CSV 파일에 저장한다."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _save_qa_reports(shp_path, qa_data, setback_type):
    """QA 보조 리포트 파일들을 생성하고 저장 경로를 반환한다."""
    base_dir = os.path.dirname(os.path.abspath(shp_path))
    qa_dir = os.path.join(base_dir, "qa")
    if not os.path.isdir(qa_dir):
        os.makedirs(qa_dir)

    stem = os.path.splitext(os.path.basename(shp_path))[0]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    label = constants.SCENARIO_LABELS.get(int(setback_type), str(int(setback_type)))
    scenario_tag = "scenario_{}".format(label)

    warning_path = os.path.join(
        qa_dir, "{}_{}_qa_warning_pnu_{}.csv".format(stem, scenario_tag, timestamp)
    )
    road20m_path = os.path.join(
        qa_dir,
        "{}_{}_qa_road20m_exclusion_{}.csv".format(stem, scenario_tag, timestamp),
    )
    centerline_path = os.path.join(
        qa_dir,
        "{}_{}_qa_apartment_centerline_{}.csv".format(stem, scenario_tag, timestamp),
    )

    warning_rows = [{"pnu": p} for p in qa_data.get("warning_pnus", [])]
    _save_qa_csv(warning_path, ["pnu"], warning_rows)
    _save_qa_csv(
        road20m_path,
        ["target_pnu", "owner_pnu"],
        qa_data.get("road20m_rows", []),
    )
    _save_qa_csv(
        centerline_path,
        ["target_pnu", "owner_pnu"],
        qa_data.get("centerline_rows", []),
    )

    return {
        "warning_path": warning_path,
        "road20m_path": road20m_path,
        "centerline_path": centerline_path,
    }


def _save_csv(rows, shp_path, setback_type):
    """result 폴더에 층별 허용면적 CSV 저장 후 경로 반환.

    파일명: result_scenario_{X}_{YYYYMMDD}.csv
    """
    base_dir = os.path.dirname(os.path.abspath(shp_path))
    result_dir = os.path.join(base_dir, "result")
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)

    label = constants.SCENARIO_LABELS.get(int(setback_type), str(int(setback_type)))
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(
        result_dir,
        "result_scenario_{}_{}.csv".format(label, date_str),
    )

    headers = [
        "pnu",
        "jimok",
        "landuse_code",
        "landuse",
        "lot_area_m2",
        "lot_area_inward_1m_m2",
        "floor",
        "height_m",
        "allowed_area_m2",
        "base_segment_count",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return csv_path


def _save_summary_csv(rows, shp_path, setback_type):
    """용도지역별 층별 요약 통계 CSV 저장 후 경로 반환.

    파일명: summary_scenario_{X}.csv
    """
    base_dir = os.path.dirname(os.path.abspath(shp_path))
    result_dir = os.path.join(base_dir, "result")
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)

    label = constants.SCENARIO_LABELS.get(int(setback_type), str(int(setback_type)))
    csv_path = os.path.join(result_dir, "summary_scenario_{}.csv".format(label))

    summary_rows = _compute_summary(rows)
    headers = [
        "landuse",
        "parcel_count",
        "floor",
        "mean_area_m2",
        "std_area_m2",
        "median_area_m2",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow(row)

    return csv_path


def _save_comparison_csv(all_results_rows, shp_path):
    """시나리오 A(타입 1) 대비 각 시나리오 변화율 요약 CSV 저장 후 경로 반환.

    파일명: comparison_vs_A.csv

    all_results_rows: dict {setback_type(int) -> rows(list)}
    시나리오 A(type=1) 결과가 반드시 포함되어야 한다.
    """
    if 1 not in all_results_rows:
        print("[comparison] 시나리오 A(type=1) 결과가 없어 비교 CSV를 생성하지 않습니다.")
        return None

    baseline_summary = _compute_summary(all_results_rows[1])
    baseline_dict = {
        (r["landuse"], r["floor"]): r["mean_area_m2"] for r in baseline_summary
    }

    comparison_rows = []
    for setback_type in sorted(all_results_rows.keys()):
        if setback_type == 1:
            continue
        label = constants.SCENARIO_LABELS.get(setback_type, str(setback_type))
        summary = _compute_summary(all_results_rows[setback_type])
        for r in summary:
            key = (r["landuse"], r["floor"])
            base_mean = baseline_dict.get(key, 0.0)
            curr_mean = r["mean_area_m2"]
            change_abs = curr_mean - base_mean
            change_rate = (
                (change_abs / base_mean * 100.0) if base_mean > 1e-9 else 0.0
            )
            comparison_rows.append(
                {
                    "scenario": label,
                    "landuse": r["landuse"],
                    "floor": r["floor"],
                    "mean_area_current_m2": round(base_mean, 4),
                    "mean_area_scenario_m2": round(curr_mean, 4),
                    "change_rate_pct": round(change_rate, 4),
                    "change_abs_m2": round(change_abs, 4),
                }
            )

    base_dir = os.path.dirname(os.path.abspath(shp_path))
    result_dir = os.path.join(base_dir, "result")
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)

    csv_path = os.path.join(result_dir, "comparison_vs_A.csv")
    headers = [
        "scenario",
        "landuse",
        "floor",
        "mean_area_current_m2",
        "mean_area_scenario_m2",
        "change_rate_pct",
        "change_abs_m2",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in comparison_rows:
            writer.writerow(row)

    return csv_path


if __name__ == "__main__":
    # --- 입력 파라미터 수집 (GH globals 우선, 없으면 CLI argv) ---
    shp_dir = globals().get("shp_dir")
    target_lot_limit = globals().get("target_lot_limit")
    setback_type_input = globals().get("setback_type")   # int or None
    all_types = bool(globals().get("all_types", False))

    if not shp_dir and len(sys.argv) > 1:
        shp_dir = sys.argv[1]
    if setback_type_input is None and len(sys.argv) > 2:
        arg2 = sys.argv[2]
        if arg2.lower() == "all":
            all_types = True
        else:
            try:
                setback_type_input = int(arg2)
            except ValueError:
                pass

    # --- 실행할 타입 목록 결정 ---
    if all_types:
        types_to_run = list(SUPPORTED_SETBACK_TYPES)
    elif setback_type_input is not None:
        types_to_run = [int(setback_type_input)]
    else:
        # 기본: 전체 시나리오
        types_to_run = list(SUPPORTED_SETBACK_TYPES)

    # 지원 타입 검증
    for t in types_to_run:
        if t not in SUPPORTED_SETBACK_TYPES:
            raise ValueError(
                "지원하지 않는 setback_type: {}. 지원 범위: {}".format(
                    t, SUPPORTED_SETBACK_TYPES
                )
            )

    shp_path = _resolve_shp_path(shp_dir)

    all_results = {}  # setback_type -> rows

    for setback_type in types_to_run:
        label = constants.SCENARIO_LABELS.get(setback_type, str(setback_type))
        params = constants.SCENARIO_PARAMS[setback_type]
        print(
            "\n[시나리오 {}] {} 시작...".format(label, params["description"])
        )

        rows, lot_count, road_count, target_count, landuse_counter, qa_data = (
            _compute_allowable_rows(
                shp_path,
                include_counter=True,
                target_lot_limit=target_lot_limit,
                setback_type=setback_type,
            )
        )

        output_csv_path = _save_csv(rows, shp_path, setback_type)
        summary_csv_path = _save_summary_csv(rows, shp_path, setback_type)
        qa_paths = _save_qa_reports(shp_path, qa_data, setback_type)

        all_results[setback_type] = rows

        print("SHP: {}".format(shp_path))
        print("시나리오: {} ({})".format(label, params["description"]))
        if target_lot_limit is None:
            print("대상 필지 제한: 전체")
        else:
            print("대상 필지 제한: 상위 {}개".format(target_lot_limit))
        print("전체 대지 수: {}, 도로 수: {}".format(lot_count, road_count))
        print("대지 landuse_code 상위 분포: {}".format(landuse_counter.most_common(10)))
        print("대상(일반주거 13/14/15) 대지 수: {}".format(target_count))
        print("층별 면적 CSV 저장: {}".format(output_csv_path))
        print("요약 통계 CSV 저장: {}".format(summary_csv_path))
        print("QA 저장 완료: {}".format(qa_paths.get("warning_path")))
        print("QA 저장 완료: {}".format(qa_paths.get("road20m_path")))
        print("QA 저장 완료: {}".format(qa_paths.get("centerline_path")))

    # --- 비교 CSV: 시나리오 A가 포함된 경우에만 생성 ---
    if 1 in all_results and len(all_results) > 1:
        comparison_csv_path = _save_comparison_csv(all_results, shp_path)
        if comparison_csv_path:
            print("\n[비교 CSV] 시나리오 A 대비 변화율 저장: {}".format(comparison_csv_path))
    elif len(all_results) == 1 and 1 not in all_results:
        print(
            "\n[비교 CSV] 시나리오 A(type=1)가 포함되지 않아 비교 CSV를 생성하지 않습니다."
        )
