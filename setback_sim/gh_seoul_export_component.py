"""GH Python Component 붙여넣기용 스크립트 (방어 버전).

================================================================
[GH 컴포넌트 셋업]
================================================================
1. GhPython 컴포넌트 1개 추가
2. 입력 파라미터 2개 (우클릭 -> Input):
     - folder_path : str
     - run         : bool
3. 출력 파라미터 1개 (우클릭 -> Output):
     - log
   ※ 출력 추가 후 'log' 노드 우클릭 -> "List Access" 선택해야
      panel에서 줄별로 보임. (Item Access면 첫 한 줄만 보임)
4. 컴포넌트 더블클릭 -> 이 스크립트 전체 붙여넣기
5. folder_path에 Panel 연결 (SHP 폴더 절대경로),
   run에 Boolean Toggle 연결
6. 결과는 log 출력 panel에서 확인
================================================================
"""

import sys
import os
import traceback
import datetime

# --- 출력 변수 사전 초기화 (예외 나도 GH가 빈 출력으로 죽지 않게) ---
log = []
out_dir = None


def _add(msg):
    """log에 timestamp 붙여서 누적."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = "[{}] {}".format(ts, msg)
    log.append(line)
    try:
        print(line)
    except Exception:
        pass


# .gh가 setback_sim/ 밖에 있을 경우 여기를 절대경로로
SETBACK_SIM_DIR_FALLBACK = (
    r""  # 예: r"C:\Users\you\...\TranslateZoning2BuildableVolume\setback_sim"
)


try:
    # --- 입력 진단 ---
    _add("=== 진단 시작 ===")
    _add("Python: {}".format(sys.version.split()[0]))
    _add("Platform: {}".format(sys.platform))

    _fp = folder_path if "folder_path" in dir() else None
    _run = run if "run" in dir() else None
    _add("folder_path = {!r}".format(_fp))
    _add("run = {!r}".format(_run))

    # --- ghenv로 .gh 파일 위치 찾기 ---
    _gh_dir = None
    try:
        _ghenv = globals().get("ghenv")
        if _ghenv is not None:
            _gh_file = _ghenv.Component.OnPingDocument().FilePath
            if _gh_file:
                _gh_dir = os.path.dirname(os.path.abspath(_gh_file))
                _add(".gh 위치: {}".format(_gh_dir))
            else:
                _add("[WARN] .gh가 아직 저장되지 않음 (File > Save 먼저)")
        else:
            _add("[WARN] ghenv 없음 -- GhPython 컴포넌트가 맞나요?")
    except Exception as e:
        _add("[WARN] ghenv 접근 실패: {}".format(e))

    # --- setback_sim 폴더 탐색 ---
    _candidates = []
    if _gh_dir:
        _candidates.extend(
            [
                _gh_dir,
                os.path.join(_gh_dir, "setback_sim"),
                os.path.join(os.path.dirname(_gh_dir), "setback_sim"),
            ]
        )
    if SETBACK_SIM_DIR_FALLBACK:
        _candidates.append(SETBACK_SIM_DIR_FALLBACK)

    _resolved_dir = None
    for cand in _candidates:
        marker = os.path.join(cand, "seoul_geom_export.py")
        if os.path.isdir(cand) and os.path.isfile(marker):
            _resolved_dir = cand
            break

    if _resolved_dir is None:
        _add("[ERROR] setback_sim 디렉토리를 찾을 수 없습니다.")
        _add("후보 경로:")
        for c in _candidates:
            _add("  - {}  (exists={})".format(c, os.path.isdir(c)))
        _add("해결: .gh 파일을 setback_sim/ 폴더 안에 저장하거나")
        _add("이 스크립트 상단의 SETBACK_SIM_DIR_FALLBACK을 수정")
    else:
        _add("setback_sim: {}".format(_resolved_dir))
        if _resolved_dir not in sys.path:
            sys.path.insert(0, _resolved_dir)

        # --- 모듈 import 단계별 진단 ---
        _modules = ["constants", "utils", "northsky", "shp_to_lot", "seoul_geom_export"]
        _imported = {}
        _import_ok = True
        for m in _modules:
            try:
                _imported[m] = __import__(m)
                _add("  import {} OK".format(m))
            except Exception as e:
                _add("  [FAIL] import {} : {}".format(m, e))
                _add("        {}".format(traceback.format_exc().splitlines()[-1]))
                _import_ok = False
                break

        if _import_ok:
            # reload (개발 편의)
            try:
                import importlib

                for m in _modules:
                    importlib.reload(_imported[m])
                _add("모듈 reload OK")
            except Exception as e:
                _add("[WARN] reload 실패 (무시 가능): {}".format(e))

            seoul_geom_export = _imported["seoul_geom_export"]

            # --- 실행 가드 ---
            if not _fp:
                _add("[WAIT] folder_path 비어있음 -- panel에 SHP 폴더 경로 연결")
            elif not os.path.isdir(_fp):
                _add("[ERROR] folder_path가 폴더가 아님: {}".format(_fp))
            elif not _run:
                _add("[WAIT] run=False -- Boolean Toggle을 True로")
                # 폴더 내 SHP 미리 보여주기
                shps = [n for n in os.listdir(_fp) if n.lower().endswith(".shp")]
                _add(
                    "폴더 안 SHP 파일 {}개: {}".format(
                        len(shps), shps[:5] + (["..."] if len(shps) > 5 else [])
                    )
                )
            else:
                # --- 실제 실행 ---
                _add(">>> export_folder 실행 시작 (target_lot_limit=10)")
                result = seoul_geom_export.export_folder(
                    folder_path=_fp,
                    types=(1, 2),
                    target_lot_limit=None,  # smoke test. 전체는 None
                    progress_every=1000,
                )
                # export_folder가 만든 logs를 그대로 이어 붙임
                for line in result.get("logs", []):
                    log.append(line)
                out_dir = result.get("out_dir")
                _add(">>> 완료. out_dir = {}".format(out_dir))

except Exception:
    _add("[EXCEPTION] 처리 중 예외 발생:")
    for line in traceback.format_exc().splitlines():
        _add("  " + line)
