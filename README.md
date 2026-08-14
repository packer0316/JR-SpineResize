# JR-SpineResize

把 Spine 用的貼圖等比縮小，並自動把 `.atlas` 修正到與新貼圖對齊，讓 Spine 播放結果與縮放前完全一致。

**內建 Spine 3.8 播放預覽**：以 .skel 為單位管理專案，套用縮放設定後可直接播放動畫，
一鍵切換「原始 / 縮放後」貼圖即時對比品質，再批次處理。

## 工作流程（v1.1）

1. **拖入資料夾或檔案** — 每個 `.skel` 自動配對其 `.atlas` 與貼圖，成為左側清單的一個專案
2. **點選專案** — 中間顯示它的檔案（skel / atlas / 貼圖們）並直接播放 Spine 動畫
3. **調整右側設定 → 套用到此專案**（或「套用到全部」）— 背景立即產生縮放後貼圖，
   播放器出現「縮放後」鈕，可在播放中即時切換對比
4. **開始處理** — 只處理已套用設定的專案，未套用的自動略過

搭配 [JR-Img-Compresser](../JR-Img-Compresser) 的縮圖流程使用，也可以獨立完成整個縮放。

---

## TL;DR — 這個工具在做什麼

| | |
|---|---|
| **會改** | `.atlas`（`xy` / `size` / `orig` / `offset` / 頁面 `size`）、貼圖 PNG |
| **不會改** | `.skel` / `.json` 骨架檔（只會原樣複製） |
| **結果** | Spine 播放的畫面大小、位置、動畫完全不變，只有貼圖解析度降低 |

---

## 為什麼 `.skel` 不需要改（也不該改）

Spine runtime 計算 RegionAttachment 頂點的方式（3.8 ~ 4.x 皆同）：

```
regionScaleX = attachment.width / region.origWidth      <- attachment.width 來自 .skel
localX       = -attachment.width / 2 + region.offsetX * regionScaleX
localX2      = localX + region.sizeWidth * regionScaleX
```

把 atlas 內 `xy / size / orig / offset / 頁面尺寸` 全部同乘 `s`：

- `origWidth` 縮小 → `regionScaleX` 等比放大
- `offsetX` 與 `sizeWidth` 各自縮小

兩邊相乘剛好抵銷，算出來的 `localX` / `localX2` **完全不變**。

Mesh 也一樣：UV 是以頁面尺寸正規化的比值，頂點座標存在 `.skel` 且與 atlas 無關。

> **所以等比縮貼圖時動 `.skel` 才會破圖。** 本工具只會把骨架檔原樣複製到輸出資料夾。

---

## 為什麼不能「整張 PNG 丟進圖片工具縮小」

兩個問題，都會實際破圖：

### 1. 滲色（cross-region bleeding）

Lanczos / Bicubic 的取樣半徑會跨過圖塊之間的間距，把隔壁圖塊的顏色吃進來。原本 2px 的 padding 縮一半後只剩 1px，擋不住。

實測（4 份真實素材，縮 50%，以「單獨縮放該圖塊」為基準）：

| 作法 | 單圖塊平均最大色差 | 最嚴重的圖塊 |
|---|---|---|
| 整張 PNG 一次縮小 | 75 ~ 195 / 255 | **246 / 255** |
| 本工具逐圖塊縮小 | **0** | **0** |

### 2. 接縫（rounding seam）

圖塊邊界落在非整數位置，四捨五入後會多切或少切一排像素。本工具改用「兩個邊界各自四捨五入再相減」求新寬度，相鄰圖塊不會出現重疊或縫隙。

---

## 安裝

```bash
cd JR-SpineResize
pip install -r requirements.txt
python main.py
```

需求：Python 3.10+、PyQt6、Pillow、numpy、imagequant、pyoxipng、mozjpeg-lossless-optimization
（後三者是壓縮引擎；缺少時會自動退回 Pillow 內建路徑，功能不減但壓縮率較差）。
Windows 可直接點 `啟動Spine縮放工具.bat`。

