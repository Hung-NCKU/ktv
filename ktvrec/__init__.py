"""ktvrec - 同時錄製麥克風與電腦播放聲音 (Windows WASAPI loopback)。"""

from .comapt import init_sta as _init_sta

# 必須在任何 `import soundcard` 之前執行，否則主執行緒會被 soundcard 設成 MTA，
# 之後所有 Shell 對話框（瀏覽資料夾、開啟資料夾）都會卡死。詳見 comapt.py。
_init_sta()

__version__ = "1.3.0"
