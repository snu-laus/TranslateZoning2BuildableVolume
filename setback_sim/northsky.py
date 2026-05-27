# -*- coding: utf-8 -*-
"""North/South sky-exposure computation (정북/정남 사선)."""

try:
    from typing import List, Optional, Any
except ImportError:  # IronPython compatibility
    pass

import itertools
import math

import Rhino.Geometry as geo  # type: ignore

try:
    from . import utils  # type: ignore
except Exception:
    import utils  # type: ignore

try:
    from . import constants  # type: ignore
except Exception:
    import constants  # type: ignore

import importlib


importlib.reload(utils)


def is_sunlight_regulated_landuse_code(landuse_code):
    # type: (Any) -> bool
    """일조권 사선 제한 대상 용도지역 코드인지 반환.

    대상: 전용주거지역(11,12), 일반주거지역(13,14,15,17)
    """
    code = utils.normalize_landuse_code(landuse_code)
    if not code:
        return False

    return code in {"11", "12", "13", "14", "15", "17"}


def create_calculator(
    target_lot,
    neighbor_lots,
    max_distance,
    height,
    ratio,
):
    # type: (Any, List[Any], float, float, float) -> "NorthSkyCalculator"
    """NorthSkyCalculator 생성에 필요한 인자를 정규화해 객체를 반환한다."""
    return NorthSkyCalculator(
        target_lot=target_lot,
        neighbor_lots=neighbor_lots,
        max_distance=float(max_distance),
        height=float(height),
        ratio=float(ratio),
    )


