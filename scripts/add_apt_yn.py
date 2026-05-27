"""토지특성정보(AL_D194) SHP에 APT_YN/BUILD_CNT/BUILD_USE_ 컬럼 추가.

용도별건물정보(AL_D198)와 PNU 기준 attribute join → 필지당 집계 → 컬럼 추가.

판별 규칙 (강남 Gangnam.shp 역공학으로 확정):
  APT_YN = "Y" if 주요용도코드 == "02000" (공동주택) else "N"

입력:
  - 토지특성정보 SHP 폴더 (예: data/_seoul/)
  - 용도별건물정보 SHP 폴더 (예: data/_seoul_buildings/)
출력:
  - data/_seoul_apt/ 에 APT_YN 추가된 SHP 25개

사용:
  python3 scripts/add_apt_yn.py
"""

import os
import sys
import time
import argparse
from collections import Counter

import geopandas as gpd
import pandas as pd


PARCEL_DIR_DEFAULT = "/Users/sanghoon/Documents/GitHub/TranslateZoning2BuildableVolume/data/_seoul"
BUILDING_DIR_DEFAULT = "/Users/sanghoon/Documents/GitHub/TranslateZoning2BuildableVolume/data/_seoul_buildings"
OUT_DIR_DEFAULT = "/Users/sanghoon/Documents/GitHub/TranslateZoning2BuildableVolume/data/_seoul_apt"

APT_CODE = "02000"  # 공동주택

SGG_NAMES = {
    '11110':'종로구','11140':'중구','11170':'용산구','11200':'성동구','11215':'광진구',
    '11230':'동대문구','11260':'중랑구','11290':'성북구','11305':'강북구','11320':'도봉구',
    '11350':'노원구','11380':'은평구','11410':'서대문구','11440':'마포구','11470':'양천구',
    '11500':'강서구','11530':'구로구','11545':'금천구','11560':'영등포구','11590':'동작구',
    '11620':'관악구','11650':'서초구','11680':'강남구','11710':'송파구','11740':'강동구',
}


def _ts():
    return time.strftime("%H:%M:%S")


def _read_shp(path, label=""):
    """cp949 우선 시도, utf-8 fallback."""
    for enc in ("cp949", "utf-8"):
        try:
            return gpd.read_file(path, encoding=enc)
        except UnicodeDecodeError:
            continue
        except Exception as e:
            # geopandas/fiona 일부 버전은 다른 예외 발생 -- 시도 다음 인코딩
            if enc == "utf-8":
                raise
            continue
    raise RuntimeError(f"인코딩 감지 실패: {path}")


def _get_sgg_code(filename):
    """AL_D194_11680_20260520.shp -> '11680'"""
    parts = os.path.basename(filename).split("_")
    return parts[2] if len(parts) >= 3 else None


