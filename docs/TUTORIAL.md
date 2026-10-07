# Windows 雙軌音訊同步擷取：實作教學

> 教學／研究用文件。讀者假設具備 Python 基礎，不需要音訊程式設計經驗。
> 所有數據都在單一台 Windows 11 機器上實測取得，量測腳本附在 [`tools/`](../tools/)。

---

## 0. 這份文件在教什麼

表面上這是一支卡拉 OK 錄音程式：同時錄下麥克風和電腦正在播放的伴奏，混成一個檔案。

但「同時錄兩個音訊來源並讓它們對齊」這件事，一旦真的動手做，會撞上一整串
**作業系統層面的問題**，而且每一個都不會給你明確的錯誤訊息 —— 它們表現成
「聲音怪怪的」、「按鈕沒反應」、「人聲好像慢半拍」。

這份文件的主題是：**如何把這類模糊的症狀，變成可量測的數字，再變成修正。**

會走過六個真實遇到的問題：

| # | 症狀 | 真正的原因 | 量測結果 |
|---|------|-----------|---------|
| 1 | 「瀏覽資料夾」按下去整個程式凍結 | COM apartment 衝突（MTA vs STA） | MTA 下對話框永不出現，凍結 >25 秒 |
| 2 | 錄久了兩軌逐漸不同步 | WASAPI 緩衝區太小造成掉樣本 | 60 秒掉 9 次，兩軌各偏 40+ ms |
| 3 | 以為有 0.1% 時脈飄移 | 被 #2 的掉樣本誤導的假數據 | 修好 #2 後飄移降到 0.003% |
| 4 | 試聽播放速度偏慢、有雜音 | 函式庫的 partial-chunk 缺陷 | 播放速率只有 0.81x |
| 5 | 人聲比伴奏晚 | 硬體延遲（輸出 + 擷取）未補償 | +63.9 ms（σ=0.7 ms） |
| 6 | 啟動用的批次檔完全不能執行 | cmd.exe 的 `chcp` 解析錯位 | 第 3 行起每一行都被打亂 |

每一節都遵循同一個流程：**觀察症狀 → 建立可重現的量測 → 定位原因 → 修正 → 驗證數字改善。**

---

## 1. 問題定義

### 1.1 要做什麼

- 來源 A：麥克風（使用者唱歌）
- 來源 B：電腦正在播放的聲音（伴奏）
- 輸出：兩條獨立分軌 + 一條對齊後的混音

### 1.2 為什麼不簡單

兩個來源是**兩個獨立的硬體裝置**，各有自己的：

- 取樣時脈（石英振盪器，彼此略有差異）
- 緩衝區與排程
- 端到端延遲

作業系統不會幫你對齊它們。你拿到的只有「資料什麼時候送到你的程式」，而那**不等於**
「聲音實際發生的時刻」。這兩者之間的落差，就是本文大部分篇幅在處理的東西。

---

## 2. 原理：WASAPI Loopback

### 2.1 為什麼不需要虛擬音效線

網路上錄製系統聲音的教學，多數會叫你裝 VB-Cable 之類的虛擬音效裝置，或打開
「立體聲混音」(Stereo Mix)。**在 Windows Vista 之後這些都不必要。**

WASAPI（Windows Audio Session API）提供 **loopback capture**：對任何一個**輸出**
裝置開一條擷取串流，拿到的就是「正在送往這個裝置的混音訊號」。

```
應用程式 A ─┐
應用程式 B ─┼─→ [Windows 音訊引擎混音] ─→ [輸出裝置] ─→ 喇叭
應用程式 C ─┘            │
                         └─→ loopback 擷取 ← 我們從這裡取資料
```

關鍵觀念：**每一個輸出裝置各有各的 loopback。** 選錯裝置就錄到一片安靜 ——
這是初學者最常踩的坑。要選「使用者實際在聽的那一個」。

### 2.2 在 Python 裡怎麼做

