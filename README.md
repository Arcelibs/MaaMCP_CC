# MaaMCP_CC

> 專為 **Claude Code** 打造的 MaaFramework Pipeline 開發工具

## 這是什麼？

MaaMCP_CC 是一個 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 伺服器，讓 Claude Code 能夠直接操控 Android 模擬器或 Windows 視窗，專門優化 **MaaFramework Pipeline 開發流程**。

### 與 [MaaMCP](https://github.com/MAA-AI/MaaMCP) 的差異

| | MaaMCP | MaaMCP_CC |
|---|---|---|
| **設計對象** | Cursor、LM Studio 等早期 AI Agent | Claude Code |
| **截圖方式** | OCR 回傳純文字 | 回傳圖片，Claude 直接用眼睛看 |
| **識別測試** | 要寫檔案 → 跑 Pipeline → 看 log | `test_recognition` 即時測試，秒出結果 |
| **連接流程** | 多個工具來回呼叫 | 單一工具完成連接 + 載入資源 |
| **執行結果** | 成功 / 失敗 | 逐節點詳細報告 + 完成時截圖 |

---

## 安裝

### 前置需求

- Python 3.10 以上
- [Claude Code](https://claude.ai/download) CLI

### 步驟一：安裝套件

```bash
pip install maamcp-cc
```

### 步驟二：Clone 此專案

```bash
git clone https://github.com/Arcelibs/MaaMCP_CC.git
cd MaaMCP_CC
```

Clone 完成後，用 Claude Code 開啟此資料夾，Claude Code 會**自動偵測 `.mcp.json`** 並詢問是否啟用 MaaMCP_CC。

### 步驟三：確認安裝成功

```bash
claude mcp list
```

看到 `MaaMCP_CC: ... ✓ Connected` 即為成功。

---

## 工具說明

### `list_devices`
掃描所有可用的 Android 模擬器（ADB）和 Windows 視窗。
**在連接前先用這個查看有哪些設備。**

### `connect_adb`
連接到 Android 設備並一次完成載入 MaaFramework 資源包。

| 參數 | 說明 |
|---|---|
| `address` | ADB 地址，如 `127.0.0.1:5555` |
| `resource_path` | 資源包路徑（含 `pipeline/` 和 `image/` 的資料夾）|
| `adb_path` | ADB 執行檔路徑，通常直接填 `adb` |

### `connect_window`
連接到 Windows 視窗（後台操作，不佔用滑鼠鍵盤）。

### `screenshot`
擷取當前畫面，回傳 base64 圖片。
**Claude 可以直接「看」這張圖，識別 UI 元素、按鈕位置、文字等。**

### `test_recognition` ⭐ 最重要的工具
在當前畫面上**即時**測試一個識別節點定義，**不需要寫入任何 Pipeline 檔案**。

回傳結果包含：
- `hit`：是否命中
- `box`：命中區域座標 `[x, y, width, height]`
- `best_result`：最佳識別結果（OCR 識別到的文字、TemplateMatch 的置信度等）
- `annotated_image_base64`：標記了命中位置的截圖（命中為綠框，未命中顯示 MISS）

**使用範例：**

測試 OCR 識別「確認」按鈕：
```json
{
  "recognition_type": "OCR",
  "params": {
    "expected": ["確認", "OK"],
    "roi": [500, 600, 400, 100]
  }
}
```

測試圖片模板比對：
```json
{
  "recognition_type": "TemplateMatch",
  "params": {
    "template": ["button_confirm.png"],
    "threshold": [0.8]
  }
}
```

### `run_task`
執行 Pipeline 任務，支援即時覆蓋節點定義（不需儲存到檔案）。

| 參數 | 說明 |
|---|---|
| `entry` | 入口節點名稱 |
| `pipeline_override` | 臨時修改的節點定義（可選）|
| `timeout_seconds` | 超時秒數，預設 120 |

回傳結果包含每個節點的識別結果、動作結果，以及任務完成時的截圖。

### `list_tasks`
列出已載入資源中的所有 Pipeline 節點，或讀取指定 JSON 檔案的節點列表。

### `stop_task` / `get_session_info`
停止任務 / 查看目前連接狀態。

---

## 開發工作流程

```
1. list_devices          → 查看有哪些設備
2. connect_adb           → 連接設備 + 載入資源
3. screenshot            → 看當前畫面
4. test_recognition      → 測試識別條件（可重複多次調整）
5. 直接修改 Pipeline JSON
6. run_task              → 驗證結果
```

---

## 技術細節

- 語言：Python 3.10+
- 依賴：[FastMCP](https://gofastmcp.com)、[MaaFw](https://github.com/MaaXYZ/MaaFramework)、OpenCV
- 授權：MIT

---

## 相關專案

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) — 底層自動化框架
- [MaaMCP](https://github.com/MAA-AI/MaaMCP) — 原版 MCP（適合 Cursor 等工具）
- [MaaStellaSora](https://github.com/Arcelibs/MaaStellaSora) — 本工具的主要使用案例
