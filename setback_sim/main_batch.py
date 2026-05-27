"""SHP → 다중 PNU 건축가능영역 3D 시각화 배치.

`shp_to_lot.py`의 SHP 로딩과 `main.py`의 단일 lot 계산을 묶어,
복수 PNU에 대한 건축가능영역 커브를 0~18m 범위 7단계 높이로 한 번에 산출한다.

GH Inputs:
- shp_path     : str. SHP 파일 경로
- pnu_list     : List[str]. 계산 대상 PNU 목록
- setback_type : int. 1=A 현행, 2=B 개정안, 3=C, 4=D, 5=E, 6=F

GH Outputs:
- lot_regions        : List[geo.Curve]. 타겟 + 인근 필지 전체 region 커브.
                        타겟별 이웃을 dedup해 한 번에 시각화할 수 있도록 합쳐 반환한다.
- target_lot_regions : List[geo.Curve]. pnu_list에 해당하는 타겟 필지 region 커브.
- buildable_regions  : List[geo.Curve]. 각 타겟 필지의 0/3/6/9/12/15/18m 측정 높이별
                        건축가능영역 커브를 평탄화한 리스트. 각 커브는 측정 높이만큼
                        Z가 평행이동되어 있어 GH에서 바로 3D 적층 시각화가 가능하다.
- cutter_breps       : List[geo.Brep]. 각 타겟 필지의 법규 시각화용 사선 cutter Brep
                        (수직/수평/사선)을 평탄화한 리스트. 측정 높이와 무관하게
                        `constants.CUTTER_VISUAL_MAX_HEIGHT_M` 기준으로 1회만 산출된다.

내부 변수(필요 시 GH output으로 추가 가능):
- buildable_regions_2d : List[List[geo.Curve]]. flatten 전 [pnu, height] 이중 리스트.
                          각 행은 측정 높이 수(7개)와 동일 길이를 유지하며,
                          target 미존재 / 계산 실패 / 특정 높이 boundary 없음 시 None.
- cutter_breps_2d      : List[List[geo.Brep]]. flatten 전 [pnu, brep] 이중 리스트.
                          target 미존재 / 계산 실패 시 빈 리스트.

성능 메모:
- LotRepository는 1회만 로드한다. 이후 각 PNU의 이웃 조회는 bbox 캐시가 재사용된다.
- 한 lot의 base_segments / lot_region_inward는 NorthSkyCalculator 생성 시 1회 계산된다.
  7개 측정 높이는 동일 calculator의 compute()만 반복하므로 setback strip 차집합만
  높이별로 다시 돈다. cutter_breps도 동일 calculator의 base_segments를 재사용한다.
"""

import os
import sys

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
    from . import constants, northsky, shp_to_lot, utils  # type: ignore
except Exception:
    import constants  # type: ignore
    import northsky  # type: ignore
    import shp_to_lot  # type: ignore
    import utils  # type: ignore

import importlib

importlib.reload(constants)
importlib.reload(utils)
importlib.reload(northsky)
importlib.reload(shp_to_lot)


# 시각화용 측정 높이(m). 0m부터 18m까지 3m 간격 7단계.
VISUALIZATION_HEIGHTS_M = (0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0)


def compute_lot_visualization(
    target_lot, other_lots, setback_type, heights=VISUALIZATION_HEIGHTS_M
):
    # type: (object, list, int, tuple) -> tuple
    """단일 target_lot의 측정 높이별 건축가능영역 커브 + 법규 사선 cutter Brep을 계산.

    동일 calculator를 재사용해 base_segments / lot_region_inward를 1회만 계산한다
    (성능 핵심). 높이별 compute()로 buildable curve를 만들고, 마지막에
    get_cutter_breps()로 시각화용 Brep을 1회 생성한다.

    Returns:
        (height_curves, cutter_breps)
        - height_curves : List[Optional[geo.Curve]]. 각 측정 높이의 buildable curve를
                          해당 높이만큼 Z 평행이동한 결과. boundary가 없으면 None.
        - cutter_breps  : List[geo.Brep]. 측정 높이와 무관하게 한 번만 생성되는
                          법규 시각화용 cutter Brep 리스트.
    """
    if not heights:
        return [], []

    max_height = max(heights)
    setback_type_int = int(setback_type)
    calc = northsky.create_calculator(
        target_lot=target_lot,
        neighbor_lots=other_lots,
        max_distance=max_height,
        height=max_height,
        ratio=constants.DEFAULT_RATIO,
    )

    height_curves = []
    for h in heights:
        calc.compute(height=float(h), type=setback_type_int)
        boundary = calc.buildable_boundary
        if boundary is None:
            height_curves.append(None)
            continue
        # XY 평면(이동 전) 상태에서 단순화 — 좁은 통로/미세 돌출 제거.
        boundary = utils.simplify_region(boundary, constants.BUILDABLE_SIMPLIFY_TOL_M)
        if boundary is None:
            height_curves.append(None)
            continue
        moved = utils.move_crv(boundary, geo.Vector3d(0.0, 0.0, float(h)))
        height_curves.append(moved)

    try:
        cutter_breps = calc.get_cutter_breps(setback_type=setback_type_int)
    except Exception as exc:
        print(
            "[batch] PNU '{}' cutter_breps 생성 실패: {}".format(
                getattr(target_lot, "pnu", ""), exc
            )
        )
        cutter_breps = []

    return height_curves, cutter_breps


