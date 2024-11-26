"""Computer Use Tool for Anthropic API"""

import base64
import time
import shlex
import os
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any, Literal, TypedDict, Union, Optional
from uuid import uuid4

from anthropic.types.beta import BetaToolComputerUse20241022Param, BetaToolUnionParam
from .sys_utils import command_run
from ..types_ import (
    Keyboard,
    LeftClickDrag,
    Mouse,
    MouseMove,
    ScreenShot,
    GetScreenInfo,
)


# Constants
OUTPUT_DIR = "/tmp/outputs"
TYPING_DELAY_MS = 12
TYPING_GROUP_SIZE = 50
TRUNCATED_MESSAGE: str = "<response clipped><NOTE>To save on context only part of this file has been shown to you.</NOTE>"

Action = Literal[
    "key",
    "type",
    "mouse_move",
    "left_click",
    "left_click_drag",
    "right_click",
    "middle_click",
    "double_click",
    "screenshot",
    "cursor_position",
    "scroll_up",
    "scroll_down",
    "get_screen_info",
]


class Resolution(TypedDict):
    width: int
    height: int


# Sizes above XGA/WXGA are not recommended
MAX_SCALING_TARGETS: dict[str, Resolution] = {
    "XGA": Resolution(width=1024, height=768),  # 4:3
    "WXGA": Resolution(width=1280, height=800),  # 16:10
    "FWXGA": Resolution(width=1366, height=768),  # ~16:9
}


class ScalingSource(StrEnum):
    COMPUTER = "computer"
    API = "api"


class ComputerToolOptions(TypedDict):
    display_height_px: int
    display_width_px: int
    display_number: int | None


@dataclass(kw_only=True, frozen=True)
class ToolResult:
    """Represents the result of a tool execution."""

    output: str | None = None
    error: str | None = None
    base64_image: str | None = None
    system: str | None = None

    def __bool__(self) -> bool:
        return any(getattr(self, field.name) for field in fields(self))

    def __add__(self, other: "ToolResult") -> "ToolResult":
        def combine_fields(
            field: str | None, other_field: str | None, concatenate: bool = True
        ) -> str | None:
            if field and other_field:
                if concatenate:
                    return field + other_field
                raise ValueError("Cannot combine tool results")
            return field or other_field

        return ToolResult(
            output=combine_fields(self.output, other.output),
            error=combine_fields(self.error, other.error),
            base64_image=combine_fields(self.base64_image, other.base64_image, False),
            system=combine_fields(self.system, other.system),
        )

    def replace(self, **kwargs: Any) -> "ToolResult":
        """Returns a new ToolResult with the given fields replaced."""
        return replace(self, **kwargs)


class CLIResult(ToolResult):
    """A ToolResult that can be rendered as a CLI output."""

    pass


class ToolFailure(ToolResult):
    """A ToolResult that represents a failure."""

    pass


class ToolError(Exception):
    """Raised when a tool encounters an error."""

    def __init__(self, message: str) -> None:
        self.message = message


def chunks(s: str, chunk_size: int) -> list[str]:
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


