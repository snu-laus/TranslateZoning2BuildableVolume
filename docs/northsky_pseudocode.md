# 정북/정남 사선(북측/남측 일조 사선) 수도코드

이 문서는 [src/northsky.py](../src/northsky.py)의 **정북/정남 사선 베이스 커브 계산 로직**을 구현 언어에 독립적인 수도코드(pseudocode)로 정리한 것입니다.

- 핵심 산출물: `BaseCrv(crv, height)` 리스트 (현재 height는 0으로 고정)
- 대상: Grasshopper/Rhino에서 토지 경계 커브를 입력 받아 사선 시작 선분들을 계산

---

## 1) 데이터/입력 정의

- `lot_region`: 대상 대지 경계 커브(폐곡선)
- `vec_exposure`: 노출(사선) 방향 벡터
  - 예: 정북사선이면 “북쪽”을 향하는 벡터(프로젝트 기준)
- `max_distance`: 탐색/영향 반경(사선 높이/거리 제한에 사용)
- `neighbor_lot_crvs_without_gong`: 주변 필지 경계 커브들(공용부 등 제외된 리스트)
- `is_center_start`: 사선 시작 선분을 “기준 세그먼트에 대해 중심 정렬”할지 여부
- `excluded_lot_crvs`(optional): 계산에서 제외하고 싶은 특정 필지 커브들

보조 유틸(개념):
- `explode(curve) -> segments[]` : 커브를 세그먼트(선분)로 분해
- `get_inside_perp_vec(seg, boundary) -> vec` : boundary 내부를 향하는 seg 수직 벡터
- `get_square_domain_from_seg(seg, plane) -> {x_interval, y_interval}` : plane 좌표계로 seg를 투영한 구간
- `get_rect_from_seg(seg, direction, height) -> rect_crv` : seg로부터 direction 방향으로 height 만큼 확장한 사각(영향 영역)
- `has_region_intersection(a, b)` / `get_union_regions(crvs)` / `get_intersection_regions(A, B)`
- `get_vertices(crv)` : 커브 꼭짓점(분절점)들
- `is_pt_on_crv(pt, crv, tol)`
- `split_crv_from_pts(crv, points, tol1, tol2)` : 특정 점들에서 커브를 분할
- `subtract_interval(base_intervals, cut_interval)` : 1D 구간들에서 다른 구간을 빼서 남은 구간 반환
- `get_joined_crvs(segs)` : 인접 세그먼트를 가능한 한 join

---

## 2) 전체 알고리즘: `compute_northsky_base_crvs(...)`

### 목표
대상 필지 경계에서 `vec_exposure` 방향을 “바라보는” 외곽 세그먼트를 찾고,
그 세그먼트마다 주변 필지로 인해 “사선 시작”이 발생하는 전면(front) 세그먼트들을 계산하여 반환.

### 수도코드

```text
function compute_northsky_base_crvs(
    lot_region,
    vec_exposure,
    max_distance,
    neighbors,
    is_center_start,
    excluded_lot_crvs=None
) -> List<BaseCrv>:

    function filter_excluded_segs(seg_base, exposure_segs):
        filtered = []
        for seg in exposure_segs:
            if excluded_lot_crvs exists and seg lies on any excluded_lot_crv:
                continue

            if seg lies on seg_base:
                # base seg 위의 seg는 허용
                filtered.append(seg)
                continue

            if seg lies on lot_region:
                # 대상 lot 외곽 자체 위에 있는 seg는 제외
                continue

            filtered.append(seg)
        return filtered


    crvs_check = neighbors + [lot_region]

    result_segments = []

    # 기준 변은 보호 방향(vec_exposure)의 반대측 외곽면을 선택
    # (정북사선: vec_exposure=북 -> 북측 변 선택)
    for seg_base in get_target_segs(lot_region, -vec_exposure):
        exposure_segs = get_exposure_base_segs(
            seg_base,
            vec_exposure,
            crvs_check,
            max_distance
        )

        exposure_segs = filter_excluded_segs(seg_base, exposure_segs)
        if exposure_segs is empty:
            continue

        if is_center_start is False:
            result_segments += exposure_segs
        else:
            result_segments += get_centered_segs(seg_base, exposure_segs, vec_exposure)


    result_segments = filter_short_segs(result_segments, vec_exposure)

    return [ BaseCrv(crv=seg, height=0) for seg in result_segments ]
```

---

## 3) 타겟 세그먼트 추출: `get_target_segs(boundary, vec)`

### 목적
`boundary`의 세그먼트 중에서, 세그먼트의 “내부 수직 벡터”가 `vec`와 충분히 같은 방향인 것만 선택.
즉, `vec` 방향으로 노출되는 외곽면을 고르는 단계.

### 수도코드

```text
function get_target_segs(boundary, vec, tol_angle=~1deg):
    targets = []
    for seg in explode(boundary):
        vec_in = get_inside_perp_vec(seg, boundary)

        # vec · vec_in 이 충분히 크면(같은 방향이면) 채택
        if dot(vec, vec_in) < sin(tol_angle):
            continue

        targets.append(seg)

    return targets
```

---

## 4) 전면(Front) 노출 세그먼트 계산: `get_exposure_base_segs(seg_base, y_vec, neighbor_crvs, max_height)`

