"""Project-level constants.

Core geometry constants:
- TOL: 연산 허용 오차
- RAW_TOL: 원시 데이터 허용 오차
- ANGLE_90_DEGREE: 90도(rad)

LANDUSE_MAP:
- DBF field `A13` (용도지역 코드) -> 한글 명칭
- 코드가 없거나 해석 불가하면 "미확인" 사용
"""

import math

# 지오메트리 연산 허용오차(모델 단위)
TOL = 0.001
# 원시 SHP 데이터 보정/판단에 쓰는 완화 오차
RAW_TOL = 0.1
# 90도 회전에 사용하는 라디안 값
ANGLE_90_DEGREE = math.pi / 2.0
# 이웃 필지 1차 bbox 프리필터 거리(m)
PREFILTER_DISTANCE_M = 300.0

# 일반주거지역 코드 집합(배치 대상 필터)
RESIDENTIAL_GENERAL_CODES = {"13", "14", "15", "17"}
# 비일조권 용도지역일 때 도로 제외 판단 거리 기준(m)
ROAD_EXCLUSION_DISTANCE_M = 20.0
# 대상 필지 내부 옵셋 거리(m)
PARCEL_INWARD_OFFSET_M = 1.0
# 건축가능영역 단순화 시 사용하는 inward/outward offset 임계치(m).
# 이 값 이하 두께의 좁은 통로/미세 돌출은 제거된다.
BUILDABLE_SIMPLIFY_TOL_M = 0.3
# 10m 미만 높이 구간에서 적용하는 고정 후퇴 깊이(m)
UNDER_10M_BUILDABLE_DEPTH_M = 1.5
# 높이 규칙 분기 기준 높이(m)
HEIGHT_LIMIT_M = 10.0
# 방식 2에서 고정 셋백을 적용하는 상한 높이(m)
SETBACK_TYPE2_FIXED_MAX_HEIGHT_M = 17.0
# 방식 2에서 10~17m 구간에 적용하는 고정 셋백 깊이(m)
SETBACK_TYPE2_FIXED_DEPTH_M = 5.0
# 사선 커터 Brep 시각화 최대 높이(m)
CUTTER_VISUAL_MAX_HEIGHT_M = 27.0

# 시나리오별 파라미터 정의
# setback_type(int) -> dict
#   label          : 시나리오 명칭 (A~F)
#   description    : 설명
#   low_threshold  : 저층 구간 상한 높이(m) — 항상 10.0
#   low_setback    : 저층 구간 고정 이격(m)
#   mid_threshold  : 중간 구간 상한 높이(m) — None이면 중간 구간 없음
#   mid_setback    : 중간 구간 고정 이격(m)
#   slope_ratio    : 사선 비율 (height × slope_ratio)
SCENARIO_PARAMS = {
    1: {
        "label": "A",
        "description": "현행 (건축법 시행령 제86조, ≤10m: 1.5m, >10m: h×0.5)",
        "low_threshold": 10.0,
        "low_setback": 1.5,
        "mid_threshold": None,
        "mid_setback": None,
        "slope_ratio": 0.5,
    },
    2: {
        "label": "B",
        "description": "개정안 (기준 높이 17m 완화, ≤10m: 1.5m, 10~17m: 5.0m, >17m: h×0.5)",
        "low_threshold": 10.0,
        "low_setback": 1.5,
        "mid_threshold": 17.0,
        "mid_setback": 5.0,
        "slope_ratio": 0.5,
    },
    3: {
        "label": "C",
        "description": "가상 시나리오 C (기준 높이 14m, ≤10m: 1.5m, 10~14m: 5.0m, >14m: h×0.5)",
        "low_threshold": 10.0,
        "low_setback": 1.5,
        "mid_threshold": 14.0,
        "mid_setback": 5.0,
        "slope_ratio": 0.5,
    },
    4: {
        "label": "D",
        "description": "가상 시나리오 D (기준 높이 20m, ≤10m: 1.5m, 10~20m: 5.0m, >20m: h×0.5)",
        "low_threshold": 10.0,
        "low_setback": 1.5,
        "mid_threshold": 20.0,
        "mid_setback": 5.0,
        "slope_ratio": 0.5,
    },
    5: {
        "label": "E",
        "description": "가상 시나리오 E (고정 이격 2.0m, ≤10m: 2.0m, 10~17m: 5.0m, >17m: h×0.5)",
        "low_threshold": 10.0,
        "low_setback": 2.0,
        "mid_threshold": 17.0,
        "mid_setback": 5.0,
        "slope_ratio": 0.5,
    },
    6: {
        "label": "F",
        "description": "가상 시나리오 F (사선 비율 h×0.4, ≤10m: 1.5m, 10~17m: 5.0m, >17m: h×0.4)",
        "low_threshold": 10.0,
        "low_setback": 1.5,
        "mid_threshold": 17.0,
        "mid_setback": 5.0,
        "slope_ratio": 0.4,
    },
}

# setback_type -> 시나리오 라벨 (A~F) 빠른 참조
SCENARIO_LABELS = {k: v["label"] for k, v in SCENARIO_PARAMS.items()}

# 기본 노출 방향벡터 X 성분
DEFAULT_VEC_EXPOSURE_X = 0.0
# 기본 노출 방향벡터 Y 성분(정북)
DEFAULT_VEC_EXPOSURE_Y = 1.0
# 기본 노출 방향벡터 Z 성분
DEFAULT_VEC_EXPOSURE_Z = 0.0
# 단일 계산 실행 시 기본 높이(m)
DEFAULT_HEIGHT_M = 15.0
# 10m 초과 높이에 대해 적용하는 후퇴 비율(깊이 = height * 0.5)
DEFAULT_RATIO = 0.5

LANDUSE_MAP = {
    "0": "지정되지않음",
    "11": "제1종전용주거지역",
    "12": "제2종전용주거지역",
    "13": "제1종일반주거지역",
    "14": "제2종일반주거지역",
    "15": "제3종일반주거지역",
    "16": "준주거지역",
    "17": "일반주거지역",
    "21": "중심상업지역",
    "22": "일반상업지역",
    "23": "근린상업지역",
    "24": "유통상업지역",
    "31": "전용공업지역",
    "32": "일반공업지역",
    "33": "준공업지역",
    "41": "보전녹지지역",
    "42": "생산녹지지역",
    "43": "자연녹지지역",
    "44": "개발제한구역",
    "51": "용도미지정지역",
    "61": "관리지역",
    "62": "보전관리지역",
    "63": "생산관리지역",
    "64": "계획관리지역",
    "71": "농림지역",
    "81": "자연환경보전지역",
}

# 용도지역 코드 해석 실패 시 표시 문자열
LANDUSE_UNKNOWN = "미확인"