打包成單一 exe：執行 `build.bat`，輸出在 `release\JR-SpineResize.exe`。

---

## 播放預覽（內建 Spine 3.8 runtime）

預覽播放不依賴任何外部 Spine runtime——工具內建了一個 Python 實作：

- 完整解析 3.8 binary `.skel`（bones / slots / skins / 全部 attachment 型別 / 全部 timeline）
- 骨骼世界變換（含 transform 繼承模式）、IK / Transform / Path 約束
- Region + Mesh（含加權與 deform 動畫）、裁切附件、四種 blend mode
- 以 28 份真實素材、94 個動畫驗證：解析 100% 對齊檔尾，470 個姿勢計算零失敗

播放器支援動畫選單、skin 選單、播放/暫停、時間軸拖曳、滾輪縮放、拖曳平移。
「縮放後」預覽走與正式輸出**完全相同**的處理路徑（含調色盤量化），所見即所得。

> 預覽僅支援 Spine 3.8 binary；其他版本或 JSON 骨架的專案仍可正常縮放處理，只是不能播放。
> `.skel` 依然只讀不寫。

## 使用方式

1. 把資料夾或檔案拖進視窗（每個 `.skel` 成為一個專案；孤兒 `.atlas` 也會列出）
2. 點選專案檢視檔案與播放動畫
3. 右側調整設定後按「套用到此專案」或「套用到全部」
4. 按「開始處理」——只處理已套用的專案，完成後跳出驗證報告

### 兩種模式

#### A. 縮放貼圖並重寫 atlas（推薦）

本工具全包：逐圖塊裁切 → 各自縮放 → 放回新頁面對應位置 → 重算 atlas。

版面配置與原本相同（只是等比縮小），所以輸出的 `.atlas` 可以直接和原檔 diff 比對。

#### B. 只重算 atlas（貼圖已在外部縮好）

沿用你原本用 JR-Img-Compresser 縮圖的流程，本工具只負責把 atlas 數值對齊。

- 「已縮好的貼圖資料夾」留空 = 貼圖就在 atlas 同一層
- 縮放比例預設由「新貼圖尺寸 ÷ atlas 宣告尺寸」自動推算

> 這個模式沒辦法避免上面說的滲色問題（貼圖不是本工具產生的），只保證座標正確。
> 對品質有要求時請用模式 A。

### 進階選項

| 選項 | 說明 |
|---|---|
| **透明處理** | 預設「預乘後縮放」。直通 alpha 的圖直接插值會把透明像素沒有意義的 RGB 混進來，邊緣出現黑邊。`pma: true` 的頁面會自動略過（來源本來就是預乘的）。 |
| **邊緣填充** | 預設「滲出顏色」2px：把圖塊顏色往外滲到透明區、alpha 維持 0，補掉 GPU Linear 取樣吃到空白的問題，輪廓完全不變。「完整外擴」會連 alpha 一起擴（部分引擎需要）。預乘頁面會自動略過。 |
| **畫布對齊** | 可補到 4 的倍數或 2 的次方。**只補畫布、不改縮放比例**，所以不影響播放結果。 |
| **檔名後綴** | 例如 `_half`，會變成 `xxx_half.atlas` / `xxx_half.png`。 |

### 壓縮設定（v1.2 起內建 JR-Img-Compresser 引擎）

貼圖輸出直接走與 JR-Img-Compresser 相同的壓縮管線，設定項目與介面也一致，
不需要再把輸出丟去另一個工具跑第二輪：

