"""
MaaMCP_CC - 專為 Claude Code 設計的 MaaFramework Pipeline 開發工具

設計理念：
- Claude Code 有視覺能力，截圖直接回傳 base64，不需要依賴 OCR 文字
- 單次工具呼叫回傳完整資訊，減少來回等待
- test_recognition 讓 Claude 能即時測試節點，不需寫入檔案再執行整個 pipeline
- run_task 回傳結構化的逐節點執行報告，方便分析失敗原因
"""

from __future__ import annotations

import base64
import ctypes
import dataclasses
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from fastmcp import FastMCP
from maa.controller import AdbController, Win32Controller
from maa.define import MaaAdbScreencapMethodEnum, MaaAdbInputMethodEnum
from maa.define import MaaWin32ScreencapMethodEnum, MaaWin32InputMethodEnum
from maa.library import Library
from maa.pipeline import (
    JActionType, JRecognitionType,
    JOCR, JTemplateMatch, JColorMatch, JFeatureMatch, JDirectHit, JDoNothing,
)
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "MaaMCP_CC",
    instructions="""
你是一個 MaaFramework Pipeline 開發助手，搭配 Claude Code 使用。

## 工作流程

1. 呼叫 list_devices 查看可用設備
2. 呼叫 connect_adb（Android）或 connect_window（Windows 應用）連接設備並載入資源
3. 呼叫 screenshot 查看當前畫面（Claude 可直接視覺分析，無需 OCR）
4. 呼叫 test_recognition 即時測試 OCR / TemplateMatch 等識別節點
5. 根據結果修改 Pipeline JSON
6. 呼叫 run_task 執行任務，取得逐節點執行報告

## 關鍵優勢

- screenshot 回傳的是 base64 圖片，Claude 可直接看到畫面
- test_recognition 不需要寫入檔案，即時測試識別定義
- run_task 回傳詳細的節點執行報告，方便追蹤失敗原因
""",
)

# ── 全域狀態 ──────────────────────────────────────────────────────────────────

_controller: Optional[Union[AdbController, Win32Controller]] = None
_resource: Optional[Resource] = None
_tasker: Optional[Tasker] = None
_resource_path: Optional[str] = None
_toolkit_initialized = False


def _ensure_toolkit():
    global _toolkit_initialized
    if not _toolkit_initialized:
        Toolkit.init_option("./")
        _toolkit_initialized = True


def _get_session():
    if _tasker is None or not _tasker.inited:
        raise RuntimeError(
            "尚未連接設備。請先呼叫 connect_adb 或 connect_window，並確認設備已連接且資源已載入。"
        )
    return _controller, _resource, _tasker


def _img_to_b64(img: np.ndarray) -> str:
    """numpy 圖片 → base64 PNG 字串"""
    _, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode()


def _safe_asdict(obj) -> Optional[dict]:
    """安全地將 dataclass 轉為 dict，忽略無法序列化的欄位"""
    if obj is None:
        return None
    try:
        return dataclasses.asdict(obj)
    except Exception:
        try:
            return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            return {"value": str(obj)}


# ── 工具函式 ──────────────────────────────────────────────────────────────────


@mcp.tool()
def list_devices() -> dict:
    """
    掃描所有可用的 Android 模擬器（ADB）和 Windows 視窗。
    在呼叫 connect_adb 或 connect_window 之前使用此工具。
    """
    _ensure_toolkit()

    adb_devices = Toolkit.find_adb_devices()
    windows = Toolkit.find_desktop_windows()

    return {
        "adb_devices": [
            {
                "name": d.name,
                "address": d.address,
                "adb_path": str(d.adb_path),
            }
            for d in adb_devices
        ],
        "windows": [
            {
                "hwnd": d.hwnd,
                "class_name": d.class_name,
                "window_name": d.window_name,
            }
            for d in windows
            if d.window_name.strip()  # 過濾空白標題的視窗
        ],
        "tip": "使用 address 欄位呼叫 connect_adb，使用 hwnd 呼叫 connect_window",
    }


