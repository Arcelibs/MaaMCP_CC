# MaaMCP_CC

> 專為 **Claude Code** 打造的 MaaFramework Pipeline 開發工具

## 這是什麼？

MaaMCP_CC 是一個 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 伺服器，讓 Claude Code 能夠直接操控 Android 模擬器或 Windows 視窗，專門優化 **MaaFramework Pipeline 開發流程**。

### 與 [MaaMCP](https://github.com/MaaXYZ/MaaMCP) 的差異

| | MaaMCP | MaaMCP_CC |
|---|---|---|
| **設計對象** | Cursor、LM Studio 等 AI Agent | Claude Code |
| **截圖方式** | OCR 回傳純文字 | 回傳圖片，Claude 直接用眼睛看 |
| **座標定位** | 靠 OCR 猜座標 | `screenshot_with_grid` 格線精確定位 |
| **模板製作** | 手動裁圖 | `crop_template` 一鍵裁切 + 產生 pipeline 用法 |
| **識別測試** | 寫檔案 → 跑 Pipeline → 看 log | `test_recognition` 即時測試 |
| **連接流程** | 多個工具來回呼叫 | 單一工具完成連接 + 載入資源 |
| **手動操作** | 需要另外控制裝置 | 內建 `click` / `swipe` |
| **資源重載** | 必須重新連接 | `reload_resource` 熱重載 |
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

### 步驟二：在你的 MaaFramework 專案中加入 MCP 設定

在專案根目錄建立 `.mcp.json`：

```json
{
  "mcpServers": {
    "MaaMCP_CC": {
      "command": "maamcp-cc"
    }
  }
}
```

用 Claude Code 開啟專案資料夾，會**自動偵測 `.mcp.json`** 並詢問是否啟用。

### 步驟三：確認安裝成功

```bash
claude mcp list
```

看到 `MaaMCP_CC: ... ✓ Connected` 即為成功。

---

## 工具一覽

### 設備管理

| 工具 | 說明 |
|---|---|
| `list_devices` | 掃描所有 ADB 設備和 Windows 視窗 |
| `connect_adb` | 連接 Android 設備 + 載入資源包 |
| `connect_window` | 連接 Windows 視窗（後台操作） |
| `get_session_info` | 查看連接狀態 |

### 畫面觀察

| 工具 | 說明 |
|---|---|
| `screenshot` | 擷取畫面（支援圖片模式 / OCR 純文字模式） |
| `screenshot_with_grid` | 帶座標格線的截圖，精確定位 UI 元素 |

### 開發輔助

| 工具 | 說明 |
|---|---|
| `crop_template` | 裁切模板圖，自動存檔 + 產生 pipeline JSON |
| `test_recognition` | 即時測試識別節點，不需寫檔 |
| `reload_resource` | 修改 pipeline JSON 後熱重載 |

### 裝置操作

| 工具 | 說明 |
|---|---|
| `click` | 點擊指定座標 |
| `swipe` | 滑動操作 |

### 任務執行

| 工具 | 說明 |
|---|---|
| `run_task` | 執行 Pipeline 任務，回傳逐節點報告 |
| `list_tasks` | 列出所有可用的 Pipeline 節點 |
| `stop_task` | 停止執行中的任務 |

---

## 開發工作流程

```
1. list_devices              → 查看有哪些設備
2. connect_adb / window      → 連接設備 + 載入資源
3. screenshot_with_grid      → 帶格線截圖，確定 UI 元素座標
4. crop_template             → 裁切模板圖（自動產生 pipeline 用法）
5. test_recognition          → 即時測試識別條件（可重複調整）
6. 修改 Pipeline JSON
7. reload_resource           → 熱重載修改後的資源
8. run_task                  → 驗證結果
```

### Token 管理策略

圖片會消耗較多 token，可依情況選擇：

| 方法 | Token 消耗 | 適用場景 |
|---|---|---|
| `screenshot(include_image=False)` | 極低 | 只需要文字資訊 |
| `screenshot()` | 中等 | 需要視覺分析 |
| `screenshot_with_grid()` | 中等 | 定位 UI 元素座標 |

圖片已自動壓縮（縮放至 960px + JPEG 60% 品質）。

---

## 重點工具詳解

### `screenshot_with_grid`

在截圖上疊加座標格線，讓 Claude 能精確讀出每個 UI 元素的座標。

```
screenshot_with_grid(grid_step=100)
```

搭配 `crop_template` 使用：先看格線確定座標 → 裁切模板。

### `crop_template`

從截圖裁切一塊區域作為模板圖，自動儲存到資源包的 `image/` 目錄。

```
crop_template(x=1150, y=25, width=80, height=30, save_name="skip_button.png")
```

回傳值直接包含可貼進 Pipeline JSON 的識別定義：

```json
{
  "recognition": "TemplateMatch",
  "template": ["skip_button.png"],
  "roi": [1150, 25, 80, 30]
}
```

預設使用上一次截圖的快取（與 `screenshot_with_grid` 看到的是同一張），設定 `new_screenshot=True` 可拍新的。

### `test_recognition`

即時測試識別節點，**不需要寫入任何 Pipeline 檔案**。

OCR 範例：
```json
{
  "recognition_type": "OCR",
  "params": { "expected": ["確認", "OK"], "roi": [500, 600, 400, 100] }
}
```

TemplateMatch 範例：
```json
{
  "recognition_type": "TemplateMatch",
  "params": { "template": ["skip_button.png"], "threshold": [0.8] }
}
```

回傳標記了命中位置的截圖（命中為綠框，未命中顯示 MISS）。

### `reload_resource`

修改 Pipeline JSON 或新增模板圖片後，呼叫即可生效，**不需要重新連接設備**。

---

## 技術細節

- 語言：Python 3.10+
- 依賴：[FastMCP](https://gofastmcp.com)、[MaaFw](https://github.com/MaaXYZ/MaaFramework)、OpenCV
- 授權：MIT

---

## 相關專案

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) — 底層自動化框架
- [MaaMCP](https://github.com/MaaXYZ/MaaMCP) — 原版 MCP（適合 Cursor 等工具）
