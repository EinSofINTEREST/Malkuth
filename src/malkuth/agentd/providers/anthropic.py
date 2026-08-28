"""Anthropic model provider.

``Model`` Protocol 뒤에 Anthropic SDK 를 바인딩한다. SDK 는 이 경계 밖으로
새어나가지 않는다 — executor 는 provider 사정을 알지 못한다.

**SDK 자체 재시도는 끈다** (05 Retry Layering): 재시도 계층은 agentd 하나이며,
provider 도 재시도하면 backoff 가 곱해져 rate limit 을 더 오래 문다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anthropic

from malkuth.agentd.executor import ModelResponse, ToolCall
from malkuth.core.agent import ModelUsage
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from malkuth.core.manifest import ModelConfig

# SDK 가 max_tokens 를 필수로 요구한다 — manifest 가 비워두면 이 값을 쓴다
DEFAULT_MAX_TOKENS = 4096


def _model_error(
    code: ErrorCode,
    message: str,
    *,
    agent: str,
    retryable: bool,
    cause: str,
    category: ErrorCategory = ErrorCategory.MODEL,
) -> MalkuthError:
    """provider 실패를 구조화 에러로 변환한다.

    기본은 MODEL 이지만 rate limit 은 **RATE_LIMIT** 으로 분리한다 —
    05 Layer Rules 는 모델 호출 boundary 가 MODEL/RATE_LIMIT/TIMEOUT 셋을
    낸다고 규정하고, RATE_LIMIT_RETRY 는 그 카테고리만 본다. 뭉개면
    정책이 겨냥한 유일한 상황에 정책이 닿지 않는다.
    """
    return MalkuthError(
        category=category,
        code=code,
        message=message,
        agent=agent,
        retryable=retryable,
        details={"cause": cause},
    )


def _is_context_overflow(err: anthropic.BadRequestError) -> bool:
    """400 중 context 초과만 골라낸다 — 나머지는 설정 오류다."""
    text = str(err).lower()
    return "context" in text or "too long" in text or "max_tokens" in text


@dataclass
class AnthropicModel:
    """The ``Model`` implementation backed by the Anthropic SDK.

    manifest 의 ``spec.model`` 이 모델을 결정한다 — 코드에 모델명을 하드코딩하지
    않는다 (02 Manifest Rules 3).
    """

    config: ModelConfig
    agent: str
    client: Any = None

    def __post_init__(self) -> None:
        if self.client is None:
            # provider SDK 재시도와 중복되면 backoff 가 곱해진다 (05 Retry Layering)
            self.client = anthropic.AsyncAnthropic(max_retries=0)

    async def run(self, prompt: str, tools: Sequence[Any]) -> ModelResponse:
        """Run one model turn.

        한 턴을 실행합니다. 재시도·라우팅 판단이 걸린 실패는 ``MODEL``
        카테고리로 변환합니다.

        **설정 오류는 그대로 전파합니다**: context 초과가 아닌 400 을
        ``LLM_002`` 로 덮으면 운영자가 프롬프트만 줄이다 시간을 버립니다.

        Args:
            prompt: The rendered prompt for this turn.
            tools: Tool specs the model may call.

        Returns:
            The model's response — tool 호출이 없으면 최종 응답입니다.

        Raises:
            MalkuthError: MODEL/``LLM_001`` rate limited, ``LLM_002`` context
                exceeded, ``LLM_003`` provider server error, ``LLM_004`` if the
                response cannot be interpreted.
            anthropic.APIStatusError: Configuration errors (unknown model,
                invalid arguments) propagate unchanged — 프레임워크가 고칠 수
                있는 실패가 아닙니다.
        """
        request: dict[str, Any] = {
            "model": self.config.name,
            "max_tokens": self.config.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.config.effort is not None:
            # sampling 파라미터는 현재 API 가 받지 않는다 — effort 가 그 자리다
            request["output_config"] = {"effort": self.config.effort}
        if tools:
            request["tools"] = [_to_tool_schema(tool) for tool in tools]

        try:
            message = await self.client.messages.create(**request)
        except anthropic.RateLimitError as err:
            raise _model_error(
                ErrorCode.LLM_001,
                "provider rate limited",
                agent=self.agent,
                retryable=True,
                cause=type(err).__name__,
                # 재시도 전략이 다르다 — 10초 시작 5회 (RATE_LIMIT_RETRY)
                category=ErrorCategory.RATE_LIMIT,
            ) from err
        except anthropic.BadRequestError as err:
            if not _is_context_overflow(err):
                raise
            raise _model_error(
                ErrorCode.LLM_002,
                "context length exceeded",
                agent=self.agent,
                # 같은 프롬프트로 다시 보내도 같은 길이다 — 줄여야 한다
                retryable=False,
                cause=type(err).__name__,
            ) from err
        except (anthropic.InternalServerError, anthropic.APIConnectionError) as err:
            raise _model_error(
                ErrorCode.LLM_003,
                "provider server error",
                agent=self.agent,
                retryable=True,
                cause=type(err).__name__,
            ) from err

        return self._interpret(message)

    def _interpret(self, message: Any) -> ModelResponse:
        """Convert a provider message into the framework's response.

        provider 응답을 프레임워크 표현으로 옮깁니다.

        Raises:
            MalkuthError: MODEL/``LLM_004`` if the payload has no usable content.
        """
        try:
            blocks = list(message.content)
            text = "".join(block.text for block in blocks if block.type == "text")
            tool_calls = tuple(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                for block in blocks
                if block.type == "tool_use"
            )
            usage = ModelUsage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            )
        except (AttributeError, TypeError, ValueError) as err:
            # 응답 모양이 계약과 다르면 그대로 흘려보내지 않는다 — 조용히 빈
            # 응답으로 처리하면 태스크가 성공한 것처럼 끝난다
            raise _model_error(
                ErrorCode.LLM_004,
                "model response could not be interpreted",
                agent=self.agent,
                retryable=False,
                cause=type(err).__name__,
            ) from err

        return ModelResponse(content=text, tool_calls=tool_calls, usage=usage)


def _to_tool_schema(tool: Any) -> dict[str, Any]:
    """Render a tool spec in the provider's format.

    tool 계약을 provider 형식으로 옮깁니다. ``mcp__`` 네임스페이스는 그대로
    유지됩니다 — 이름이 곧 출처 판별이기 때문입니다 (05 Layer Rules).
    """
    schema = tool if isinstance(tool, dict) else tool.to_tool_schema()
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "input_schema": schema.get("parameters") or schema.get("input_schema") or {},
    }


__all__ = ["DEFAULT_MAX_TOKENS", "AnthropicModel"]
