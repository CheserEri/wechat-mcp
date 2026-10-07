"""抖音 Web 解析：浏览器桥 + ``aweme/v1/web/aweme/detail`` 接口。

现状（2026-10-06 实测）
----------------------
解析走**浏览器桥**：用本机 Chrome/Edge 打开作品页，签名由页面自己完成，再从
页面里把详情取回来（见 :func:`fetch_video_via_browser`）。解析与下载都由
``links.py`` 调度，无需登录、也无需导出 Cookie。

为什么不自己算签名
------------------
抖音从 2025 年起给 Web 接口套上了 **ArgusSecurityPlugin** 风控：

- 只带 Cookie 请求 → ``403 Blocked by ArgusSecurityPlugin Uifid Not Found``
- 补上 ``Uifid`` 请求头 → ``403 ... Signature Not Found``
- 还要一个 **``a_bogus`` 签名参数**，由页面里混淆 JS 实时生成

内置 yt-dlp（2026.08.19）的 ``DouyinIE`` 只有一行 ``TODO``，没有实现签名。

本模块曾按 f2 的公开实现移植过一份纯 Python ``a_bogus``，并用真实抓包验证过：
原样重放那条请求返回 ``200``，换成本模块算出的签名返回
``403 ... Signature Invalid``。逐字段解码比对后确认**抖音已经轮换过 SDK**：
线上 ``webmssdk.es5.js`` 的自定义字符表变成了
``Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe``，
而 256 字节置换表藏在 JSVMP 虚拟机里（不是可提取的字面量）。也就是说离线复现
签名要重做一遍 SDK 逆向，且抖音随时可能再轮换——不如直接借浏览器跑一遍页面。

保留的离线实现
--------------
``sm3_digest``：纯 Python 国密 SM3，已用标准测试向量与 ``gmssl`` 交叉验证，
可独立复用（不引入 ``gmssl`` 依赖）。
``ABogus``：与 f2 参考实现**逐字节一致**（用固定时间与随机序列对拍验证过），
算法本身正确，只是对不上抖音当前版本；作为参考保留，**不在解析链路里使用**。

署名见 ``THIRD_PARTY_NOTICES.md``（a_bogus 移植自 f2，Apache-2.0）。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# SM3（GB/T 32905-2016）
# --------------------------------------------------------------------------- #

_SM3_IV = (
    0x7380166F,
    0x4914B2B9,
    0x172442D7,
    0xDA8A0600,
    0xA96F30BC,
    0x163138AA,
    0xE38DEE4D,
    0xB0FB0E4E,
)
_MASK32 = 0xFFFFFFFF


def _rotl32(value: int, bits: int) -> int:
    bits %= 32
    return ((value << bits) | (value >> (32 - bits))) & _MASK32


def _sm3_p0(value: int) -> int:
    return value ^ _rotl32(value, 9) ^ _rotl32(value, 17)


def _sm3_p1(value: int) -> int:
    return value ^ _rotl32(value, 15) ^ _rotl32(value, 23)


def _sm3_ff(x: int, y: int, z: int, j: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _sm3_gg(x: int, y: int, z: int, j: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | ((~x & _MASK32) & z)


def _sm3_cf(v: list[int], block: bytes) -> None:
    words = [
        int.from_bytes(block[i : i + 4], "big") for i in range(0, 64, 4)
    ]
    w = list(words)
    for j in range(16, 68):
        w.append(
            _sm3_p1(w[j - 16] ^ w[j - 9] ^ _rotl32(w[j - 3], 15))
            ^ _rotl32(w[j - 13], 7)
            ^ w[j - 6]
        )
    w1 = [w[j] ^ w[j + 4] for j in range(64)]

    a, b, c, d, e, f, g, h = v
    for j in range(64):
        t = 0x79CC4519 if j < 16 else 0x7A879D8A
        ss1 = _rotl32(
            (_rotl32(a, 12) + e + _rotl32(t, j)) & _MASK32, 7
        )
        ss2 = ss1 ^ _rotl32(a, 12)
        tt1 = (_sm3_ff(a, b, c, j) + d + ss2 + w1[j]) & _MASK32
        tt2 = (_sm3_gg(e, f, g, j) + h + ss1 + w[j]) & _MASK32
        d = c
        c = _rotl32(b, 9)
        b = a
        a = tt1
        h = g
        g = _rotl32(f, 19)
        f = e
        e = _sm3_p0(tt2)
    for index, value in enumerate((a, b, c, d, e, f, g, h)):
        v[index] ^= value


def sm3_digest(data: bytes) -> bytes:
    """计算 SM3 摘要，返回 32 字节。

    自检向量：``sm3_digest(b"abc").hex()`` 应为
    ``66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0``。
    """
    payload = bytes(data)
    length = len(payload)
    payload += b"\x80"
    while len(payload) % 64 != 56:
        payload += b"\x00"
    payload += (length * 8).to_bytes(8, "big")

    state = list(_SM3_IV)
    for offset in range(0, len(payload), 64):
        _sm3_cf(state, payload[offset : offset + 64])
    return b"".join(value.to_bytes(4, "big") for value in state)


# --------------------------------------------------------------------------- #
# 字节/字符工具
# --------------------------------------------------------------------------- #


class _StringProcessor:
    """f2 同名类的移植：字符串 ↔ 码点、JS 无符号右移、随机混淆字节。"""

    @staticmethod
    def to_char_str(values: list[int]) -> str:
        return "".join(chr(i) for i in values)

    @staticmethod
    def to_char_array(text: str) -> list[int]:
        return [ord(char) for char in text]

    @staticmethod
    def js_shift_right(value: int, bits: int) -> int:
        return (value % 0x100000000) >> bits

    @staticmethod
    def generate_random_bytes(length: int = 3) -> str:
        """生成一组伪随机字节（放在签名最前面的混淆头）。"""
        result: list[str] = []
        for _ in range(length):
            seed = int(random.random() * 10000)
            result.extend(
                [
                    chr(((seed & 255) & 170) | 1),
                    chr(((seed & 255) & 85) | 2),
                    chr((_StringProcessor.js_shift_right(seed, 8) & 170) | 5),
                    chr((_StringProcessor.js_shift_right(seed, 8) & 85) | 40),
                ]
            )
        return "".join(result)


# 256 字节置换表（原样取自 f2）。注意 ``transform_bytes`` 会**原地改写**它，
# 所以每次签名都必须新建 :class:`ABogus`，不能复用实例。
_BIG_ARRAY = [
    121, 243, 55, 234, 103, 36, 47, 228, 30, 231, 106, 6, 115, 95, 78, 101, 250, 207, 198, 50,
    139, 227, 220, 105, 97, 143, 34, 28, 194, 215, 18, 100, 159, 160, 43, 8, 169, 217, 180, 120,
    247, 45, 90, 11, 27, 197, 46, 3, 84, 72, 5, 68, 62, 56, 221, 75, 144, 79, 73, 161,
    178, 81, 64, 187, 134, 117, 186, 118, 16, 241, 130, 71, 89, 147, 122, 129, 65, 40, 88, 150,
    110, 219, 199, 255, 181, 254, 48, 4, 195, 248, 208, 32, 116, 167, 69, 201, 17, 124, 125, 104,
    96, 83, 80, 127, 236, 108, 154, 126, 204, 15, 20, 135, 112, 158, 13, 1, 188, 164, 210, 237,
    222, 98, 212, 77, 253, 42, 170, 202, 26, 22, 29, 182, 251, 10, 173, 152, 58, 138, 54, 141,
    185, 33, 157, 31, 252, 132, 233, 235, 102, 196, 191, 223, 240, 148, 39, 123, 92, 82, 128, 109,
    57, 24, 38, 113, 209, 245, 2, 119, 153, 229, 189, 214, 230, 174, 232, 63, 52, 205, 86, 140,
    66, 175, 111, 171, 246, 133, 238, 193, 99, 60, 74, 91, 225, 51, 76, 37, 145, 211, 166, 151,
    213, 206, 0, 200, 244, 176, 218, 44, 184, 172, 49, 216, 93, 168, 53, 21, 183, 41, 67, 85,
    224, 155, 226, 242, 87, 177, 146, 70, 190, 12, 162, 19, 137, 114, 25, 165, 163, 192, 23, 59,
    9, 94, 179, 107, 35, 7, 142, 131, 239, 203, 149, 136, 61, 249, 14, 156,
]

# 两个自定义 base64 字符表（第 0 个用于最终编码，第 1 个用于 UA 编码）
_CHARACTER = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
_CHARACTER2 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"


class _CryptoUtility:
    """f2 同名类的移植：SM3 加盐、置换加密、自定义 base64。"""

    def __init__(self, salt: str, alphabets: list[str]) -> None:
        self.salt = salt
        self.base64_alphabet = alphabets
        self.big_array = list(_BIG_ARRAY)

    @staticmethod
    def sm3_to_array(data: str | list[int]) -> list[int]:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        digest = sm3_digest(raw)
        return list(digest)

    def add_salt(self, param: str) -> str:
        return param + self.salt

    def params_to_array(self, param: str | list[int], add_salt: bool = True) -> list[int]:
        if isinstance(param, str) and add_salt:
            param = self.add_salt(param)
        return self.sm3_to_array(param)

    def transform_bytes(self, values: list[int]) -> str:
        """用 256 字节置换表做异或流变换（会原地改写 ``self.big_array``）。"""
        text = _StringProcessor.to_char_str(values)
        result: list[str] = []
        index_b = self.big_array[1]
        initial_value = 0
        value_e = 0
        for index, char in enumerate(text):
            if index == 0:
                initial_value = self.big_array[index_b]
                total = index_b + initial_value
                self.big_array[1] = initial_value
                self.big_array[index_b] = index_b
            else:
                total = initial_value + value_e
            total %= len(self.big_array)
            result.append(chr(ord(char) ^ self.big_array[total]))

            value_e = self.big_array[(index + 2) % len(self.big_array)]
            total = (index_b + value_e) % len(self.big_array)
            initial_value = self.big_array[total]
            self.big_array[total] = self.big_array[(index + 2) % len(self.big_array)]
            self.big_array[(index + 2) % len(self.big_array)] = initial_value
            index_b = total
        return "".join(result)

    def base64_encode(self, text: str, alphabet_index: int = 0) -> str:
        binary = "".join(f"{ord(char):08b}" for char in text)
        padding = (6 - len(binary) % 6) % 6
        binary += "0" * padding
        table = self.base64_alphabet[alphabet_index]
        out = "".join(
            table[int(binary[i : i + 6], 2)] for i in range(0, len(binary), 6)
        )
        return out + "=" * (padding // 2)

    def abogus_encode(self, text: str, alphabet_index: int) -> str:
        table = self.base64_alphabet[alphabet_index]
        out: list[str] = []
        for i in range(0, len(text), 3):
            if i + 2 < len(text):
                n = (ord(text[i]) << 16) | (ord(text[i + 1]) << 8) | ord(text[i + 2])
            elif i + 1 < len(text):
                n = (ord(text[i]) << 16) | (ord(text[i + 1]) << 8)
            else:
                n = ord(text[i]) << 16
            for shift, mask in zip(range(18, -1, -6), (0xFC0000, 0x03F000, 0x0FC0, 0x3F)):
                if shift == 6 and i + 1 >= len(text):
                    break
                if shift == 0 and i + 2 >= len(text):
                    break
                out.append(table[(n & mask) >> shift])
        out.append("=" * ((4 - len(out) % 4) % 4))
        return "".join(out)

    @staticmethod
    def rc4_encrypt(key: bytes, plaintext: str) -> bytes:
        box = list(range(256))
        j = 0
        for i in range(256):
            j = (j + box[i] + key[i % len(key)]) % 256
            box[i], box[j] = box[j], box[i]
        i = j = 0
        out = bytearray()
        for char in plaintext:
            i = (i + 1) % 256
            j = (j + box[i]) % 256
            box[i], box[j] = box[j], box[i]
            out.append(ord(char) ^ box[(box[i] + box[j]) % 256])
        return bytes(out)


class _BrowserFingerprintGenerator:
    """生成一个浏览器指纹串（屏幕/窗口尺寸 + 平台），喂给签名算法。"""

    @classmethod
    def generate(cls, platform: str = "Win32") -> str:
        inner_w = random.randint(1024, 1920)
        inner_h = random.randint(768, 1080)
        outer_w = inner_w + random.randint(24, 32)
        outer_h = inner_h + random.randint(75, 90)
        size_w = random.randint(1024, 1920)
        size_h = random.randint(768, 1080)
        avail_w = random.randint(1280, 1920)
        avail_h = random.randint(800, 1080)
        screen_y = random.choice([0, 30])
        return (
            f"{inner_w}|{inner_h}|{outer_w}|{outer_h}|0|{screen_y}|0|0|"
            f"{size_w}|{size_h}|{avail_w}|{avail_h}|{inner_w}|{inner_h}|24|24|{platform}"
        )


class ABogus:
    """a_bogus 签名生成器（移植自 f2）。

    每个实例只应用一次：``transform_bytes`` 会改写内部的 256 字节置换表，
    复用实例会让后续签名结果错误。
    """

    def __init__(
        self,
        fp: str = "",
        user_agent: str = "",
        options: list[int] | None = None,
    ) -> None:
        self.aid = 6383
        self.page_id = 0
        self.salt = "cus"
        self.boe = False
        self.ddrt = 8.5
        self.ic = 8.5
        self.paths = [
            "^/webcast/",
            "^/aweme/v1/",
            "^/aweme/v2/",
            "/v1/message/send",
            "^/live/",
            "^/captcha/",
            "^/ecom/",
        ]
        self.options = list(options or [0, 1, 14])
        self.ua_key = b"\x00\x01\x0E"
        self.crypto = _CryptoUtility(self.salt, [_CHARACTER, _CHARACTER2])
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        )
        self.browser_fp = fp or _BrowserFingerprintGenerator.generate("Win32")
        self.sort_index = [
            18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35, 57, 39, 41, 43, 22,
            28, 32, 60, 36, 23, 29, 33, 37, 44, 45, 59, 46, 47, 48, 49, 50, 24, 25, 65, 66, 70, 71,
        ]
        self.sort_index_2 = [
            18, 20, 26, 30, 34, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28, 32, 36, 23, 29, 33,
            37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52, 53, 54, 55, 57, 58, 59, 60, 65, 66, 70, 71,
        ]

    def generate_abogus(self, params: str, body: str = "") -> str:
        """返回 ``a_bogus`` 值（192 字符左右）。"""
        ab_dir: dict[int, Any] = {
            8: 3,
            15: {
                "aid": self.aid,
                "pageId": self.page_id,
                "boe": self.boe,
                "ddrt": self.ddrt,
                "paths": self.paths,
                "track": {"mode": 0, "delay": 300, "paths": []},
                "dump": True,
                "rpU": "",
            },
            18: 44,
            19: [1, 0, 1, 0, 1],
            66: 0,
            69: 0,
            70: 0,
            71: 0,
        }

        start = int(time.time() * 1000)
        array1 = self.crypto.params_to_array(self.crypto.params_to_array(params))
        array2 = self.crypto.params_to_array(self.crypto.params_to_array(body))
        array3 = self.crypto.params_to_array(
            self.crypto.base64_encode(
                _StringProcessor.to_char_str(
                    self.crypto.rc4_encrypt(self.ua_key, self.user_agent)
                ),
                1,
            ),
            add_salt=False,
        )
        end = int(time.time() * 1000)

        ab_dir[20] = (start >> 24) & 255
        ab_dir[21] = (start >> 16) & 255
        ab_dir[22] = (start >> 8) & 255
        ab_dir[23] = start & 255
        # 与 JS 的 ``>> 0`` 一致：这两个高位字节**不截断**（可能 > 255），
        # 截断会让服务端解码出的时间戳对不上。
        ab_dir[24] = int(start / 256 / 256 / 256 / 256)
        ab_dir[25] = int(start / 256 / 256 / 256 / 256 / 256)

        ab_dir[26] = (self.options[0] >> 24) & 255
        ab_dir[27] = (self.options[0] >> 16) & 255
        ab_dir[28] = (self.options[0] >> 8) & 255
        ab_dir[29] = self.options[0] & 255

        ab_dir[30] = int(self.options[1] / 256) & 255
        ab_dir[31] = (self.options[1] % 256) & 255
        ab_dir[32] = (self.options[1] >> 24) & 255
        ab_dir[33] = (self.options[1] >> 16) & 255

        ab_dir[34] = (self.options[2] >> 24) & 255
        ab_dir[35] = (self.options[2] >> 16) & 255
        ab_dir[36] = (self.options[2] >> 8) & 255
        ab_dir[37] = self.options[2] & 255

        ab_dir[38] = array1[21]
        ab_dir[39] = array1[22]
        ab_dir[40] = array2[21]
        ab_dir[41] = array2[22]
        ab_dir[42] = array3[23]
        ab_dir[43] = array3[24]

        ab_dir[44] = (end >> 24) & 255
        ab_dir[45] = (end >> 16) & 255
        ab_dir[46] = (end >> 8) & 255
        ab_dir[47] = end & 255
        ab_dir[48] = ab_dir[8]
        ab_dir[49] = int(end / 256 / 256 / 256 / 256)
        ab_dir[50] = int(end / 256 / 256 / 256 / 256 / 256)

        ab_dir[51] = (self.page_id >> 24) & 255
        ab_dir[52] = (self.page_id >> 16) & 255
        ab_dir[53] = (self.page_id >> 8) & 255
        ab_dir[54] = self.page_id & 255
        ab_dir[55] = self.page_id
        ab_dir[56] = self.aid
        ab_dir[57] = self.aid & 255
        ab_dir[58] = (self.aid >> 8) & 255
        ab_dir[59] = (self.aid >> 16) & 255
        ab_dir[60] = (self.aid >> 24) & 255

        ab_dir[64] = len(self.browser_fp)
        ab_dir[65] = len(self.browser_fp)

        sorted_values = [ab_dir.get(i, 0) for i in self.sort_index]
        edge_fp_array = _StringProcessor.to_char_array(self.browser_fp)

        ab_xor = 0
        for index in range(len(self.sort_index_2) - 1):
            if index == 0:
                ab_xor = ab_dir.get(self.sort_index_2[index], 0)
            ab_xor ^= ab_dir.get(self.sort_index_2[index + 1], 0)

        sorted_values.extend(edge_fp_array)
        sorted_values.append(ab_xor)

        payload = (
            _StringProcessor.generate_random_bytes()
            + self.crypto.transform_bytes(sorted_values)
        )
        return self.crypto.abogus_encode(payload, 0)


# --------------------------------------------------------------------------- #
# Cookie / 请求
# --------------------------------------------------------------------------- #

_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
_UA = _DEFAULT_UA

_DOUYIN_HOSTS = {"douyin.com", "iesdouyin.com"}
_VIDEO_ID_RE = re.compile(r"/(?:video|note|slides)/(\d{6,})")
_VIDEO_ID_QUERY_RE = re.compile(r"[?&](?:modal_id|aweme_id|vid)=(\d{6,})")


def is_douyin_url(url: str) -> bool:
    """是否抖音地址（含短链主机 ``v.douyin.com``）。"""
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower().split(":", 1)[0]
    except ValueError:
        return False
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return any(host == item or host.endswith("." + item) for item in _DOUYIN_HOSTS)


def extract_aweme_id(url: str) -> str:
    """从抖音地址里取作品 ID；取不到返回空串。"""
    if not url:
        return ""
    match = _VIDEO_ID_RE.search(url) or _VIDEO_ID_QUERY_RE.search(url)
    if match:
        return match.group(1)
    # ``www.douyin.com/1234567890123456789`` 这类裸 ID 路径
    match = re.search(r"douyin\.com/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def parse_cookies_file(path: str) -> dict[str, str]:
    """读取 Netscape 格式 cookies.txt，返回 ``name → value``。"""
    cookies: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and parts[5]:
                    cookies[parts[5]] = parts[6]
    except OSError:
        return {}
    return cookies


def _web_params(aweme_id: str) -> list[tuple[str, str]]:
    """构造与真实 Web 客户端一致的查询参数（顺序参与签名，勿随意调整）。"""
    return [
        ("device_platform", "webapp"),
        ("aid", "6383"),
        ("channel", "channel_pc_web"),
        ("aweme_id", aweme_id),
        ("update_version_code", "170400"),
        ("pc_client_type", "1"),
        ("pc_libra_divert", "Windows"),
        ("support_h265", "1"),
        ("support_dash", "1"),
        ("version_code", "190500"),
        ("version_name", "19.5.0"),
        ("cookie_enabled", "true"),
        ("screen_width", "1920"),
        ("screen_height", "1080"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("browser_name", "Chrome"),
        ("browser_version", "131.0.0.0"),
        ("browser_online", "true"),
        ("engine_name", "Blink"),
        ("engine_version", "131.0.0.0"),
        ("os_name", "Windows"),
        ("os_version", "10"),
        ("cpu_core_num", "12"),
        ("device_memory", "8"),
        ("platform", "PC"),
        ("downlink", "10"),
        ("effective_type", "4g"),
        ("round_trip_time", "50"),
    ]


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #


@dataclass
class DouyinVideo:
    """一条抖音作品的解析结果。"""

    aweme_id: str = ""
    ok: bool = False
    title: str = ""
    uploader: str = ""
    duration: int | None = None
    description: str = ""
    video_url: str = ""
    images: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def is_images(self) -> bool:
        return bool(self.images) and not self.video_url


def _pick_play_url(video: dict[str, Any]) -> str:
    """挑一个可下载的播放地址（优先码率最高的 ``bit_rate``）。"""
    candidates: list[tuple[int, str]] = []
    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        urls = ((entry.get("play_addr") or {}).get("url_list")) or []
        bitrate = entry.get("bit_rate") or 0
        if urls:
            candidates.append((int(bitrate or 0), str(urls[0])))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    urls = ((video.get("play_addr") or {}).get("url_list")) or []
    return str(urls[0]) if urls else ""


def parse_detail(detail: dict[str, Any]) -> DouyinVideo:
    """把接口返回的 ``aweme_detail`` 转成 :class:`DouyinVideo`。"""
    video = detail.get("video") or {}
    author = detail.get("author") or {}
    duration_ms = video.get("duration") or detail.get("duration") or 0
    try:
        duration = int(int(duration_ms) / 1000)
    except (TypeError, ValueError):
        duration = None

    images: list[str] = []
    for entry in detail.get("images") or []:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("url_list") or []
        if urls:
            images.append(str(urls[-1]))

    return DouyinVideo(
        aweme_id=str(detail.get("aweme_id") or ""),
        ok=True,
        title=str(detail.get("desc") or "").strip(),
        uploader=str(author.get("nickname") or "").strip(),
        duration=duration if duration else None,
        description=str(detail.get("desc") or "").strip(),
        video_url=_pick_play_url(video),
        images=images,
    )


def fetch_video(
    aweme_id: str,
    *,
    cookies_file: str = "",
    user_agent: str = _DEFAULT_UA,
    timeout: float = 20.0,
) -> DouyinVideo:
    """调用详情接口取回一条作品；失败时在 ``error`` 里说明原因。"""
    if not aweme_id:
        return DouyinVideo(error="未能从链接中识别作品 ID")

    cookies = parse_cookies_file(cookies_file) if cookies_file else {}
    params = urllib.parse.urlencode(_web_params(aweme_id))
    bogus = ABogus(user_agent=user_agent).generate_abogus(params, "")
    url = f"{_DETAIL_API}?{params}&a_bogus={bogus}"

    headers = {
        "User-Agent": user_agent,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    uifid = cookies.get("UIFID")
    if uifid:
        # ArgusSecurityPlugin 会单独校验这个请求头（缺了报 Uifid Not Found）
        headers["Uifid"] = uifid

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:120]
        except Exception:  # pragma: no cover - 读取失败时忽略
            pass
        detail = f"HTTP {exc.code}"
        if body:
            detail = f"{detail}（{body}）"
        if "Uifid" in body or "Signature" in body:
            detail += "；抖音风控拒绝了该签名（当前 SDK 版本已轮换，本模块的 a_bogus 暂不可用）"
        return DouyinVideo(aweme_id=aweme_id, error=detail)
    except Exception as exc:
        return DouyinVideo(aweme_id=aweme_id, error=f"请求失败：{exc}")

    if not isinstance(payload, dict) or not payload.get("aweme_detail"):
        status = ""
        if isinstance(payload, dict):
            status = f"（status_code={payload.get('status_code')}）"
        return DouyinVideo(
            aweme_id=aweme_id, error=f"接口未返回作品数据{status}"
        )
    return parse_detail(payload["aweme_detail"])


# --------------------------------------------------------------------------- #
# 浏览器桥（当前唯一可行的取数方式）
# --------------------------------------------------------------------------- #

# 详情缓存：解析与下载是两次独立调用，缓存让下载直接复用解析结果，避免为
# 同一条作品启动两次浏览器。play_addr 直链带时效，故 TTL 不宜太长。
_DETAIL_TTL = 300.0
_DETAIL_CACHE_SIZE = 32
_DETAIL_CACHE: "OrderedDict[str, tuple[float, DouyinVideo]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
# 浏览器实例较重（内存 + 启动开销），同时只跑一个，避免多链接并发时起一堆。
_BROWSER_LOCK = threading.Lock()


def _cache_lookup(aweme_id: str) -> DouyinVideo | None:
    now = time.time()
    with _CACHE_LOCK:
        entry = _DETAIL_CACHE.get(aweme_id)
        if entry is None:
            return None
        ts, video = entry
        if now - ts > _DETAIL_TTL:
            _DETAIL_CACHE.pop(aweme_id, None)
            return None
        _DETAIL_CACHE.move_to_end(aweme_id)
        return video


def _cache_store(aweme_id: str, video: DouyinVideo) -> None:
    with _CACHE_LOCK:
        _DETAIL_CACHE[aweme_id] = (time.time(), video)
        _DETAIL_CACHE.move_to_end(aweme_id)
        while len(_DETAIL_CACHE) > _DETAIL_CACHE_SIZE:
            _DETAIL_CACHE.popitem(last=False)


def forget_cached(aweme_id: str = "") -> None:
    """清详情缓存；不传 ID 则全清（直链过期导致下载失败时用）。"""
    with _CACHE_LOCK:
        if aweme_id:
            _DETAIL_CACHE.pop(aweme_id, None)
        else:
            _DETAIL_CACHE.clear()


def fetch_video_via_browser(
    aweme_id: str,
    *,
    logger: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
    attempts: int = 25,
    interval: float = 0.5,
) -> DouyinVideo:
    """借本机浏览器的 JS 环境取回作品详情。

    流程：用**临时用户目录**启动一个无头 Chrome/Edge → 打开作品页（页面此时
    会种下 ``Uifid`` Cookie 并加载安全 SDK）→ 在页面里用 XHR 轮询详情接口，
    直到 SDK 把签名补齐、接口返回 200。

    头几次必然失败（``Uifid Not Found``）：``Uifid`` 是页面首个请求由服务器
    下发后才写进 Cookie 的，所以这里**轮询重试**而不是只试一次。

    之所以绕这么大一圈：接口要求 ``a_bogus`` 签名，而签名算法藏在 JSVMP 混淆
    的 ``webmssdk`` 里且会随版本轮换（见 :func:`fetch_video` 的说明）。借浏览器
    跑一遍页面，签名由页面自己完成，不用逆向、也不依赖登录态。
    """
    if not aweme_id:
        return DouyinVideo(error="未能从链接中识别作品 ID")

    from . import browser  # 延迟导入：没装浏览器时不必付这个开销

    if not browser.browser_available():
        return DouyinVideo(
            aweme_id=aweme_id,
            error="未找到 Chrome/Edge，无法解析抖音链接（安装 Edge 后重试）",
        )

    log = logger or (lambda level, message: None)
    query = urllib.parse.urlencode(_web_params(aweme_id))
    path = f"{urllib.parse.urlsplit(_DETAIL_API).path}?{query}"
    reason = ""
    try:
        with _BROWSER_LOCK:
            page = browser.BrowserSession(logger=log, timeout=timeout)
            try:
                page.start()
                page.navigate(f"https://www.douyin.com/video/{aweme_id}")
                # 等页面真正落到抖音域：导航会替换执行上下文，过早发请求会落在
                # about:blank 上（同源策略下相对路径必然失败）。
                # 刻意**不**等 readyState=complete——视频页要加载视频资源，很慢。
                page.wait_for(
                    "location.host.indexOf('douyin.com') >= 0",
                    timeout=min(timeout, 15.0),
                )
                for _ in range(max(1, attempts)):
                    try:
                        status, body = page.xhr_get(
                            path, timeout_ms=int(min(timeout, 15.0) * 1000)
                        )
                    except Exception as exc:
                        # 页面仍在跳转（抖音是 SPA，会做客户端路由），下一轮再试。
                        reason = f"页面尚未就绪：{exc}"
                        time.sleep(interval)
                        continue
                    if status == 200:
                        try:
                            payload = json.loads(body)
                        except json.JSONDecodeError:
                            reason = "接口返回的不是 JSON"
                            break
                        detail = payload.get("aweme_detail")
                        if detail:
                            return parse_detail(detail)
                        reason = (
                            "接口未返回作品数据"
                            f"（status_code={payload.get('status_code')}）"
                        )
                        break
                    # 前几次必然 403（Uifid 还没种下），继续等；网络层失败
                    # （status <= 0）也可能是页面刚就绪，同样再试几轮。
                    reason = (body or "").strip()[:120] or f"HTTP {status}"
                    time.sleep(interval)
            finally:
                # 清理很慢（杀进程树 + 删几百 MB 的临时 profile），丢到后台
                # 线程，别挡住解析结果的返回。
                page.close(blocking=False)
    except Exception as exc:
        return DouyinVideo(aweme_id=aweme_id, error=f"浏览器解析失败：{exc}")
    return DouyinVideo(aweme_id=aweme_id, error=reason or "抖音解析失败")


def video_for(
    aweme_id: str,
    *,
    logger: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
    force: bool = False,
) -> DouyinVideo:
    """带缓存的 :func:`fetch_video_via_browser`（仅缓存成功结果）。"""
    if not force:
        hit = _cache_lookup(aweme_id)
        if hit is not None:
            return hit
    video = fetch_video_via_browser(aweme_id, logger=logger, timeout=timeout)
    if video.ok:
        _cache_store(aweme_id, video)
    return video


def download_video(
    video_url: str,
    outdir: str,
    *,
    filename: str = "",
    cookies_file: str = "",
    user_agent: str = _DEFAULT_UA,
    timeout: float = 30.0,
    max_bytes: int = 0,
    logger: Callable[[str, str], None] | None = None,
) -> str | None:
    """下载 ``play_addr`` 直链到 ``outdir``，返回本地路径。

    ``max_bytes`` 为 0 表示不限；超出上限时中止并删掉半成品。
    """
    log = logger or (lambda level, message: None)
    if not video_url:
        return None
    try:
        Path(outdir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("error", f"创建下载目录失败：{exc}")
        return None

    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", filename).strip(" ._")[:80]
    target = Path(outdir) / (f"{safe}.mp4" if safe else f"douyin_{int(time.time())}.mp4")

    cookies = parse_cookies_file(cookies_file) if cookies_file else {}
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    request = urllib.request.Request(video_url, headers=headers)
    written = 0
    too_big = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(target, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        too_big = True
                        break
                    handle.write(chunk)
    except Exception as exc:
        log("warning", f"下载抖音视频失败：{exc}")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    if too_big:
        log("warning", f"抖音视频超过大小上限（{max_bytes // 1048576} MB），已放弃")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    if not target.is_file() or target.stat().st_size == 0:
        log("warning", "下载抖音视频失败：文件为空")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return str(target)


def cleanup_temp(path: str) -> None:
    """删除残留的临时文件（失败时忽略）。"""
    try:
        os.remove(path)
    except OSError:
        pass
