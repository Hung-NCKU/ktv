"""COM apartment 管理。

soundcard 一被匯入，就會在當下的執行緒上呼叫 CoInitializeEx(MTA)。但 Windows 的
檔案／資料夾對話框和 os.startfile 走的是 Shell，需要 STA；在 MTA 執行緒上呼叫它們
會直接卡死 —「瀏覽…」和「開啟錄音資料夾」按鈕就是這樣沒反應的。

解法是分工：
  - GUI 主執行緒搶在 soundcard 之前進 STA。soundcard 之後再要求 MTA 會收到
    RPC_E_CHANGED_MODE，它自己就會忽略並照常運作（見 mediafoundation._COMLibrary）。
  - 錄音執行緒各自明確進 MTA，讓 WASAPI 物件待在自己的 apartment 裡。

這樣可行的前提是 soundcard 的 _Microphone 只存裝置 id 字串，不跨執行緒持有 COM
指標；實際的 IAudioClient 是在誰呼叫 recorder() 就在誰的執行緒上建立。
"""

from __future__ import annotations

import ctypes
import sys

COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106

_ole32 = ctypes.windll.ole32 if sys.platform == "win32" else None


def _init(flags: int) -> bool:
    """初始化本執行緒的 COM。回傳是否由我們持有參考（決定要不要 uninit）。"""
    if _ole32 is None:
        return False
    hr = _ole32.CoInitializeEx(None, flags) & 0xFFFFFFFF
    # RPC_E_CHANGED_MODE：這條執行緒已經在另一種 apartment 了，我們沒拿到參考。
    return hr != RPC_E_CHANGED_MODE


def init_sta() -> bool:
    """給 GUI／Shell 對話框用。"""
    return _init(COINIT_APARTMENTTHREADED)


def init_mta() -> bool:
    """給 WASAPI 擷取執行緒用。"""
    return _init(COINIT_MULTITHREADED)


def uninit() -> None:
    if _ole32 is not None:
        _ole32.CoUninitialize()


def is_sta() -> bool:
    """本執行緒是不是 STA。用來確認 Shell 對話框叫得動。"""
    if _ole32 is None:
        return True
    hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) & 0xFFFFFFFF
    if hr == RPC_E_CHANGED_MODE:
        return False
    _ole32.CoUninitialize()
    return True
