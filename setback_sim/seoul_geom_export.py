"""Seoul-wide buildable polygon SHP exporter (for new GH component).

폴더 안의 모든 .shp 파일에 대해 type 1, 2 시나리오를 모두 실행하고
필지 × 층 × 타입 단위 buildable 폴리곤을 Shapefile로 저장한다.

핵심 효율:
- 필지마다 NorthSkyCalculator를 1회만 init (이웃 lot 로딩 비용)
- 동일 calculator로 (층 1~7) × (타입 1, 2) = 14회 compute
- 타입별 Writer를 동시에 열어 한 번의 parcel iteration으로 끝냄
"""

import os
import datetime

import shapefile  # pyshp

try:
    from . import constants, northsky, utils, shp_to_lot  # type: ignore
except Exception:
    import constants  # type: ignore
    import northsky  # type: ignore
    import utils  # type: ignore
    import shp_to_lot  # type: ignore


FLOOR_HEIGHT_M = 3.0
MAX_FLOOR = 7
DEFAULT_TYPES = (1, 2)  # 1=현행, 2=제안

KOREA_TM_PRJ_WKT = (
    'PROJCS["Korea_2000_Korea_Central_Belt_2010",'
    'GEOGCS["GCS_Korea_2000",'
    'DATUM["D_Korea_2000",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",200000.0],'
    'PARAMETER["False_Northing",600000.0],'
    'PARAMETER["Central_Meridian",127.0],'
    'PARAMETER["Scale_Factor",1.0],'
    'PARAMETER["Latitude_Of_Origin",38.0],'
    'UNIT["Meter",1.0]]'
)


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _now_stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _curve_to_ring(curve):
    """Rhino closed Curve -> [(x, y), ...] 닫힌 링. 실패 시 None."""
    if curve is None:
        return None
    try:
        pts = utils.get_vertices(curve)
    except Exception:
        return None
    if not pts or len(pts) < 3:
        return None
    coords = [(float(p.X), float(p.Y)) for p in pts]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords if len(coords) >= 4 else None


def _ring_hash(ring, decimals=3):
    """링을 mm 단위로 반올림한 튜플로 변환 -- 동일성 비교용.

    동일 알고리즘이 만든 결과끼리 비교하므로 vertex 순서/개수가 보존됨.
    """
    if not ring:
        return None
    return tuple((round(x, decimals), round(y, decimals)) for x, y in ring)


def _open_writer(out_path_base):
    """SHP Writer 생성 + 필드 정의. (Writer, out_shp_path) 반환.

    스키마: 층 범위 인코딩 (floor_from, floor_to). 동일 모양이면 한 레코드.
    """
    w = shapefile.Writer(out_path_base, shapeType=shapefile.POLYGON, encoding="utf-8")
    w.field("pnu", "C", 20)
    w.field("floor_from", "N", 2, 0)
    w.field("floor_to", "N", 2, 0)
    w.field("type", "N", 1, 0)
    w.field("h_base_m", "N", 6, 2)
    w.field("h_top_m", "N", 6, 2)
    w.field("zone_cd", "C", 3)
    w.field("area_m2", "N", 12, 4)
    w.field("seg_cnt", "N", 3, 0)
    return w, out_path_base + ".shp"


def _flush_pending(writer, pending, pnu_str, type_int, zone_str):
    """pending record를 SHP에 1줄 쓰기."""
    writer.poly([pending["ring"]])
    writer.record(
        pnu_str,
        pending["floor_from"],
        pending["floor_to"],
        type_int,
        FLOOR_HEIGHT_M * (pending["floor_from"] - 1),
        FLOOR_HEIGHT_M * pending["floor_to"],
        zone_str,
        pending["area"],
        pending["seg_cnt"],
    )


def _write_prj(out_path_base):
    with open(out_path_base + ".prj", "w") as f:
        f.write(KOREA_TM_PRJ_WKT)


def _done_marker_path(out_dir, stem):
    """완료 마커 파일 경로."""
    return os.path.join(out_dir, "{}.done".format(stem))


