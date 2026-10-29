"""合成动画回归；不使用用户表情或网络。"""

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, features

from wechat_extractor import _process_local_files, process_image


def animation(size=(64, 48), *, loop=0, format="GIF"):
    frames = [Image.new("RGB", size, color) for color in ("red", "green", "blue")]
    output = BytesIO()
    kwargs = {"format": format, "save_all": True, "append_images": frames[1:], "duration": [70, 110, 150]}
    if loop is not None:
        kwargs["loop"] = loop
    frames[0].save(output, **kwargs)
    return output.getvalue()


def metadata(data):
    with Image.open(BytesIO(data)) as image:
        frames = image.n_frames
        loop = image.info.get("loop")
        durations = []
        for index in range(frames):
            image.seek(index)
            durations.append(image.info.get("duration", 0))
        return frames, image.size, durations, loop


class ImageProcessingTests(unittest.TestCase):
    def test_small_animated_gif_keeps_original_bytes_and_all_frames(self):
        original = animation()
        result = process_image(original)
        self.assertEqual(metadata(result), (3, (64, 48), [70, 110, 150], 0))
        self.assertEqual(result, original)

    def test_gif_at_dimension_limit_keeps_finite_loop(self):
        original = animation((400, 200), loop=2)
        self.assertEqual(process_image(original), original)

    def test_large_animated_gif_preserves_frames_timing_and_loop(self):
        result = process_image(animation((800, 400), loop=2))
        self.assertEqual(metadata(result), (3, (400, 200), [70, 110, 150], 2))

    def test_large_gif_without_loop_does_not_gain_infinite_loop(self):
        result = process_image(animation((800, 400), loop=None))
        self.assertEqual(metadata(result), (3, (400, 200), [70, 110, 150], None))

    def test_transparent_animation_keeps_frame_content(self):
        frames = []
        for color in ("red", "green", "blue"):
            frame = Image.new("RGBA", (800, 400))
            ImageDraw.Draw(frame).rectangle((200, 100, 600, 300), fill=color)
            frames.append(frame)
        source = BytesIO()
        frames[0].save(source, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0, disposal=2)
        result = process_image(source.getvalue())
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.n_frames, 3)
            for index, color in enumerate(((255, 0, 0), (0, 128, 0), (0, 0, 255))):
                image.seek(index)
                frame = image.convert("RGBA")
                self.assertEqual(frame.getpixel((0, 0))[3], 0)
                self.assertEqual(frame.getpixel((200, 100))[:3], color)

    def test_resize_error_preserves_original_animation(self):
        original = animation((800, 400))
        with patch("wechat_extractor._resize_gif", side_effect=OSError("synthetic encoder error")):
            self.assertEqual(process_image(original), original)

    def test_encoder_flattening_cannot_replace_an_animation(self):
        original = animation((800, 400))
        static = BytesIO()
        Image.new("RGB", (400, 200), "red").save(static, format="GIF")
        with patch("wechat_extractor._resize_gif", return_value=static.getvalue()):
            self.assertEqual(process_image(original), original)

    def test_static_formats_still_resize(self):
        for format in ("GIF", "JPEG", "PNG"):
            with self.subTest(format=format):
                source = BytesIO()
                Image.new("RGB", (800, 400), "blue").save(source, format=format)
                with Image.open(BytesIO(process_image(source.getvalue()))) as result:
                    self.assertEqual(result.format, format)
                    self.assertEqual(result.size, (400, 200))

    def test_animated_png_keeps_all_frames(self):
        original = animation(format="PNG")
        self.assertEqual(process_image(original), original)

    @unittest.skipUnless(features.check("webp"), "Pillow WebP support unavailable")
    def test_animated_webp_keeps_all_frames(self):
        original = animation(format="WEBP")
        self.assertEqual(process_image(original), original)

    def test_local_file_export_writes_multiple_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gif"
            source.write_bytes(animation())
            output = root / "export"
            output.mkdir()
            files = _process_local_files([str(source)], str(output), True, set(), None)
            self.assertEqual(len(files), 1)
            self.assertEqual(metadata(Path(files[0]).read_bytes())[0], 3)


if __name__ == "__main__":
    unittest.main()