class ComputerTool:
    """
    A tool that allows the agent to interact with the screen, keyboard, and mouse of the current computer.
    The tool parameters are defined by Anthropic and are not editable.
    """

    name: Literal["computer"] = "computer"
    api_type: Literal["computer_20241022"] = "computer_20241022"
    width: Optional[int]
    height: Optional[int]
    display_num: Optional[int]
    xdotool: Optional[str]

    _screenshot_delay = 2.0
    _scaling_enabled = True

    def __init__(self) -> None:
        super().__init__()

        self.xdotool = None
        self.width = None
        self.height = None
        self.display_num = None
        self._display_prefix = ""

    def get_screen_info(self, docker_image_id: str) -> tuple[int, int, Optional[int]]:
        result = self.shell(
            docker_image_id,
            "echo $WIDTH,$HEIGHT,$DISPLAY_NUM",
            take_screenshot=False,
        )
        assert not result.error, result.error
        assert result.output, "Could not get screen info"
        width, height, display_num = map(
            lambda x: None if not x else int(x), result.output.split(",")
        )
        if width is None:
            width = 1080
        if height is None:
            height = 1920

        self.width = width
        self.height = height
        if display_num is not None:
            self.display_num = int(display_num)
            self._display_prefix = f"DISPLAY=:{self.display_num} "
        else:
            self.display_num = None
            self._display_prefix = ""
        assert self._display_prefix is not None
        self.xdotool = f"{self._display_prefix}xdotool"
        return width, height, display_num

    def __call__(
        self,
        *,
        docker_image_id: str,
        action: Action,
        text: str | None = None,
        coordinate: tuple[int, int] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if action == "get_screen_info":
            self.get_screen_info(docker_image_id)
            screenshot_res = self.screenshot(docker_image_id)
            return ToolResult(
                output=f"width: {self.width}, height: {self.height}, display_num: {self.display_num}",
                error=screenshot_res.error,
                base64_image=screenshot_res.base64_image,
            )

        if self.width is None or self.height is None:
            raise ToolError("Please first get screen info using get_screen_info tool")

        if action in ("mouse_move", "left_click_drag"):
            if coordinate is None:
                raise ToolError(f"coordinate is required for {action}")
            if text is not None:
                raise ToolError(f"text is not accepted for {action}")
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
                raise ToolError(f"{coordinate} must be a tuple of length 2")
            if not all(isinstance(i, int) and i >= 0 for i in coordinate):
                raise ToolError(f"{coordinate} must be a tuple of non-negative ints")

            x, y = self.scale_coordinates(
                ScalingSource.API, coordinate[0], coordinate[1]
            )

            if action == "mouse_move":
                return self.shell(
                    docker_image_id, f"{self.xdotool} mousemove --sync {x} {y}"
                )
            elif action == "left_click_drag":
                return self.shell(
                    docker_image_id,
                    f"{self.xdotool} mousedown 1 mousemove --sync {x} {y} mouseup 1",
                )

        if action in ("key", "type"):
            if text is None:
                raise ToolError(f"text is required for {action}")
            if coordinate is not None:
                raise ToolError(f"coordinate is not accepted for {action}")
            if not isinstance(text, str):
                raise ToolError(output=f"{text} must be a string")

            if action == "key":
                return self.shell(docker_image_id, f"{self.xdotool} key -- {text}")
            elif action == "type":
                results: list[ToolResult] = []
                for chunk in chunks(text, TYPING_GROUP_SIZE):
                    cmd = f"{
                        self.xdotool} type --delay {TYPING_DELAY_MS} -- {shlex.quote(chunk)}"
                    results.append(
                        self.shell(docker_image_id, cmd, take_screenshot=False)
                    )
                screenshot_base64 = self.screenshot(docker_image_id).base64_image
                return ToolResult(
                    output="".join(result.output or "" for result in results),
                    error="".join(result.error or "" for result in results),
                    base64_image=screenshot_base64,
                )

        if action in (
            "left_click",
            "right_click",
            "double_click",
            "middle_click",
            "screenshot",
            "cursor_position",
            "scroll_up",
            "scroll_down",
        ):
            if text is not None:
                raise ToolError(f"text is not accepted for {action}")
            if coordinate is not None:
                raise ToolError(f"coordinate is not accepted for {action}")

            if action == "screenshot":
                return self.screenshot(docker_image_id)
            elif action == "cursor_position":
                result = self.shell(
                    docker_image_id,
                    f"{self.xdotool} getmouselocation --shell",
                    take_screenshot=False,
                )
                output = result.output or ""
                x, y = self.scale_coordinates(
                    ScalingSource.COMPUTER,
                    int(output.split("X=")[1].split("\n")[0]),
                    int(output.split("Y=")[1].split("\n")[0]),
                )
                return result.replace(output=f"X={x},Y={y}")
            else:
                if action in ("scroll_up", "scroll_down"):
                    button = "4" if action == "scroll_up" else "5"
                    return self.shell(
                        docker_image_id,
                        f"{
                            self.xdotool} click --repeat 1 {button}",
                    )
                else:
                    click_arg = {
                        "left_click": "1",
                        "right_click": "3",
                        "middle_click": "2",
                        "double_click": "--repeat 2 --delay 500 1",
                    }[action]
                    return self.shell(
                        docker_image_id, f"{self.xdotool} click {click_arg}"
                    )

        raise ToolError(f"Invalid action: {action}")

    def screenshot(self, docker_image_id: str) -> ToolResult:
        """Take a screenshot of the current screen and return the base64 encoded image."""
        if self.width is None or self.height is None:
            self.get_screen_info(docker_image_id)
        assert self.width and self.height
        # output_dir = Path(OUTPUT_DIR)
        # output_dir.mkdir(parents=True, exist_ok=True)
        mkdir_res = self.shell(
            command=f"mkdir -p {OUTPUT_DIR}",
            docker_image_id=docker_image_id,
            take_screenshot=False,
        )
        path = f"{OUTPUT_DIR}/screenshot_{uuid4().hex}.png"

        screenshot_cmd = f"{
            self._display_prefix}scrot -f {path} -p"

        self.shell(docker_image_id, screenshot_cmd, take_screenshot=False)

        if self._scaling_enabled:
            x, y = self.scale_coordinates(
                ScalingSource.COMPUTER, self.width, self.height
            )
            self.shell(
                docker_image_id,
                f"convert {path} -resize {x}x{y}! {path}",
                take_screenshot=False,
            )

        # Copy file from docker to tmp
        _, stdout, stderr = command_run(
            f"docker cp {docker_image_id}:{path} {path}",
            truncate_after=None,
        )

        if os.path.exists(path):
            with open(path, "rb") as f:
                base64_image = base64.b64encode(f.read()).decode("utf-8")

            return ToolResult(output="", error=stderr, base64_image=base64_image)

        raise ToolError(f"Failed to take screenshot: {stderr}")

    def shell(
        self, docker_image_id: str, command: str, take_screenshot: bool = True
    ) -> ToolResult:
        """Run a shell command and return the output, error, and optionally a screenshot."""
        _, stdout, stderr = command_run(
            f"docker exec {docker_image_id} sh -c '{command}'"
        )
        base64_image = None

        if take_screenshot:
            # delay to let things settle before taking a screenshot
            time.sleep(self._screenshot_delay)
            base64_image = self.screenshot(docker_image_id).base64_image

        return ToolResult(output=stdout, error=stderr, base64_image=base64_image)

    def scale_coordinates(
        self, source: ScalingSource, x: int, y: int
    ) -> tuple[int, int]:
        """Scale coordinates to a target maximum resolution."""

        if self.width is None or self.height is None:
            raise ToolError("Please first get screen info using get_screen_info tool")

        if not self._scaling_enabled:
            return x, y
        ratio = self.width / self.height
        target_dimension = None
        for dimension in MAX_SCALING_TARGETS.values():
            # allow some error in the aspect ratio - not ratios are exactly 16:9
            if abs(dimension["width"] / dimension["height"] - ratio) < 0.02:
                if dimension["width"] < self.width:
                    target_dimension = dimension
                break
        if target_dimension is None:
            return x, y
        # should be less than 1
        x_scaling_factor = target_dimension["width"] / self.width
        y_scaling_factor = target_dimension["height"] / self.height
        if source == ScalingSource.API:
            if x > self.width or y > self.height:
                raise ToolError(f"Coordinates {x}, {y} are out of bounds")
            # scale up
            return round(x / x_scaling_factor), round(y / y_scaling_factor)
        # scale down
        return round(x * x_scaling_factor), round(y * y_scaling_factor)


Computer = ComputerTool()


def run_computer_tool(
    action: Union[Keyboard, Mouse, ScreenShot, GetScreenInfo],
) -> tuple[str, str]:
    if isinstance(action, GetScreenInfo):
        result = Computer(
            action="get_screen_info", docker_image_id=action.docker_image_id
        )
    elif isinstance(action, ScreenShot):
        result = Computer(action="screenshot", docker_image_id=action.docker_image_id)
    elif isinstance(action, Keyboard):
        result = Computer(
            action=action.action,
            text=action.text,
            docker_image_id=action.docker_image_id,
        )
    elif isinstance(action, Mouse):
        if isinstance(action.action, MouseMove):
            result = Computer(
                docker_image_id=action.docker_image_id,
                action="mouse_move",
                coordinate=(action.action.x, action.action.y),
            )
        elif isinstance(action.action, LeftClickDrag):
            result = Computer(
                docker_image_id=action.docker_image_id,
                action="left_click_drag",
                coordinate=(action.action.x, action.action.y),
            )
        else:
            result = Computer(
                docker_image_id=action.docker_image_id, action=action.action.button_type
            )

    output = f"stdout: {result.output or ''}, stderr: {result.error or ''}"
    image = result.base64_image or ""
    return output, image