| 設定 | 說明 |
|---|---|
| **模式** | `無損`（預設）：像素零損失，Pillow 編碼後由 oxipng 重新最佳化；`智慧有損`：imagequant（pngquant 核心，同 TinyPNG）256 色量化，可調品質與漸層抖動 |
| **色彩格式** | RGBA8888 / RGBA5551 / RGBA4444 / RGB565——模擬引擎 16-bit 貼圖轉檔後的實際畫面，同時大幅縮小檔案；可加開 Bayer 量化抖動減少漸層斷階 |
| **最佳化強度** | oxipng 等級（快速 / 標準 / 極限） |
| **移除中繼資料** | 移除 EXIF 等 metadata（保留 ICC） |
| **目標檔案大小** | 指定 KB 數，二分搜尋符合大小的最高品質（智慧有損模式） |

另有「絕不變大保護」：比例 100%（尺寸調整關閉）且未做任何量化時，
若壓縮結果反而比原檔大，會直接沿用原檔（同 TinyPNG 行為）。

「尺寸調整」可以整個關閉——此時比例固定 100%，工具就變成
「保持尺寸、只壓縮貼圖並原樣保留 atlas 數值」的批次壓縮器。

> **套用設定後**，檔案面板會即時顯示每張貼圖「原始 → 處理後」的大小與
> 增減百分比（背景以壓縮引擎的快速模式實算，不是用面積比亂猜）。

### 輸出

- **子資料夾**（預設）：輸出到 `<atlas 所在資料夾>/resized/`
- **指定路徑**：批次處理時會保留來源的相對目錄結構
- **原地覆蓋**：直接改寫原檔，覆寫前會建立一次 `.bak` 備份

---

## 驗證報告

每次處理都會檢查以下項目，任何一項失敗就不會寫出 atlas：

- 頁面宣告尺寸與實際輸出 PNG 尺寸一致
- 每個區塊都完整落在頁面範圍內
- 區塊之間沒有重疊
- `offset + size <= orig`
- 區塊尺寸不為 0
- 區塊名稱（name + index）沒有重複
- 縮放前後的區塊名稱集合完全相同
- 骨架需要的區塊在 atlas 中都存在（JSON 骨架可完整比對；binary 只做弱參考）

另外會回報**幾何漂移**：

- `座標完全精確 N / M 個區塊`
- `最大幾何偏移 X 原始像素`

### 關於幾何漂移

atlas 座標必須是整數。Spine 算出來的長度是 `attachment 尺寸 × (size / orig)`，而 `size` 與 `orig` 都得是整數。

例如 `size=21, orig=22` 縮一半的理想值是 `10.5 / 11`，但 21/22 這個分數沒辦法用更小的整數精確表示，只能取 `10/10` 或 `10/11`，兩者都差約半個像素。

**這是縮小 atlas 本身的數學下限，不是實作問題。** 程式的作法是先固定實際像素矩形，再在候選整數中挑一組讓 `size/orig` 與 `offset/orig` 兩個比值合計誤差最小的組合。

未裁切的區塊（`offset` 為 0 且 `orig == size`）則是**零誤差**。

實測結果（縮 50%）：最大偏移約 **1 個原始像素**，多數素材在 0.4 ~ 1.0 px 之間。

---

## 支援格式

| 項目 | 支援 |
|---|---|
| atlas 格式 | legacy（Spine ≤ 4.0，`rotate/xy/size/orig/offset`）與 modern（Spine ≥ 4.1，`bounds/offsets`）自動辨識 |
| 骨架格式 | `.skel` binary（3.8 / 4.x 標頭自動辨識）、`.json` |
| 旋轉區塊 | 支援（`rotate: true` 與 4.x 的角度寫法） |
| 裁切區塊 | 支援 |
| 多頁 atlas | 支援 |
| 多個 atlas 共用同一張貼圖 | 支援，同批次只會渲染一次；版面不同時會提出警告 |
| 貼圖格式 | PNG / WebP / JPG |

解析器會完整保留原檔格式（縮排、`size: 606,606` 與 `xy: 2, 489` 的分隔符差異、未知屬性），
未改動的檔案讀進來再寫出去是 **byte-identical**（28 份真實素材實測通過）。