本專案用 [`soundcard`](https://github.com/bastibe/SoundCard)，它把 loopback 包裝成
「看起來像麥克風的錄音裝置」：

```python
import soundcard as sc

# 每個喇叭都能當成 loopback 來源
for spk in sc.all_speakers():
    lb = sc.get_microphone(spk.id, include_loopback=True)
    print(lb.name, lb.isloopback)   # -> True

# 錄 2 秒系統播放的聲音
with lb.recorder(samplerate=48000, channels=2) as rec:
    data = rec.record(numframes=48000 * 2)   # (96000, 2) float32
```

驗證它真的有效的方法：播一個已知頻率的純音，錄回來做 FFT。

```python
# tools 裡的做法：播 440 Hz，錄回來驗證主頻率
spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
freq = np.fft.rfftfreq(len(seg), 1 / sr)
print(freq[np.argmax(spec)])     # -> 440.0
```

**實測結果：440.0 Hz，完全吻合。** 這一步很重要 —— 它排除了「根本沒錄到東西」
和「錄到的是別的東西」兩種可能，後面的量測才站得住腳。

---

## 3. 環境建置

### 3.1 需求

- Windows 10 或 11（loopback 是 Windows 專有機制）
- Python 3.10 以上（本專案在 3.14.7 上開發）

### 3.2 安裝

```bat
git clone https://github.com/Hung-NCKU/ktv.git
cd ktv
setup.bat
```

`setup.bat` 會建立 `.venv`、安裝套件、列出偵測到的音訊裝置。

### 3.3 套件選擇的理由

| 套件 | 用途 | 為什麼選它 |
|------|------|-----------|
| `soundcard` | WASAPI 存取 | 純 Python + cffi，原生支援 loopback，不需編譯 |
| `soundfile` | WAV 讀寫 | 包裝 libsndfile，支援 24-bit 與 float |
| `numpy` | 訊號處理 | 混音、重新取樣、FFT |
| `imageio-ffmpeg` | MP3/M4A 轉檔 | **把 ffmpeg 執行檔直接裝進 venv**，不需 sudo、不需系統安裝 |

最後一項值得特別說明。常見做法是要求使用者自己安裝 ffmpeg 並加入 PATH，但那是
部署時最常失敗的環節。`imageio-ffmpeg` 把靜態編譯的 ffmpeg 當成 Python 套件的
資料檔發佈：

```python
import imageio_ffmpeg
exe = imageio_ffmpeg.get_ffmpeg_exe()
# -> ...\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
```

整個環境自我封閉在專案資料夾裡，刪掉 `.venv` 就乾淨移除。

### 3.4 關於 WSL：為什麼不行

如果你想過「用 WSL 跑比較乾淨」——**不行**。WSL2 是虛擬機，沒有 Windows 音訊裝置
的直通權限：

- 看不到實體麥克風
- 拿不到 WASAPI loopback
- 常見的 PulseAudio 橋接方案只能「播出去」，不能穩定地「錄進來」

擷取這一段必須在 Windows 原生執行。後製轉檔跑在哪裡都無所謂，但既然 ffmpeg 已經
在 venv 裡，也就沒有跨界的理由。

---

## 4. 程式架構

### 4.1 模組

```
ktvrec/
  comapt.py     COM apartment 管理（第 5.1 節）
  devices.py    裝置列舉與解析
  recorder.py   擷取核心：雙執行緒 + 大緩衝 + 獨立寫檔（第 5.2 節）
  mixer.py      對齊、飄移修正、延遲補償、混音（第 5.3、5.5 節）
  take.py       錄好但尚未存檔的一段錄音
  player.py     試聽播放器（第 5.4 節）
  waveform.py   波形圖與拖曳定位
  encode.py     ffmpeg 轉檔
  config.py     設定持久化
  gui.py        tkinter 介面
  cli.py        命令列介面
```

### 4.2 資料流

```
                    ┌─ 麥克風執行緒 ─→ 佇列 ─→ writer ─→ mic.wav ─┐
   開始錄音 ────────┤                                              ├─→ Take
                    └─ loopback 執行緒 → 佇列 ─→ writer ─→ system.wav ┘
                                                                      │
                               ┌──────────────────────────────────────┘
                               ↓
                        對齊 + 飄移修正 + 延遲補償 + 增益
                               ↓
                     記憶體中的混音 ──→ 試聽（可反覆調整重混）
                               ↓
                        按下「確定存檔」才寫檔
```

### 4.3 兩個設計決定

**(a) 分軌永遠保留原音。** 增益與延遲補償只套用在混音上，`mic.wav` 和
`system.wav` 是未經處理的素材。理由：事後想重新平衡或重新對齊時，不必重錄。
研究用途上這更重要 —— 原始資料不該被處理過的版本取代。

**(b) 錄音期間就寫入硬碟，但寫在暫存目錄。** 五分鐘雙軌 48 kHz stereo float32
約 230 MB，不適合全放記憶體；而且程式當掉時素材要還在。所以錄進
`recordings/.pending_<時間戳>/`，使用者按下存檔才搬進正式資料夾。

---

## 5. 六個真實的坑

### 5.1 COM Apartment：為什麼對話框會凍結

#### 症狀

GUI 上的「瀏覽資料夾」按鈕按下去，整個程式凍結，沒有錯誤訊息。

#### 背景知識

Windows 的 COM 有兩種執行緒模型（apartment）：

- **STA**（Single-Threaded Apartment）：有訊息迴圈，UI 與 Shell 元件需要它
- **MTA**（Multi-Threaded Apartment）：沒有訊息迴圈，適合背景工作

一條執行緒進入哪一種是**一次性決定**，之後不能改（除非把所有參考都釋放）。

#### 定位

`soundcard` 在**被匯入時**就對當下的執行緒呼叫 COM 初始化：

```python
# soundcard/mediafoundation.py
class _COMLibrary:
    def __init__(self):
        COINIT_MULTITHREADED = 0x0
        if platform.win32_ver()[0] == '8':
            hr = _ole32.CoInitialize(_ffi.NULL)          # STA
        else:
            hr = _ole32.CoInitializeEx(_ffi.NULL, COINIT_MULTITHREADED)   # MTA
```

Windows 11 走下面那條 → 主執行緒變成 MTA。而 tkinter 的資料夾對話框
(`tk_chooseDirectory` → `SHBrowseForFolder`) 需要 STA。

用 ctypes 直接驗證：

```python
hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) & 0xFFFFFFFF
if hr == 0x80010106:        # RPC_E_CHANGED_MODE
    print("這條執行緒已經是 MTA")
```

```
匯入 soundcard 前: 主執行緒是 STA
匯入 soundcard 後: 主執行緒是 MTA  ← 對話框會卡住
```

#### 量測：對照實驗

寫一支腳本，在兩種 apartment 下各開一次資料夾對話框，並用背景執行緒在 4 秒後
列舉本行程的可見視窗：

| apartment | 4 秒後的可見視窗 | 對話框呼叫 |
|-----------|-----------------|-----------|
| MTA | （沒有輸出 —— 整個行程凍結） | 逾時 >25 秒 |
| STA | `#32770 "選擇資料夾"`、`TkTopLevel` | 4.50 秒正常返回 |

MTA 下連背景執行緒的 `print` 都出不來，說明整個行程（含 GIL）都被卡住。

#### 修正：分工

- **GUI 主執行緒搶先進 STA**，在任何 `import soundcard` 之前。
  soundcard 隨後要求 MTA 會收到 `RPC_E_CHANGED_MODE`，而它的程式碼本來就會
  優雅地忽略這個錯誤（設 `com_loaded = False` 且不拋出）。
- **擷取執行緒各自明確進 MTA**，讓 WASAPI 物件待在自己的 apartment 裡。

```python
# ktvrec/__init__.py —— 必須在任何 import soundcard 之前
from .comapt import init_sta as _init_sta
_init_sta()
```

```python
# ktvrec/recorder.py，每條擷取執行緒開頭
def run(self):
    held = comapt.init_mta()
    try:
        ...
    finally:
        if held:
            comapt.uninit()
```

這樣可行的前提是 `soundcard` 的 `_Microphone` **只存裝置 id 字串**，不跨執行緒
持有 COM 指標；實際的 `IAudioClient` 是在誰呼叫 `recorder()` 就在誰的執行緒上建立。
讀過原始碼確認這一點，才敢這樣切。

#### 驗證

```
主執行緒 apartment: STA (需要 STA)
擷取執行緒 apartment: MTA (需要 MTA)
錄音仍正常: True   錯誤: []
```

#### 教學重點

> 函式庫在 **import 時**產生的副作用，可能跟你自己的程式架構衝突。
> 這類問題不會出現在 API 文件裡，只能讀原始碼。

---

### 5.2 緩衝區大小：掉樣本的代價

#### 症狀

長時間錄音後兩軌不同步，而且偶爾出現 `data discontinuity in recording` 警告。

#### 背景知識

WASAPI 擷取是**環形緩衝區**：裝置持續寫入，你的程式持續讀出。如果讀得不夠快，
新資料會覆寫舊資料 —— 那段聲音就**永遠消失**，而且兩軌從此錯開。

#### 定位

`soundcard` 的 `blocksize` 參數直接決定 WASAPI 緩衝區長度：

```python
# soundcard/mediafoundation.py:549
bufferduration = int(blocksize / samplerate * 10000000)   # 百奈秒
hr = self._ptr[0][0].lpVtbl.Initialize(
    self._ptr[0], sharemode, streamflags, bufferduration, 0, ppMixFormat[0], _ffi.NULL)
```

原本的設定 `blocksize=1024` 在 48 kHz 下只有 **21.3 毫秒**。任何一次 GC 暫停、
磁碟延遲或作業系統排程抖動都足以溢位。

而且原本的擷取迴圈把檔案寫入放在裡面：

```python
while not stop:
    data = rec.record(numframes=bs)
    fh.write(data)          # ← 磁碟一卡頓，擷取就跟著停
```

#### 修正

兩件事一起做：

1. **緩衝區放大到 1 秒**（`buffer_frames=48000`），留足排程餘裕
2. **寫檔搬到獨立執行緒**，擷取迴圈只把資料丟進佇列

```python
q = queue.Queue(maxsize=600)
writer = threading.Thread(target=_drain, args=(q, fh), daemon=True)
writer.start()
with src.recorder(samplerate=sr, channels=ch, blocksize=buffer_frames) as rec:
    while not stop:
        data = rec.record(numframes=pull_frames)   # 每次拉 100 ms
        q.put(data)                                 # 不阻塞在磁碟上
```

#### 量測結果（60 秒雙軌錄音）

| 設定 | 掉資料次數 | 麥克風偏差 | 系統音偏差 |
|------|-----------|-----------|-----------|
| 21 ms 緩衝、寫檔在擷取迴圈內 | 9 | −41.5 ms | −46.8 ms |
| 1 秒緩衝、獨立寫檔執行緒 | **0** | **−0.2 ms** | **+1.8 ms** |

> **關於重現性**：上表的「9 次」是當時那台機器在當時的負載下觀測到的。
> 用 `tools/measure_capture.py --buffer-frames 1024 --inline-write` 重跑時，
> **掉資料的警告不一定每次都觸發** —— 它取決於當下的 CPU 與磁碟負載。
> 後來在較空閒的系統上重跑，警告 0 次，但偏差依然異常：
>
> ```
> 壞  60s  mic     掉資料=  0  偏差=    +1.1 ms  比例= +0.0019%
> 壞  60s  system  掉資料=  0  偏差=   -33.4 ms  比例= -0.0556%
> ```
>
> **比較可靠的判斷指標是「偏差異常放大且兩軌不對稱」**，而不是警告次數。
> 想穩定觸發警告，可以一邊跑量測一邊製造負載（開瀏覽器、跑編譯、複製大檔案）。
>
> 這本身也是個教訓：**時序相關的 bug 不一定每次都現形**。看到一次就要當真，
> 不能因為重跑時沒出現就認為沒事。

#### 怎麼偵測掉資料

`soundcard` 只透過 `warnings` 回報，沒有其他管道。因為警告是在擷取執行緒上發出的，
可以用 thread id 當 key 分辨是哪一軌：

```python
_DISCONTINUITIES: dict[int, int] = {}

def hook(message, category, filename, lineno, file=None, line=None):
    if "discontinuity" in str(message):
        tid = threading.get_ident()
        if tid in _DISCONTINUITIES:
            _DISCONTINUITIES[tid] += 1
            return
    prev(message, category, filename, lineno, file, line)

warnings.showwarning = hook
```

程式會把次數顯示在狀態列。**看到非零就代表那次錄音的同步不可靠。**

#### 教學重點

> 即時音訊處理的迴圈裡不要做阻塞 I/O。緩衝區大小是延遲與穩健性的權衡，
> 錄音場景應該偏向穩健 —— 使用者不在意錄音有 1 秒延遲，但在意掉音。

---

### 5.3 時脈飄移：如何不被假數據騙

這一節是整份文件**方法論上最重要**的部分。

#### 最初的（錯誤的）結論

兩個獨立裝置的取樣時脈必然略有差異。想法是：記下每軌串流的實際起訖時刻，
用「牆鐘時間 vs 取樣數」回推真實取樣率，再重新取樣校正。

第一次量測：

```
mic 實測取樣率    48051.5 Hz
system 實測取樣率 47998.2 Hz
```

差了 **0.1%**。推論：一首四分鐘的歌，人聲會偏離伴奏約 0.25 秒 —— 聽得出來。
看起來很合理，而且我據此寫進了文件。

#### 為什麼這是錯的

後來做了一個簡單的檢查：**如果這是真的時脈差，偏差應該隨時間成比例放大。**

| 錄音長度 | mic 偏差 | 比例 | system 偏差 | 比例 |
|---------|---------|------|------------|------|
| 10 s | +4.9 ms | +0.049% | −0.8 ms | −0.008% |
| 30 s | −4.6 ms | −0.016% | −10.9 ms | −0.037% |
| 60 s | −41.5 ms | −0.069% | −46.8 ms | −0.078% |

**不成比例，而且符號會變。** 更關鍵的是 60 秒時**兩軌同方向偏移**。兩個獨立時脈
不會默契地往同一邊跑 —— 這不是時脈飄移。

對照 5.2 節：60 秒那次有 9 次 `data discontinuity`。**偏差的來源是掉樣本。**

修好 5.2 之後重測：

| 錄音長度 | 掉資料 | mic 偏差 | 比例 | system 偏差 | 比例 |
|---------|-------|---------|------|------------|------|
| 30 s | 0 | −1.1 ms | −0.004% | +0.5 ms | +0.002% |
| 60 s | 0 | −0.2 ms | −0.000% | +1.8 ms | +0.003% |
| 60 s | 0 | +0.6 ms | +0.001% | +2.2 ms | +0.004% |

**兩個裝置的時脈其實都穩在 48000 Hz，差距在 0.003% 以內** ——
四分鐘累積約 8 ms，聽不出來。原本那個 0.1% 完全是掉樣本污染出來的假象。

#### 修正後的設計

飄移修正保留了，但只在**可信的條件下**才動手：

```python
MIN_DRIFT_FIX_SEC = 15.0     # 太短時量測誤差大於真實飄移
MAX_DRIFT_RATIO = 0.005      # 超過這個比例代表量測出錯

fixed = (t.discontinuities == 0          # ← 關鍵：掉過資料就不修
         and span >= MIN_DRIFT_FIX_SEC
         and 0 < ratio <= MAX_DRIFT_RATIO)
```

第一個條件是本節的教訓：**掉過資料的軌道，少掉的取樣數是「真的不見的時間」，
不是時脈跑得慢。** 拉伸整軌等於把一個局部的斷點抹散到整首歌，只會更糟。

#### 教學重點

> 這是研究方法的核心問題：**一個看起來合理的量測，可能在量另一件事。**
>
> 保護自己的方法：
> 1. **檢查量測是否符合物理預期的尺度關係。** 時脈飄移應該與時間成正比 ——
>    不成正比就是警訊。
> 2. **留意不該相關的東西卻相關了。** 兩個獨立時脈同方向偏移，是原因共通的訊號。
> 3. **先排除已知的汙染源，再解讀數據。** 掉樣本的警告其實一直都在，
>    我卻先去解釋數字、而不是先處理警告。

---

### 5.4 播放器的 partial-chunk 陷阱

#### 症狀

試聽功能做好後，播放頭前進得比實際時間慢。

#### 量測

```
播放頭單調遞增: True
播放頭速率: 0.808x (期望 ~1.0)
3 秒後位置: 2.38s
```

0.81x —— 餵資料跟不上即時，代表播放會有斷音。

#### 定位

讀 `soundcard` 的播放實作：

```python
# soundcard/mediafoundation.py，_Player.play()
while data.nbytes > 0:
    towrite = self._render_available_frames()     # 緩衝區目前的空位
    if towrite == 0:
        time.sleep(0.001)
        continue
    bytes = data[:towrite].ravel().tobytes()      # 但 data 可能比空位短！
    buffer = self._render_buffer(towrite)         # 仍然跟 WASAPI 要 towrite 個 frame
    _ffi.memmove(buffer[0], bytes, len(bytes))    # 只填了 len(bytes)
    self._render_release(towrite)                 # 卻宣告 towrite 個 frame 都有效
    data = data[towrite:]
```

**當餵進去的資料比緩衝區空位短時**，`GetBuffer(towrite)` 取得的空間只有前面一部分
被填入，剩下的保留緩衝區裡的殘留內容，而 `ReleaseBuffer(towrite)` 卻宣告整段都是
有效音訊 —— 殘留資料被當成聲音播出去，而且每一塊只貢獻一部分真實資料，
播放速率因此掉下來。

原本的設計是「送小塊以求定位靈敏」（chunk 1200 frames，buffer 4800 frames），
正好踩中。

#### 修正

每次餵滿一整個緩衝區，最後不足的一塊補零：

```python
self.chunk = buffer_frames          # 不能小於緩衝區

block = self.data[start:end]
if len(block) < self.chunk:
    block = np.pad(block, ((0, self.chunk - len(block)), (0, 0)))
p.play(block)
```

#### 驗證

```
播放頭速率: 1.020x (期望 ~1.0)
3 秒後位置: 3.00s
```

但速率正常不代表音質沒問題。用 loopback 把播放輸出錄回來，和 `soundcard` 原生
整段播放做對照（量 1 kHz 純音的主峰能量佔比）：

| 執行次數 | 原生整段播放 | 本專案 Player |
|---------|------------|--------------|
| 1 | 72.0% | 69.6% |
| 2 | 73.8% | 72.3% |
| 3 | 71.8% | **74.6%** |

第一次看起來差 2.4 個百分點，但三次下來兩者範圍完全重疊，第三次本專案還比較好。
**那 2.4 點是雜訊，播放品質與原生相當。**

（順帶一提：~72% 的純度偏低是該音訊裝置路徑本身的特性 —— 每次錄到的振幅都不同，
顯示有動態音量處理。不是程式造成的。）

#### 教學重點

> **單次量測的差異不要直接當結論。** 重複三次、看範圍是否重疊，是最低成本的
> 變異數檢查。如果我只跑一次，就會去追一個不存在的問題。
>
> 另外：有「對照組」才能分辨「我的程式有問題」和「這個環境本來就這樣」。

---

### 5.5 硬體延遲補償

#### 症狀

使用者回報：「感覺 MIC 有延遲。」

#### 為什麼會這樣

這其實是 loopback 架構的**固有特性**，不是 bug：

```
伴奏資料 ──→ [音訊引擎] ──→ [輸出裝置緩衝] ──→ 喇叭發聲
                  │                                 │
                  └─→ loopback 擷取                 │ 空氣傳播
                      （時間點 T）                   ↓
                                              使用者聽到（時間點 T + 輸出延遲）
                                                     ↓ 使用者跟著唱
                                              麥克風收音
                                                     ↓ 擷取延遲
                                              程式收到（T + 輸出延遲 + 擷取延遲）
```

Loopback 抓到的是「即將送去喇叭」的訊號，但使用者是在那之後才聽到。他跟著唱的
聲音還要再經過麥克風的擷取延遲才進來。**兩段延遲相加，人聲就落在伴奏後面。**

只用「資料抵達時刻」對齊補不了這一段 —— 那個時刻本身就已經包含了延遲。

#### 量測方法

這是本專案最乾淨的一次量測，方法值得學：

1. 合成一串**短脈衝**（6 ms 的 1 kHz 窗化正弦，每秒一個，共 8 個）
2. 透過喇叭**外放**播放，同時錄 loopback 與麥克風
3. 兩軌各自做 **1 kHz 帶通 → 取包絡 → 閾值偵測起始點**
4. 套上 mixer 算出的對齊位移後，比較同一個脈衝落在哪裡

```python
def band_env(x):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(f < 850) | (f > 1150)] = 0          # 只留 1 kHz 附近
    env = np.abs(np.fft.irfft(spec, n=len(x)))
    k = np.ones(int(0.004 * SR))
    return np.convolve(env, k / k.sum(), mode="same")    # 4 ms 平滑
```

用脈衝起始點比用互相關好：**結果可以逐一檢視**，一眼看出一致性。

#### 結果

```
每個脈衝的 mic-system 差值 (ms):
  +64.0  +65.0  +65.9  +63.8  +64.0  +63.7  +63.8  +63.6

中位數 +63.9 ms，標準差 0.7 ms
```

標準差 0.7 ms —— 同一次錄音內高度一致。

**反例：** 第一次量測時預設輸出是無線耳機，結果是 +303.6 ms，但互相關峰值
與旁瓣比只有 1.34，而且麥克風根本收不到戴在頭上的耳機聲音。
**那次量測不可信，不應該採用。** 判斷量測品質跟做量測一樣重要。

#### 重要修正：單次精準 ≠ 可重現

把同一個量測重跑五次（同一台機器、同一組裝置），結果如下：

| 次 | mic 對齊位移 | 需要的補償 | 兩者相加（物理延遲） |
|---|---|---|---|
| A | +13.1 ms | +63.9 ms | 77.0 ms |
| B | +30.3 ms | +42.0 ms | 72.3 ms |
| C | +9.9 ms | +64.3 ms | 74.2 ms |
| D | +13.4 ms | +69.0 ms | 82.4 ms |
| E | +9.5 ms | +62.1 ms | 71.6 ms |

每一次的標準差都只有 0.1–0.2 ms，但**不同次之間需要的補償值從 42 到 69 ms**，
相差 27 ms。

拆解之後原因很清楚：

- **物理延遲其實相當穩定**：約 75 ± 5 ms（最右欄）
- **變動來自對齊基準**：`t_start` 量的是「Python 收到第一塊資料的時刻」，
  其中包含不固定的串流啟動延遲，兩軌各開一次就差了 9.5 ~ 30.3 ms

也就是說，**誤差的來源是我的對齊方法本身，不是硬體延遲在變。**

#### 這對使用者意味著什麼

存下來的補償值只能做到「大致正確」—— 典型誤差 ±10 ms，偶爾 ±15 ms。
10～15 ms 的人聲偏移在感知邊緣（約 15–20 ms 以上開始聽得出 slap 感），
所以多數情況可用，偶爾會覺得差一點。

想徹底解決，要換掉對齊的依據：**WASAPI 的 `IAudioCaptureClient::GetBuffer`
會回傳該筆資料的 QPC 時間戳**，那才是「這段聲音實際發生的時刻」，不受
Python 排程影響。`soundcard` 目前把這個參數忽略掉：

```python
# soundcard/mediafoundation.py
# ignore _ole32.AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, since we don't use time stamps.
```

拿到那個時間戳就能用裝置自己的時基對齊，把這 20 ms 的不確定性消掉。
見第 7.1 節。

#### 教學重點

> **「同一次量測內很一致」和「每次量測都得到同樣結果」是兩件事。**
>
> 我原本只跑一次，看到 σ=0.7 ms 就認定量得很準 —— 其實那只證明了
> 「這一次錄音裡的 8 個脈衝彼此一致」，完全沒有驗證「換一次錄音還會不會一樣」。
>
> 判斷一個量測值能不能當常數使用，必須**跨次重複**，而不只是看單次的離散程度。
> 而且當跨次變動出現時，要拆解它來自哪一環 —— 這裡拆出「物理延遲穩定、
> 對齊基準不穩」，直接指出了正確的改進方向。

#### 修正：可調整的時間微調

延遲因裝置而異（實測：螢幕喇叭 64 ms、無線耳機 300 ms 級），無法寫死。
做成使用者可調的參數：

```python
@dataclass
class TrackInfo:
    ...
    manual_offset_ms: float = 0.0    # 負值 = 把這一軌往前拉
```

混音時要注意：**位移可能是負的**，不能簡單地 `max(0, offset)`，否則會切掉軌道開頭。
正確做法是先算出所有軌道的起點（可為負），再以最早的起點當原點：

```python
start = (int(round((t.t_start - t0) * samplerate))
         + int(round(t.manual_offset_ms / 1000.0 * samplerate)))
...
origin = min(start for _, _, _, start in prepared)      # 可能是負數
n_out = max(start - origin + data.shape[0] for ...)
offset = start - origin                                  # 保證 >= 0
```

#### 驗證

套用不同微調值，重新量混音裡的殘餘偏差：

| 套用微調 | 殘餘偏差 |
|---------|---------|
| 0 ms | +66.3 ms |
| −32 ms | +34.3 ms |
| **−64 ms** | **+2.3 ms** |
| −96 ms | −29.7 ms |

完全線性，每調 32 ms 殘餘就變 32 ms。−64 幾乎歸零。

剩下的 2.3 ms 是喇叭到麥克風約 0.8 公尺的空氣傳播（343 m/s）。戴耳機錄音時
不會有這一項。

#### 一個設計上的教訓

第一版把這個值存成單一全域設定。問題：換到延遲差很多的裝置時，會沿用上一個裝置的值
—— 而使用者**不會察覺**。改成依輸出裝置分別記住，沒記錄過的裝置一律從 0 開始：

```python
def _saved_offset(self) -> float:
    """沒有記錄過的裝置一律從 0 開始。
    不沿用上一個裝置的值 —— 延遲本來就因裝置而異，沿用等於給一個
    幾乎肯定錯、而且使用者不會察覺的數字。"""
```

> **沉默的錯誤比明顯的錯誤危險。** 0 是明顯的「還沒校準」；沿用別的裝置的 64
> 看起來像「已經校準好了」。

---

### 5.6 兩個環境層面的坑

這兩個跟音訊無關，但都是 Windows 開發的典型陷阱。

#### (a) tkinter 的 `_w` 名稱衝突

自訂的波形 widget 一 `grid()` 就拋出莫名的錯誤：

```
_tkinter.TclError: invalid window shortcut, "0" should be '-', 'x', or '^'
```

原因：

```python
class WaveformView(tk.Canvas):
    def __init__(self, ...):
        self._w = 0        # ← 想存畫布寬度
        self._h = height
```

**`tkinter.Misc._w` 是 widget 的 Tk 路徑名稱字串。** 蓋掉它，所有 Tk 呼叫都會壞。
改名為 `_cw` / `_ch` 即解決。

> 繼承框架類別時，私有屬性名稱也可能撞車。遇到「不可能發生」的錯誤，
> 先懷疑是不是蓋掉了父類別的東西。

#### (b) cmd.exe 的 `chcp` 解析錯位

啟動用的批次檔完全不能執行：

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
```

```
'~dp0"' is not recognized as an internal or external command
'環境，請先執行' is not recognized as an internal or external command
```

第 3 行是純 ASCII 卻被打亂。原因：**cmd.exe 以位元組位移逐行讀取批次檔，
中途切換編碼會讓讀取位置錯位**，後面每一行都被重新切割。

修正方式是**根本不要在批次檔裡切換編碼**：

- 批次檔用系統原生編碼存檔（本例為 cp950），中文直接正確顯示
- 需要 UTF-8 的地方改在 Python 裡設定，不影響批次檔解析：

```python
ctypes.windll.kernel32.SetConsoleOutputCP(65001)
ctypes.windll.kernel32.SetConsoleCP(65001)
```

**另一個相關的坑：** 批次檔裡用相對路徑啟動程式，換一種啟動方式就失效：

```bat
start "" ".venv\Scripts\pythonw.exe" -m ktvrec       ← cmd /c 可以，Start-Process 不行
start "KTV" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" -m ktvrec   ← 都可以
```

> **驗證啟動腳本時要用使用者真正會用的方式。** 開發期間我都是直接呼叫
> `pythonw`，完全繞過批次檔，所以這個 bug 存活了很久。

---

## 6. 量測方法論總結

從這六個問題歸納出的做法：

### 6.1 先讓症狀變成數字

「聲音怪怪的」無法除錯。要先找到一個能重複執行、輸出數字的量測。

| 症狀 | 變成什麼數字 |
|------|------------|
| 對話框沒反應 | 對話框視窗是否在 N 秒內出現 |
| 不同步 | `data discontinuity` 次數、取樣數 vs 牆鐘時間 |
| 播放怪 | 播放頭速率（位置變化 ÷ 時間變化） |
| 人聲慢半拍 | 同一個脈衝在兩軌的樣本位置差 |

### 6.2 善用 loopback 當量測儀器

Loopback 讓程式能**錄下自己的輸出**，這是一個完整的量測迴路：

```
合成已知訊號 → 播放 → loopback 錄回 → 分析
```

5.4 節驗證播放品質、5.5 節量延遲都用這個迴路。這是音訊程式設計相對幸運的地方：
不需要額外硬體就能自我驗證。

### 6.3 判斷量測是否可信

本專案採用的判準：

- **一致性**：重複量測的標準差（5.5 節的 0.7 ms vs 無線耳機那次的雜亂）
- **訊噪比**：相關峰值與旁瓣的比值（1.34 太低 → 棄用）
- **尺度關係**：是否符合物理預期（5.3 節的關鍵檢查）
- **對照組**：和已知的參考實作比較（5.4 節）
- **重複次數**：單次差異可能只是雜訊（5.4 節的三次重測）

### 6.4 修正後要重新量

每一節都以「修正後的數字」結尾。這不只是確認修好了，有時會發現：

- 修正的幅度不如預期（還有別的原因）
- 修好了 A 卻讓 B 變差
- **原本的問題診斷是錯的**（5.3 節正是這樣翻盤的）

---

## 7. 研究延伸方向

這個專案刻意保留了一些未解決的問題，適合當作練習或研究題目：

### 7.1 用裝置時間戳取代牆鐘對齊（最有價值的改進）

第 5.5 節量到的問題：目前用 `t_start`（Python 收到第一塊資料的時刻）對齊，
會引入 **20 ms 級的隨機不確定性**，而物理延遲本身只有 ±5 ms 的變動。
**對齊方法是誤差的主要來源。**

正解是用裝置自己的時基。`IAudioCaptureClient::GetBuffer` 的簽章：

```c
HRESULT GetBuffer(
  BYTE   **ppData,
  UINT32 *pNumFramesToRead,
  DWORD  *pdwFlags,
  UINT64 *pu64DevicePosition,      // ← 裝置位置
  UINT64 *pu64QPCPosition          // ← QPC 時間戳
);
```

`soundcard` 把後兩個參數丟掉了。取回它們之後，兩軌就能用**同一個 QPC 時基**
對齊，不受 Python 排程影響。

實作路徑：
1. Fork `soundcard`，在 `_record_chunk` 保留 `pu64QPCPosition`
2. 用第一塊資料的 QPC 值取代 `t_start`
3. 用 `tools/measure_latency.py` 重跑五次，看跨次變動是否從 27 ms 降下來

**這是一個邊界清楚、有量化驗收標準的題目**，適合當作實作練習。

### 7.2 自動延遲校準

即使對齊修好了，輸出裝置到麥克風的物理延遲還是要量。可以做成自動：錄音開始前
播一段校準訊號（掃頻、chirp 或 MLS），用互相關自動求出延遲。

**難點：** 戴耳機時麥克風收不到輸出，這個方法失效。可能的替代：
查詢 WASAPI 的 `IAudioClient::GetStreamLatency` 與 `GetDevicePeriod`，
但那只涵蓋 API 層的延遲，不含裝置與無線傳輸的部分。
**研究問題：API 回報的延遲與實測延遲差多少？**（本專案實測物理延遲約 75 ms，
可以拿來和 API 回報值對照。）

### 7.3 時脈飄移的長期行為

本專案只量到 60 秒，且結論是「飄移可忽略」。但：

- 更長的錄音（30 分鐘以上）累積效應如何？
- 溫度變化會不會影響石英振盪器，造成飄移率隨時間改變？
- 如果真的需要修正，線性重新取樣是否足夠，或需要 sinc 插值？

### 7.4 回音消除

目前喇叭外放時麥克風會收到伴奏，混音後伴奏出現兩次。文件的建議是「戴耳機」。
正解是 AEC（Acoustic Echo Cancellation）—— 已知參考訊號（system 軌）的情況下，
用自適應濾波器（NLMS 等）從 mic 軌消除它。

**這是很好的 DSP 練習題**：參考訊號是現成的，而且可以用本專案的量測框架驗證效果。

### 7.5 獨佔模式（Exclusive Mode）

目前用 WASAPI 共享模式，Windows 會做格式轉換與混音。獨佔模式可拿到更低延遲與
位元完全一致的資料，但會鎖住裝置。**研究問題：獨佔模式下的延遲與飄移特性有何不同？**

### 7.6 跨平台

Loopback 是 Windows 機制。對應的方案：

- macOS：需要虛擬裝置（BlackHole、Loopback），或 Core Audio Taps（macOS 14.2+）
- Linux：PulseAudio/PipeWire 的 monitor source

**比較題目**：三個平台的 loopback 延遲特性差異。

---

## 8. 參考資料

### 官方文件

- [WASAPI Loopback Recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- [AUDCLNT_BUFFERFLAGS](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/ne-audioclient-_audclnt_bufferflags) — `DATA_DISCONTINUITY` 的定義
- [COM Apartments: Processes, Threads, and Apartments](https://learn.microsoft.com/en-us/windows/win32/com/processes--threads--and-apartments)
- [IAudioRenderClient::GetBuffer](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiorenderclient-getbuffer) — 5.4 節問題的規格依據

### 原始碼

- [SoundCard](https://github.com/bastibe/SoundCard) — 本專案的 WASAPI 存取層。
  `mediafoundation.py` 值得完整讀過，本文件引用的三個行為都在裡面。
- [python-soundfile](https://github.com/bastibe/python-soundfile)

### 本專案的量測腳本

- [`tools/measure_latency.py`](../tools/measure_latency.py) — 5.5 節的延遲量測
- [`tools/measure_capture.py`](../tools/measure_capture.py) — 5.2、5.3 節的掉樣本與飄移量測

---

## 附錄：如何重現本文的量測

```bat
setup.bat

REM 5.5 節：量測麥克風相對系統音的延遲（需要喇叭外放）
.venv\Scripts\python tools\measure_latency.py --output "ASUS VG328"

REM 5.2、5.3 節：掉樣本與時脈飄移
.venv\Scripts\python tools\measure_capture.py --duration 60
.venv\Scripts\python tools\measure_capture.py --duration 60 --buffer-frames 1024 --inline-write
```

最後一行刻意用原本有問題的設定，可以重現掉樣本的現象 —— 對照用。

> **注意**：量測結果與硬體、驅動、系統負載都有關。本文的數字來自單一台機器，
> 不應當成普適常數。重點是方法，不是數值。
