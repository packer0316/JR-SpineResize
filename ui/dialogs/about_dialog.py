"""關於／原理說明"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from config.constants import APP_NAME, APP_TITLE
from config.version import BUILD_DATE, VERSION

_EXPLANATION = """
<h3>為什麼只改 .atlas，不改 .skel？</h3>
<p>Spine runtime 計算貼圖頂點的方式是：</p>
<pre>regionScaleX = attachment.width / region.origWidth
localX       = -attachment.width / 2 + region.offsetX * regionScaleX
localX2      = localX + region.sizeWidth * regionScaleX</pre>
<p>
其中 <code>attachment.width</code> 來自 <b>.skel</b>，其餘三個值來自 <b>.atlas</b>。
把 atlas 內的 <code>xy / size / orig / offset / 頁面尺寸</code> 全部同乘 s 之後：
<code>origWidth</code> 縮小讓 <code>regionScaleX</code> 等比放大，而
<code>offsetX</code> 與 <code>sizeWidth</code> 各自縮小，兩者相乘剛好抵銷，
算出來的 <code>localX</code> 與 <code>localX2</code> 完全不變。
</p>
<p>
Mesh 也一樣：UV 是以頁面尺寸正規化的比值，頂點座標存在 .skel 且與 atlas 無關。
</p>
<p><b>結論：等比縮貼圖時 .skel 不需要也不應該修改，本工具只會原樣複製它。</b></p>

<h3>整張圖直接縮小會發生什麼事？</h3>
<ul>
<li><b>滲色</b>：縮放濾鏡的取樣半徑會跨過圖塊之間的間距，把隔壁圖塊的顏色吃進來。
實測在真實素材上，單一圖塊邊緣的色差最高可達 246/255。</li>
<li><b>接縫</b>：圖塊邊界落在非整數位置，四捨五入後會多切或少切一排像素。</li>
</ul>
<p>
本工具改成逐圖塊裁切、各自縮放、再放回新頁面對應位置，
兩個問題都不會發生（實測與「單獨縮放該圖塊」的結果完全一致，色差 0）。
</p>

<h3>殘留誤差</h3>
<p>
atlas 座標必須是整數。被裁切過的區塊（<code>offset</code> 不為 0 或
<code>orig</code> 不等於 <code>size</code>）在縮小後，
<code>size/orig</code> 這個分數不一定能用更小的整數精確表示，
會殘留約半個到一個原始像素的誤差。這是縮小 atlas 的數學下限，
處理報告中會明確列出實際數值。
</p>

<h3>合圖編輯器：每個元件各自縮放為什麼也安全？</h3>
<p>
上面的算式只用到 <b>同一個區塊自己</b> 的三個數字。<code>size</code> /
<code>orig</code> / <code>offset</code> 是這個區塊內部的比值，只要
<b>這個區塊</b> 的 x 與 y 用同一個比例縮，算出來的 <code>localX</code> /
<code>localX2</code> 就完全不變——隔壁元件用什麼比例完全無關。
</p>
<p>
元件在頁面上的 <b>位置</b> 只影響 UV 取樣的起點，Spine 會拿 <code>xy</code>
與頁面尺寸重算 UV，所以重新排版也不會破圖。Mesh 的 UV 同樣是「元件內的
正規化比值」，跟著元件走。
</p>
<p>
<b>唯一的限制是同一個元件的 x/y 必須同比例</b>，這也是編輯器只提供等比縮放
（沒有單獨拉寬、拉高）的原因。
</p>

<h3>一張合圖被多份 .skel 共用時</h3>
<p>
實測素材裡有兩組「三個 <code>.skel</code> 指向同一張 <code>.png</code>」。
只要其中一份 atlas 自己重排版面，另外兩份的座標就全錯。所以版面是
<b>貼圖的屬性，不是 atlas 的屬性</b>：編輯單位是「一張合圖 + 所有引用它的
atlas」，元件以來源頁面矩形認身分並取所有 atlas 的聯集，
輸出的貼圖會畫出聯集裡的每一個元件，套用後共用者拿到完全一樣的座標。
</p>
"""


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"關於 {APP_NAME}")
        self.setMinimumSize(620, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel(f"{APP_NAME} — {APP_TITLE}")
        title.setProperty("role", "heading")
        layout.addWidget(title)

        subtitle = QLabel(f"版本 {VERSION}　建置日期 {BUILD_DATE}")
        subtitle.setProperty("role", "hint")
        layout.addWidget(subtitle)

        browser = QTextBrowser()
        browser.setHtml(_EXPLANATION)
        browser.setOpenExternalLinks(True)
        layout.addWidget(browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.setCenterButtons(True)
        layout.addWidget(buttons, 0, Qt.AlignmentFlag.AlignRight)