### 목적
`seg_base`를 기준으로, 주변 필지들이 만드는 “사선 시작에 기여하는” 전면 세그먼트를 계산.

핵심 아이디어:
1) `seg_base` 근처에 `max_height` 범위의 직사각형 영향 영역을 만들고
2) 그 영역과 주변 필지들의 교집합 영역을 만든 뒤
3) 교집합 경계에서 `-y_vec` 방향을 바라보는 세그먼트들을 후보로 수집
4) 후보를 평면 좌표계로 정렬하여, `seg_base`의 x구간을 “가리는” 것들만 앞에서부터 선택

### 수도코드

```text
function get_exposure_base_segs(seg_base, y_vec, neighbor_crvs, max_height):

    # seg_base 시작점 기준으로 평면 구성
    # - y_vec: 전면 방향
    # - x_vec: y_vec를 90도 회전한 축
    x_vec = rotate90deg(y_vec)
    plane = Plane(origin=seg_base.start, x_axis=x_vec, y_axis=-y_vec)

    base_interval = project_seg_to_plane_x_interval(seg_base, plane)
    base_intervals = [normalize_interval(base_interval)]


    # 영향 영역(rect) 생성: seg_base에서 -y_vec 방향으로 max_height 만큼
    region = rect_from_seg(seg_base, direction=-y_vec, height=max_height)

    # 주변 커브 중 rect와 교차하는 것만 필터
    filtered_neighbors = [crv for crv in neighbor_crvs if intersects(region, crv)]
    if filtered_neighbors empty:
        return []


    union_neighbors = union_regions(filtered_neighbors)
    intersections = intersection_regions([region], union_neighbors)
    if intersections empty:
        return []


    vertices = collect_all_vertices(filtered_neighbors)

    # 후보 세그먼트들을 (square_domain -> segment) 매핑으로 저장
    # square_domain은 (y 오름차순, 그 다음 x) 정렬 가능하도록 설계된 키
    dict_domain = {}

    for each intersection_region in intersections:
        # 교집합 경계에서 -y_vec 방향을 바라보는 세그먼트 추출
        for target in get_target_segs(intersection_region, vec=-y_vec):

            cutters = [v for v in vertices if point_is_on_curve(v, target)]
            if cutters not empty:
                target_segs = split_curve_by_points(target, cutters)
            else:
                target_segs = [target]

            for target_seg in target_segs:
                square_domain = project_seg_to_plane_square_domain(target_seg, plane)
                dict_domain[square_domain] = target_seg


    # 앞(front)에서부터 base_interval을 가리는(seg_base를 덮는) 세그먼트를 선택
    segs_front = []

    for square_domain in sort(dict_domain.keys()) by (y asc, x asc):
        cut_interval = square_domain.x_interval

        diff = subtract_interval(base_intervals, cut_interval)

        # 실제로 base_intervals가 "줄어들었는지" 검사
        if length(base_intervals) - length(diff) < TOL:
            continue

        segs_front.append(dict_domain[square_domain])

        # 다 가려졌으면 종료
        if diff empty OR length(diff) < TOL:
            break

        base_intervals = diff


    return segs_front
```

---

## 5) 중심 정렬: `get_centered_segs(seg_base, exposure_segs, vec_exposure)`

### 목적
사선 시작 세그먼트(`exposure_seg`)의 양 끝점을 `seg_base` 위에 “중심적으로” 맞춰 정렬.

- 각 끝점이 `seg_base` 위에 있으면 그대로 사용
- 아니면 `vec_exposure` 방향으로 `seg_base`에 투영한 점을 찾고,
  원래점과 투영점의 중간점을 사용

### 수도코드

```text
function get_centered_segs(seg_base, exposure_segs, vec_exposure):
    return [ get_centered_seg(seg_base, seg, vec_exposure) for seg in exposure_segs ]


function get_centered_seg(seg_base, exposure_seg, vec):
    pts = []

    for pt in [exposure_seg.start, exposure_seg.end]:
        if pt lies on seg_base:
            pts.append(pt)
        else:
            pt_projected = project_point_along_vector_to_curve(pt, vec, seg_base)
            if pt_projected missing:
                pts.append(pt)
            else:
                pts.append( midpoint(pt, pt_projected) )

    return LineCurve(pts[0], pts[1])
```

---

## 6) 너무 짧은(의미 없는) 세그먼트 제거: `filter_short_segs(segs, vec_in)`

### 목적
연속된 세그먼트를 join한 뒤, 사선의 “가로 방향”으로 거의 길이가 없는(거의 점 수준) 결과를 제거.

- `vec_check = vec_in`을 90도 회전한 벡터
- `abs(dot(vec_check, (start - end)))`가 임계값보다 작으면 제거

### 수도코드

```text
function filter_short_segs(segs, vec_in):
    vec_check = rotate90deg(vec_in)

    filtered = []

    for crv in join_curves(segs):
        projected_length = abs( dot(vec_check, (crv.start - crv.end)) )

        if projected_length < threshold(=0.5):
            continue

        filtered += explode(crv)

    return filtered
```

---

## 7) 출력

- `compute_northsky_base_segments(...)`는 위 결과에서 `BaseCrv.crv`만 뽑아 `Curve[]`로 반환하는 단순 래퍼.
- `NorthSkyBaseCurveCalculator`는 GH에서 쓰기 편하게 파라미터를 멤버로 들고 있는 래퍼.
