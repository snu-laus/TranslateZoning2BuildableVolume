"""SHP -> Lot selection helpers.

`LotRepository`는 SHP를 1회 로드하고,
- target_lot 조회
- other_lots 조회(옵션: bbox 사전 필터)
를 제공한다.
"""

import os
import sys
from typing import List, Optional

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
    from . import utils, constants
except Exception:
    import utils
    import constants
import importlib

importlib.reload(utils)
importlib.reload(constants)


def print_lot_info(lot):
    # type: (utils.Lot) -> None
    """필지 핵심 속성을 콘솔에 출력한다."""
    print(f"PNU: {lot.pnu}")
    print(f"지목: {lot.jimok}")
    print(f"면적: {lot.area:.2f} ㎡")
    print(f"용도: {lot.landuse}")


class LotRepository(object):
    def __init__(self, shp_path):
        # type: (str) -> None
        """SHP를 로드해 lot/road 저장소를 초기화한다."""
        if not shp_path or not os.path.isfile(shp_path):
            raise FileNotFoundError("SHP 파일을 찾을 수 없습니다: {}".format(shp_path))

        self.shp_path = shp_path
        shapes, records, fields = utils.read_shp_file(shp_path)
        parcels = utils.get_parcels_from_shapes(shapes, records, fields)
        self.lots, self.roads = utils.classify_parcels(parcels)
        self._bbox_cache = {}

    def get_target_lot(self, pnu):
        # type: (str) -> Optional[utils.Lot]
        """PNU에 해당하는 대상 lot를 찾아 반환한다."""
        if pnu is None:
            return None
        pnu_text = str(pnu)
        return next((lot for lot in self.lots if str(lot.pnu) == pnu_text), None)

    def _get_bbox(self, lot):
        # type: (utils.Lot) -> Optional[geo.BoundingBox]
        """lot의 bbox를 계산하고 캐시에 저장해 재사용한다."""
        key = id(lot)
        cached = self._bbox_cache.get(key)
        if cached is not None:
            return cached

        region = getattr(lot, "region", None)
        if not region:
            self._bbox_cache[key] = None
            return None

        try:
            bb = region.GetBoundingBox(True)
        except Exception:
            bb = None

        if bb is None or (hasattr(bb, "IsValid") and not bb.IsValid):
            self._bbox_cache[key] = None
            return None

        self._bbox_cache[key] = bb
        return bb

    def get_other_lots(self, target_lot):
        # type: (utils.Lot) -> List[utils.Lot]
        """대상 lot를 제외한 주변 lot 목록을 반환한다."""
        if target_lot is None:
            return []

        others = [lot for lot in self.lots if lot is not target_lot]

        bb_target = self._get_bbox(target_lot)
        if not bb_target:
            return others

        bb_query = geo.BoundingBox(bb_target.Min, bb_target.Max)
        dist = max(float(constants.PREFILTER_DISTANCE_M), 0.0)
        bb_query.Inflate(dist, dist, 0.0)

        return [
            lot
            for lot in others
            if utils.is_bbox_overlapping(bb_query, self._get_bbox(lot))
        ]

    def get_target_and_others(self, pnu):
        # type: (str) -> tuple
        """PNU로 대상 lot와 이외 lot 목록을 함께 반환한다."""
        target_lot = self.get_target_lot(pnu)
        if target_lot is None:
            raise ValueError("PNU '{}'에 해당하는 필지를 찾을 수 없습니다.".format(pnu))
        other_lots = self.get_other_lots(target_lot)
        return target_lot, other_lots


if __name__ == "__main__":
    ### 메인 실행 코드 ###
    # 인풋 값 설정
    # 파일 경로 읽기
    shp_path = globals().get("shp_path", None)
    if not shp_path or not os.path.isfile(shp_path):
        raise FileNotFoundError(f"SHP 파일을 찾을 수 없습니다: {shp_path}")
    # PNU 읽기
    pnu = globals().get("pnu", None)
    if not pnu:
        raise ValueError("PNU 값이 제공되지 않았습니다.")

    repo = LotRepository(shp_path)
    lots, roads = repo.lots, repo.roads
    # 데이터 확인
    print(f"대지: {len(lots)}개, 도로: {len(roads)}개")

    # 3. 입력된 PNU에 해당하는 필지 선택
    selected_lot = repo.get_target_lot(pnu)
    if not selected_lot:
        raise ValueError("PNU '{}'에 해당하는 필지를 찾을 수 없습니다.".format(pnu))
    print("선택된 PNU의 필지 정보")
    print_lot_info(selected_lot)

    target_lot = selected_lot
    other_lots = repo.get_other_lots(target_lot)
