"""Local image matching; deliberately knows nothing about input or Windows."""

import cv2
import numpy as np

from .models import Candidate, Detection, Frame, Rect, Region, Settings, Template


def validate_template(image: np.ndarray) -> None:
    if (
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
    ):
        raise ValueError("模板必须为 8 位 BGR 彩色图片")
    height, width = image.shape[:2]
    if min(height, width) < 12 or max(height, width) > 1024 or height * width > 600_000:
        raise ValueError("模板边长应为 12–1024 像素，总面积不超过 600000 像素")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 8 or float(np.mean(cv2.Canny(gray, 40, 120) > 0)) < 0.01:
        raise ValueError("模板近乎纯色或缺少边缘，请框选包含文字和边框的完整按钮")


def match(frame: Frame, template: Template, region: Region, settings: Settings) -> Detection:
    try:
        validate_template(template.image)
        settings.validate()
        if not template.compatible(frame.geometry):
            return Detection(reason="窗口尺寸或 DPI 已改变，请重新采集模板")
        image = frame.image
        if (
            image.dtype != np.uint8
            or image.ndim != 3
            or image.shape[2] != 3
            or image.shape[:2] != (frame.geometry.height, frame.geometry.width)
        ):
            return Detection(reason="截图像素与客户区尺寸不一致")
        area = region.pixels(frame.geometry.width, frame.geometry.height)
        roi = image[area.y : area.y + area.height, area.x : area.x + area.width]
        height, width = template.image.shape[:2]
        if height > area.height or width > area.width:
            return Detection(reason="模板大于搜索区域")
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        # A small valid button can occupy <0.1% of a full-HD frame. Global
        # variance would incorrectly classify that scene as blank.
        if int(gray.max()) < 12 or int(gray.max()) - int(gray.min()) < 8:
            return Detection(reason="截图为空白或黑屏")
        template_gray = cv2.cvtColor(template.image, cv2.COLOR_BGR2GRAY)
        scores = cv2.matchTemplate(gray, template_gray, cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(scores).all():
            return Detection(reason="匹配计算无效")
        best = float(scores.max())
        candidates = []
        examined = 0
        # Suppress only near-identical placements, never a whole template width.
        # A second independent peak is enough to reject; retain up to 8 for preview.
        for _ in range(8):
            _, score, _, location = cv2.minMaxLoc(scores)
            if score < settings.threshold:
                break
            x, y = location
            patch = roi[y : y + height, x : x + width]
            color_score = float(
                cv2.matchTemplate(patch, template.image, cv2.TM_CCOEFF_NORMED)[0, 0]
            )
            appearance_score = 1.0 - float(np.mean(cv2.absdiff(patch, template.image))) / 255.0
            combined_score = min(score, color_score, appearance_score)
            best = combined_score if examined == 0 else max(best, combined_score)
            examined += 1
            if combined_score >= settings.threshold:
                candidates.append(
                    Candidate(Rect(area.x + x, area.y + y, width, height), combined_score)
                )
            rx, ry = max(2, width // 3), max(2, height // 3)
            scores[max(0, y - ry) : y + ry + 1, max(0, x - rx) : x + rx + 1] = -1
        if examined == 8 and float(scores.max()) >= settings.threshold:
            return Detection(tuple(candidates), best, "候选峰过多，无法可靠确认唯一按钮")
        reason = (
            ""
            if len(candidates) == 1
            else ("存在多个独立候选，拒绝操作" if candidates else "无匹配或分数低于阈值")
        )
        return Detection(tuple(candidates), best, reason)
    except (ValueError, cv2.error) as exc:
        return Detection(reason=f"识别配置无效：{exc}")
