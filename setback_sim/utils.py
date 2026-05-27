"""Shared utilities for GH/Rhino scripts.

주의:
- 일부 기능(Shapefile 로딩)은 `pyshp`(shapefile) 설치가 필요합니다.
- 정북사선 등 순수 지오메트리 유틸을 쓰는 경우를 위해 `shapefile` import는 optional 입니다.
"""

import shapefile  # type: ignore
import os
import functools
from typing import List, Tuple, Any, Optional, Union
import ghpythonlib.components as ghcomp
import Rhino  # type: ignore
import Rhino.Geometry as geo
import math

import constants  # type: ignore

TOL = constants.TOL  # 연산 허용 오차
RAW_TOL = constants.RAW_TOL  # 원시 데이터 허용 오차
BIGNUM = 1000000000
CLIPPER_TOL = TOL
OP_TOL = TOL
ANGLE_TOL = math.pi / 180 * TOL


def convert_io_to_list(func):
    """인풋/아웃풋의 Curve를 리스트로 정규화한다."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        new_args = []
        for arg in args:
            if isinstance(arg, geo.Curve):
                arg = [arg]
            new_args.append(arg)

        result = func(*new_args, **kwargs)

        if isinstance(result, geo.Curve):
            result = [result]

        if hasattr(result, "__dict__"):
            for key, values in result.__dict__.items():
                if isinstance(values, geo.Curve):
                    setattr(result, key, [values])

        return result

    return wrapper


def not_allow_list_input(func):
    """리스트 입력을 허용하지 않는 함수 데코레이터."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if any(isinstance(arg, list) for arg in args):
            raise ValueError("{}'s must be one".format(func.__name__))
        return func(*args, **kwargs)

    return wrapper


class Comp:
    """레거시 comp 인터페이스 호환 래퍼 (ghpythonlib ClipperComponents 기반)."""

    @convert_io_to_list
    def _polyline_boolean(
        self,
        crvs0,
        crvs1,
        boolean_type=None,
        plane=None,
        tol=CLIPPER_TOL,
    ):
        # type: (List[geo.Curve], List[geo.Curve], int, Optional[geo.Plane], float) -> List[geo.Curve]
        if not crvs0 or not crvs1:
            return []
        if plane is None:
            plane = geo.Plane.WorldXY

        result = ghcomp.ClipperComponents.PolylineBoolean(
            crvs0, crvs1, boolean_type, plane, tol
        )
        return _normalize_ghcomp_result(result)

    def polyline_boolean_intersection(
        self,
        crvs0,
        crvs1,
        plane=None,
        tol=CLIPPER_TOL,
    ):
        # type: (Union[geo.Curve, List[geo.Curve]], Union[geo.Curve, List[geo.Curve]], Optional[geo.Plane], float) -> List[geo.Curve]
        return self._polyline_boolean(crvs0, crvs1, 0, plane, tol)

    def polyline_boolean_union(
        self,
        crvs0,
        crvs1,
        plane=None,
        tol=CLIPPER_TOL,
    ):
        # type: (Union[geo.Curve, List[geo.Curve]], Union[geo.Curve, List[geo.Curve]], Optional[geo.Plane], float) -> List[geo.Curve]
        return self._polyline_boolean(crvs0, crvs1, 1, plane, tol)

    def polyline_boolean_difference(
        self,
        crvs0,
        crvs1,
        plane=None,
        tol=CLIPPER_TOL,
    ):
        # type: (Union[geo.Curve, List[geo.Curve]], Union[geo.Curve, List[geo.Curve]], Optional[geo.Plane], float) -> List[geo.Curve]
        return self._polyline_boolean(crvs0, crvs1, 2, plane, tol)

    def polyline_boolean_xor(
        self,
        crvs0,
        crvs1,
        plane=None,
        tol=CLIPPER_TOL,
    ):
        # type: (Union[geo.Curve, List[geo.Curve]], Union[geo.Curve, List[geo.Curve]], Optional[geo.Plane], float) -> List[geo.Curve]
        return self._polyline_boolean(crvs0, crvs1, 3, plane, tol)

    @not_allow_list_input
    def _polyline_containment(self, region, pt, plane=None, tol=OP_TOL):
        # type: (geo.Curve, geo.Point3d, Optional[geo.Plane], float) -> int
        return ghcomp.ClipperComponents.PolylineContainment(region, pt, plane, tol)

    def polyline_containment_inside(self, region, pt, plane=None, tol=OP_TOL):
        # type: (geo.Curve, geo.Point3d, Optional[geo.Plane], float) -> bool
        return self._polyline_containment(region, pt, plane, tol) == 1

    def polyline_containment_on(self, region, pt, plane=None, tol=OP_TOL):
        # type: (geo.Curve, geo.Point3d, Optional[geo.Plane], float) -> bool
        return self._polyline_containment(region, pt, plane, tol) == -1

    def polyline_containment_outside(self, region, pt, plane=None, tol=OP_TOL):
        # type: (geo.Curve, geo.Point3d, Optional[geo.Plane], float) -> bool
        return self._polyline_containment(region, pt, plane, tol) == 0


comp = Comp()


class Parcel:
    """기본 필지 클래스"""

    def __init__(
        self,
        region: geo.Curve,
        pnu: str,
        jimok: str,
        landuse_code: str,
        landuse: str,
        apt_yn: str,
        hole_regions: List[geo.Curve],
    ):
        self.region = region  # 외부 경계 커브
        self.hole_regions = (
            hole_regions if hole_regions is not None else []
        )  # 내부 구멍들
        self.pnu = pnu
        self.jimok = jimok
        self.landuse_code = landuse_code
        self.landuse = landuse
        self.apt_yn = "" if apt_yn is None else str(apt_yn).strip().upper()
        self.is_apartment = self.apt_yn == "Y"
        self._area = None

    @property
    def area(self) -> float:
        """필지 면적 계산"""
        if self._area is None:
            outer_area = get_area(self.region)
            hole_area = get_area(self.hole_regions) if self.hole_regions else 0.0
            self._area = outer_area - hole_area
        return self._area

    def preprocess_curve(self) -> bool:
        """커브 전처리 (invalid 제거, 자체교차 제거, 단순화)"""
        if not self.region or not self.region.IsValid:
            return False

        # 자체교차 확인
        intersection_events = geo.Intersect.Intersection.CurveSelf(self.region, TOL)
        if intersection_events:
            simplified = self.region.Simplify(
                geo.CurveSimplifyOptions.All, RAW_TOL, 1.0
            )
            if simplified:
                self.region = simplified
            else:
                return False

        # 일반 단순화
        simplified = self.region.Simplify(geo.CurveSimplifyOptions.All, RAW_TOL, 1.0)
        if simplified:
            self.region = simplified

        # 내부 구멍들도 처리
        valid_holes = []
        for hole in self.hole_regions:
            if hole and hole.IsValid:
                simplified_hole = hole.Simplify(
                    geo.CurveSimplifyOptions.All, RAW_TOL, 1.0
                )
                if simplified_hole:
                    valid_holes.append(simplified_hole)
                else:
                    valid_holes.append(hole)
        self.hole_regions = valid_holes

        return True


