"""页面解析与指纹（docs/11-link-backfill-and-monitoring.md §6.2）。

- ``extract_main_text(html) -> (title, text)``：标准库 ``HTMLParser``，跳过导航 / 脚本等区块，优先 ``<article>`` / ``<main>``；
- ``normalize_title``：标题比对用归一化（剥离站点后缀）；
- ``simhash64`` / ``hamming_distance``：正文 64 位 SimHash（有符号存储，可直接写 ``BIGINT``）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser

MAX_TEXT_CHARS = 40000
MAIN_TEXT_MIN_CHARS = 200
SIMHASH_MIN_TEXT_CHARS = 80

_MASK64 = (1 << 64) - 1
_SIGN_BIT = 1 << 63

SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "aside", "header", "iframe", "template"})
MAIN_TAGS = frozenset({"article", "main"})
BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul", "title", "body", "html", "head",
})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

TITLE_SEPARATORS = (" - ", " | ", " _ ")
TITLE_SUFFIX_MIN_REMAINING = 4

_WS_RE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


class _MainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.main_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "meta":
            attr = {k.lower(): (v or "") for k, v in attrs}
            if self.og_title is None and (attr.get("property", "").lower() == "og:title" or attr.get("name", "").lower() == "og:title"):
                content = _collapse(attr.get("content", ""))
                if content:
                    self.og_title = content
            return
        if tag in VOID_TAGS:
            if tag in BLOCK_TAGS:
                self._append(" ")
            return
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth > 0:
            return
        if tag == "title":
            self.in_title = True
        if tag in MAIN_TAGS:
            self.main_depth += 1
        if tag in BLOCK_TAGS:
            self._append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <tag/>：自闭合，不进入跳过 / 正文区域
        if tag.lower() == "meta":
            self.handle_starttag(tag, attrs)
        elif tag.lower() in BLOCK_TAGS:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth > 0:
            return
        if tag == "title":
            self.in_title = False
        if tag in MAIN_TAGS and self.main_depth > 0:
            self.main_depth -= 1
        if tag in BLOCK_TAGS:
            self._append(" ")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth > 0:
            return
        self._append(data)

    def _append(self, data: str) -> None:
        if self.skip_depth > 0 or self.in_title:
            return
        self.all_parts.append(data)
        if self.main_depth > 0:
            self.main_parts.append(data)


def extract_main_text(html: str) -> tuple[str | None, str]:
    """返回 ``(title, text)``：``title`` 取 ``og:title``，其次 ``<title>``；``text`` 为正文纯文本。

    跳过 script/style/noscript/svg/canvas/form/nav/footer/aside/header/iframe；``<article>`` / ``<main>``
    内文本合并后 ≥ 200 字符时采用，否则用全文；合并空白、截断 40000 字符。
    """
    if not html:
        return None, ""
    parser = _MainTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 时保留已解析部分
        pass
    title = parser.og_title or _collapse("".join(parser.title_parts)) or None
    main_text = _collapse("".join(parser.main_parts))
    text = main_text if len(main_text) >= MAIN_TEXT_MIN_CHARS else _collapse("".join(parser.all_parts))
    return (title[:1000] if title else None), text[:MAX_TEXT_CHARS]


def _strip_punct_and_space(text: str) -> str:
    return "".join(ch for ch in text if not (ch.isspace() or unicodedata.category(ch)[0] in ("P", "Z", "C")))


def normalize_title(title: str | None) -> str:
    """NFKC → 去掉站点后缀（最后一个 ``" - "`` / ``" | "`` / ``" _ "`` / ``"｜"`` 之后的部分，仅当剩余 ≥ 4 字符）
    → 去标点与空白 → 小写；``None`` → ``""``。"""
    if not title:
        return ""
    # 全角竖线 "｜" 在 NFKC 下变为 "|"（两侧无空格），先标准化为 " | " 以统一按分隔符处理
    text = unicodedata.normalize("NFKC", title.replace("｜", " | "))
    cut = max(text.rfind(sep) for sep in TITLE_SEPARATORS)
    if cut >= 0:
        remaining = text[:cut].strip()
        if len(remaining) >= TITLE_SUFFIX_MIN_REMAINING:
            text = remaining
    return _strip_punct_and_space(text).lower()


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2EBEF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文音节
    )


def simhash_features(text: str) -> Counter[str]:
    """SimHash 特征：中文按字符 3-gram（连续 CJK 片段不足 3 字时取整段）、英文 / 数字按小写单词；值为频次。"""
    features: Counter[str] = Counter()
    if not text:
        return features
    text = unicodedata.normalize("NFKC", text).lower()
    cjk_run: list[str] = []
    word: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        run = "".join(cjk_run)
        if len(run) < 3:
            features[run] += 1
        else:
            for i in range(len(run) - 2):
                features[run[i:i + 3]] += 1
        cjk_run.clear()

    def flush_word() -> None:
        if word:
            features["".join(word)] += 1
            word.clear()

    for ch in text:
        if _is_cjk(ch):
            flush_word()
            cjk_run.append(ch)
        elif ch.isalnum():
            flush_cjk()
            word.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    return features


def _feature_hash(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")


def to_signed64(value: int) -> int:
    value &= _MASK64
    return value - (1 << 64) if value >= _SIGN_BIT else value


def to_unsigned64(value: int) -> int:
    return value & _MASK64


def simhash64(text: str) -> int:
    """64 位 SimHash（权重 = 频次），返回**有符号** 64 位整数：``v - (1 << 64) if v >= 1 << 63 else v``。

    无任何特征时返回 0。特征哈希使用 blake2b（进程间稳定，不受 ``PYTHONHASHSEED`` 影响）。
    """
    features = simhash_features(text)
    if not features:
        return 0
    vector = [0] * 64
    for feature, weight in features.items():
        h = _feature_hash(feature)
        for bit in range(64):
            if h >> bit & 1:
                vector[bit] += weight
            else:
                vector[bit] -= weight
    value = 0
    for bit, score in enumerate(vector):
        if score > 0:
            value |= 1 << bit
    return to_signed64(value)


def text_simhash(text: str | None, min_chars: int = SIMHASH_MIN_TEXT_CHARS) -> int | None:
    """正文不足 ``min_chars``（80）字符时返回 ``None``（不做正文比对，只比标题）。"""
    if not text or len(text) < min_chars:
        return None
    return simhash64(text)


def hamming_distance(a: int, b: int) -> int:
    """先 ``& ((1 << 64) - 1)`` 转回无符号再异或，取 ``bit_count()``。"""
    return ((a & _MASK64) ^ (b & _MASK64)).bit_count()
