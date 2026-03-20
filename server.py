"""
MaaMCP_CC - 專為 Claude Code 設計的 MaaFramework Pipeline 開發工具

設計理念：
- Claude Code 有視覺能力，截圖直接回傳 base64 ImageContent
- 單次工具呼叫回傳完整資訊，減少來回等待
- test_recognition 即時測試識別節點，不需寫入檔案
- find_and_click 一步完成「找文字→點擊→截圖」
- 完整 Pipeline 管理：產生→儲存→執行→驗證→迭代
"""

from __future__ import annotations

import base64
import ctypes
import dataclasses
import json
import threading
import time
from datetime import datetime
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
MaaMCP_CC 是基於 MaaFramework 的 MCP 服務，專為 Claude Code 設計，
提供 Android 設備（ADB）和 Windows 視窗的自動化控制能力。

## 安全約束（重要）

- 所有設備操作必須且僅能透過本 MCP 提供的工具函式執行
- 嚴禁自己寫 Python 腳本來連接設備或操作畫面
- 嚴禁在終端中直接執行 adb 命令（如 adb devices、adb shell 等）
- 嚴禁使用其他第三方庫或方法與設備交互
- 嚴禁繞過本 MCP 工具自行實作設備控制邏輯
- 不要問使用者模擬器類型、ADB 地址等資訊，直接呼叫 list_devices 自己找

## 標準工作流程

1. **設備發現與連接**
   - 呼叫 list_devices() 掃描可用的 ADB 設備和 Windows 視窗
   - 若發現多個設備/視窗，向使用者展示列表並等待選擇，嚴禁自動決策
   - 使用 connect_adb() 或 connect_window() 建立連接

2. **互動式自動化循環（核心工作模式）**
   - 呼叫 screenshot() 查看當前畫面，告訴使用者你看到了什麼
   - 等待使用者指令（例如「點訪客登入」「滑到下面」「輸入帳號」）
   - 執行操作：
     a. 使用者說「點 XX」→ 呼叫 find_and_click("XX") 一步完成
     b. 或用視覺分析 + click(x, y) 手動操作
     c. 操作後自動截圖確認結果
   - 每完成一個操作，記錄對應的 Pipeline JSON 節點
   - 重複以上步驟

3. **Pipeline 生成與驗證**
   - 操作完成後，呼叫 get_pipeline_protocol() 取得格式規範
   - 將執行過的有效操作轉換為 Pipeline JSON（只保留成功路徑）
   - 呼叫 save_pipeline() 儲存
   - 呼叫 run_task() 驗證 Pipeline 是否正常運行
   - 根據結果迭代優化，直到穩定

## 螢幕識別策略

- **優先使用 OCR**：呼叫 screenshot(include_image=False) 只回傳結構化文字，token 消耗極低
- **按需使用截圖**：僅當以下情況才回傳圖片：
  1. OCR 結果不足以做出決策（需要識別圖標、圖像、顏色、佈局等）
  2. 反覆 OCR + 操作後介面無預期變化，需要視覺確認
- **find_and_click 最高效**：使用者說「點 XX」時直接呼叫，一次完成 OCR→點擊→截圖

## 操作後自動截圖

每次執行 click、swipe、find_and_click 等操作後，自動截圖讓使用者看到結果。
find_and_click 已內建此功能，使用 click/swipe 時需自行補上 screenshot。

## 滾動/翻頁策略

- ADB（Android 設備/模擬器）：使用 swipe() 實現頁面滾動
- Windows（桌面視窗）：使用 scroll() 實現列表滾動；僅在需要拖曳手勢時才用 swipe()

## Pipeline 生成關鍵原則

- 只保留成功路徑，不包含失敗的嘗試
- 優先使用 OCR 識別文字，比座標匹配更穩健
- 合理設置 roi 識別區域提高效率
- 節點命名使用描述性中文名稱
- 使用 post_delay 處理頁面載入等待

## Pipeline 驗證與迭代

1. 呼叫 run_task() 驗證 Pipeline
2. 檢查逐節點執行報告，找到失敗節點
3. 分析失敗原因（識別失敗、座標偏移、畫面變化等）
4. 修改 Pipeline：放寬 OCR 條件、調整 roi、增加 post_delay、添加備選節點
5. 重新執行驗證，直到穩定成功
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
        return None, None, None
    return _controller, _resource, _tasker