class NorthSkyCalculator(object):
    """정북사선을 고려한 기준선/건축가능영역 계산기.

    외부에서 호출하는 메서드는 `compute()` 하나만 사용한다.
    계산 결과는 멤버 변수에 저장된다.
    - `base_segments`: List[geo.Curve]
    - `buildable_boundary`: Optional[geo.Curve]
    """

    def __init__(
        self,
        target_lot,
        neighbor_lots,
        max_distance,
        height,
        ratio,
    ):
        # type: (Any, List[Any], float, float, float) -> None
        """계산에 필요한 입력과 상태를 초기화한다."""
        if target_lot is None:
            raise ValueError("target_lot is required.")
        if neighbor_lots is None:
            raise ValueError("neighbor_lots is required.")

        self.target_lot = target_lot
        self.neighbor_lots = list(neighbor_lots)

        self.lot_region = target_lot.region

        self.vec_exposure = geo.Vector3d(
            constants.DEFAULT_VEC_EXPOSURE_X,
            constants.DEFAULT_VEC_EXPOSURE_Y,
            constants.DEFAULT_VEC_EXPOSURE_Z,
        )
        self.max_distance = float(max_distance)

        self.height = float(height)
        self.ratio = float(ratio)

        self.neighbor_lots = self._prefilter_neighbor_lots(
            lot_region=self.lot_region,
            neighbor_lots=self.neighbor_lots,
            max_distance=self.max_distance,
        )

        self.neighbor_lot_crvs_without_gong = []
        for lot in self.neighbor_lots:
            region = lot.region
            if region:
                self.neighbor_lot_crvs_without_gong.append(region)

        self.base_segments = []  # type: List[geo.Curve]
        self.buildable_boundary = None  # type: Optional[geo.Curve]
        self.buildable_boundary_raw = None  # type: Optional[geo.Curve]
        self.lot_region_inward = self.lot_region  # type: geo.Curve
        self.qa_has_road20m_exclusion = False
        self.qa_road20m_exclusion_owner_pnus = set()
        self.qa_has_apartment_centerline = False
        self.qa_apartment_centerline_owner_pnus = set()

        self.base_segments = self._compute_base_segments(
            lot_region=self.lot_region,
            neighbor_lots=self.neighbor_lots,
        )

        if constants.PARCEL_INWARD_OFFSET_M > 0:
            self.lot_region_inward = utils.offset_region_inward(
                self.lot_region, constants.PARCEL_INWARD_OFFSET_M
            )

    def compute(self, height=None, type=None):
        # type: (Optional[float], Optional[int]) -> None
        """주어진 높이에서 buildable boundary를 계산한다."""
        if height is not None:
            self.height = float(height)

        if type is None:
            raise ValueError(
                "type is required. (1=A현행, 2=B개정안, 3=C, 4=D, 5=E, 6=F)"
            )

        calc_type = int(type)

        self.buildable_boundary_raw = self._compute_buildable_boundary(
            region=self.lot_region,
            base_segments=self.base_segments,
            height=self.height,
            setback_type=calc_type,
        )

        if not self.buildable_boundary_raw or not self.lot_region_inward:
            self.buildable_boundary = None
            return

        intersections = utils.get_intersection_regions(
            [self.buildable_boundary_raw], [self.lot_region_inward]
        )
        self.buildable_boundary = (
            max(intersections, key=utils.get_area) if intersections else None
        )

    def get_cutter_breps(self, setback_type):
        # type: (int) -> List[geo.Brep]
        """법규 시각화용 커터 Brep(수직/수평/사선)을 생성한다."""
        mode = int(setback_type)
        params = constants.SCENARIO_PARAMS.get(mode)
        if params is None:
            raise ValueError("Unsupported setback_type: {}".format(setback_type))

        h = float(constants.CUTTER_VISUAL_MAX_HEIGHT_M)
        cut_distance = self._get_setback_at_height(h, mode)
        vertical_height = min(h, constants.HEIGHT_LIMIT_M)

        mid_threshold = params["mid_threshold"]
        has_mid_tier = mid_threshold is not None
        slope_ratio = float(params["slope_ratio"])

        breps = []
        for base_seg in self.base_segments:
            if vertical_height > constants.TOL:
                seg_vertical_top = utils.move_crv(
                    base_seg, geo.Vector3d(0.0, 0.0, vertical_height)
                )
                vertical_breps = geo.Brep.CreateFromLoft(
                    [base_seg, seg_vertical_top],
                    geo.Point3d.Unset,
                    geo.Point3d.Unset,
                    geo.LoftType.Straight,
                    False,
                )
                if vertical_breps:
                    for brep in vertical_breps:
                        if brep and brep.IsValid:
                            breps.append(brep)

            if h <= constants.HEIGHT_LIMIT_M or cut_distance <= constants.TOL:
                continue

            seg_limit = utils.move_crv(
                base_seg, geo.Vector3d(0.0, 0.0, constants.HEIGHT_LIMIT_M)
            )
            limit_distance = self._get_setback_at_height(constants.HEIGHT_LIMIT_M, mode)
            seg_limit_inset = self._move_curve_inward(
                seg_limit,
                limit_distance,
            )

            step_strip = utils.make_closed_crv_from_crv_crv(seg_limit, seg_limit_inset)
            if step_strip and step_strip.IsValid:
                step_breps = geo.Brep.CreatePlanarBreps(step_strip, constants.TOL)
                if step_breps:
                    for brep in step_breps:
                        if brep and brep.IsValid:
                            breps.append(brep)

            if has_mid_tier and h > float(mid_threshold):
                fixed_h = float(mid_threshold)
                fixed_distance = self._get_setback_at_height(fixed_h, mode)

                seg_limit_fixed_inset = self._move_curve_inward(
                    seg_limit, fixed_distance
                )
                jump_strip = utils.make_closed_crv_from_crv_crv(
                    seg_limit_inset, seg_limit_fixed_inset
                )
                if jump_strip and jump_strip.IsValid:
                    jump_breps = geo.Brep.CreatePlanarBreps(jump_strip, constants.TOL)
                    if jump_breps:
                        for brep in jump_breps:
                            if brep and brep.IsValid:
                                breps.append(brep)

                seg_fixed = utils.move_crv(base_seg, geo.Vector3d(0.0, 0.0, fixed_h))
                seg_fixed_inset = self._move_curve_inward(seg_fixed, fixed_distance)

                fixed_vertical_breps = geo.Brep.CreateFromLoft(
                    [seg_limit_fixed_inset, seg_fixed_inset],
                    geo.Point3d.Unset,
                    geo.Point3d.Unset,
                    geo.LoftType.Straight,
                    False,
                )
                if fixed_vertical_breps:
                    for brep in fixed_vertical_breps:
                        if brep and brep.IsValid:
                            breps.append(brep)

                fixed_post_distance = fixed_h * slope_ratio
                seg_fixed_post_inset = self._move_curve_inward(
                    seg_fixed, fixed_post_distance
                )
                fixed_jump_strip = utils.make_closed_crv_from_crv_crv(
                    seg_fixed_inset, seg_fixed_post_inset
                )
                if fixed_jump_strip and fixed_jump_strip.IsValid:
                    fixed_jump_breps = geo.Brep.CreatePlanarBreps(
                        fixed_jump_strip, constants.TOL
                    )
                    if fixed_jump_breps:
                        for brep in fixed_jump_breps:
                            if brep and brep.IsValid:
                                breps.append(brep)

                seg_top = utils.move_crv(base_seg, geo.Vector3d(0.0, 0.0, h))
                seg_top_inset = self._move_curve_inward(seg_top, cut_distance)

                p0 = seg_fixed_post_inset.PointAtStart
                p1 = seg_fixed_post_inset.PointAtEnd
                q0 = seg_top_inset.PointAtStart
                q1 = seg_top_inset.PointAtEnd
            else:
                seg_top = utils.move_crv(base_seg, geo.Vector3d(0.0, 0.0, h))
                seg_top_inset = self._move_curve_inward(seg_top, cut_distance)

                p0 = seg_limit_inset.PointAtStart
                p1 = seg_limit_inset.PointAtEnd
                q0 = seg_top_inset.PointAtStart
                q1 = seg_top_inset.PointAtEnd

            direct = p0.DistanceTo(q0) + p1.DistanceTo(q1)
            crossed = p0.DistanceTo(q1) + p1.DistanceTo(q0)
            if crossed < direct:
                q0, q1 = q1, q0

            slope_brep = geo.Brep.CreateFromCornerPoints(p0, p1, q1, q0, constants.TOL)
            if slope_brep and slope_brep.IsValid:
                breps.append(slope_brep)

        return breps

    def _get_setback_at_height(self, height, setback_type):
        # type: (float, int) -> float
        """입력 높이에서 적용해야 할 setback 깊이를 반환한다.

        SCENARIO_PARAMS에 정의된 시나리오 파라미터를 사용:
          - setback_type 1 (A): 현행 법규
          - setback_type 2 (B): 개정안 (기준 높이 17m)
          - setback_type 3 (C): 가상 시나리오 (기준 높이 14m)
          - setback_type 4 (D): 가상 시나리오 (기준 높이 20m)
          - setback_type 5 (E): 가상 시나리오 (저층 이격 2.0m)
          - setback_type 6 (F): 가상 시나리오 (사선 비율 h×0.4)
        """
        mode = int(setback_type)
        params = constants.SCENARIO_PARAMS.get(mode)
        if params is None:
            raise ValueError("Unsupported setback_type: {}".format(setback_type))

        if height <= float(params["low_threshold"]):
            return float(params["low_setback"])

        mid_threshold = params["mid_threshold"]
        if mid_threshold is not None and height <= float(mid_threshold):
            return float(params["mid_setback"])

        return float(height) * float(params["slope_ratio"])

    def _move_curve_inward(self, crv, distance):
        # type: (geo.Curve, float) -> geo.Curve
        """노출 반대 방향으로 커브를 distance만큼 평행 이동한다."""
        move_vec = geo.Vector3d(-self.vec_exposure)
        move_vec.Unitize()
        move_vec *= distance
        return utils.move_crv(crv, move_vec)

    def _make_cutter_strip(
        self,
        base_seg,
        height,
        setback_type,
    ):
        # type: (geo.Curve, float, int) -> Optional[geo.Curve]
        """base segment와 setback 선으로 차집합용 strip 커브를 만든다."""
        cut_distance = self._get_setback_at_height(height, setback_type)
        if cut_distance <= constants.TOL:
            return None

        moved = self._move_curve_inward(base_seg, cut_distance)
        strip = utils.make_closed_crv_from_crv_crv(base_seg, moved)
        if not strip or not strip.IsValid:
            return None
        return strip

    def _get_target_segs(self, boundary, vec, tol=math.radians(1)):
        # type: (geo.Curve, geo.Vector3d, float) -> List[geo.Curve]
        """경계 세그먼트 중 vec 방향 조건을 만족하는 대상 세그먼트를 추린다."""
        targets = []
        for seg in utils.explode(boundary):
            vec_in = utils.get_inside_perp_vec(seg, boundary)
            if vec * vec_in < math.sin(tol):
                continue
            targets.append(seg)
        return targets

    def _get_exposure_base_segs(self, seg, y_vec, neighbor_crvs, max_height):
        # type: (geo.Curve, geo.Vector3d, List[geo.Curve], float) -> List[geo.Curve]
        """
        description: 노출 기준선(seg)에서 노출된 영역의 경계선 세그먼트들을 계산한다.

        seg: 노출 기준선이 될 세그먼트
        y_vec: 노출 방향 벡터 (예: 정북사선이면 (0,1,0))
        neighbor_crvs: 세그먼트에서 노출된 영역과 겹치는 이웃 lot들의 경계선들
        max_height: 노출 기준선에서 최대 높이 (세그먼트에서 max_height만큼 떨어진 곳까지 노출 영역을 계산한다.)

        return: 노출 기준선에서 노출된 영역의 경계선 세그먼트들
        """
        x_vec = geo.Vector3d(y_vec)
        x_vec.Rotate(constants.ANGLE_90_DEGREE, geo.Vector3d.ZAxis)
        plane = geo.Plane(seg.PointAtStart, x_vec, y_vec)

        base_interval = utils.get_square_domain_from_seg(seg, plane).x_interval
        if base_interval.IsIncreasing:
            base_intervals = [base_interval]
        else:
            base_interval.Swap()
            base_intervals = [base_interval]

        # 노출 방향(y_vec)으로 영향권 영역을 만든다.
        region = utils.get_rect_from_seg(seg, y_vec, max_height).crv

        # region과 겹치는 이웃토지, 즉 일조권 계산의 영향권 토지들만 남겨둔다.
        filtered_neighbor_lots = [
            crv for crv in neighbor_crvs if utils.has_region_intersection(region, crv)
        ]
        if not filtered_neighbor_lots:
            return []

        # 깔금한 지오메트리 연산을위해 인접대지를 한번에 합친다.
        intersections = utils.get_intersection_regions(
            [region], utils.get_union_regions(filtered_neighbor_lots)
        )

        if not intersections:
            return []

        vertices = list(
            itertools.chain(
                *[utils.get_vertices(crv) for crv in filtered_neighbor_lots]
            )
        )

        # 아래의 과정을 거쳐 다시한번 깔금한 정북사선 기준 세그먼트들을 구한다.
        dict_domain = {}
        for intersection in intersections:
            for target in self._get_target_segs(intersection, y_vec):
                pts_cutter = [
                    v for v in vertices if utils.is_pt_on_crv(v, target, constants.TOL)
                ]
                if pts_cutter:
                    target_segs = utils.split_crv_from_pts(
                        target, pts_cutter, constants.TOL, constants.TOL
                    )
                else:
                    target_segs = [target]
                for target_seg in target_segs:
                    square_domain = utils.get_square_domain_from_seg(target_seg, plane)
                    dict_domain[square_domain] = target_seg

        # 노출 기준선(seg)에서 노출된 영역의 경계선 세그먼트들 중에서, seg의 양 끝점에서 시작하는 세그먼트들을 우선적으로 선택한다.
        segs_front = []
        for square_domain in sorted(dict_domain.keys()):
            diff_intervals = utils.subtract_interval(
                base_intervals, square_domain.x_interval
            )
            if (
                sum(i.Length for i in base_intervals)
                - sum(i.Length for i in diff_intervals)
            ) < constants.TOL:
                continue

            segs_front.append(dict_domain[square_domain])
            if (
                not diff_intervals
                or sum(i.Length for i in diff_intervals) < constants.TOL
            ):
                break

            base_intervals = diff_intervals

        return segs_front

    def _get_centered_seg(self, seg_base, seg_exposure, vec):
        # type: (geo.Curve, geo.Curve, geo.Vector3d) -> geo.Curve
        """노출 세그먼트를 기준 세그먼트 중앙 기준으로 재배치한다."""
        pts = []
        for pt in (seg_exposure.PointAtStart, seg_exposure.PointAtEnd):
            if utils.is_pt_on_crv(pt, seg_base):
                pts.append(pt)
            else:
                pt_projected = utils.get_pt_from_pt_to_crvs(pt, -vec, [seg_base])
                if not pt_projected:
                    pts.append(pt)
                else:
                    pts.append((pt + pt_projected) / 2)
        return geo.LineCurve(pts[0], pts[1])

    def _filter_short_segs(self, segs, vec_in):
        # type: (List[geo.Curve], geo.Vector3d) -> List[geo.Curve]
        """유효 길이 미만 세그먼트를 제거하고 세그먼트를 정리한다."""
        vec_check = geo.Vector3d(vec_in)
        vec_check.Rotate(constants.ANGLE_90_DEGREE, geo.Vector3d.ZAxis)

        filtered = []
        for crv in utils.get_joined_crvs(segs):
            if math.fabs(vec_check * (crv.PointAtStart - crv.PointAtEnd)) < 0.5:
                continue
            filtered += utils.explode(crv)

        return filtered

    def _filter_excluded_segs(self, lot_region, seg_base, segs):
        # type: (geo.Curve, geo.Curve, List[geo.Curve]) -> List[geo.Curve]
        """기준 규칙에 따라 제외해야 할 세그먼트를 걸러낸다."""
        filtered = []
        for seg in segs:
            if utils.is_seg_on_crv(seg, seg_base):
                filtered.append(seg)
                continue
            if utils.is_seg_on_crv(seg, lot_region):
                continue
            filtered.append(seg)
        return filtered

    def _get_curve_bbox(self, crv):
        # type: (Any) -> Optional[geo.BoundingBox]
        """커브의 유효한 bounding box를 반환한다."""
        if not crv:
            return None
        try:
            bb = crv.GetBoundingBox(True)
        except Exception:
            try:
                bb = crv.GetBoundingBox(geo.Plane.WorldXY)
            except Exception:
                bb = None
        if bb is None:
            return None
        if hasattr(bb, "IsValid") and not bb.IsValid:
            return None
        return bb

    def _prefilter_neighbor_lots(self, lot_region, neighbor_lots, max_distance):
        # type: (geo.Curve, List[Any], float) -> List[Any]
        """bbox 기반 거리 조건으로 이웃 필지를 사전 필터링한다."""
        bb_target = self._get_curve_bbox(lot_region)
        if not bb_target:
            return [lot for lot in (neighbor_lots or []) if lot is not None]

        bb_query = geo.BoundingBox(bb_target.Min, bb_target.Max)
        inflate_dist = max(float(max_distance), constants.TOL)
        bb_query.Inflate(inflate_dist, inflate_dist, 0.0)

        filtered = []
        for lot in neighbor_lots or []:
            region = lot.region
            bb = self._get_curve_bbox(region)
            if utils.is_bbox_overlapping(bb_query, bb):
                filtered.append(lot)
        return filtered

    def _is_general_residential_apartment(self, lot):
        # type: (utils.Lot) -> bool
        """일반주거지역 공동주택 필지인지 판별한다."""
        if lot is None:
            return False
        if not bool(getattr(lot, "is_apartment", False)):
            return False
        code = utils.normalize_landuse_code(getattr(lot, "landuse_code", ""))
        return code in constants.RESIDENTIAL_GENERAL_CODES

    def _get_owner_lot_for_seg(self, seg, candidate_lots):
        # type: (geo.Curve, List[utils.Lot]) -> Optional[utils.Lot]
        """세그먼트를 소유한 후보 필지를 찾아 반환한다."""
        for lot in candidate_lots:
            region = lot.region
            if region and utils.is_seg_on_crv(seg, region):
                return lot

        best_lot = None
        best_overlap = 0.0
        for lot in candidate_lots:
            region = lot.region
            if not region:
                continue
            overlap_len = utils.get_overlapped_length(seg, region)
            if overlap_len > best_overlap:
                best_overlap = overlap_len
                best_lot = lot

        if best_overlap > constants.TOL:
            return best_lot
        return None

    def _is_road_centerline_case(self, seg_base, seg_exposure, owner_lot):
        # type: (geo.Curve, geo.Curve, Optional[utils.Lot]) -> bool
        """도로 중심선 기준 보정이 필요한 케이스인지 판별한다."""
        if not self._is_general_residential_apartment(owner_lot):
            return False

        vec_back = geo.Vector3d(self.vec_exposure)
        vec_back.Reverse()
        pt_mid = seg_exposure.PointAtNormalizedLength(0.5)
        pt_on_base = utils.get_pt_from_pt_to_crvs(pt_mid, vec_back, [seg_base])
        if not pt_on_base:
            return False

        return pt_mid.DistanceTo(pt_on_base) > constants.TOL

    def _get_gap_distance_from_base(self, seg_base, seg_exposure):
        # type: (geo.Curve, geo.Curve) -> float
        """노출 세그먼트와 기준 세그먼트 사이 거리를 계산한다."""
        vec_back = geo.Vector3d(self.vec_exposure)
        vec_back.Reverse()
        pt_mid = seg_exposure.PointAtNormalizedLength(0.5)
        pt_on_base = utils.get_pt_from_pt_to_crvs(pt_mid, vec_back, [seg_base])
        if not pt_on_base:
            return 0.0
        return pt_mid.DistanceTo(pt_on_base)

    def _compute_base_segments(self, lot_region, neighbor_lots):
        # type: (geo.Curve, List[Any]) -> List[geo.Curve]
        """법규/예외 규칙을 반영한 최종 기준 세그먼트 집합을 계산한다."""
        neighbor_lot_crvs_without_gong = [
            lot.region for lot in (neighbor_lots or []) if lot.region
        ]

        crvs_check = list(neighbor_lot_crvs_without_gong) + [lot_region]
        target_segs = self._get_target_segs(lot_region, -self.vec_exposure)

        # base_segments 계산 과정에서, 노출 기준선이 lot_region의 경계에 붙어있는 경우는 제외한다.
        result_bases = []  # type: List[geo.Curve]
        for seg_base in target_segs:
            segs_exposure = self._get_exposure_base_segs(
                seg_base, self.vec_exposure, crvs_check, self.max_distance
            )
            segs_filtered = self._filter_excluded_segs(
                lot_region=lot_region,
                seg_base=seg_base,
                segs=segs_exposure,
            )

            if not segs_filtered:
                continue

            for seg in segs_filtered:
                owner_lot = self._get_owner_lot_for_seg(seg, self.neighbor_lots)
                gap_distance = self._get_gap_distance_from_base(seg_base, seg)

                if owner_lot and not is_sunlight_regulated_landuse_code(
                    owner_lot.landuse_code
                ):
                    if gap_distance >= (
                        constants.ROAD_EXCLUSION_DISTANCE_M - constants.TOL
                    ):
                        self.qa_has_road20m_exclusion = True
                        if owner_lot.pnu:
                            self.qa_road20m_exclusion_owner_pnus.add(owner_lot.pnu)
                    continue

                if self._is_road_centerline_case(seg_base, seg, owner_lot):
                    self.qa_has_apartment_centerline = True
                    if owner_lot.pnu:
                        self.qa_apartment_centerline_owner_pnus.add(owner_lot.pnu)
                    result_bases.append(
                        self._get_centered_seg(seg_base, seg, self.vec_exposure)
                    )
                else:
                    result_bases.append(seg)

        filtered_segs = self._filter_short_segs(result_bases, self.vec_exposure)

        return filtered_segs

    def _compute_buildable_boundary(
        self,
        region,
        base_segments,
        height,
        setback_type,
    ):
        # type: (geo.Curve, List[geo.Curve], float, int) -> Optional[geo.Curve]
        """기준 세그먼트 커터를 차집합해 buildable boundary를 계산한다."""
        if not region:
            return None
        if not base_segments:
            return region

        cutters = []
        for base_seg in base_segments:
            strip = self._make_cutter_strip(base_seg, height, setback_type)
            if strip:
                cutters.append(strip)

        if not cutters:
            return region

        result_regions = utils.get_difference_regions_one_to_one(region, cutters)
        if not result_regions:
            return None

        result_region = max(result_regions, key=lambda r: utils.get_area(r))
        simplified = result_region.Simplify(
            geo.CurveSimplifyOptions.All, constants.TOL, 1.0
        )
        return simplified or result_region