---

## 安全機制

- **擋二次縮放**：atlas 宣告的頁面尺寸與實際貼圖不符時直接報錯。重複執行不會把已縮過的圖再縮一次。
- **原地覆蓋備份**：覆寫前建立 `.bak`，且不會用已處理的檔案蓋掉既有備份。
- **驗證未過就不輸出**：atlas 只有在所有檢查通過後才寫出。

---

## 專案結構

```
JR-SpineResize/
├── main.py                    # 程式進入點
├── requirements.txt
│
├── config/                    # 常數、版本、使用者設定
├── models/
│   ├── atlas_data.py          # Atlas 資料模型（保留原始格式）
│   ├── process_options.py
│   └── spine_asset.py
├── core/
│   ├── atlas_parser.py        # .atlas 解析與序列化
│   ├── rect_mapper.py         # 座標重算（核心數學）
│   ├── page_renderer.py       # 逐圖塊縮放與重繪
│   ├── compressor.py          # 壓縮引擎（imagequant / oxipng / mozjpeg，同 JR-Img-Compresser）
│   ├── skeleton_reader.py     # .skel / .json 標頭唯讀解析
│   ├── validator.py           # 輸出驗證與漂移統計
│   ├── asset_scanner.py       # atlas 掃描與配對
│   ├── project_scanner.py     # 以 .skel 為單位組專案
│   ├── pipeline.py            # 處理管線（無 Qt 相依）
│   └── spine/                 # 內建 Spine 3.8 runtime（僅預覽用，唯讀）
│       ├── binary_parser.py   # .skel 完整解析
│       ├── skeleton_data.py   # 資料模型
│       ├── animation.py       # timeline 與曲線
│       ├── runtime.py         # 骨骼/約束/頂點計算
│       ├── texture_store.py   # atlas 區塊還原成獨立貼圖
│       └── qt_renderer.py     # QPainter 渲染器
├── ui/                        # PyQt6 介面
├── utils/                     # 影像與檔案工具
└── tests/
    └── verify.py              # 對自己的素材重跑完整驗證
```

`core/` 完全不相依 Qt，可以直接在腳本或批次流程中重用。

---

## 對自己的素材重跑驗證

```bash
py -3 tests/verify.py "D:/game/assets/spine" 50
```

會在系統暫存目錄的副本上跑完整流程（**不會動到原始素材**），輸出每份資產的：

- atlas round-trip 是否 byte-identical
- 頂點偏移（直接用 spine-runtimes 的算式比對縮放前後的四角座標）
- 整張縮小 vs 逐圖塊縮小的色差
- 座標完全精確的區塊數
- 內建驗證是否全數通過

實測結果（28 份 Spine 3.8 素材）：

| 縮放 | 最大頂點偏移 | 整張縮小色差 | 逐圖塊色差 | 失敗 |
|---|---|---|---|---|
| 75% | 1.01 px | 16 ~ 146 | **0** | 0 |
| 50% | 1.11 px | 16 ~ 146 | **0** | 0 |
| 33% | 2.11 px | 16 ~ 146 | **0** | 0 |
| 25% | 3.27 px | 16 ~ 146 | **0** | 0 |

偏移大致等於 `1 / (2 × 縮放比)` 個原始像素，符合整數量化的理論下限。

---

## 建議工作流程

v1.2 起壓縮引擎已內建（與 JR-Img-Compresser 同一套），一律一步到位：

**要縮尺寸**：模式 A + 尺寸調整開啟，壓縮模式依素材選
`無損`（要再進引擎轉檔的素材）或 `智慧有損`（直接上線的素材，最小）。

**只要壓縮、不縮尺寸**：關閉「啟用 Resize」即可，atlas 會原樣複製。

先縮放後壓縮的順序由管線自動保證——縮放發生在編碼之前，
不會出現「量化過的顏色再經一次插值」的二次劣化。