def _require_session():
    ctrl, res, tasker = _get_session()
    if tasker is None:
        return None, None, None, {
            "success": False,
            "error": "尚未連接設備。請先呼叫 connect_adb 或 connect_window。",
        }
    return ctrl, res, tasker, None


def _img_to_image_content(img: np.ndarray, quality: int = 60, max_width: int = 960) -> ImageContent:
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    b64 = base64.b64encode(buf.tobytes()).decode()
    return ImageContent(type="image", data=b64, mimeType="image/jpeg")


def _safe_asdict(obj) -> Optional[dict]:
    if obj is None:
        return None
    try:
        return dataclasses.asdict(obj)
    except Exception:
        try:
            return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            return {"value": str(obj)}


# ── Pipeline 協議文件 ─────────────────────────────────────────────────────────

PIPELINE_DOCUMENTATION = """
# MaaFramework Pipeline 協議文件

## 概述

Pipeline 是 MaaFramework 的任務流水線，採用 JSON 格式描述，由若干節點（Node）構成。
每個節點包含識別條件和執行動作，節點間透過 next 欄位連結形成執行流程。

## 基礎結構

```json
{
    "節點名稱": {
        "recognition": "識別演算法",
        "action": "執行動作",
        "next": ["後續節點1", "後續節點2"]
    }
}
```

## 執行邏輯

1. 從入口節點開始，按順序檢測 next 列表中的每個節點
2. 當某個節點的識別條件匹配成功時，執行該節點的動作
3. 動作執行完成後，繼續檢測該節點的 next 列表
4. 當 next 為空或全部超時未匹配時，任務結束

## 識別演算法類型

### DirectHit
直接命中，不進行識別。適用於入口節點或確定性操作。

### OCR
文字識別。

參數：
- `expected`: string | list<string> - 期望匹配的文字，支援正則表達式
- `roi`: [x, y, w, h] - 識別區域，可選，預設全螢幕 [0, 0, 0, 0]

範例：
```json
{
    "點擊設定": {
        "recognition": "OCR",
        "expected": "設定",
        "roi": [0, 100, 200, 50],
        "action": "Click"
    }
}
```

### TemplateMatch
模板匹配（找圖）。

參數：
- `template`: string | list<string> - 模板圖片路徑（相對於 image 資料夾）
- `roi`: [x, y, w, h] - 識別區域，可選
- `threshold`: double - 匹配閾值，可選，預設 0.7

### ColorMatch
顏色匹配。

參數：
- `lower`: [r, g, b] | list<[r, g, b]> - 顏色下限
- `upper`: [r, g, b] | list<[r, g, b]> - 顏色上限
- `roi`: [x, y, w, h] - 識別區域，可選

## 動作類型

### DoNothing
什麼都不做。常用於入口節點。

### Click
點擊操作。

參數：
- `target`: true | [x, y] | [x, y, w, h] | "節點名" - 點擊位置
  - true: 點擊當前識別到的位置（預設）
  - [x, y]: 固定座標點
  - [x, y, w, h]: 在區域內隨機點擊
  - "節點名": 點擊之前某節點識別到的位置
- `target_offset`: [x, y, w, h] - 在 target 基礎上的偏移，可選

### LongPress
長按操作。

參數：
- `target`: 同 Click
- `duration`: uint - 長按時間（毫秒），預設 1000

### Swipe
滑動操作。

參數：
- `begin`: true | [x, y] | [x, y, w, h] | "節點名" - 起始位置
- `end`: true | [x, y] | [x, y, w, h] | "節點名" - 結束位置
- `duration`: uint - 滑動時間（毫秒），預設 200

### Scroll
滑鼠滾輪（僅 Windows）。

參數：
- `dx`: int - 水平滾動距離
- `dy`: int - 垂直滾動距離（正值向上，負值向下，建議使用 120 的倍數）

### InputText
輸入文字。

參數：
- `input_text`: string - 要輸入的文字

### ClickKey
按鍵點擊。

參數：
- `key`: int | list<int> - 虛擬按鍵碼
  - Android: 返回鍵(4), Home(3), 選單(82), Enter(66)
  - Windows: Enter(13), ESC(27), Tab(9)

### StartApp / StopApp
啟動/停止應用（僅 Android）。

參數：
- `package`: string - 套件名或 Activity

## 通用屬性

- `next`: string | list<string> - 後續節點列表，按順序嘗試識別
- `post_delay`: uint - 執行動作後、識別 next 前的延遲（毫秒），預設 200

## 完整範例

```json
{
    "開始任務": {
        "recognition": "DirectHit",
        "action": "DoNothing",
        "next": ["點擊訪客登入"]
    },
    "點擊訪客登入": {
        "recognition": "OCR",
        "expected": "訪客登入",
        "action": "Click",
        "post_delay": 2000,
        "next": ["跳過劇情"]
    },
    "跳過劇情": {
        "recognition": "OCR",
        "expected": "跳過",
        "action": "Click",
        "next": ["進入主畫面"]
    },
    "進入主畫面": {
        "recognition": "OCR",
        "expected": "主選單",
        "action": "Click"
    }
}
```

## 生成 Pipeline 的最佳實踐

1. **只保留成功路徑**：不包含失敗的嘗試
2. **優先使用 OCR 識別**：文字匹配比座標匹配更穩健
3. **合理設置 ROI**：縮小識別區域提高速度和準確性
4. **節點命名清晰**：使用描述性中文名稱
5. **處理等待場景**：增加 post_delay 或用中間節點檢測載入完成
6. **鏈式結構**：確保 next 欄位正確連結，形成完整流程
"""