def process_one(sgg_code, parcel_path, bldg_path, out_path):
    """단일 구 처리. 결과 dict 반환."""
    name = SGG_NAMES.get(sgg_code, "?")
    print(f"[{_ts()}] {name}({sgg_code}) 시작")
    t0 = time.time()

    parcels = _read_shp(parcel_path)
    bldgs = _read_shp(bldg_path)
    print(f"[{_ts()}]   parcels={len(parcels):,}  bldgs={len(bldgs):,}")

    # 건물을 PNU(A2) 기준으로 집계
    bldgs_clean = bldgs[["A2", "A24"]].copy()
    bldgs_clean["A24"] = bldgs_clean["A24"].astype(str).str.strip()
    bldgs_clean["A2"] = bldgs_clean["A2"].astype(str).str.strip()

    # APT_YN 판별을 위해 "이 필지에 공동주택이 하나라도 있는가?" 가 더 정확함
    agg = bldgs_clean.groupby("A2").agg(
        BUILD_CNT=("A2", "size"),
        BUILD_USE_=("A24", "first"),  # 대표 용도 (= 첫 건물의 코드)
        _has_apt=("A24", lambda s: (s == APT_CODE).any()),
    ).reset_index()
    agg["APT_YN"] = agg["_has_apt"].map({True: "Y", False: "N"})
    agg = agg.drop(columns=["_has_apt"])

    # parcels.A1 (PNU) = bldgs.A2 (PNU) 로 join
    parcels = parcels.copy()
    parcels["A1"] = parcels["A1"].astype(str).str.strip()

    merged = parcels.merge(
        agg, left_on="A1", right_on="A2", how="left",
        suffixes=("", "_y"),
    )
    # 건물 없는 필지는 default 값
    merged["APT_YN"] = merged["APT_YN"].fillna("N")
    merged["BUILD_CNT"] = merged["BUILD_CNT"].fillna(0).astype(int)
    merged["BUILD_USE_"] = merged["BUILD_USE_"].fillna("")

    # APT_YN 분포
    apt_counts = Counter(merged["APT_YN"])
    print(f"[{_ts()}]   APT_YN 분포: Y={apt_counts['Y']:,}  N={apt_counts['N']:,}")

    # 필요 컬럼만 남기기 (코드가 사용하는 A1/A11/A13 + 추가 컬럼)
    keep_cols = ["A1", "A11", "A13", "BUILD_CNT", "BUILD_USE_", "APT_YN", "geometry"]
    available = [c for c in keep_cols if c in merged.columns]
    slim = merged[available].copy()

    # 안전: 모든 string-likely 컬럼을 str로 강제 (dbf width 자동 추정 문제 회피)
    for c in ["A1", "A11", "A13", "BUILD_USE_", "APT_YN"]:
        if c in slim.columns:
            slim[c] = slim[c].astype(str)

    # 저장 (cp949) -- 경고 억제 위해 stderr 캡처
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        slim.to_file(out_path, encoding="cp949")
    elapsed = time.time() - t0
    print(f"[{_ts()}]   -> {os.path.basename(out_path)}  ({elapsed:.1f}s)")

    return {
        "sgg_code": sgg_code,
        "name": name,
        "parcels": len(parcels),
        "bldgs": len(bldgs),
        "apt_y": apt_counts["Y"],
        "apt_n": apt_counts["N"],
        "elapsed_s": elapsed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parcel-dir", default=PARCEL_DIR_DEFAULT)
    parser.add_argument("--bldg-dir", default=BUILDING_DIR_DEFAULT)
    parser.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    parser.add_argument("--only", default=None, help="특정 시군구코드만 처리 (예: 11680)")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    parcel_shps = sorted(
        f for f in os.listdir(args.parcel_dir)
        if f.startswith("AL_D194_") and f.endswith(".shp")
    )

    # sgg_code -> parcel/bldg 경로 매핑
    print(f"[{_ts()}] === 시작: {len(parcel_shps)}개 SHP ===")
    print(f"[{_ts()}] 출력: {args.out_dir}")

    results = []
    for pshp in parcel_shps:
        sgg = _get_sgg_code(pshp)
        if args.only and sgg != args.only:
            continue
        ppath = os.path.join(args.parcel_dir, pshp)

        bldg_candidates = [
            f for f in os.listdir(args.bldg_dir)
            if f.startswith(f"AL_D198_{sgg}_") and f.endswith(".shp")
        ]
        if not bldg_candidates:
            print(f"[{_ts()}] [WARN] {sgg}: 건물 SHP 없음, 건너뜀")
            continue
        bpath = os.path.join(args.bldg_dir, bldg_candidates[0])

        out_name = pshp.replace(".shp", "_with_apt.shp")
        out_path = os.path.join(args.out_dir, out_name)

        try:
            res = process_one(sgg, ppath, bpath, out_path)
            results.append(res)
        except Exception as e:
            print(f"[{_ts()}] [FAIL] {sgg}: {e}")
            import traceback
            traceback.print_exc()

    # 요약
    print()
    print("=" * 80)
    print(f"{'구':<10} {'코드':<6} {'필지':>8} {'건물':>8} {'APT Y':>7} {'APT N':>8} {'시간':>7}")
    print("-" * 80)
    total_p = total_b = total_y = total_n = total_t = 0
    for r in results:
        print(f"{r['name']:<10} {r['sgg_code']:<6} {r['parcels']:>8,} {r['bldgs']:>8,} "
              f"{r['apt_y']:>7,} {r['apt_n']:>8,} {r['elapsed_s']:>6.1f}s")
        total_p += r["parcels"]; total_b += r["bldgs"]
        total_y += r["apt_y"]; total_n += r["apt_n"]; total_t += r["elapsed_s"]
    print("-" * 80)
    print(f"{'TOTAL':<10} {'':<6} {total_p:>8,} {total_b:>8,} {total_y:>7,} {total_n:>8,} {total_t:>6.1f}s")
    print()
    print(f"APT_YN=Y 비율: {total_y/(total_y+total_n)*100:.2f}%")


if __name__ == "__main__":
    main()