class Road(Parcel):
    """도로 클래스"""

    pass


class Lot(Parcel):
    """대지 클래스"""

    def __init__(
        self,
        curve_crv: geo.Curve,
        pnu: str,
        jimok: str,
        landuse_code: str,
        landuse: str,
        apt_yn: str,
        hole_regions: List[geo.Curve] = None,
    ):
        super().__init__(
            curve_crv,
            pnu,
            jimok,
            landuse_code,
            landuse,
            apt_yn,
            hole_regions,
        )
        self.is_flag_lot = False  # 자루형 토지 여부
        self.has_road_access = False  # 도로 접근 여부


def read_shp_file(file_path):
    # type: (str) -> Tuple[List[Any], List[Any], List[str]]
    """shapefile을 읽어서 shapes와 records를 반환.

    인코딩은 utf-8 우선 시도, 실패 시 cp949(한국 표준 토지특성정보)로 fallback.
    """
    last_err = None
    for enc in ("utf-8", "cp949"):
        try:
            sf = shapefile.Reader(file_path, encoding=enc)
            shapes = sf.shapes()
            records = sf.records()  # 여기서 디코딩 발생
            fields = [field[0] for field in sf.fields[1:]]
            return shapes, records, fields
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise RuntimeError(
        "SHP 인코딩 감지 실패 (utf-8/cp949 모두 실패): {} ({})".format(file_path, last_err)
    )


def get_curve_from_points(
    points: List[Tuple[float, float]], start_idx: int, end_idx: int
) -> Optional[geo.PolylineCurve]:
    """점 리스트에서 특정 구간의 커브를 생성"""
    # 최소 3개의 점이 필요
    if end_idx - start_idx < 3:
        return None

    # 시작과 끝 점이 동일하지 않으면(닫혀있지 않으면) None 반환
    first_pt = points[start_idx]
    last_pt = points[end_idx - 1]
    if first_pt[0] != last_pt[0] or first_pt[1] != last_pt[1]:
        return None

    curve_points = [
        geo.Point3d(points[i][0], points[i][1], 0) for i in range(start_idx, end_idx)
    ]

    curve_crv = geo.PolylineCurve(curve_points)
    return curve_crv if curve_crv and curve_crv.IsValid else None


def get_part_indices(shape):
    # type: (Any) -> List[Tuple[int, int]]
    """shape의 각 파트의 시작과 끝 인덱스를 반환"""
    if not hasattr(shape, "parts") or len(shape.parts) <= 1:
        return [(0, len(shape.points))]

    parts = list(shape.parts) + [len(shape.points)]
    return [(parts[i], parts[i + 1]) for i in range(len(shape.parts))]


def get_intersection_points(
    curve_a: geo.Curve, curve_b: geo.Curve, tol: float = TOL
) -> List[geo.Point3d]:
    """두 커브 사이의 교차점을 계산합니다."""
    intersections = geo.Intersect.Intersection.CurveCurve(curve_a, curve_b, tol, tol)
    if not intersections:
        return []
    return [event.PointA for event in intersections]


def get_vertices(curve):
    # type: (geo.Curve) -> List[geo.Point3d]
    """커브의 모든 정점(Vertex)들을 추출합니다."""
    if not curve:
        return []
    vertices = [curve.PointAt(curve.SpanDomain(i)[0]) for i in range(curve.SpanCount)]
    if not curve.IsClosed:
        vertices.append(curve.PointAtEnd)
    return vertices