# ── 設備發現與連接 ────────────────────────────────────────────────────────────


@mcp.tool()
def list_devices() -> dict:
    """
    掃描所有可用的 ADB 設備和 Windows 視窗。
    連接設備前必須先呼叫此工具。

    重要：若發現多個設備，必須向使用者展示列表並等待選擇，嚴禁自動決策。
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
            if d.window_name.strip()
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
        adb_path: ADB 可執行檔路徑（從 list_devices 取得）
    """
    global _controller, _resource, _tasker, _resource_path
    _ensure_toolkit()

    try:
        ctrl = AdbController(adb_path=adb_path, address=address)
    except RuntimeError as e:
        return {"success": False, "error": f"建立 ADB Controller 失敗: {e}"}

    if not ctrl.post_connection().wait().succeeded:
        return {"success": False, "error": f"無法連接到 ADB 設備: {address}"}

    res = Resource()
    if not res.post_bundle(resource_path).wait().succeeded:
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
    }


@mcp.tool()
def connect_window(
    hwnd: int,
    resource_path: str,
    screencap_method: str = "FramePool",
    input_method: str = "Seize",
) -> dict:
    """
    連接到 Windows 視窗並載入 MaaFramework 資源包。

    Args:
        hwnd: 視窗句柄（從 list_devices 取得的 hwnd 數值）
        resource_path: MaaFramework 資源包路徑
        screencap_method: 截圖方式，可選：FramePool, GDI, DXGI_DesktopDup
                         若截圖異常（黑屏、花屏），嘗試切換方式重新連接
        input_method: 輸入方式，可選：Seize, SendMessage
                     若操作無反應，嘗試切換方式重新連接
    """
    global _controller, _resource, _tasker, _resource_path
    _ensure_toolkit()

    screencap_map = {
        "FramePool": MaaWin32ScreencapMethodEnum.FramePool,
        "GDI": MaaWin32ScreencapMethodEnum.GDI,
        "DXGI_DesktopDup": MaaWin32ScreencapMethodEnum.DXGI_DesktopDup,
    }
    input_map = {
        "Seize": MaaWin32InputMethodEnum.Seize,
        "SendMessage": MaaWin32InputMethodEnum.SendMessage,
    }

    sc_method = screencap_map.get(screencap_method, MaaWin32ScreencapMethodEnum.FramePool)
    in_method = input_map.get(input_method, MaaWin32InputMethodEnum.Seize)

    try:
        ctrl = Win32Controller(
            hWnd=ctypes.c_void_p(hwnd),
            screencap_method=sc_method,
            mouse_method=in_method,
            keyboard_method=in_method,
        )
    except RuntimeError as e:
        return {"success": False, "error": f"建立 Win32 Controller 失敗: {e}"}

    if not ctrl.post_connection().wait().succeeded:
        return {"success": False, "error": f"無法連接到視窗 hwnd={hwnd}"}

    res = Resource()
    if not res.post_bundle(resource_path).wait().succeeded:
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
        "screencap_method": screencap_method,
        "input_method": input_method,
        "available_tasks_preview": node_list[:30],
        "total_tasks": len(node_list),
    }