def _is_district_done(out_dir, stem):
    """이 구가 이미 완료됐는지 (.done 마커 존재 여부)."""
    return os.path.isfile(_done_marker_path(out_dir, stem))


def _write_done_marker(out_dir, stem, info):
    """완료 마커 작성 (실패해도 무시)."""
    try:
        with open(_done_marker_path(out_dir, stem), "w", encoding="utf-8") as f:
            f.write("done_at: {}\n".format(_now_stamp()))
            for k, v in info.items():
                f.write("{}: {}\n".format(k, v))
    except Exception:
        pass


def process_one_shp(shp_path, out_dir, types=DEFAULT_TYPES,
                    target_lot_limit=None, progress_every=1000, log_fn=None,
                    dedup_floors=True):
    """단일 SHP 파일을 처리하고 type별 SHP 출력.

    dedup_floors=True 면 동일 모양의 연속 층을 단일 record로 병합 (floor_from~floor_to).

    Returns: dict {
        "shp_path": str,
        "outputs": {type_int: (out_shp_path, feature_count)},
        "parcel_total": int,
        "parcel_processed": int,
        "warnings": int,
        "raw_floor_count": {type_int: int},   # dedup 전 가상 카운트
    }
    """
    if log_fn is None:
        log_fn = print

    stem = os.path.splitext(os.path.basename(shp_path))[0]
    log_fn("[{}] >>> {} 시작".format(_ts(), stem))

    repo = shp_to_lot.LotRepository(shp_path)
    target_lots = [
        lot for lot in repo.lots
        if utils.normalize_landuse_code(getattr(lot, "landuse_code", ""))
        in constants.RESIDENTIAL_GENERAL_CODES
    ]
    if target_lot_limit is not None and target_lot_limit > 0:
        target_lots = target_lots[:target_lot_limit]

    n_target = len(target_lots)
    log_fn("[{}] {}: 대상 필지 {}개".format(_ts(), stem, n_target))

    # 타입별 Writer 동시 오픈
    stamp = _now_stamp()
    writers = {}
    out_paths = {}
    counts = {}
    out_base_paths = {}
    for t in types:
        label = constants.SCENARIO_LABELS.get(int(t), str(int(t)))
        base = os.path.join(
            out_dir, "{}_buildable_type{}_{}_{}".format(stem, t, label, stamp)
        )
        w, out_shp = _open_writer(base)
        writers[t] = w
        out_paths[t] = out_shp
        out_base_paths[t] = base
        counts[t] = 0

    warnings = 0
    processed = 0
    total_height = FLOOR_HEIGHT_M * MAX_FLOOR
    raw_floor_count = {t: 0 for t in types}  # dedup 전 (필지 × 층) 카운트

    for idx, lot in enumerate(target_lots):
        if progress_every and idx > 0 and idx % progress_every == 0:
            log_fn("[{}] {}: 진행 {}/{} (warn {})".format(
                _ts(), stem, idx, n_target, warnings
            ))

        other_lots = repo.get_other_lots(lot)

        # calculator 1회 init, 모든 (type, floor) 재사용
        try:
            calc = northsky.create_calculator(
                target_lot=lot,
                neighbor_lots=other_lots,
                max_distance=total_height,
                height=FLOOR_HEIGHT_M,
                ratio=constants.DEFAULT_RATIO,
            )
        except Exception:
            warnings += 1
            continue

        if calc.lot_region_inward is None:
            warnings += 1
            continue

        pnu_str = str(getattr(lot, "pnu", ""))[:20]
        zone_str = str(getattr(lot, "landuse_code", ""))[:3]
        type_int_cache = {t: int(t) for t in types}

        for t in types:
            t_int = type_int_cache[t]
            pending = None  # {floor_from, floor_to, ring, hash, area, seg_cnt}

            for floor in range(1, MAX_FLOOR + 1):
                height_top = FLOOR_HEIGHT_M * floor
                try:
                    calc.compute(height=height_top, type=t_int)
                    buildable = calc.buildable_boundary
                except Exception:
                    warnings += 1
                    # 이 타입의 남은 층 스킵 + pending flush
                    if pending is not None:
                        _flush_pending(writers[t], pending, pnu_str, t_int, zone_str)
                        counts[t] += 1
                        pending = None
                    break

                ring = _curve_to_ring(buildable) if buildable else None
                if not ring:
                    # 유효 폴리곤 없음 -- pending 끊고 다음 층으로
                    if pending is not None:
                        _flush_pending(writers[t], pending, pnu_str, t_int, zone_str)
                        counts[t] += 1
                        pending = None
                    continue

                raw_floor_count[t] += 1
                try:
                    area = float(utils.get_area(buildable))
                except Exception:
                    area = 0.0

                if not dedup_floors:
                    # 매 층 단일 record (floor_from == floor_to)
                    one = {
                        "floor_from": floor, "floor_to": floor,
                        "ring": ring, "hash": None,
                        "area": area, "seg_cnt": len(calc.base_segments or []),
                    }
                    _flush_pending(writers[t], one, pnu_str, t_int, zone_str)
                    counts[t] += 1
                    continue

                # --- dedup 경로 ---
                h = _ring_hash(ring)
                if pending is None:
                    pending = {
                        "floor_from": floor, "floor_to": floor,
                        "ring": ring, "hash": h,
                        "area": area,
                        "seg_cnt": len(calc.base_segments or []),
                    }
                elif pending["hash"] == h and pending["floor_to"] == floor - 1:
                    # 직전 층과 동일 모양 + 연속 -> 범위만 확장
                    pending["floor_to"] = floor
                else:
                    # 모양 바뀜 -> 이전 flush, 새 pending 시작
                    _flush_pending(writers[t], pending, pnu_str, t_int, zone_str)
                    counts[t] += 1
                    pending = {
                        "floor_from": floor, "floor_to": floor,
                        "ring": ring, "hash": h,
                        "area": area,
                        "seg_cnt": len(calc.base_segments or []),
                    }

            # 층 루프 끝 -> 잔여 pending flush
            if pending is not None:
                _flush_pending(writers[t], pending, pnu_str, t_int, zone_str)
                counts[t] += 1

        processed += 1

    # writer close + prj 작성
    for t in types:
        try:
            writers[t].close()
            _write_prj(out_base_paths[t])
        except Exception as e:
            log_fn("[{}] {}: type {} writer close 실패: {}".format(
                _ts(), stem, t, e
            ))

    for t in types:
        raw = raw_floor_count[t]
        dd = counts[t]
        ratio_txt = ""
        if dedup_floors and raw > 0:
            ratio_txt = "  (dedup: {} -> {}, {:.0f}% reduction)".format(
                raw, dd, (1 - dd / float(raw)) * 100
            )
        log_fn("[{}] {}: type {} -> {} features{}".format(
            _ts(), stem, t, dd, ratio_txt
        ))

    return {
        "shp_path": shp_path,
        "outputs": {t: (out_paths[t], counts[t]) for t in types},
        "parcel_total": n_target,
        "parcel_processed": processed,
        "warnings": warnings,
        "raw_floor_count": raw_floor_count,
    }


