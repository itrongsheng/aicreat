"""媒体接口的请求模型（docs/10 §4.1、§5.1、§8；docs/04 §6.13、§7.8、§7.9）。

结构性约束（类型、长度、枚举、数组项数）在此声明，错误按 docs/04 §5.1 以 ``loc=["body", …]`` 返回；依赖配置或数据库的规则
（``content_id`` 与 ``usage_type`` 的组合、``from_content_prompt`` 须带 ``content_id``、``count ≤ media_config.image.max_count_per_request``、
``resolution`` / ``aspect_ratio`` 属于 ``media_config.*.allowed_*``、参考图数量、``duration ≤ video.max_duration``、``input_reference``
与 ``first_frame_image_url`` 互斥、敏感词、真实模式参考 URL 公网校验 4222 等）由 ``media_service`` 实现。

``usage_type=reference`` 只允许上传素材使用（图片、视频传入均 400）；视频不能作封面（``cover`` 400）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "ImageGenerateBody",
    "ImageUsageType",
    "PROMPT_MAX",
    "NEGATIVE_PROMPT_MAX",
    "REFERENCE_URL_MAX",
    "VideoGenerateBody",
    "VideoUsageType",
]

PROMPT_MAX = 4000
NEGATIVE_PROMPT_MAX = 2000
REFERENCE_URL_MAX = 1000
MAX_COUNT = 4
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 1

PositiveId = Annotated[int, Field(gt=0)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
MediaUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REFERENCE_URL_MAX)]
ShortEnum = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
ImageUsageType = Literal["cover", "inline", "standalone"]
VideoUsageType = Literal["inline", "standalone"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageGenerateBody(_Body):
    """``POST /admin/media/images/generate``（docs/10 §4.1）。

    ``prompt`` 在 ``from_content_prompt=false`` 时必填；``from_content_prompt=true`` 时须带 ``content_id``（因此只能与
    ``cover`` / ``inline`` 组合）；``cover`` / ``inline`` 必须带 ``content_id``，``standalone`` 的 ``content_id`` 必须为空。
    ``resolution`` / ``aspect_ratio`` 省略时按「项目级路由 ``params_json`` > 全局路由 ``params_json`` > ``media_config`` 默认值」取值。
    """

    project_id: PositiveId
    content_id: PositiveId | None = None
    usage_type: ImageUsageType
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, max_length=PROMPT_MAX)] | None = None
    from_content_prompt: bool = False
    count: int = Field(1, ge=1, le=MAX_COUNT)
    resolution: ShortEnum | None = None
    aspect_ratio: ShortEnum | None = None
    reference_image_urls: list[MediaUrl] = Field(default_factory=list, max_length=MAX_REFERENCE_IMAGES)
    model: ModelId | None = None


class VideoGenerateBody(_Body):
    """``POST /admin/media/videos/generate``（docs/10 §5.1）：一次只创建一个资产；``aspect_ratio`` 与 ``size`` 二选一（同时给出
    保留 ``aspect_ratio``）；``duration`` / ``resolution`` / ``aspect_ratio`` / ``generate_audio`` 省略时取路由 ``params_json`` 或
    ``media_config.video`` 默认值。"""

    project_id: PositiveId
    content_id: PositiveId | None = None
    usage_type: VideoUsageType
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PROMPT_MAX)]
    negative_prompt: Annotated[str, StringConstraints(strip_whitespace=True, max_length=NEGATIVE_PROMPT_MAX)] | None = None
    duration: int | None = Field(None, ge=1, le=60)
    resolution: ShortEnum | None = None
    aspect_ratio: ShortEnum | None = None
    size: ShortEnum | None = None
    input_reference: MediaUrl | None = None
    reference_image_urls: list[MediaUrl] = Field(default_factory=list, max_length=MAX_REFERENCE_IMAGES)
    reference_video_urls: list[MediaUrl] = Field(default_factory=list, max_length=MAX_REFERENCE_VIDEOS)
    reference_audio_urls: list[MediaUrl] = Field(default_factory=list, max_length=MAX_REFERENCE_AUDIOS)
    first_frame_image_url: MediaUrl | None = None
    last_frame_image_url: MediaUrl | None = None
    generate_audio: bool | None = None
    model: ModelId | None = None