@mcp.tool()
def get_session_info() -> dict:
    """取得當前連接狀態和已載入資源的資訊。"""
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


# ── 螢幕識別 ─────────────────────────────────────────────────────────────────


@mcp.tool()
def screenshot(include_image: bool = True) -> list:
    """
    擷取當前螢幕畫面。

    Args:
        include_image: True=回傳壓縮圖片供視覺分析，False=只回傳 OCR 文字結果（省 token）

    策略建議：
    - 優先使用 include_image=False（OCR 模式），token 消耗極低
    - 僅當 OCR 不足以判斷時才用 include_image=True
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

    ctrl.post_screencap().wait()
    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗"}))]

    h, w = img.shape[:2]

    if not include_image:
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
        }, ensure_ascii=False, default=str))]

    meta = {"success": True, "width": w, "height": h}
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(img),
    ]


@mcp.tool()
def screenshot_with_grid(grid_step: int = 100) -> list:
    """
    擷取螢幕畫面並疊加座標格線，精確定位 UI 元素座標。
    搭配 crop_template 使用：先看格線確定座標，再裁切模板圖。

    Args:
        grid_step: 格線間距（像素），預設 100
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_screencap().wait()
    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗"}))]

    h, w = img.shape[:2]
    result = img.copy()

    for x in range(0, w, grid_step):
        cv2.line(result, (x, 0), (x, h), (0, 0, 255), 1, cv2.LINE_AA)
    for y in range(0, h, grid_step):
        cv2.line(result, (0, y), (w, y), (0, 0, 255), 1, cv2.LINE_AA)

    for x in range(0, w, grid_step):
        cv2.rectangle(result, (x + 1, 0), (x + 40, 20), (0, 0, 0), -1)
        cv2.putText(result, str(x), (x + 2, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    for y in range(0, h, grid_step):
        cv2.rectangle(result, (0, y + 1), (40, y + 20), (0, 0, 0), -1)
        cv2.putText(result, str(y), (2, y + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)

    meta = {"success": True, "width": w, "height": h, "grid_step": grid_step}
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(result),
    ]


@mcp.tool()
def test_recognition(
    recognition_type: str,
    params: dict,
) -> list:
    """
    在當前螢幕畫面上即時測試一個識別節點定義。
    不需要寫入 Pipeline 檔案，直接測試識別結果。

    Args:
        recognition_type: 識別類型：OCR, TemplateMatch, FeatureMatch, ColorMatch, DirectHit
        params: 識別參數（格式與 Pipeline JSON 相同）
            OCR: {"expected": ["確認"], "roi": [100, 200, 400, 100]}
            TemplateMatch: {"template": ["btn.png"], "threshold": [0.8], "roi": [0, 0, 1280, 720]}
            ColorMatch: {"lower": [[100, 150, 200]], "upper": [[120, 170, 220]], "roi": [0, 0, 500, 500]}
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

    reco_type_map = {
        "OCR": (JRecognitionType.OCR, JOCR),
        "TemplateMatch": (JRecognitionType.TemplateMatch, JTemplateMatch),
        "FeatureMatch": (JRecognitionType.FeatureMatch, JFeatureMatch),
        "ColorMatch": (JRecognitionType.ColorMatch, JColorMatch),
        "DirectHit": (JRecognitionType.DirectHit, JDirectHit),
    }

    if recognition_type not in reco_type_map:
        return {"success": False, "error": f"不支援的識別類型: {recognition_type}", "valid_types": list(reco_type_map.keys())}

    reco_type, param_class = reco_type_map[recognition_type]

    try:
        reco_param = param_class(**params)
    except TypeError as e:
        return {"success": False, "error": f"參數格式錯誤: {e}"}

    ctrl.post_screencap().wait()
    img = ctrl.cached_image
    if img is None:
        return {"success": False, "error": "截圖失敗"}

    reco_job = tasker.post_recognition(reco_type, reco_param, img)
    reco_job.wait()

    task_detail = tasker.get_task_detail(reco_job.job_id)
    if task_detail is None or not task_detail.node_id_list:
        return {"success": False, "error": "識別執行失敗"}

    node = tasker.get_node_detail(task_detail.node_id_list[0])
    if node is None or node.recognition is None:
        return {"success": False, "error": "無法取得識別詳情"}

    reco = node.recognition
    result: dict = {
        "success": True,
        "recognition_type": recognition_type,
        "params_used": params,
        "hit": reco.hit,
        "box": list(reco.box) if reco.box else None,
    }

    if reco.best_result:
        result["best_result"] = _safe_asdict(reco.best_result)
    if reco.all_results:
        result["all_results_count"] = len(reco.all_results)
        if len(reco.all_results) <= 10:
            result["all_results"] = [_safe_asdict(r) for r in reco.all_results]

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


# ── 設備控制 ──────────────────────────────────────────────────────────────────


@mcp.tool()
def click(x: int, y: int) -> dict:
    """
    點擊螢幕上的指定座標。

    Args:
        x: X 座標
        y: Y 座標

    提醒：點擊後建議呼叫 screenshot() 確認操作結果。
    或直接使用 find_and_click() 一步完成「找文字→點擊→截圖」。
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_click(x, y).wait()
    return {"success": True, "clicked": [x, y]}


@mcp.tool()
def double_click(x: int, y: int) -> dict:
    """
    在螢幕上雙擊指定座標。

    Args:
        x: X 座標
        y: Y 座標
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_click(x, y).wait()
    time.sleep(0.1)
    ctrl.post_click(x, y).wait()
    return {"success": True, "double_clicked": [x, y]}


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> dict:
    """
    在螢幕上滑動。用於捲動列表、翻頁等操作。
    ADB 場景下優先使用此工具進行滾動。

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
def scroll(x: int, y: int) -> dict:
    """
    滑鼠滾輪操作（僅 Windows 視窗有效，ADB 請用 swipe）。

    Args:
        x: 水平滾動增量（建議 120 的倍數）
        y: 垂直滾動增量（正值向上，負值向下，建議 120 的倍數）
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    if isinstance(ctrl, AdbController):
        return {"success": False, "error": "scroll 不支援 ADB，請使用 swipe 進行滑動"}

    ctrl.post_scroll(x, y).wait()
    return {"success": True, "scroll": [x, y]}


@mcp.tool()
def input_text(text: str) -> dict:
    """
    在設備上輸入文字。支援中文、英文等。
    需要先點擊輸入框使其獲得焦點。

    Args:
        text: 要輸入的文字
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_input_text(text).wait()
    return {"success": True, "text": text}


@mcp.tool()
def click_key(key: int) -> dict:
    """
    按下虛擬按鍵。

    Args:
        key: 虛擬按鍵碼
            Android: 返回鍵=4, Home=3, 選單=82, Enter=66, 刪除=67, 音量+=24, 音量-=25
            Windows: Enter=13, ESC=27, 退格=8, Tab=9, 空格=32, 方向鍵=37/38/39/40
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    ctrl.post_key_down(key).wait()
    time.sleep(0.05)
    ctrl.post_key_up(key).wait()
    return {"success": True, "key": key}


@mcp.tool()
def find_and_click(
    text: str,
    wait_seconds: float = 1.5,
) -> list:
    """
    用 OCR 尋找畫面上的文字，找到後自動點擊其中心位置，最後截圖回傳結果。
    這是互動式開發 Pipeline 時最常用的工具：使用者說「點 XX」→ 呼叫此工具。

    成功後會回傳建議的 Pipeline JSON 節點定義，可直接用於 Pipeline 開發。

    Args:
        text: 要尋找並點擊的文字（例如「訪客登入」「確認」「跳過」）
        wait_seconds: 點擊後等待多少秒再截圖（預設 1.5 秒，等待畫面切換）
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

    ctrl.post_screencap().wait()
    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗"}))]

    ocr_param = JOCR(expected=[text])
    reco_job = tasker.post_recognition(JRecognitionType.OCR, ocr_param, img)
    reco_job.wait()

    task_detail = tasker.get_task_detail(reco_job.job_id)
    if task_detail is None or not task_detail.node_id_list:
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": f"找不到文字「{text}」",
            "tip": "試試用 screenshot 看畫面確認文字內容，或用 click(x, y) 手動指定座標",
        }, ensure_ascii=False))]

    node = tasker.get_node_detail(task_detail.node_id_list[0])
    if node is None or node.recognition is None or not node.recognition.hit:
        all_texts = []
        if node and node.recognition and node.recognition.all_results:
            all_texts = [
                _safe_asdict(r).get("text", "") if _safe_asdict(r) else ""
                for r in node.recognition.all_results[:10]
            ]
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": f"OCR 未命中「{text}」",
            "detected_texts": [t for t in all_texts if t],
            "tip": "參考 detected_texts 調整搜尋詞",
        }, ensure_ascii=False))]

    box = node.recognition.box
    cx = box[0] + box[2] // 2
    cy = box[1] + box[3] // 2

    ctrl.post_click(cx, cy).wait()

    time.sleep(wait_seconds)
    ctrl.post_screencap().wait()
    result_img = ctrl.cached_image

    result = {
        "success": True,
        "found_text": text,
        "box": list(box),
        "clicked": [cx, cy],
        "pipeline_node": {
            "recognition": "OCR",
            "expected": [text],
            "action": "Click",
            "next": ["TODO_下一個節點"],
        },
    }
    content: list = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    if result_img is not None:
        content.append(_img_to_image_content(result_img))
    return content