@mcp.tool()
def connect_adb(
    address: str,
    resource_path: str,
    adb_path: str = "adb",
) -> dict:
    """
    連接到 Android 設備（ADB）並載入 MaaFramework 資源包。

    Args:
        address: ADB 設備地址，例如 "127.0.0.1:5555"（從 list_devices 取得）
        resource_path: MaaFramework 資源包路徑，即包含 pipeline/ 和 image/ 子目錄的資料夾
        adb_path: ADB 可執行檔路徑，通常為 "adb"（已在 PATH 中）
                  或完整路徑如 "C:/platform-tools/adb.exe"
    """
    global _controller, _resource, _tasker, _resource_path
    _ensure_toolkit()

    try:
        ctrl = AdbController(adb_path=adb_path, address=address)
    except RuntimeError as e:
        return {"success": False, "error": f"建立 ADB Controller 失敗: {e}"}

    job = ctrl.post_connection()
    job.wait()

    if not ctrl.connected:
        return {"success": False, "error": f"無法連接到 ADB 設備: {address}"}

    res = Resource()
    res_job = res.post_bundle(resource_path)
    res_job.wait()

    if not res.loaded:
        return {"success": False, "error": f"無法載入資源包: {resource_path}"}

    tasker = Tasker()
    if not tasker.bind(res, ctrl):
        return {"success": False, "error": "Tasker 綁定失敗"}

    _controller = ctrl
    _resource = res
    _tasker = tasker
    _resource_path = resource_path

    w, h = ctrl.resolution
    node_list = res.node_list

    return {
        "success": True,
        "device": address,
        "resolution": {"width": w, "height": h},
        "resource_path": resource_path,
        "available_tasks_preview": node_list[:30],
        "total_tasks": len(node_list),
        "tip": "現在可以使用 screenshot、test_recognition、run_task 等工具",
    }


@mcp.tool()
def connect_window(
    hwnd: int,
    resource_path: str,
) -> dict:
    """
    連接到 Windows 視窗並載入 MaaFramework 資源包。
    Windows 後台截圖不佔用滑鼠鍵盤。

    Args:
        hwnd: 視窗句柄（從 list_devices 取得的 hwnd 數值）
        resource_path: MaaFramework 資源包路徑
    """
    global _controller, _resource, _tasker, _resource_path
    _ensure_toolkit()

    try:
        ctrl = Win32Controller(
            hWnd=ctypes.c_void_p(hwnd),
            screencap_method=MaaWin32ScreencapMethodEnum.Background,
            mouse_method=MaaWin32InputMethodEnum.Seize,
            keyboard_method=MaaWin32InputMethodEnum.Seize,
        )
    except RuntimeError as e:
        return {"success": False, "error": f"建立 Win32 Controller 失敗: {e}"}

    job = ctrl.post_connection()
    job.wait()

    if not ctrl.connected:
        return {"success": False, "error": f"無法連接到視窗 hwnd={hwnd}"}

    res = Resource()
    res_job = res.post_bundle(resource_path)
    res_job.wait()

    if not res.loaded:
        return {"success": False, "error": f"無法載入資源包: {resource_path}"}

    tasker = Tasker()
    if not tasker.bind(res, ctrl):
        return {"success": False, "error": "Tasker 綁定失敗"}

    _controller = ctrl
    _resource = res
    _tasker = tasker
    _resource_path = resource_path

    w, h = ctrl.resolution
    node_list = res.node_list

    return {
        "success": True,
        "hwnd": hwnd,
        "resolution": {"width": w, "height": h},
        "resource_path": resource_path,
        "available_tasks_preview": node_list[:30],
        "total_tasks": len(node_list),
        "tip": "現在可以使用 screenshot、test_recognition、run_task 等工具",
    }


@mcp.tool()
def screenshot() -> dict:
    """
    擷取當前螢幕畫面，回傳 base64 PNG 圖片。
    Claude 可直接視覺分析圖片內容，識別 UI 元素、文字、按鈕位置等。
    這是開發 Pipeline 節點時最重要的工具——先看畫面再寫識別條件。
    """
    ctrl, _, _ = _get_session()

    job = ctrl.post_screencap()
    job.wait()

    img = ctrl.cached_image
    if img is None:
        return {"success": False, "error": "截圖失敗，請確認設備連接正常"}

    h, w = img.shape[:2]

    return {
        "success": True,
        "image_base64": _img_to_b64(img),
        "width": w,
        "height": h,
        "format": "image/png",
        "tip": "使用 test_recognition 測試識別條件，參數中的 roi 格式為 [x, y, width, height]",
    }


