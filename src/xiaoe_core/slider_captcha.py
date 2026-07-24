"""Automatic slider captcha solver using OpenCV and CDP.

Integrates the OpenCV gap-detection approach from the standalone slider test
script into the pipeline so that Xiaoe password login can solve slider
challenges without handing off to the user.

Works with both CdpClient (Chrome/Edge) and EgoPage (Ego Browser) because
they share the same ``page.command(method, params)`` CDP interface.
"""

import base64
import json
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


class CaptchaAnalyzer:
    """Find the slider gap in a captcha screenshot using OpenCV."""

    def find_gap(self, image_data: bytes) -> Tuple[int, int, int, int]:
        """Return (x, y, w, h) of the most likely gap in *image_data* (PNG bytes)."""
        nparr = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Failed to decode captcha screenshot")

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edge = cv2.Canny(blur, 80, 180)
        contours, _ = cv2.findContours(edge, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates: List[Tuple[int, int, int, int]] = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if 20 < w < 120 and 20 < h < 120:
                candidates.append((x, y, w, h))

        if not candidates:
            raise ValueError("No gap candidate found in captcha screenshot")

        return max(candidates, key=lambda v: v[2] * v[3])


# ---------------------------------------------------------------------------
# Mouse trajectory
# ---------------------------------------------------------------------------


def generate_track(distance: int) -> List[int]:
    """Generate a human-like drag trajectory returning cumulative x offsets."""
    track: List[int] = []
    current = 0
    while current < distance:
        if current < distance * 0.7:
            step = random.randint(5, 10)
        else:
            step = random.randint(1, 4)
        current += step
        if current > distance:
            current = distance
        track.append(current)
    return track


# ---------------------------------------------------------------------------
# CDP-based slider solver
# ---------------------------------------------------------------------------


def _debug(*args: Any) -> None:
    """Print debug diagnostics to stderr so they reach the terminal."""
    print("[slider_solver]", *args, file=sys.stderr)


class SliderSolver:
    """Solve a visible slider captcha on a CDP-controlled page."""

    def __init__(self, page: Any) -> None:
        self.page = page

    # -- public API ----------------------------------------------------------

    def solve(self) -> bool:
        """Attempt to solve a slider captcha.

        Returns ``True`` when the drag was executed.  The caller should still
        verify whether the login actually succeeded afterwards.
        """
        self._enable_input()

        # 1. Locate the captcha container (handles inline + iframe).
        container = self._find_captcha_container()
        if container is None:
            _debug("captcha container not found")
            return False
        _debug("captcha container:", json.dumps(container, indent=None))

        # 2. Take a screenshot clipped to the captcha area.
        screenshot_bytes = self._capture_clip(container)
        if screenshot_bytes is None:
            _debug("screenshot failed")
            return False
        _debug("screenshot captured:", len(screenshot_bytes), "bytes")

        # 3. Find the gap inside the cropped captcha image.
        try:
            gap_x, gap_y, gap_w, gap_h = CaptchaAnalyzer().find_gap(screenshot_bytes)
        except ValueError as exc:
            _debug("gap detection failed:", exc)
            return False
        _debug("gap found at", gap_x, gap_y, gap_w, gap_h)

        # 4. Locate the draggable slider button.
        slider = self._find_slider_button(container)
        if slider is None:
            _debug("slider button not found — trying fallback estimation")
            slider = self._estimate_slider_position(container)
            if slider is None:
                return False
        _debug("slider button:", json.dumps(slider, indent=None))

        # 5. Convert to absolute page coordinates and compute drag distance.
        # The clipped screenshot uses scale=1, so coordinates already match
        # CSS pixels relative to the container's top-left corner.
        gap_center_x_css = container["left"] + gap_x + gap_w / 2.0
        slider_center_x_css = slider["x"] + slider["width"] / 2.0
        slider_center_y_css = slider["y"] + slider["height"] / 2.0

        distance = int(gap_center_x_css - slider_center_x_css)
        _debug(
            "distance:",
            distance,
            "| gap_css_x:",
            round(gap_center_x_css, 1),
            "| slider_css_x:",
            round(slider_center_x_css, 1),
        )

        if distance <= 0:
            _debug("distance <= 0, cannot drag")
            return False

        # 6. Execute the drag.
        self._drag(slider_center_x_css, slider_center_y_css, distance)
        _debug("drag completed")
        return True

    # -- element discovery ---------------------------------------------------

    def _find_captcha_container(self) -> Optional[Dict[str, float]]:
        """Return ``{left, top, width, height}`` of the captcha wrapper.

        Searches both inline containers and iframes whose source suggests a
        captcha origin.
        """
        expression = """(() => {
          const visible = node => {
            if (!node) return false;
            const r = node.getBoundingClientRect();
            const s = getComputedStyle(node);
            return r.width > 20 && r.height > 20 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          // 1. Inline Netease / common captcha containers
          const wrappers = [
            '.nc_wrapper', '.nc-container', '.yidun_slider',
            '.captcha-container', '.slider-captcha',
            '[class*="captcha"]', '[id*="captcha"]',
            '.verify-wrap', '.slide-verify',
          ];
          for (const sel of wrappers) {
            const el = document.querySelector(sel);
            if (visible(el)) {
              const r = el.getBoundingClientRect();
              return { left: r.x, top: r.y, width: r.width, height: r.height, iframe: false };
            }
          }
          // 2. Captcha served inside an iframe
          const iframes = document.querySelectorAll('iframe');
          for (const iframe of iframes) {
            const src = (iframe.src || '').toLowerCase();
            if (/(captcha|verify|slide|nc|yidun|geetest)/i.test(src) && visible(iframe)) {
              const r = iframe.getBoundingClientRect();
              return { left: r.x, top: r.y, width: r.width, height: r.height, iframe: true };
            }
          }
          return null;
        })()"""
        try:
            result = self.page.command(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            value = result.get("result", {}).get("value")
            if isinstance(value, dict) and "left" in value:
                return value
        except Exception:
            pass
        return None

    def _find_slider_button(
        self, container: Dict[str, float]
    ) -> Optional[Dict[str, float]]:
        """Return ``{x, y, width, height}`` of the draggable slider button.

        When the captcha lives in an iframe we need to evaluate inside that
        frame; otherwise we search the main document.
        """
        if container.get("iframe"):
            return self._find_slider_in_iframe(container)

        selectors = [
            ".nc_iconfont",
            ".btn_slide",
            '[class*="nc_iconfont"]',
            ".slider-btn",
            ".slider-button",
            ".slide-btn",
            '[class*="slide"]',
            ".captcha-slider-btn",
            ".yidun_slider_btn",
            ".nc-lang-cnt",
        ]
        expression = (
            "(() => {"
            "  const visible = node => {"
            "    if (!node) return false;"
            "    const r = node.getBoundingClientRect();"
            "    return r.width > 10 && r.height > 10;"
            "  };"
            "  const selectors = " + json.dumps(selectors) + ";"
            "  for (const sel of selectors) {"
            "    const el = document.querySelector(sel);"
            "    if (visible(el)) {"
            "      const r = el.getBoundingClientRect();"
            "      return { x: r.x, y: r.y, width: r.width, height: r.height };"
            "    }"
            "  }"
            "  return null;"
            "})()"
        )

        try:
            result = self.page.command(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            value = result.get("result", {}).get("value")
            if isinstance(value, dict) and "x" in value:
                return value
        except Exception:
            pass
        return None

    def _find_slider_in_iframe(
        self, container: Dict[str, float]
    ) -> Optional[Dict[str, float]]:
        """Try to locate the slider button inside a captcha iframe.

        We enumerate execution contexts to find the iframe's context, evaluate
        the button query there, then offset the result by the iframe's page
        position.
        """
        # Get execution contexts so we can target the iframe.
        try:
            result = self.page.command("Runtime.evaluate", {
                "expression": """
                (() => {
                  const iframes = document.querySelectorAll('iframe');
                  for (const iframe of iframes) {
                    const src = (iframe.src || '').toLowerCase();
                    if (/(captcha|verify|slide|nc|yidun|geetest)/i.test(src)) {
                      try {
                        const doc = iframe.contentDocument || iframe.contentWindow.document;
                        const selectors = [
                          '.nc_iconfont', '.btn_slide', '[class*="nc_iconfont"]',
                          '.slider-btn', '.slider-button', '.slide-btn',
                          '[class*="slide"]', '.captcha-slider-btn',
                          '.yidun_slider_btn',
                        ];
                        for (const sel of selectors) {
                          const el = doc.querySelector(sel);
                          if (el) {
                            const r = el.getBoundingClientRect();
                            const iframeRect = iframe.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) {
                              return {
                                x: iframeRect.x + r.x,
                                y: iframeRect.y + r.y,
                                width: r.width,
                                height: r.height
                              };
                            }
                          }
                        }
                      } catch(e) {
                        // cross-origin iframe — fall through to estimation
                      }
                    }
                  }
                  return null;
                })()
                """,
                "returnByValue": True,
            })
            value = result.get("result", {}).get("value")
            if isinstance(value, dict) and "x" in value:
                return value
        except Exception:
            pass
        return None

    def _estimate_slider_position(
        self, container: Dict[str, float]
    ) -> Optional[Dict[str, float]]:
        """Fallback: guess slider button position from the container geometry.

        Most slider captchas place the button at the bottom-left of the
        captcha area, roughly 50-60 px tall.
        """
        track_height = 56.0
        btn_width = 48.0
        btn_height = 48.0
        return {
            "x": container["left"] + 2,
            "y": container["top"] + container["height"] - track_height + (track_height - btn_height) / 2,
            "width": btn_width,
            "height": btn_height,
        }

    # -- CDP helpers ---------------------------------------------------------

    def _enable_input(self) -> None:
        try:
            self.page.command("Input.enable")
        except Exception:
            pass

    def _capture_clip(self, container: Dict[str, float]) -> Optional[bytes]:
        """Take a screenshot clipped to the captcha container.

        Uses CSS-pixel viewport coordinates so the output image dimensions
        match CSS pixels (scale=1, the CDP default for clips).
        """
        clip = {
            "x": container["left"],
            "y": container["top"],
            "width": container["width"],
            "height": container["height"],
            "scale": 1,
        }
        try:
            result = self.page.command(
                "Page.captureScreenshot",
                {"format": "png", "clip": clip},
            )
            data = result.get("data")
            if data:
                return base64.b64decode(data)
        except Exception as exc:
            _debug("captureScreenshot with clip failed:", exc)
            # Fallback: full-page screenshot and crop to container.
            return self._capture_fullpage_fallback(container)
        return None

    def _capture_fullpage_fallback(
        self, container: Dict[str, float]
    ) -> Optional[bytes]:
        """Fallback: take a full-page screenshot and crop to the container."""
        try:
            result = self.page.command(
                "Page.captureScreenshot",
                {"format": "png"},
            )
            data = result.get("data")
            if not data:
                return None
            raw = base64.b64decode(data)
            nparr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return None

            dpr = self._get_device_pixel_ratio()
            h, w = img.shape[:2]
            # Full-page screenshot is in physical pixels; container coords are CSS.
            x1 = max(0, int(container["left"] * dpr))
            y1 = max(0, int(container["top"] * dpr))
            x2 = min(w, int((container["left"] + container["width"]) * dpr))
            y2 = min(h, int((container["top"] + container["height"]) * dpr))
            if x2 <= x1 or y2 <= y1:
                return None

            cropped = img[y1:y2, x1:x2]
            _, buf = cv2.imencode(".png", cropped)
            return buf.tobytes()
        except Exception as exc:
            _debug("fullpage fallback also failed:", exc)
        return None

    def _get_device_pixel_ratio(self) -> float:
        try:
            result = self.page.command(
                "Runtime.evaluate",
                {"expression": "window.devicePixelRatio", "returnByValue": True},
            )
            value = result.get("result", {}).get("value")
            if isinstance(value, (int, float)):
                return float(value)
        except Exception:
            pass
        return 1.0

    def _drag(self, start_x: float, start_y: float, distance: int) -> None:
        track = generate_track(distance)

        def mouse(type_: str, x: float, y: float, button: Optional[str] = None) -> None:
            params: Dict[str, Any] = {"type": type_, "x": x, "y": y}
            if button:
                params["button"] = button
                params["clickCount"] = 1
            self.page.command("Input.dispatchMouseEvent", params)

        mouse("mouseMoved", start_x, start_y)
        time.sleep(0.05)
        mouse("mousePressed", start_x, start_y, "left")
        time.sleep(0.05)

        for offset in track:
            mouse("mouseMoved", start_x + offset, start_y + random.uniform(-2, 2), "left")
            time.sleep(random.uniform(0.005, 0.02))

        time.sleep(0.02)
        mouse("mouseReleased", start_x + distance, start_y, "left")
