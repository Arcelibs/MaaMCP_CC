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
from typing import List, Optional, Union

import cv2
import numpy as np
from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from maa.controller import AdbController, Win32Controller
from maa.define import MaaWin32ScreencapMethodEnum, MaaWin32InputMethodEnum
from maa.pipeline import (
    JRecognitionType,
    JOCR, JTemplateMatch, JColorMatch, JFeatureMatch, JDirectHit,
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
3. 呼叫 screenshot 查看當前畫面（Claude 可直接視覺分析）
4. 需要精確座標時，呼叫 screenshot_with_grid 取得帶格線的截圖
5. 呼叫 crop_template 裁切 UI 元素作為模板圖
6. 呼叫 test_recognition 即時測試 OCR / TemplateMatch 等識別節點
7. 根據結果修改 Pipeline JSON
8. 呼叫 run_task 執行任務，取得逐節點執行報告

## Token 管理策略

- screenshot(include_image=False)：只回傳 OCR 文字，極省 token（日常操作用）
- screenshot()：回傳壓縮圖片，適合需要視覺分析時使用
- screenshot_with_grid()：帶座標格線，定位 UI 元素時使用
- 圖片已自動壓縮縮放，但仍比純文字消耗更多 token

## 關鍵優勢

- screenshot_with_grid 讓 Claude 能精確定位 UI 座標
- crop_template 直接裁切模板圖並提供 Pipeline JSON 用法
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
    """取得當前 session，未連接時回傳錯誤 dict 而非拋例外"""
    if _tasker is None or not _tasker.inited:
        return None, None, None
    return _controller, _resource, _tasker


def _require_session():
    """取得當前 session，未連接時回傳錯誤訊息供工具直接 return"""
    ctrl, res, tasker = _get_session()
    if tasker is None:
        return None, None, None, {
            "success": False,
            "error": "尚未連接設備。請先呼叫 connect_adb 或 connect_window。",
        }
    return ctrl, res, tasker, None


def _img_to_image_content(img: np.ndarray, quality: int = 60, max_width: int = 960) -> ImageContent:
    """numpy 圖片 → MCP ImageContent（壓縮 + 縮放，控制 token 消耗）"""
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    b64 = base64.b64encode(buf.tobytes()).decode()
    return ImageContent(type="image", data=b64, mimeType="image/jpeg")


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
def screenshot(include_image: bool = True) -> list:
    """
    擷取當前螢幕畫面。預設回傳壓縮圖片供 Claude 視覺分析。
    若 token 不足，可設定 include_image=False 只回傳 OCR 文字結果。

    Args:
        include_image: 是否回傳圖片。True=圖片+metadata，False=只回傳 OCR 文字結果（省 token）
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

    job = ctrl.post_screencap()
    job.wait()

    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗，請確認設備連接正常"}))]

    h, w = img.shape[:2]

    if not include_image:
        # OCR 模式：只回傳文字，極省 token
        ocr_param = JOCR()
        reco_job = tasker.post_recognition(JRecognitionType.OCR, ocr_param, img)
        reco_job.wait()
        task_detail = tasker.get_task_detail(reco_job.job_id)
        ocr_results = []
        if task_detail and task_detail.node_id_list:
            node = tasker.get_node_detail(task_detail.node_id_list[0])
            if node and node.recognition and node.recognition.all_results:
                for r in node.recognition.all_results:
                    d = _safe_asdict(r)
                    if d:
                        ocr_results.append(d)

        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "width": w, "height": h,
            "mode": "ocr_only",
            "ocr_results": ocr_results,
            "tip": "如需視覺分析，呼叫 screenshot(include_image=True) 或 screenshot_with_grid",
        }, ensure_ascii=False, default=str))]

    meta = {
        "success": True,
        "width": w,
        "height": h,
        "tip": "使用 test_recognition 測試識別條件，roi 格式為 [x, y, width, height]",
    }
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(img),
    ]


@mcp.tool()
def test_recognition(
    recognition_type: str,
    params: dict,
) -> list:
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
        annotated_image: 標記了識別結果的截圖（命中為綠框，未命中顯示 MISS）
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

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

    return [
        TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str)),
        _img_to_image_content(annotated),
    ]


@mcp.tool()
def run_task(
    entry: str,
    pipeline_override: Optional[dict] = None,
    timeout_seconds: int = 120,
) -> list:
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
        screenshot_on_complete: 任務結束時的畫面
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

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
    final_img = None
    try:
        screencap_job = ctrl.post_screencap()
        screencap_job.wait()
        final_img = ctrl.cached_image
    except Exception:
        pass

    result_data = {
        "success": success,
        "status": str(task_detail.status),
        "entry": entry,
        "total_nodes_executed": len(nodes_info),
        "nodes_executed": nodes_info,
        "failed_node": failed_node,
    }
    content: list = [TextContent(type="text", text=json.dumps(result_data, ensure_ascii=False, default=str))]
    if final_img is not None:
        content.append(_img_to_image_content(final_img))
    return content


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
def click(x: int, y: int) -> dict:
    """
    點擊螢幕上的指定座標。
    開發 Pipeline 時用來手動導航遊戲到特定畫面，以便截圖或測試識別。

    Args:
        x: X 座標
        y: Y 座標
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_click(x, y).wait()
    return {"success": True, "clicked": [x, y]}


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> dict:
    """
    在螢幕上滑動。用於捲動列表、翻頁等操作。

    Args:
        x1: 起始 X 座標
        y1: 起始 Y 座標
        x2: 結束 X 座標
        y2: 結束 Y 座標
        duration: 滑動時間（毫秒），預設 500
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_swipe(x1, y1, x2, y2, duration).wait()
    return {"success": True, "from": [x1, y1], "to": [x2, y2], "duration": duration}


@mcp.tool()
def reload_resource() -> dict:
    """
    重新載入資源包。修改 Pipeline JSON 或新增模板圖片後，
    呼叫此工具即可生效，不需要重新連接設備。
    """
    global _resource, _tasker

    if _resource is None or _resource_path is None or _controller is None:
        return {"success": False, "error": "尚未連接設備，請先呼叫 connect_adb 或 connect_window"}

    res = Resource()
    res_job = res.post_bundle(_resource_path)
    res_job.wait()

    if not res.loaded:
        return {"success": False, "error": f"資源載入失敗: {_resource_path}"}

    tasker = Tasker()
    if not tasker.bind(res, _controller):
        return {"success": False, "error": "Tasker 重新綁定失敗"}

    _resource = res
    _tasker = tasker

    return {
        "success": True,
        "resource_path": _resource_path,
        "total_tasks": len(res.node_list),
        "tasks_preview": res.node_list[:30],
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
def screenshot_with_grid(grid_step: int = 100) -> list:
    """
    擷取當前螢幕畫面並疊加座標格線，方便精確定位 UI 元素的座標。
    開發 Pipeline 時的核心工具：先用此工具看清楚每個按鈕/元素的精確座標，
    再據此設定 roi、template 的裁切區域等。

    Args:
        grid_step: 格線間距（像素），預設 100
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    job = ctrl.post_screencap()
    job.wait()

    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗"}))]

    h, w = img.shape[:2]
    result = img.copy()

    # 格線
    for x in range(0, w, grid_step):
        cv2.line(result, (x, 0), (x, h), (0, 0, 255), 1, cv2.LINE_AA)
    for y in range(0, h, grid_step):
        cv2.line(result, (0, y), (w, y), (0, 0, 255), 1, cv2.LINE_AA)

    # 座標標籤（黑底黃字）
    for x in range(0, w, grid_step):
        cv2.rectangle(result, (x + 1, 0), (x + 40, 20), (0, 0, 0), -1)
        cv2.putText(result, str(x), (x + 2, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    for y in range(0, h, grid_step):
        cv2.rectangle(result, (0, y + 1), (40, y + 20), (0, 0, 0), -1)
        cv2.putText(result, str(y), (2, y + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)

    meta = {
        "success": True,
        "width": w,
        "height": h,
        "grid_step": grid_step,
        "tip": "根據格線座標確定 roi 區域，格式為 [x, y, width, height]",
    }
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(result),
    ]


@mcp.tool()
def crop_template(
    x: int,
    y: int,
    width: int,
    height: int,
    save_name: str,
    new_screenshot: bool = False,
) -> list:
    """
    從螢幕截圖裁切一塊區域，儲存為模板圖片供 TemplateMatch 使用。
    通常搭配 screenshot_with_grid 使用：先看格線確定座標，再裁切。

    預設使用上一次截圖的快取（與 screenshot_with_grid 看到的是同一張），
    設定 new_screenshot=True 可強制拍新截圖。

    裁切的圖片會儲存到已載入資源包的 image/ 目錄下。

    Args:
        x: 裁切起始 X 座標
        y: 裁切起始 Y 座標
        width: 裁切寬度
        height: 裁切高度
        save_name: 儲存檔名（不含路徑，例如 "skip_button.png"）
        new_screenshot: 是否拍新截圖。False=使用快取（預設），True=拍新的
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    if new_screenshot or ctrl.cached_image is None:
        job = ctrl.post_screencap()
        job.wait()

    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗，請先呼叫 screenshot 或設定 new_screenshot=True"}))]

    img_h, img_w = img.shape[:2]

    # 邊界檢查
    if x < 0 or y < 0 or x + width > img_w or y + height > img_h:
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": f"裁切區域超出螢幕範圍 ({img_w}x{img_h})",
            "requested": {"x": x, "y": y, "width": width, "height": height},
        }))]

    cropped = img[y:y + height, x:x + width]

    # 儲存到資源包的 image/ 目錄
    if _resource_path:
        save_dir = Path(_resource_path) / "image"
    else:
        save_dir = Path("./assets/resource/image")

    save_dir.mkdir(parents=True, exist_ok=True)

    if not save_name.endswith(".png"):
        save_name += ".png"
    save_path = save_dir / save_name

    cv2.imwrite(str(save_path), cropped)

    meta = {
        "success": True,
        "saved_to": str(save_path),
        "roi": [x, y, width, height],
        "template_size": {"width": width, "height": height},
        "pipeline_usage": {
            "recognition": "TemplateMatch",
            "template": [save_name],
            "roi": [x, y, width, height],
        },
        "tip": "上方是可以直接貼到 Pipeline JSON 的識別定義",
    }
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(cropped),
    ]


@mcp.tool()
def get_session_info() -> dict:
    """
    取得當前連接狀態和已載入資源的資訊。
    """
    ctrl, res, tasker = _get_session()
    if tasker is None:
        return {"connected": False}

    try:
        w, h = ctrl.resolution
        return {
            "connected": True,
            "inited": tasker.inited,
            "resource_path": _resource_path,
            "resolution": {"width": w, "height": h},
            "total_tasks": len(res.node_list) if res else 0,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


# ── 入口點 ────────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