def export_folder(folder_path, types=DEFAULT_TYPES,
                  target_lot_limit=None, progress_every=1000, log_fn=None,
                  dedup_floors=True, skip_if_done=True):
    """폴더 내 모든 .shp 파일 처리.

    Returns: dict {
        "folder": str,
        "out_dir": str,
        "results": [process_one_shp() results...],
        "logs": [str, ...]
    }
    """
    logs = []
    log_file_handle = [None]  # list로 closure 공유

    def _log(msg):
        logs.append(msg)
        fh = log_file_handle[0]
        if fh is not None:
            try:
                fh.write(msg + "\n")
                fh.flush()  # 라인 단위 즉시 flush (실시간 tail -f 가능)
            except Exception:
                pass
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    if not folder_path or not os.path.isdir(folder_path):
        _log("[{}] [ERROR] 폴더가 아닙니다: {}".format(_ts(), folder_path))
        return {"folder": folder_path, "out_dir": None, "results": [], "logs": logs,
                "log_file": None}

    shp_files = sorted([
        os.path.join(folder_path, n)
        for n in os.listdir(folder_path)
        if n.lower().endswith(".shp")
    ])
    if not shp_files:
        _log("[{}] [ERROR] .shp 파일 없음: {}".format(_ts(), folder_path))
        return {"folder": folder_path, "out_dir": None, "results": [], "logs": logs,
                "log_file": None}

    out_dir = os.path.join(folder_path, "result_geom")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # 로그 파일 오픈 (line-buffered)
    log_file_path = os.path.join(out_dir, "run_log_{}.txt".format(_now_stamp()))
    try:
        log_file_handle[0] = open(log_file_path, "w", encoding="utf-8", buffering=1)
    except Exception as e:
        print("[WARN] log file open 실패: {}".format(e))
        log_file_path = None

    _log("[{}] [START] {} shp files, types={}".format(
        _ts(), len(shp_files), list(types)
    ))
    _log("[{}] 출력 폴더: {}".format(_ts(), out_dir))
    if log_file_path:
        _log("[{}] 로그 파일: {}".format(_ts(), log_file_path))

    results = []
    skipped = 0
    for i, shp_path in enumerate(shp_files, 1):
        stem = os.path.splitext(os.path.basename(shp_path))[0]

        # 이미 완료된 구는 스킵
        if skip_if_done and _is_district_done(out_dir, stem):
            _log("[{}] ({}/{}) [SKIP] {} -- .done 마커 존재".format(
                _ts(), i, len(shp_files), os.path.basename(shp_path)
            ))
            skipped += 1
            continue

        _log("[{}] ({}/{}) {}".format(
            _ts(), i, len(shp_files), os.path.basename(shp_path)
        ))
        try:
            res = process_one_shp(
                shp_path,
                out_dir,
                types=types,
                target_lot_limit=target_lot_limit,
                progress_every=progress_every,
                log_fn=_log,
                dedup_floors=dedup_floors,
            )
            results.append(res)
            # 성공 시 .done 마커 작성
            _write_done_marker(out_dir, stem, {
                "parcel_total": res.get("parcel_total", 0),
                "parcel_processed": res.get("parcel_processed", 0),
                "warnings": res.get("warnings", 0),
                "type1_features": res.get("outputs", {}).get(1, ("", 0))[1] if 1 in types else 0,
                "type2_features": res.get("outputs", {}).get(2, ("", 0))[1] if 2 in types else 0,
            })
        except Exception as e:
            _log("[{}] [FAIL] {} : {}".format(_ts(), os.path.basename(shp_path), e))

    # 집계
    total_features = {t: 0 for t in types}
    total_raw = {t: 0 for t in types}
    total_parcels = 0
    total_warn = 0
    for r in results:
        total_parcels += r.get("parcel_total", 0)
        total_warn += r.get("warnings", 0)
        for t, (_p, c) in r.get("outputs", {}).items():
            total_features[t] = total_features.get(t, 0) + c
        for t, n in r.get("raw_floor_count", {}).items():
            total_raw[t] = total_raw.get(t, 0) + n

    _log("[{}] [DONE] 처리 {}개 / 스킵 {}개, 누적 필지 {}, warnings {}".format(
        _ts(), len(results), skipped, total_parcels, total_warn
    ))
    for t in types:
        n = total_features[t]
        raw = total_raw.get(t, 0)
        if dedup_floors and raw > 0:
            _log("[{}] type {} 총 features: {} (raw {} -> dedup {:.1f}% reduction)".format(
                _ts(), t, n, raw, (1 - n / float(raw)) * 100
            ))
        else:
            _log("[{}] type {} 총 features: {}".format(_ts(), t, n))

    # 로그 파일 close
    if log_file_handle[0] is not None:
        try:
            log_file_handle[0].close()
        except Exception:
            pass

    return {
        "folder": folder_path,
        "out_dir": out_dir,
        "results": results,
        "logs": logs,
        "log_file": log_file_path,
    }