def explode(crv):
    # type: (geo.Curve) -> List[geo.Curve]
    """커브를 segment들로 분해합니다.

    - 폴리라인/폴리커브는 각 세그먼트로 분해
    - 일반 Curve는 가능한 경우 Polyline 근사 후 분해
    """
    if not crv:
        return []

    # PolylineCurve
    if isinstance(crv, geo.PolylineCurve):
        pl = crv.ToPolyline()
        pts = list(pl)
        if len(pts) < 2:
            return []
        return [geo.LineCurve(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

    # PolyCurve 등: DuplicateSegments를 먼저 시도
    segs = list(crv.DuplicateSegments())
    segs = [s for s in segs if s and s.IsValid and s.GetLength() > TOL]
    if segs:
        return segs

    # fallback: polyline 근사
    pl_crv = crv.ToPolyline(0, 0, RAW_TOL, RAW_TOL, RAW_TOL, 0, 0, 0, True)
    if pl_crv:
        return explode(pl_crv)

    return [crv]


def get_inside_perp_vec(
    seg: geo.Curve, boundary: geo.Curve, tol: float = TOL
) -> geo.Vector3d:
    """segment 기준, boundary 내부를 향하는 수직 벡터를 반환합니다."""
    a = seg.PointAtStart
    b = seg.PointAtEnd
    tan = b - a
    tan.Z = 0
    if tan.Length < 1e-9:
        return geo.Vector3d(0, 0, 0)
    tan.Unitize()

    perp = geo.Vector3d(-tan.Y, tan.X, 0)
    perp.Unitize()

    mid = seg.PointAtNormalizedLength(0.5)
    eps = max(tol * 10.0, 0.01)
    plane = geo.Plane.WorldXY

    inside = boundary.Contains(mid + perp * eps, plane, tol)
    if inside != geo.PointContainment.Outside:
        return perp

    return -perp


class _SquareDomain(object):
    """Plane 좌표계로 투영한 segment의 x/y 구간.

    - northsky 로직에서 dict key 및 sorting에 사용.
    - sorting은 y(사선 방향 거리) 오름차순 우선.
    """

    def __init__(self, x_min, x_max, y_min, y_max):
        # type: (float, float, float, float) -> None
        if x_min <= x_max:
            self.x_interval = geo.Interval(x_min, x_max)
        else:
            self.x_interval = geo.Interval(x_max, x_min)
        if y_min <= y_max:
            self.y_interval = geo.Interval(y_min, y_max)
        else:
            self.y_interval = geo.Interval(y_max, y_min)

        # 해시 안정화를 위한 스냅
        snap = 1e-6
        self._key = (
            round(self.x_interval.T0 / snap) * snap,
            round(self.x_interval.T1 / snap) * snap,
            round(self.y_interval.T0 / snap) * snap,
            round(self.y_interval.T1 / snap) * snap,
        )

    def __lt__(self, other):
        return (
            self.y_interval.T0,
            self.y_interval.T1,
            self.x_interval.T0,
            self.x_interval.T1,
        ) < (
            other.y_interval.T0,
            other.y_interval.T1,
            other.x_interval.T0,
            other.x_interval.T1,
        )

    def __hash__(self):
        return hash(self._key)

    def __eq__(self, other):
        return isinstance(other, _SquareDomain) and self._key == other._key


def _plane_uv(plane, pt):
    # type: (geo.Plane, geo.Point3d) -> Tuple[float, float]
    rc, u, v = plane.ClosestParameter(pt)
    if rc:
        return float(u), float(v)
    # fallback: manually project using axes
    op = pt - plane.Origin
    return float(op * plane.XAxis), float(op * plane.YAxis)


def get_square_domain_from_seg(seg, plane):
    # type: (geo.Curve, geo.Plane) -> _SquareDomain
    """segment를 plane 좌표계로 투영해 x/y 구간(domain)을 반환합니다."""
    pts = [seg.PointAtStart, seg.PointAtEnd]
    uvs = [_plane_uv(plane, p) for p in pts]
    xs = [uv[0] for uv in uvs]
    ys = [uv[1] for uv in uvs]
    return _SquareDomain(min(xs), max(xs), min(ys), max(ys))


class _RectRegion(object):
    def __init__(self, crv):
        # type: (geo.Curve) -> None
        self.crv = crv


def get_rect_from_seg(
    seg: geo.Curve, vec: geo.Vector3d, distance: float
) -> _RectRegion:
    """segment를 기준으로 vec 방향으로 distance만큼 뻗은 직사각형 region을 만듭니다."""
    a = seg.PointAtStart
    b = seg.PointAtEnd
    v = geo.Vector3d(vec)
    v.Z = 0
    if v.Length < 1e-9:
        v = geo.Vector3d(0, 0, 0)
    else:
        v.Unitize()
        v *= float(distance)

    a2 = a + v
    b2 = b + v
    poly = geo.Polyline([a, b, b2, a2, a])
    return _RectRegion(geo.PolylineCurve(poly))


def is_pt_on_crv(pt, crv, tol=TOL):
    # type: (geo.Point3d, geo.Curve, float) -> bool
    """pt가 crv 위에 있는지 확인"""
    rc, param = crv.ClosestPoint(pt, tol)
    if not rc:
        return False

    closest_pt = crv.PointAt(param)
    if closest_pt.DistanceTo(pt) <= tol:
        return True

    return False


def is_seg_on_crv(seg, crv, tol=TOL):
    # type: (geo.Curve, geo.Curve, float) -> bool
    """seg가 crv 위에 있는지 확인"""
    # seg의 끝점 밑 중점은 crv 위에 있어야 한다.
    for pt in (seg.PointAtStart, seg.PointAtEnd):
        if not is_pt_on_crv(pt, crv, tol):
            return False

    pt_mid = seg.PointAtNormalizedLength(0.5)
    if not is_pt_on_crv(pt_mid, crv, tol):
        return False

    return True


def get_overlapped_curves(
    curve_a: geo.Curve, curve_b: geo.Curve, tol: float = TOL
) -> List[geo.Curve]:
    """두 커브가 겹치는 구간의 커브들을 반환합니다."""
    intersection_points = get_intersection_points(curve_a, curve_b)
    if not intersection_points:
        return []

    params = [curve_a.SpanDomain(i)[0] for i in range(curve_a.SpanCount)]
    params += [curve_a.ClosestPoint(pt, tol)[1] for pt in intersection_points]
    shatter_result = ghcomp.Shatter(curve_a, params)

    if not shatter_result:
        return []

    # ghcomp.Shatter는 결과가 1개일 때 단일 Curve 객체를 반환할 수 있다.
    if isinstance(shatter_result, geo.Curve):
        shatter_result = [shatter_result]

    overlapped_segments = [seg for seg in shatter_result if is_seg_on_crv(seg, curve_b)]
    if not overlapped_segments:
        return []

    return geo.Curve.JoinCurves(overlapped_segments)


def get_overlapped_length(curve_a, curve_b):
    # type: (geo.Curve, geo.Curve) -> float
    """두 커브가 겹치는 총 길이를 계산합니다."""
    overlapped_curves = get_overlapped_curves(curve_a, curve_b)
    if not overlapped_curves:
        return 0.0
    return sum(crv.GetLength() for crv in overlapped_curves)


def get_curves_from_shape(
    shape: Any,
) -> Tuple[Optional[geo.PolylineCurve], List[geo.PolylineCurve]]:
    """shape에서 외부 경계와 내부 구멍 커브들을 추출"""
    boundary_region = None
    hole_regions = []

    part_indices = get_part_indices(shape)

    for i, (start_idx, end_idx) in enumerate(part_indices):
        curve_crv = get_curve_from_points(shape.points, start_idx, end_idx)
        if curve_crv:
            if i == 0:
                boundary_region = curve_crv
            else:
                hole_regions.append(curve_crv)

    # 단일 폴리곤이고 닫혀있지 않은 경우 처리
    if boundary_region is None and len(part_indices) == 1:
        points = [geo.Point3d(pt[0], pt[1], 0) for pt in shape.points]
        if len(points) >= 3:
            if points[0].DistanceTo(points[-1]) > TOL:
                points.append(points[0])
            curve_crv = geo.PolylineCurve(points)
            if curve_crv and curve_crv.IsValid:
                boundary_region = curve_crv

    return boundary_region, hole_regions


def get_field_value(
    record: List[Any], fields: List[str], field_name: str, default: str = "Unknown"
) -> str:
    """레코드에서 특정 필드값을 안전하게 추출.

    필드가 존재하지 않거나 값이 None이면 default 반환.
    """
    try:
        index = fields.index(field_name)
    except ValueError:
        return default
    value = record[index]
    return default if value is None else value


def create_parcel_from_shape(
    shape: Any, record: List[Any], fields: List[str]
) -> Optional[Parcel]:
    """shape에서 Parcel 객체 생성"""
    boundary_region, hole_regions = get_curves_from_shape(shape)

    if not boundary_region or not boundary_region.IsValid:
        return None

    pnu = get_field_value(record, fields, "A1")  # 구 PNU
    jimok = get_field_value(record, fields, "A11")  # 구 JIMOK
    raw_landuse_code = get_field_value(record, fields, "A13", default="")
    landuse_code = "" if raw_landuse_code is None else str(raw_landuse_code).strip()
    if landuse_code.endswith(".0"):
        landuse_code = landuse_code[:-2]
    landuse = constants.LANDUSE_MAP.get(landuse_code, constants.LANDUSE_UNKNOWN)
    apt_yn = get_field_value(record, fields, "APT_YN", default="N")
    apt_yn = "" if apt_yn is None else str(apt_yn).strip().upper()

    # jimok=='' 케이스: 외곽+구멍을 가진 도넛 형태의 도로/공유지 parcel이 분류 정보 없이
    # 들어오는 경우가 있다(예: 강남 SHP의 일부 본번). 현재 알고리즘은 hole_regions를
    # 사용하지 않으므로 Lot으로 분류되면 거대한 솔리드 polygon처럼 보여 정북사선 계산을
    # 오염시킨다. 따라서 빈 jimok도 Road로 처리해 lots에서 빼낸다.
    if jimok == "도로" or not jimok:
        parcel = Road(
            boundary_region,
            pnu,
            jimok,
            landuse_code,
            landuse,
            apt_yn,
            hole_regions,
        )
    else:
        parcel = Lot(
            boundary_region,
            pnu,
            jimok,
            landuse_code,
            landuse,
            apt_yn,
            hole_regions,
        )

    return parcel if parcel.preprocess_curve() else None


def has_intersection(
    curve_a: geo.Curve,
    curve_b: geo.Curve,
    plane: geo.Plane = geo.Plane.WorldXY,
    tol: float = TOL,
) -> bool:
    """두 커브가 교차하는지 여부를 확인합니다."""
    return geo.Curve.PlanarCurveCollision(curve_a, curve_b, plane, tol)


def has_region_intersection(
    region_a: geo.Curve, region_b: geo.Curve, tol: float = TOL
) -> bool:
    """두 영역(닫힌 커브)이 겹치거나 접하는지 검사합니다."""
    if not region_a or not region_b:
        return False

    plane = geo.Plane.WorldXY
    if geo.Curve.PlanarCurveCollision(region_a, region_b, plane, tol):
        return True

    # collision이 false여도 포함 관계일 수 있으니 샘플 점으로 검사
    pt_b = region_b.PointAtNormalizedLength(0.5)
    if region_a.Contains(pt_b, plane, tol) != geo.PointContainment.Outside:
        return True

    pt_a = region_a.PointAtNormalizedLength(0.5)
    if region_b.Contains(pt_a, plane, tol) != geo.PointContainment.Outside:
        return True

    return False


def normalize_landuse_code(value):
    # type: (Any) -> str
    """용도지역 코드를 비교 가능한 문자열 형태로 정규화한다."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def is_bbox_overlapping(bb_a, bb_b):
    # type: (Optional[geo.BoundingBox], Optional[geo.BoundingBox]) -> bool
    """두 bbox가 XY 기준으로 겹치는지 판별한다."""
    if not bb_a or not bb_b:
        return False
    return not (
        bb_a.Max.X < bb_b.Min.X
        or bb_a.Min.X > bb_b.Max.X
        or bb_a.Max.Y < bb_b.Min.Y
        or bb_a.Min.Y > bb_b.Max.Y
    )


def move_crv(crv_to_move, vec):
    # type: (geo.Curve, geo.Vector3d) -> geo.Curve
    """단일라인 crv를 vec 만큼 이동시킨다."""
    if not crv_to_move:
        return None
    crv_moved = crv_to_move.DuplicateCurve()
    crv_moved.Translate(vec)
    return crv_moved


def get_joined_crv(crvs, tol=TOL):
    # type: (List[geo.Curve], float) -> Optional[geo.Curve]
    """커브들을 조인해서 가능하면 단일 커브를 반환한다."""
    joined = get_joined_crvs(crvs, tol)
    if not joined:
        return None
    if len(joined) == 1:
        return joined[0]

    closed = [c for c in joined if c and c.IsClosed]
    if closed:
        return max(closed, key=lambda c: get_area(c))
    return max(joined, key=lambda c: c.GetLength())


def make_closed_crv_from_crv_crv(
    crv_a: geo.Curve, crv_b: geo.Curve, check_intersection: bool = True
) -> Optional[geo.Curve]:
    """두 커브의 끝을 연결해 닫힌 영역 커브를 만든다."""
    if not crv_a or not crv_b:
        return None

    crv_to_cap0 = geo.LineCurve(crv_a.PointAtStart, crv_b.PointAtStart)
    crv_to_cap1 = geo.LineCurve(crv_a.PointAtEnd, crv_b.PointAtEnd)
    if check_intersection and has_intersection(crv_to_cap0, crv_to_cap1):
        crv_to_cap0 = geo.LineCurve(crv_a.PointAtStart, crv_b.PointAtEnd)
        crv_to_cap1 = geo.LineCurve(crv_a.PointAtEnd, crv_b.PointAtStart)

    return get_joined_crv([crv_a, crv_b, crv_to_cap0, crv_to_cap1])


def _curve_boolean_difference(
    subject: geo.Curve, cutter: geo.Curve, tol: float = TOL
) -> List[geo.Curve]:
    """닫힌 커브 2개에 대한 차집합 결과를 리스트로 반환한다."""
    if not subject or not cutter:
        return [subject] if subject else []

    diff = geo.Curve.CreateBooleanDifference(subject, cutter, tol)

    if diff:
        return [d for d in diff if d and d.IsValid]

    if not has_region_intersection(subject, cutter, tol):
        return [subject]

    return []


def get_difference_regions(
    regions_a: Union[geo.Curve, List[geo.Curve]],
    regions_b: Union[geo.Curve, List[geo.Curve]],
    tol: float = TOL,
    remove_particles: bool = True,
    plane: Optional[geo.Plane] = None,
) -> List[geo.Curve]:
    """regions_a - regions_b 차집합 결과를 리스트로 반환한다."""
    if not isinstance(regions_a, list):
        regions_a = [regions_a]
    if not isinstance(regions_b, list):
        regions_b = [regions_b]

    result = [r for r in regions_a if r]
    cutters = [c for c in regions_b if c]
    for cutter in cutters:
        next_regions = comp.polyline_boolean_difference(
            result, [cutter], plane=plane, tol=CLIPPER_TOL
        )
        if not next_regions:
            # fallback: Rhino boolean
            fallback = []
            for region in result:
                fallback += _curve_boolean_difference(region, cutter, tol)
            next_regions = fallback

        result = [r for r in next_regions if r]
        if not result:
            return []

    if not remove_particles:
        return result

    area_tol = max(tol * tol, 1e-8)
    filtered = []
    for region in result:
        area = geo.AreaMassProperties.Compute(region).Area
        if area > area_tol:
            filtered.append(region)
    return filtered


def get_difference_regions_one_to_one(
    region_a: geo.Curve,
    regions_b: List[geo.Curve],
    tol: float = TOL,
    remove_particles: bool = True,
    plane: Optional[geo.Plane] = None,
) -> List[geo.Curve]:
    """region_a 에서 regions_b를 하나씩 차집합 해준다."""
    if not isinstance(region_a, geo.Curve):
        raise ValueError("region_a must be curve")

    result = [region_a]
    for crv in regions_b:
        result = get_difference_regions(result, crv, tol, remove_particles, plane)
        if not result:
            return []
    return result


def _normalize_ghcomp_result(result):
    """ghpythonlib.components 결과를 Curve 리스트로 정규화."""
    if result is None:
        return []
    if isinstance(result, tuple):
        # 일반적으로 첫 출력이 커브 리스트
        result = result[0] if result else []
    if isinstance(result, list):
        return [c for c in result if c]
    # iterable이지만 list가 아닌경우
    if hasattr(result, "__iter__") and not isinstance(result, str):
        return [c for c in result if c]
    return [result]


def get_union_regions(crvs):
    # type: (List[geo.Curve]) -> List[geo.Curve]
    """Clipper PolylineBoolean Union을 우선 사용해 결과 영역 커브들을 반환합니다."""
    if not crvs:
        return []

    valid = [c for c in crvs if c]
    if not valid:
        return []

    result_list = list(geo.Curve.CreateBooleanUnion(valid, TOL))
    if result_list:
        return simplify_crvs_by_reducing_segs(result_list)

    unioned = [valid[0]]
    for crv in valid[1:]:
        merged = comp.polyline_boolean_union(unioned, [crv], tol=CLIPPER_TOL)
        if merged:
            unioned = merged
        else:
            unioned.append(crv)
    if unioned:
        return simplify_crvs_by_reducing_segs([c for c in unioned if c])

    result_region_union = _normalize_ghcomp_result(ghcomp.RegionUnion(valid))
    return simplify_crvs_by_reducing_segs(result_region_union)


def get_intersection_regions(
    regions_a: List[geo.Curve], regions_b: List[geo.Curve]
) -> List[geo.Curve]:
    """Clipper PolylineBoolean Intersection을 우선 사용해 교차 영역 커브들을 반환합니다."""
    if not regions_a or not regions_b:
        return []

    valid_a = [c for c in regions_a if c]
    valid_b = [c for c in regions_b if c]
    if not valid_a or not valid_b:
        return []

    clipped = comp.polyline_boolean_intersection(valid_a, valid_b, tol=CLIPPER_TOL)
    clipped = [c for c in clipped if c]
    if clipped:
        return simplify_crvs_by_reducing_segs(clipped)

    clipped_relaxed = comp.polyline_boolean_intersection(valid_a, valid_b, tol=RAW_TOL)
    clipped_relaxed = [c for c in clipped_relaxed if c]
    if clipped_relaxed:
        return simplify_crvs_by_reducing_segs(clipped_relaxed)

    region_intersection = _normalize_ghcomp_result(
        ghcomp.RegionIntersection(valid_a, valid_b)
    )
    region_intersection = [c for c in region_intersection if c]
    if region_intersection:
        return simplify_crvs_by_reducing_segs(region_intersection)

    rhino_results = []
    for crv_a in valid_a:
        for crv_b in valid_b:
            intersection = geo.Curve.CreateBooleanIntersection(crv_a, crv_b, TOL)
            if intersection:
                rhino_results.extend([c for c in intersection if c])

    if rhino_results:
        return simplify_crvs_by_reducing_segs(rhino_results)

    return []


def simplify_crvs_by_reducing_segs(crvs, tol=TOL, angle_tol=ANGLE_TOL):
    # type: (List[geo.Curve], float, float) -> List[geo.Curve]
    """simplify_crv_by_reducing_segs 여러번 호출"""

    return [simplify_crv_by_reducing_segs(crv, tol, angle_tol) for crv in crvs]


def simplify_crv_by_reducing_segs(crv, tol=TOL, angle_tol=ANGLE_TOL):
    # type: (geo.Curve, float, float) -> geo.Curve
    """ReduceSegments의 tol 기준, MergeColinearSegments의 angel_tol기준으로 정리하여 세그먼트를 줄인다"""

    # polyline을 만들 crv의 vertices를 추출한다
    pts_to_make_polyline = get_vertices(crv)
    if crv.IsClosed:
        pts_to_make_polyline.append(pts_to_make_polyline[0])

    # simplify 시킨다
    polyline_to_simplify = geo.Polyline(pts_to_make_polyline)
    polyline_to_simplify.MergeColinearSegments(angle_tol, True)
    # ReduceSegments에 사용된 알고리즘의 한계로 닫힌 커브의 경우 닫히는 점(시작점과 끝점)은 정리가 안될 수 있다. 주의!
    polyline_to_simplify.ReduceSegments(tol)
    if polyline_to_simplify.IsClosed and polyline_to_simplify.Count > 3:
        # 첫 점(마지막 점과 동일)에 대해서는 ReduceSegments 동작하지 않는 문제 대응
        # 첫 점과 양옆의 이웃하는 점들 가지고 단순화 알고리즘 적용
        pt_items = [polyline_to_simplify[i] for i in range(polyline_to_simplify.Count)]
        pt_first = pt_items[0]
        pt1 = pt_items[1]
        pt2 = pt_items[polyline_to_simplify.Count - 2]
        if geo.Line(pt1, pt2).DistanceTo(pt_first, True) <= tol:
            # 시작점과 마지막 점을 지우고 끝점을 이어준다.
            polyline_to_simplify.RemoveAt(0)
            polyline_to_simplify.RemoveAt(polyline_to_simplify.Count - 1)
            polyline_to_simplify.Add(polyline_to_simplify.First)

    # 커브의 변형이 없으면 원래 커브를 리턴해준다.
    if polyline_to_simplify.Count == len(pts_to_make_polyline):
        return crv

    polycrv_simplified = polyline_to_simplify.ToPolylineCurve()

    if not polycrv_simplified.IsValid:
        # 오차 이내의 커브일 경우 원치않는 결과를 만들 수 있다. 너무 작은 경우 단순화 하지 않음
        # GeoInput("1130510300101940011", [“R1"]),
        return crv

    return polycrv_simplified


def split_crv_from_pts(
    crv: geo.Curve,
    pts: List[geo.Point3d],
    split_tol: float = TOL,
    join_tol: float = TOL,
) -> List[geo.Curve]:
    """커브를 pts 위치에서 Split합니다."""
    if not crv:
        return []
    if not pts:
        return [crv]

    params = []
    for pt in pts:
        rc, t = crv.ClosestPoint(pt, split_tol)
        if rc:
            params.append(t)

    if not params:
        return [crv]

    # 유사 파라미터 중복 제거
    params = sorted(params)
    uniq = []
    for t in params:
        if not uniq or abs(t - uniq[-1]) > 1e-9:
            uniq.append(t)

    pieces = crv.Split(uniq)
    if not pieces:
        return [crv]
    return [p for p in pieces if p and p.IsValid and p.GetLength() > split_tol]


def subtract_interval(
    intervals: List[geo.Interval], interval_to_subtract: geo.Interval
) -> List[geo.Interval]:
    """intervals에서 interval_to_subtract를 빼고 남은 Interval 리스트를 반환합니다."""
    if not intervals:
        return []

    sub = geo.Interval(interval_to_subtract)
    if not sub.IsIncreasing:
        sub.Swap()

    out = []
    for itv in intervals:
        cur = geo.Interval(itv)
        if not cur.IsIncreasing:
            cur.Swap()

        # no overlap
        if cur.T1 <= sub.T0 or cur.T0 >= sub.T1:
            out.append(cur)
            continue

        # left remainder
        if cur.T0 < sub.T0 - 1e-12:
            out.append(geo.Interval(cur.T0, min(cur.T1, sub.T0)))

        # right remainder
        if cur.T1 > sub.T1 + 1e-12:
            out.append(geo.Interval(max(cur.T0, sub.T1), cur.T1))

    return [i for i in out if i.Length > 0]


def get_joined_crvs(
    crvs: List[geo.Curve], tol: float = TOL, preserve_direction: bool = False
) -> List[geo.Curve]:
    """커브들을 Join한 결과를 리스트로 반환합니다."""
    if not crvs:
        return []
    inputs = [c for c in crvs if c]
    if preserve_direction:
        joined = geo.Curve.JoinCurves(inputs, tol, True)
    else:
        joined = geo.Curve.JoinCurves(inputs, tol)
    return [c for c in joined if c]


def get_intersection_params_from_crv_crv(crv_a, crv_b, tol=TOL):
    # type: (geo.Curve, geo.Curve, float) -> List[float]
    """두 커브가 intersection 되어있는 곳을 파라미터로 얻는다

    Args:
        crv_a: intersection할 커브
        crv_b: intersection할 커브
        tol: tolerance 값

    Returns:
        교차파라미터들 (crv_a 기준)
    """

    def get_unsafe_intersection_results(crv_a, crv_b, tol=TOL):
        # type: (geo.Curve, geo.Curve, float) -> Tuple[List[float], List[geo.Interval]]
        """crv_a과 crv_b의 교차점 및 겹친 구간을 얻는다"""
        intersect_params, overlap_intervals = [], []
        intersection_events = geo.Intersect.Intersection.CurveCurve(
            crv_a, crv_b, tol, tol
        )
        for event in intersection_events:
            if event.IsPoint:
                intersect_params.append(event.ParameterA)
            if event.IsOverlap:
                overlap_intervals.append(event.OverlapA)
        return intersect_params, overlap_intervals

    params, overlap_intervals = get_unsafe_intersection_results(crv_a, crv_b, tol)
    for interval in overlap_intervals:
        params += [interval.T0, interval.T1]

    return params


def get_intersection_pts_from_crv_crv(crv_a, crv_b, tol=TOL, cull_duplicates=True):
    # type: (geo.Curve, geo.Curve, float, bool) -> List[geo.Point3d]
    """두 커브가 intersection 되어있는 곳을 점으로 얻는다
    오버랩이 발생하면, 오버랩 구간의 시작과 끝점이 나온다.

    Args:
        crv_a: intersection할 커브 a
        crv_b: intersection할 커브 b
        tol: tolerance 값

    Returns:
        교차점들
    """
    params = get_intersection_params_from_crv_crv(crv_a, crv_b, tol)
    if not params:
        return []

    pts = [crv_a.PointAt(param) for param in params]
    if cull_duplicates:
        pts = list(geo.Point3d.CullDuplicates(pts, tol))

    return pts


def get_pts_from_pt_to_crvs(base_pt, vec, crvs, tol=OP_TOL, skip_base=True):
    # type: (geo.Point3d, geo.Vector3d, List[geo.Curve], float, bool) -> List[geo.Point3d]
    """base_pt에서 vec 방향으로 직선을 연장했을 때 crvs와 교차되는 모든 점들 구하기

    Args:
        base_pt : 기준점
        vec : 연장 방향
        crvs : 대상 커브들
        tol : 허용오차

    Returns:
        교차점들
    """

    if not isinstance(crvs, list):
        crvs = [crvs]
    vec = geo.Vector3d(vec)
    vec.Unitize()
    line_crv = geo.LineCurve(base_pt, base_pt + vec * BIGNUM)
    inter_pts = []
    for crv in crvs:
        inter_pts += get_intersection_pts_from_crv_crv(crv, line_crv, tol, False)

    # base_pt와 같은 점은 스킵. base_pt가 crvs위에 있는 경우.
    if skip_base:
        inter_pts = list(
            filter(lambda pt: not base_pt.EpsilonEquals(pt, tol), inter_pts)
        )

    return inter_pts


def get_pt_from_pt_to_crvs(
    base_pt, vec, crvs, tol=OP_TOL, index_offset=0, skip_base=True
) -> Optional[geo.Point3d]:
    """pt에서 vec 방향으로 ray를 쏴서 crvs와의 가장 가까운 교차점을 반환합니다."""

    inter_pts = get_pts_from_pt_to_crvs(base_pt, vec, crvs, tol, skip_base)
    if not inter_pts:
        return None

    # 가장 가까운점 리턴해야 하는 경우
    if index_offset == 0:
        if len(inter_pts) == 1:
            return inter_pts[0]
        return min(inter_pts, key=base_pt.DistanceToSquared)

    # index_offset에 맞게 리턴해야 하는 경우. 없으면 리턴 None
    inter_pts = list(geo.Point3d.CullDuplicates(inter_pts, tol))
    if len(inter_pts) <= index_offset:
        return None

    inter_pts.sort(key=base_pt.DistanceToSquared)
    return inter_pts[index_offset]


def get_parcels_from_shapes(
    shapes: List[Any], records: List[Any], fields: List[str]
) -> List[Parcel]:
    """모든 shape에서 Parcel 객체들을 생성"""
    parcels = []

    for shape, record in zip(shapes, records):
        parcel = create_parcel_from_shape(shape, record, fields)
        if parcel:
            parcels.append(parcel)

    return parcels


def classify_parcels(parcels):
    # type: (List[Parcel]) -> Tuple[List[Lot], List[Road]]
    """Parcel 리스트를 Lot과 Road로 분류"""
    lots = []
    roads = []

    for parcel in parcels:
        if isinstance(parcel, Road):
            roads.append(parcel)
        else:
            lots.append(parcel)

    return lots, roads


def get_area(regions):
    # type: (Union[List[geo.Curve], geo.Curve]) -> float
    """영역 커브의 면적을 계산합니다."""
    if not isinstance(regions, list):
        regions = [regions]

    area = sum([geo.AreaMassProperties.Compute(r).Area for r in regions])
    return round(area, 6)


def get_straight_skeleton(region_curve):
    """
    스트레이트 스켈레톤 알고리즘 기반 중심선 추출
    """
    # 1. 입력 커브를 폴리라인으로 변환
    if not region_curve.IsClosed:
        return None

    polyline = None
    if isinstance(region_curve, geo.PolylineCurve):
        polyline = region_curve.ToPolyline()
    else:
        # 곡선일 경우 분할하여 근사화
        polyline_curve = region_curve.ToPolyline(0, 0, 0.1, 0.1, 0.1, 0, 0, 0, True)
        polyline = polyline_curve.ToPolyline()

    points = list(polyline)
    if points[0].DistanceTo(points[-1]) < 0.001:
        points.pop()  # 중복 끝점 제거

    n = len(points)
    skeleton_lines = []

    # 2. 각 꼭짓점에서 이등분선(Bisector) 방향 계산
    bisectors = []
    for i in range(n):
        p_prev = points[(i - 1 + n) % n]
        p_curr = points[i]
        p_next = points[(i + 1) % n]

        v1 = p_prev - p_curr
        v2 = p_next - p_curr
        v1.Unitize()
        v2.Unitize()

        # 두 벡터의 합으로 이등분선 방향 설정
        bisect_vec = v1 + v2

        # 직선이 평행한 경우 처리
        if bisect_vec.Length < 1e-6:
            bisect_vec = geo.Vector3d(-v1.Y, v1.X, 0)
        else:
            bisect_vec.Unitize()

        # 내부 방향 확인 (Cross Product 활용)
        cross = geo.Vector3d.CrossProduct(v1, v2)
        if cross.Z > 0:  # 시계/반시계 방향에 따라 반전 필요할 수 있음
            bisect_vec *= -1

        bisectors.append(bisect_vec)

    # 3. 이웃한 이등분선 간의 교점 계산 (Event Simulation)
    # 단순화를 위해 각 꼭짓점에서 시작하는 이등분선과 다음 이등분선의 교점을 연결
    new_points = []
    for i in range(n):
        line1 = geo.Line(points[i], points[i] + bisectors[i] * 1000)
        next_idx = (i + 1) % n
        line2 = geo.Line(
            points[next_idx], points[next_idx] + bisectors[next_idx] * 1000
        )

        rc, a, b = geo.Intersect.Intersection.LineLine(line1, line2)
        if rc:
            intersect_pt = line1.PointAt(a)
            # 원래 꼭짓점에서 교점까지의 선을 스켈레톤의 일부로 추가
            skeleton_lines.append(geo.LineCurve(points[i], intersect_pt))
            skeleton_lines.append(geo.LineCurve(points[next_idx], intersect_pt))
            new_points.append(intersect_pt)

    # 4. 교점들끼리 연결하여 내부 중심선 완성
    for i in range(len(new_points)):
        p1 = new_points[i]
        p2 = new_points[(i + 1) % len(new_points)]
        if p1.DistanceTo(p2) > 0.001:
            skeleton_lines.append(geo.LineCurve(p1, p2))

    return geo.Curve.JoinCurves(skeleton_lines)


# 실행 예시
# skeleton = get_straight_skeleton(input_region)


class Offset:
    class _PolylineOffsetResult:
        def __init__(self):
            self.contour: Optional[List[geo.Curve]] = None
            self.holes: Optional[List[geo.Curve]] = None

    @convert_io_to_list
    def polyline_offset(
        self,
        crvs: List[geo.Curve],
        dist: float,
        miter: int = BIGNUM,
        closed_fillet: int = 2,
        open_fillet: int = 2,
        tol: float = TOL,
    ) -> _PolylineOffsetResult:
        """
        Args:
            crv (_type_): _description_
            dist (float): offset할 거리
            miter : miter
            closed_fillet : 0 = round, 1 = square, 2 = miter
            open_fillet : 0 = round, 1 = square, 2 = butt

        Returns:
            _type_: _PolylineOffsetResult
        """
        if not crvs:
            raise ValueError("No Curves to offset")
        print(f"Offsetting {crvs} curves with distances {dist} and miter {miter}")
        print([crv.IsClosed for crv in crvs])
        print([crv.IsValid for crv in crvs])
        print(get_area(crvs))
        plane = geo.Plane(geo.Point3d(0, 0, crvs[0].PointAtEnd.Z), geo.Vector3d.ZAxis)
        result = ghcomp.ClipperComponents.PolylineOffset(
            crvs,
            [float(dist)],
            plane,
            tol,
            closed_fillet,
            open_fillet,
            miter,
        )
        print(f"Offset result: {result}")

        polyline_offset_result = Offset._PolylineOffsetResult()
        for name in ("contour", "holes"):
            setattr(polyline_offset_result, name, result[name])
        return polyline_offset_result


def offset_region_inward(region, dist):
    # type: (geo.Curve, float) -> Optional[geo.Curve]
    """영역 커브를 안쪽으로 offset 한다.
    Args:
        region: offset할 대상 커브
        dist: offset할 거리

    Returns:
        offset 후 커브
    """

    if not region:
        return None
    if not dist:
        return region
    if not region.IsClosed:
        return None

    source_area = get_area(region)
    if source_area <= TOL:
        return None

    candidates = []
    plane = geo.Plane.WorldXY
    for signed_dist in (-abs(float(dist)), abs(float(dist))):
        offset_curves = region.Offset(
            plane,
            signed_dist,
            TOL,
            geo.CurveOffsetCornerStyle.Sharp,
        )
        if not offset_curves:
            continue
        for crv in offset_curves:
            if not crv or not crv.IsValid or not crv.IsClosed:
                continue
            area = get_area(crv)
            if area < source_area - TOL and area > TOL:
                candidates.append(crv)

    if not candidates:
        return None

    return max(candidates, key=get_area)


def offset_regions_outward(
    regions: Union[geo.Curve, List[geo.Curve]], dist: float, miter: int = BIGNUM
) -> List[geo.Curve]:
    """영역 커브를 바깥쪽으로 offset 한다.
    단일커브나 커브리스트 관계없이 커브 리스트로 리턴한다.
    Args:
        region: offset할 대상 커브
        dist: offset할 거리
    returns:
        offset 후 커브
    """
    if isinstance(regions, geo.Curve):
        regions = [regions]

    return [offset_region_outward(region, dist, miter) for region in regions]


def offset_region_outward(
    region: geo.Curve, dist: float, miter: float = BIGNUM
) -> geo.Curve:
    """영역 커브를 바깥쪽으로 offset 한다.
    단일 커브를 받아서 단일 커브로 리턴한다.
    Args:
        region: offset할 대상 커브
        dist: offset할 거리

    Returns:
        offset 후 커브
    """

    if not dist:
        return region
    if not isinstance(region, geo.Curve):
        raise ValueError("region must be curve")
    return Offset().polyline_offset(region, dist, miter).contour[0]


def simplify_region(region, tol):
    # type: (geo.Curve, float) -> Optional[geo.Curve]
    """region을 tol 만큼 inward → outward offset 하여 좁은 통로/미세 돌출을 제거한다.

    morphological opening 과 동일한 동작이며, 다음 두 가지 사항을 보강한다.
      1) inward 결과가 여러 조각으로 분리되면 면적이 가장 큰 조각만 유지한다.
      2) outward 단계에서 acute corner의 miter join이 원본 region을 벗어나는
         것을 막기 위해 miter 한도를 작게(2 * tol) 두고, 최종 결과를 원본 region
         과 교집합 처리해 어떤 경우에도 원본 영역 바깥으로 새지 않도록 한다.

    단일 객체를 받아 단일 객체를 반환한다. 정리 결과가 사라질 만큼 작은 영역이면
    None을 반환한다.

    Args:
        region: 단일 닫힌 영역 커브
        tol: 정리 임계치(m). 이 값 이하 두께의 돌출/통로는 제거된다.

    Returns:
        정리된 단일 영역 커브, 또는 None.
    """
    if not region or not region.IsClosed:
        return None
    if tol is None or tol <= TOL:
        return region

    # 1) inward offset — offset_region_inward 가 이미 가장 큰 조각만 골라낸다.
    eroded = offset_region_inward(region, tol)
    if not eroded or not eroded.IsClosed:
        return None

    # 2) outward offset — miter 한도를 작게 두어 acute corner 의 needle 돌출을 사전 차단.
    dilated = offset_region_outward(eroded, tol, miter=2.0)
    if not dilated or not dilated.IsClosed:
        return None

    # 3) 원본과 intersection — 남은 miter overshoot 까지 원본 안쪽으로 클램프.
    clipped = get_intersection_regions([dilated], [region])
    if not clipped:
        return None

    return max(clipped, key=get_area)