def collect_targets_and_neighbors(repo, pnu_list):
    # type: (shp_to_lot.LotRepository, list) -> tuple
    """pnu_list 기반으로 target lot 시퀀스, target별 이웃 매핑, 합친 lot 리스트를 반환.

    - target_lots          : pnu_list 입력 순서를 유지. 못 찾은 PNU는 None.
    - other_lots_by_target : id(target) -> 이웃 lot 리스트 (target 1회만 조회).
    - all_lots             : 타겟 + 모든 이웃을 dedup해 합친 리스트.
    """
    target_lots = []
    other_lots_by_target = {}

    for pnu in pnu_list:
        target = repo.get_target_lot(pnu)
        target_lots.append(target)
        if target is None:
            continue
        if id(target) in other_lots_by_target:
            continue
        other_lots_by_target[id(target)] = repo.get_other_lots(target)

    seen_ids = set()
    all_lots = []
    for target in target_lots:
        if target is None or id(target) in seen_ids:
            continue
        seen_ids.add(id(target))
        all_lots.append(target)
    for target in target_lots:
        if target is None:
            continue
        for neighbor in other_lots_by_target.get(id(target), []):
            if id(neighbor) in seen_ids:
                continue
            seen_ids.add(id(neighbor))
            all_lots.append(neighbor)

    return target_lots, other_lots_by_target, all_lots


def run_batch(shp_path, pnu_list, setback_type, heights=VISUALIZATION_HEIGHTS_M):
    # type: (str, list, int, tuple) -> tuple
    """배치 진입점.

    Returns:
        (lot_regions, target_lot_regions,
         buildable_regions_2d, buildable_regions,
         cutter_breps_2d, cutter_breps)
    """
    if not shp_path:
        raise ValueError("shp_path is required.")
    if pnu_list is None:
        raise ValueError("pnu_list is required.")
    if setback_type is None:
        raise ValueError("setback_type is required.")

    setback_type_int = int(setback_type)
    height_count = len(heights)

    repo = shp_to_lot.LotRepository(shp_path)

    target_lots, other_lots_by_target, all_lots = collect_targets_and_neighbors(
        repo, list(pnu_list)
    )

    lot_regions = [lot.region for lot in all_lots if lot.region is not None]
    target_lot_regions = [
        lot.region
        for lot in target_lots
        if lot is not None and lot.region is not None
    ]

    buildable_regions_2d = []
    cutter_breps_2d = []
    for target in target_lots:
        if target is None:
            buildable_regions_2d.append([None] * height_count)
            cutter_breps_2d.append([])
            continue
        others = other_lots_by_target.get(id(target), [])
        try:
            curves, breps = compute_lot_visualization(
                target_lot=target,
                other_lots=others,
                setback_type=setback_type_int,
                heights=heights,
            )
            if len(curves) != height_count:
                curves = list(curves) + [None] * (height_count - len(curves))
        except Exception as exc:
            print(
                "[batch] PNU '{}' 계산 실패: {}".format(
                    getattr(target, "pnu", ""), exc
                )
            )
            curves = [None] * height_count
            breps = []
        buildable_regions_2d.append(curves)
        cutter_breps_2d.append(breps)

    buildable_regions = [
        crv
        for sublist in buildable_regions_2d
        for crv in sublist
        if crv is not None
    ]
    cutter_breps = [
        brep
        for sublist in cutter_breps_2d
        for brep in sublist
        if brep is not None
    ]

    return (
        lot_regions,
        target_lot_regions,
        buildable_regions_2d,
        buildable_regions,
        cutter_breps_2d,
        cutter_breps,
    )


if __name__ == "__main__":
    # GH globals → outputs
    shp_path = globals().get("shp_path")
    pnu_list = globals().get("pnu_list")
    setback_type = globals().get("setback_type")

    if pnu_list is None:
        pnu_list = []

    (
        lot_regions,
        target_lot_regions,
        buildable_regions_2d,
        buildable_regions,
        cutter_breps_2d,
        cutter_breps,
    ) = run_batch(
        shp_path=shp_path,
        pnu_list=pnu_list,
        setback_type=setback_type,
    )

    matched = sum(1 for r in target_lot_regions if r is not None)
    print(
        "[batch] PNU 입력 {}, target 매칭 {}, lot_regions {}, "
        "buildable_regions(flatten) {}, cutter_breps(flatten) {}".format(
            len(pnu_list),
            matched,
            len(lot_regions),
            len(buildable_regions),
            len(cutter_breps),
        )
    )