@mcp.tool()
def test_recognition(
    recognition_type: str,
    params: dict,
) -> dict:
    """
    在當前螢幕畫面上即時測試一個識別節點定義。
    不需要寫入 Pipeline 檔案，直接測試識別結果。
    這是最快的 Pipeline 開發迭代工具。

    Args:
        recognition_type: 識別類型，可用值：
            - "OCR"           文字識別（最常用）
            - "TemplateMatch" 圖片模板比對
            - "FeatureMatch"  特徵點比對
            - "ColorMatch"    顏色比對
            - "DirectHit"     無需識別，直接命中

        params: 識別參數，格式與 Pipeline JSON 相同：
            OCR 範例：
                {"expected": ["確認", "OK"], "roi": [100, 200, 400, 100]}
            TemplateMatch 範例：
                {"template": ["button.png"], "threshold": [0.8], "roi": [0, 0, 1280, 720]}
            ColorMatch 範例：
                {"lower": [[100, 150, 200]], "upper": [[120, 170, 220]], "roi": [0, 0, 500, 500]}

    Returns:
        hit: 是否命中
        box: 命中區域 [x, y, width, height]
        best_result: 最佳識別結果的詳細資訊
        all_results_count: 所有候選結果的數量
        annotated_image_base64: 標記了識別結果的截圖（命中為綠框，未命中顯示 MISS）
    """
    ctrl, _, tasker = _get_session()

    # 驗證識別類型
    reco_type_map = {
        "OCR": (JRecognitionType.OCR, JOCR),
        "TemplateMatch": (JRecognitionType.TemplateMatch, JTemplateMatch),
        "FeatureMatch": (JRecognitionType.FeatureMatch, JFeatureMatch),
        "ColorMatch": (JRecognitionType.ColorMatch, JColorMatch),
        "DirectHit": (JRecognitionType.DirectHit, JDirectHit),
    }

    if recognition_type not in reco_type_map:
        return {
            "success": False,
            "error": f"不支援的識別類型: {recognition_type}",
            "valid_types": list(reco_type_map.keys()),
        }

    reco_type, param_class = reco_type_map[recognition_type]

    # 建立參數物件
    try:
        reco_param = param_class(**params)
    except TypeError as e:
        return {
            "success": False,
            "error": f"參數格式錯誤: {e}",
            "hint": f"{recognition_type} 的有效欄位請參考 Pipeline 協議文件",
        }

    # 先截圖取得當前畫面
    screencap_job = ctrl.post_screencap()
    screencap_job.wait()
    img = ctrl.cached_image
    if img is None:
        return {"success": False, "error": "截圖失敗"}

    # 執行識別
    reco_job = tasker.post_recognition(reco_type, reco_param, img)
    reco_job.wait()

    # 取得識別結果
    task_detail = tasker.get_task_detail(reco_job.job_id)
    if task_detail is None or not task_detail.node_id_list:
        return {"success": False, "error": "識別執行失敗，無法取得結果"}

    node = tasker.get_node_detail(task_detail.node_id_list[0])
    if node is None or node.recognition is None:
        return {"success": False, "error": "無法取得識別詳情"}

    reco = node.recognition

    # 整理結果
    result: dict = {
        "success": True,
        "recognition_type": recognition_type,
        "params_used": params,
        "hit": reco.hit,
        "box": list(reco.box) if reco.box else None,
    }

    # 最佳結果
    if reco.best_result:
        result["best_result"] = _safe_asdict(reco.best_result)

    # 所有結果數量
    if reco.all_results:
        result["all_results_count"] = len(reco.all_results)
        if len(reco.all_results) <= 5:
            result["all_results"] = [_safe_asdict(r) for r in reco.all_results]

    # 生成標記圖（讓 Claude 看到識別命中的位置）
    annotated = img.copy()
    if reco.hit and reco.box:
        x, y, w, h = reco.box
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 220, 0), 3)
        label = f"HIT [{x},{y},{w},{h}]"
        cv2.putText(annotated, label, (x, max(20, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
    else:
        cv2.putText(annotated, f"MISS: {recognition_type}", (10, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 220), 2)

    result["annotated_image_base64"] = _img_to_b64(annotated)
    return result


@mcp.tool()
def run_task(
    entry: str,
    pipeline_override: Optional[dict] = None,
    timeout_seconds: int = 120,
) -> dict:
    """
    執行 Pipeline 任務，回傳詳細的逐節點執行報告。
    支援即時覆蓋節點定義，方便快速測試修改而不需要寫入檔案。

    Args:
        entry: 入口任務節點名稱（必須存在於已載入的資源中，或在 pipeline_override 中定義）
        pipeline_override: 臨時覆蓋的節點定義 dict。
            可用於測試修改後的節點，無需儲存到 JSON 檔案：
            例如 {"MyNode": {"recognition": "OCR", "expected": ["確認"], "action": "Click"}}
        timeout_seconds: 任務超時秒數，預設 120 秒

    Returns:
        success: 任務是否成功完成
        status: 最終狀態
        nodes_executed: 執行過的每個節點詳情（識別是否命中、執行什麼動作）
        failed_node: 第一個失敗的節點名稱（如果有）
        screenshot_on_complete: 任務結束時的畫面（base64）
    """
    ctrl, _, tasker = _get_session()

    override = pipeline_override or {}
    task_job = tasker.post_task(entry, override)

    # 等待完成（帶超時）
    completed = threading.Event()

    def _wait():
        task_job.wait()
        completed.set()

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    finished = completed.wait(timeout=timeout_seconds)

    if not finished:
        tasker.post_stop().wait()
        return {
            "success": False,
            "error": f"任務超時（{timeout_seconds} 秒），已強制停止",
            "entry": entry,
        }

    task_detail = tasker.get_task_detail(task_job.job_id)
    if task_detail is None:
        return {"success": False, "error": "無法取得任務詳情"}

    success = task_detail.status.succeeded

    # 整理逐節點執行記錄
    nodes_info: List[dict] = []
    failed_node: Optional[str] = None

    for node_id in task_detail.node_id_list:
        node = tasker.get_node_detail(node_id)
        if node is None:
            continue

        node_info: dict = {
            "name": node.name,
            "completed": node.completed,
        }

        # 識別結果
        if node.recognition:
            reco = node.recognition
            reco_info: dict = {
                "algorithm": str(reco.algorithm),
                "hit": reco.hit,
                "box": list(reco.box) if reco.box else None,
            }
            if reco.best_result:
                reco_info["best_result"] = _safe_asdict(reco.best_result)
            node_info["recognition"] = reco_info

        # 動作結果
        if node.action:
            act = node.action
            node_info["action"] = {
                "type": str(act.action),
                "success": act.success,
                "target_box": list(act.box) if act.box else None,
            }

        nodes_info.append(node_info)

        if not node.completed and failed_node is None:
            failed_node = node.name

    # 任務結束後截圖
    final_b64 = None
    try:
        screencap_job = ctrl.post_screencap()
        screencap_job.wait()
        final_img = ctrl.cached_image
        if final_img is not None:
            final_b64 = _img_to_b64(final_img)
    except Exception:
        pass

    return {
        "success": success,
        "status": str(task_detail.status),
        "entry": entry,
        "total_nodes_executed": len(nodes_info),
        "nodes_executed": nodes_info,
        "failed_node": failed_node,
        "screenshot_on_complete_base64": final_b64,
    }


@mcp.tool()
def list_tasks(pipeline_path: Optional[str] = None) -> dict:
    """
    列出可用的 Pipeline 任務節點。

    Args:
        pipeline_path: 可選。直接讀取指定 JSON 檔案的節點列表。
                       如果不提供，則列出已載入資源中的所有節點。
    """
    if pipeline_path:
        try:
            path = Path(pipeline_path)
            data: dict = json.loads(path.read_text(encoding="utf-8"))
            return {
                "source": str(path),
                "tasks": sorted(data.keys()),
                "count": len(data),
            }
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"JSON 格式錯誤: {e}"}
        except FileNotFoundError:
            return {"success": False, "error": f"找不到檔案: {pipeline_path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    if _resource and _resource.loaded:
        nodes = _resource.node_list
        return {
            "source": _resource_path,
            "tasks": sorted(nodes),
            "count": len(nodes),
        }

    return {
        "success": False,
        "error": "尚未連接設備，且未提供 pipeline_path。請先呼叫 connect_adb 或 connect_window。",
    }


@mcp.tool()
def stop_task() -> dict:
    """
    停止當前正在執行的 Pipeline 任務。
    """
    if _tasker is None:
        return {"success": False, "error": "尚未連接設備"}

    _tasker.post_stop().wait()
    return {"success": True, "message": "已發送停止指令"}


@mcp.tool()
def get_session_info() -> dict:
    """
    取得當前連接狀態和已載入資源的資訊。
    """
    if _tasker is None or _controller is None:
        return {"connected": False}

    try:
        w, h = _controller.resolution
        return {
            "connected": True,
            "inited": _tasker.inited,
            "resource_path": _resource_path,
            "resolution": {"width": w, "height": h},
            "total_tasks": len(_resource.node_list) if _resource else 0,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


# ── 入口點 ────────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
