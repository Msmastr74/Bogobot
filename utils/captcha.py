import hashlib
import hmac
import random
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


SECRET_KEY = secrets.token_bytes(32)
LABELS = list("ABCDEF")
NUM_DISTRACTORS = len(LABELS) - 1
MIN_START_DISTANCE = 60

@dataclass
class CaptchaChallenge:
    challenge_id: str
    image_path: Path
    prompt: str
    answer_hash: str
    created_at: float
    expires_in_seconds: int = 120
    max_attempts: int = 1


def hash_answer(challenge_id: str, answer: str) -> str:
    normalized = answer.strip().lower()
    msg = f"{challenge_id}:{normalized}".encode("utf-8")
    return hmac.new(SECRET_KEY, msg, hashlib.sha256).hexdigest()


def verify_answer(challenge: CaptchaChallenge, answer: str) -> bool:
    return hmac.compare_digest(
        hash_answer(challenge.challenge_id, answer),
        challenge.answer_hash,
    )


def is_expired(challenge: CaptchaChallenge) -> bool:
    return time.time() - challenge.created_at > challenge.expires_in_seconds


class OcclusionPathCaptchaGenerator:
    def __init__(
        self,
        output_dir: str | Path | None = None,
        width: int = 900,
        height: int = 560,
        expires_in_seconds: int = 120,
        max_attempts: int = 1,
    ) -> None:
        self.output_dir = Path(output_dir) if output_dir is not None else Path(tempfile.gettempdir()) / "bogobot_captchas"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.expires_in_seconds = expires_in_seconds
        self.max_attempts = max_attempts

    def generate(self) -> CaptchaChallenge:
        challenge_id = secrets.token_urlsafe(16)
        img, answer = self._generate_image()

        image_path = self.output_dir / f"{challenge_id}.png"
        img.save(image_path)

        prompt = (
            "Trace the thin dark line from START. "
            "It continues behind the gray blocks. "
            "Enter the letter where it exits."
        )

        return CaptchaChallenge(
            challenge_id=challenge_id,
            image_path=image_path,
            prompt=prompt,
            answer_hash=hash_answer(challenge_id, answer),
            created_at=time.time(),
            expires_in_seconds=self.expires_in_seconds,
            max_attempts=self.max_attempts,
        )

    def _generate_image(self) -> tuple[Image.Image, str]:
        img = Image.new("RGB", (self.width, self.height), (245, 244, 240))
        draw = ImageDraw.Draw(img)

        font_big = self._font(34)
        font_med = self._font(23)
        font_small = self._font(17)

        self._draw_background(draw)
        path_shade = random.randint(85, 145)

        exit_ys = self._spread_positions(len(LABELS), 20, self.height - 20)
        random.shuffle(exit_ys)

        exits = [(label, self.width - 90, y) for label, y in zip(LABELS, exit_ys)]
        correct_label, end_x, end_y = random.choice(exits)

        start = (80, self._diagonal_y_from(end_y, 110, self.height - 110))
        end = (end_x, end_y)

        p0 = start
        p1 = (
            random.randint(210, 330),
            start[1] + random.randint(-50, 50),
        )
        p2 = (
            random.randint(560, 700),
            end[1] + random.randint(-50, 50),
        )
        p3 = end

        true_path = self._cubic_bezier_points(p0, p1, p2, p3, steps=140)
        used_starts: list[tuple[int, int]] = [start]

        wrong_exits = [exit_point for exit_point in exits if exit_point[0] != correct_label]
        distractor_exits = [
            wrong_exits[index % len(wrong_exits)]
            for index in range(NUM_DISTRACTORS)
        ]
        random.shuffle(distractor_exits)
        for exit_point in distractor_exits:
            self._draw_distractor_curve(draw, exit_point, path_shade, used_starts)

        draw.line(true_path, fill=(path_shade, path_shade, path_shade), width=1)

        sx, sy = start
        draw.ellipse((sx - 8, sy - 8, sx + 8, sy + 8), fill=(35, 35, 35))
        draw.text((sx - 38, sy - 45), "START", fill=(0, 0, 0), font=font_med)

        for label, x, y in exits:
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(30, 30, 30))
            self._draw_label_badge(draw, label, x + 22, y - 21, font_big)

        self._draw_occluders(draw)
        self._draw_fake_marks(draw, font_small)

        img = img.filter(ImageFilter.SMOOTH)

        draw = ImageDraw.Draw(img)
        draw.text((sx - 38, sy - 45), "START", fill=(0, 0, 0), font=font_med)

        for label, x, y in exits:
            self._draw_label_badge(draw, label, x + 22, y - 21, font_big)

        return img, correct_label

    def _draw_distractor_curve(
        self,
        draw: ImageDraw.ImageDraw,
        exit_point: tuple[str, int, int],
        path_shade: int,
        used_starts: list[tuple[int, int]],
    ) -> None:
        _, end_x, end_y = exit_point

        p0 = self._distractor_start(end_y, used_starts)
        used_starts.append(p0)

        p3 = (end_x, end_y)
        p1 = (
            random.randint(180, 380),
            random.randint(40, self.height - 40),
        )
        p2 = (
            random.randint(520, 760),
            end_y + random.randint(-140, 140),
        )

        pts = self._cubic_bezier_points(p0, p1, p2, p3, steps=140)
        draw.line(pts, fill=(path_shade, path_shade, path_shade), width=1)
        sx, sy = p0
        draw.ellipse((sx - 8, sy - 8, sx + 8, sy + 8), fill=(35, 35, 35))

    def _distractor_start(
        self,
        target_y: int,
        used_starts: list[tuple[int, int]],
    ) -> tuple[int, int]:
        def candidate() -> tuple[int, int]:
            return (
                self._grid_jitter([55, 90, 125, 160], 10),
                self._grid_jitter(
                    self._diagonal_y_lanes(target_y, 60, self.height - 60),
                    12,
                ),
            )

        best = candidate()
        best_distance = self._nearest_start_distance(best, used_starts)
        for _ in range(64):
            current = candidate()
            current_distance = self._nearest_start_distance(current, used_starts)
            if current_distance >= MIN_START_DISTANCE:
                return current
            if current_distance > best_distance:
                best = current
                best_distance = current_distance
        return best

    def _nearest_start_distance(
        self,
        point: tuple[int, int],
        used_starts: list[tuple[int, int]],
    ) -> float:
        if not used_starts:
            return float("inf")

        px, py = point
        return min(
            ((px - ux) ** 2 + (py - uy) ** 2) ** 0.5
            for ux, uy in used_starts
        )

    def _grid_jitter(self, values: list[int], jitter: int) -> int:
        return random.choice(values) + random.randint(-jitter, jitter)

    def _diagonal_y_lanes(
        self,
        target_y: int,
        low: int,
        high: int,
        min_delta: int = 120,
    ) -> list[int]:
        return [
            y
            for y in (80, 140, 220, 300, 380, 460)
            if low <= y <= high and abs(y - target_y) >= min_delta
        ] or [
            self._diagonal_y_from(target_y, low, high, min_delta),
        ]

    def _diagonal_y_from(
        self,
        target_y: int,
        low: int,
        high: int,
        min_delta: int = 120,
    ) -> int:
        ranges: list[tuple[int, int]] = []
        upper_high = target_y - min_delta
        lower_low = target_y + min_delta

        if upper_high >= low:
            ranges.append((low, min(upper_high, high)))
        if lower_low <= high:
            ranges.append((max(lower_low, low), high))
        if not ranges:
            return random.randint(low, high)

        start, end = random.choice(ranges)
        return random.randint(start, end)

    def _draw_occluders(self, draw: ImageDraw.ImageDraw) -> None:
        blocks = [
            (
                300 + random.randint(-25, 25),
                75 + random.randint(-10, 10),
                635 + random.randint(-25, 25),
                185 + random.randint(-10, 10),
            ),
            (
                260 + random.randint(-25, 25),
                210 + random.randint(-10, 10),
                675 + random.randint(-25, 25),
                335 + random.randint(-10, 10),
            ),
            (
                305 + random.randint(-25, 25),
                360 + random.randint(-10, 10),
                635 + random.randint(-25, 25),
                490 + random.randint(-10, 10),
            ),
        ]

        for x0, y0, x1, y1 in blocks:
            dx = random.randint(-18, 18)
            dy = random.randint(-10, 10)
            fill = random.randint(188, 214)

            draw.rounded_rectangle(
                (x0 + dx, y0 + dy, x1 + dx, y1 + dy),
                radius=random.randint(18, 30),
                fill=(fill, fill, fill),
                outline=(100, 100, 100),
                width=2,
            )

        for _ in range(4):
            x = random.randint(250, 500)
            y = random.randint(80, self.height - 80)
            w = random.randint(50, 100)
            h = random.randint(50, 100)
            fill = random.randint(190, 218)

            draw.rounded_rectangle(
                (x, y, x + w, y + h),
                radius=16,
                fill=(fill, fill, fill),
                outline=(110, 110, 110),
                width=1,
            )

    def _draw_label_badge(
        self,
        draw: ImageDraw.ImageDraw,
        label: str,
        x: int,
        y: int,
        font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    ) -> None:
        draw.rounded_rectangle(
            (x, y, x + 46, y + 46),
            radius=11,
            fill=(255, 255, 255),
            outline=(0, 0, 0),
            width=2,
        )
        draw.text((x + 12, y + 2), label, fill=(0, 0, 0), font=font)

    def _cubic_bezier_points(
        self,
        p0: tuple[int, int],
        p1: tuple[int, int],
        p2: tuple[int, int],
        p3: tuple[int, int],
        steps: int,
    ) -> list[tuple[int, int]]:
        pts = []

        for i in range(steps + 1):
            t = i / steps
            x = (
                (1 - t) ** 3 * p0[0]
                + 3 * (1 - t) ** 2 * t * p1[0]
                + 3 * (1 - t) * t**2 * p2[0]
                + t**3 * p3[0]
            )
            y = (
                (1 - t) ** 3 * p0[1]
                + 3 * (1 - t) ** 2 * t * p1[1]
                + 3 * (1 - t) * t**2 * p2[1]
                + t**3 * p3[1]
            )
            pts.append((int(x), int(y)))

        return pts

    def _spread_positions(self, count: int, low: int, high: int) -> list[int]:
        step = (high - low) // (count - 1)
        return [low + i * step + random.randint(-14, 14) for i in range(count)]

    def _draw_background(self, draw: ImageDraw.ImageDraw) -> None:
        for _ in range(80):
            x = random.randint(0, self.width)
            y = random.randint(0, self.height)
            r = random.randint(5, 22)
            shade = random.randint(224, 250)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(shade, shade, shade))

        for _ in range(30):
            x0 = random.randint(0, self.width)
            y0 = random.randint(0, self.height)
            x1 = x0 + random.randint(-110, 110)
            y1 = y0 + random.randint(-110, 110)
            shade = random.randint(185, 225)
            draw.line((x0, y0, x1, y1), fill=(shade, shade, shade), width=1)

    def _draw_fake_marks(
        self,
        draw: ImageDraw.ImageDraw,
        font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    ) -> None:
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ123456789"

        for _ in range(20):
            x = random.randint(120, self.width - 170)
            y = random.randint(25, self.height - 40)
            shade = random.randint(155, 210)
            draw.text((x, y), random.choice(chars), fill=(shade, shade, shade), font=font)

    def _font(self, size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
        for name in [
            "DejaVuSans-Bold.ttf",
            "Arial.ttf",
            "LiberationSans-Bold.ttf",
            "DejaVuSans.ttf",
        ]:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass

        return ImageFont.load_default()