# ── 模板裁切 ──────────────────────────────────────────────────────────────────


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
    搭配 screenshot_with_grid 使用：先看格線確定座標，再裁切。

    Args:
        x: 裁切起始 X 座標
        y: 裁切起始 Y 座標
        width: 裁切寬度
        height: 裁切高度
        save_name: 儲存檔名（例如 "skip_button.png"）
        new_screenshot: 是否拍新截圖。False=使用快取（預設），True=拍新的
    """
    ctrl, _, _, err = _require_session()
    if err:
        return err

    if new_screenshot or ctrl.cached_image is None:
        ctrl.post_screencap().wait()

    img = ctrl.cached_image
    if img is None:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": "截圖失敗"}))]

    img_h, img_w = img.shape[:2]
    if x < 0 or y < 0 or x + width > img_w or y + height > img_h:
        return [TextContent(type="text", text=json.dumps({
            "success": False,
            "error": f"裁切區域超出螢幕範圍 ({img_w}x{img_h})",
        }))]

    cropped = img[y:y + height, x:x + width]

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
        "pipeline_usage": {
            "recognition": "TemplateMatch",
            "template": [save_name],
            "roi": [x, y, width, height],
        },
    }
    return [
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
        _img_to_image_content(cropped),
    ]


# ── Pipeline 管理 ─────────────────────────────────────────────────────────────


@mcp.tool()
def get_pipeline_protocol() -> str:
    """
    取得 MaaFramework Pipeline 協議文件。

    在需要生成 Pipeline JSON 時呼叫此工具，取得格式規範和最佳實踐。
    包含所有識別演算法、動作類型、參數說明和完整範例。

    使用流程：
    1. 完成自動化操作後呼叫此工具
    2. 根據文件規範將操作轉換為 Pipeline JSON
    3. 呼叫 save_pipeline() 儲存
    4. 呼叫 run_task() 驗證
    """
    return PIPELINE_DOCUMENTATION


@mcp.tool()
def save_pipeline(
    pipeline_json: str,
    output_path: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    """
    儲存 Pipeline JSON 到檔案。

    Args:
        pipeline_json: Pipeline JSON 字串
        output_path: 輸出檔案路徑（可選，預設存到 ~/Documents/MaaMCP/）
        name: Pipeline 名稱（可選，用於產生預設檔名）

    Returns:
        成功回傳儲存的檔案路徑，失敗回傳錯誤訊息
    """
    try:
        pipeline = json.loads(pipeline_json)
    except json.JSONDecodeError as e:
        return f"Pipeline JSON 格式錯誤: {e}"

    if not isinstance(pipeline, dict) or not pipeline:
        return "Pipeline JSON 結構錯誤: 頂層必須是非空物件"

    if output_path:
        filepath = Path(output_path)
    else:
        maamcp_dir = Path.home() / "Documents" / "MaaMCP"
        maamcp_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if name:
            safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")
            safe_name = safe_name.strip()[:50] or "pipeline"
            filepath = maamcp_dir / f"{safe_name}_{timestamp}.json"
        else:
            filepath = maamcp_dir / f"pipeline_{timestamp}.json"

    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(pipeline, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return f"寫入檔案失敗: {e}"

    return str(filepath.absolute())


@mcp.tool()
def load_pipeline(pipeline_path: str) -> Union[dict, str]:
    """
    讀取已有的 Pipeline JSON 檔案。

    Args:
        pipeline_path: Pipeline JSON 檔案路徑

    Returns:
        成功回傳 Pipeline 內容（dict），失敗回傳錯誤訊息
    """
    path = Path(pipeline_path)
    if not path.exists():
        return f"Pipeline 檔案不存在: {pipeline_path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            pipeline = json.load(f)
        if not isinstance(pipeline, dict):
            return "Pipeline 格式錯誤: 頂層必須是物件"
        return pipeline
    except json.JSONDecodeError as e:
        return f"JSON 解析失敗: {e}"
    except OSError as e:
        return f"讀取失敗: {e}"


@mcp.tool()
def list_tasks(pipeline_path: Optional[str] = None) -> dict:
    """
    列出可用的 Pipeline 任務節點。

    Args:
        pipeline_path: 可選，直接讀取指定 JSON 檔案。不提供則列出已載入資源的節點。
    """
    if pipeline_path:
        try:
            data: dict = json.loads(Path(pipeline_path).read_text(encoding="utf-8"))
            return {"source": pipeline_path, "tasks": sorted(data.keys()), "count": len(data)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    if _resource and _resource.loaded:
        nodes = _resource.node_list
        return {"source": _resource_path, "tasks": sorted(nodes), "count": len(nodes)}

    return {"success": False, "error": "尚未連接設備，且未提供 pipeline_path。"}


@mcp.tool()
def run_task(
    entry: str,
    pipeline_override: Optional[dict] = None,
    timeout_seconds: int = 120,
) -> list:
    """
    執行 Pipeline 任務，回傳詳細的逐節點執行報告。

    Args:
        entry: 入口任務節點名稱
        pipeline_override: 臨時覆蓋的節點定義（可選，用於測試修改而不需儲存到檔案）
        timeout_seconds: 超時秒數，預設 120

    重要：執行前請確保設備處於 Pipeline 入口節點假設的起始畫面。
    """
    ctrl, _, tasker, err = _require_session()
    if err:
        return err

    override = pipeline_override or {}
    task_job = tasker.post_task(entry, override)

    completed = threading.Event()

    def _wait():
        task_job.wait()
        completed.set()

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    finished = completed.wait(timeout=timeout_seconds)

    if not finished:
        tasker.post_stop().wait()
        return {"success": False, "error": f"任務超時（{timeout_seconds} 秒），已強制停止", "entry": entry}

    task_detail = tasker.get_task_detail(task_job.job_id)
    if task_detail is None:
        return {"success": False, "error": "無法取得任務詳情"}

    success = task_detail.status.succeeded

    nodes_info: List[dict] = []
    failed_node: Optional[str] = None

    for node_id in task_detail.node_id_list:
        node = tasker.get_node_detail(node_id)
        if node is None:
            continue

        node_info: dict = {"name": node.name, "completed": node.completed}

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

    final_img = None
    try:
        ctrl.post_screencap().wait()
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


# ── 資源管理 ──────────────────────────────────────────────────────────────────


@mcp.tool()
def reload_resource() -> dict:
    """
    重新載入資源包。修改 Pipeline JSON 或新增模板圖片後呼叫此工具即可生效，不需重新連接設備。
    """
    global _resource, _tasker

    if _resource is None or _resource_path is None or _controller is None:
        return {"success": False, "error": "尚未連接設備"}

    res = Resource()
    if not res.post_bundle(_resource_path).wait().succeeded:
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
    """停止當前正在執行的 Pipeline 任務。"""
    if _tasker is None:
        return {"success": False, "error": "尚未連接設備"}

    _tasker.post_stop().wait()
    return {"success": True, "message": "已發送停止指令"}


# ── 入口點 ────────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
