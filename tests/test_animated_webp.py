"""动态 webp -> gif 转码：Fandom 缩略图服务不支持动画 webp，实测 1528 个 webp
里 242 个（15.8%）缩略图生成失败，全部是动图。转回 GIF 是唯一稳定支持的路径。
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image

from wiki_translate.files import _looks_like_webp, _sniff_mime, convert_animated_webp_to_gif


def _animated_webp(n_frames: int = 3, size=(10, 10)) -> bytes:
    colors = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255)]
    frames = [Image.new("RGBA", size, colors[i % len(colors)]) for i in range(n_frames)]
    buf = BytesIO()
    frames[0].save(
        buf, format="WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    return buf.getvalue()


def _static_webp(size=(10, 10)) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 255)).save(buf, format="WEBP")
    return buf.getvalue()


def test_looks_like_webp_true_for_riff_webp():
    assert _looks_like_webp(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is True


def test_looks_like_webp_false_for_gif():
    assert _looks_like_webp(b"GIF89a" + b"\x00" * 20) is False


def test_looks_like_webp_false_for_short_bytes():
    assert _looks_like_webp(b"RIFF") is False


def test_convert_animated_webp_produces_gif_and_flags_converted():
    webp = _animated_webp(n_frames=3)
    out, converted = convert_animated_webp_to_gif(webp)
    assert converted is True
    assert out.startswith(b"GIF89a")
    assert _sniff_mime(out) == "image/gif"


def test_convert_animated_webp_preserves_frame_count():
    webp = _animated_webp(n_frames=5)
    out, converted = convert_animated_webp_to_gif(webp)
    assert converted is True
    img = Image.open(BytesIO(out))
    assert img.is_animated is True
    assert img.n_frames == 5


def test_convert_static_webp_left_unchanged():
    """静态 webp 缩略图本来就正常（实测 A165.webp 这类），不该被转码。"""
    webp = _static_webp()
    out, converted = convert_animated_webp_to_gif(webp)
    assert converted is False
    assert out == webp


def test_convert_non_webp_bytes_left_unchanged():
    png_like = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    out, converted = convert_animated_webp_to_gif(png_like)
    assert converted is False
    assert out == png_like


def test_convert_corrupted_webp_magic_falls_back_gracefully():
    """RIFF/WEBP 魔数对了但内容是垃圾——不能崩，也不能吞掉原始字节。"""
    garbage = b"RIFF\x00\x00\x00\x00WEBP" + b"\xff" * 100
    out, converted = convert_animated_webp_to_gif(garbage)
    assert converted is False
    assert out == garbage


def test_convert_empty_bytes_does_not_raise():
    out, converted = convert_animated_webp_to_gif(b"")
    assert converted is False
    assert out == b""
