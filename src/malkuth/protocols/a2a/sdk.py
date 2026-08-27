"""A2A transport bound to the official SDK.

``PeerTransport`` 뒤에 ``a2a-sdk`` 를 바인딩한다. SDK 는 이 경계 밖으로
새어나가지 않는다 — ``A2AClient`` 는 protobuf 를 알지 못한다.

SDK 의 ``send_message`` 는 protobuf 를 요구하고 **스트림**을 돌려주므로,
여기서 프레임워크 타입과 오가는 변환을 맡는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import A2AError

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.protocols.a2a.errors import a2a_error, submit_failed, task_rejected, unreachable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from a2a.client import Client

    from malkuth.core.agent import TaskRequest

TOKEN_HEADER = "x-malkuth-a2a-token"  # noqa: S105 — 헤더 이름이지 값이 아니다
CALLER_HEADER = "x-malkuth-a2a-caller"

STATE_NAMES: dict[int, str] = {
    pb.TaskState.TASK_STATE_SUBMITTED: "submitted",
    pb.TaskState.TASK_STATE_WORKING: "working",
    pb.TaskState.TASK_STATE_COMPLETED: "completed",
    pb.TaskState.TASK_STATE_FAILED: "failed",
    pb.TaskState.TASK_STATE_CANCELED: "canceled",
    pb.TaskState.TASK_STATE_REJECTED: "failed",
}
"""SDK enum → ``map_status`` 가 아는 이름.

``REJECTED`` 를 ``failed`` 로 접는다: 프레임워크의 ``TaskStatus`` 에 거절이
따로 없고, 호출자 입장에서 둘 다 "peer 가 하지 않았다" 이다.
"""


def state_name(state: int) -> str:
    """protobuf 상태를 이름으로 — 미지의 값은 그대로 드러낸다.

    조용히 completed 로 접으면 실패한 위임이 성공으로 보인다.
    """
    known = STATE_NAMES.get(state)
    return known if known is not None else f"unknown-{state}"


def build_message(task: TaskRequest) -> pb.Message:
    """태스크를 A2A 메시지로 옮긴다.

    입력은 JSON 본문 한 덩이로 싣는다 — peer 가 같은 ``TaskRequest`` 계약을
    쓰므로 구조를 잃지 않는다.
    """
    payload = {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "node_id": task.node_id,
        "input": task.input,
        "trace": task.trace.model_dump(mode="json"),
    }
    # task_id 는 **비워 둔다**: 값을 채우면 SDK 가 기존 task 의 후속 메시지로
    # 읽어 아직 없는 task 를 찾다 실패한다. 새 위임은 새 task 다
    return pb.Message(
        message_id=task.task_id,
        role=pb.Role.ROLE_USER,
        parts=[pb.Part(text=json.dumps(payload, ensure_ascii=False))],
    )


def read_output(task: pb.Task) -> dict[str, Any]:
    """peer 의 산출물을 꺼낸다 — JSON 이 아니면 원문을 그대로 싣는다."""
    texts = [
        part.text
        for artifact in task.artifacts
        for part in artifact.parts
        if part.WhichOneof("content") == "text"
    ]
    if not texts:
        return {}
    joined = "".join(texts)
    try:
        loaded = json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}
    return loaded if isinstance(loaded, dict) else {"result": loaded}


@dataclass
class SdkPeerTransport:
    """The ``PeerTransport`` implementation backed by ``a2a-sdk``.

    peer 주소는 **runtime 이 주입**합니다 — 에이전트는 자신이 부를 수 있는
    peer 의 주소를 스스로 알아내지 않습니다 (03 Discovery).
    """

    agent: str
    """이 transport 를 소유한 caller — 에러의 출처 표시에 쓰인다."""

    addresses: Mapping[str, str]
    timeout_s: float = 120.0
    _clients: dict[str, Client] = field(default_factory=dict, init=False)

    def call_headers(self, token: str, headers: Mapping[str, str]) -> dict[str, str]:
        """peer 호출에 실을 헤더.

        caller 이름이 없으면 callee 가 어느 방향인지 확인할 수 없고, token 이
        없으면 이름 주장만 남는다 — 둘 다 있어야 03 의 이중 방어가 성립한다.
        """
        return {TOKEN_HEADER: token, CALLER_HEADER: self.agent, **dict(headers)}

    async def send(
        self, *, callee: str, task: TaskRequest, token: str, headers: Mapping[str, str]
    ) -> TaskResult:
        """Send one task to a peer and await its terminal result.

        peer 에게 태스크를 보내고 종료 상태를 기다립니다.

        Args:
            callee: The peer agent name.
            task: The task to delegate.
            token: The per-edge token the callee verifies.
            headers: Extra headers — trace 전파에 쓰입니다.

        Returns:
            The peer's result.

        Raises:
            MalkuthError: A2A/``A2A_002`` if the peer is unreachable,
                ``A2A_001`` if the stream ends with no terminal state,
                ``A2A_003`` if the peer reports failure.
        """
        client = await self._client(callee, token=token, headers=headers)
        request = pb.SendMessageRequest(message=build_message(task))

        final: pb.Task | None = None
        try:
            async for response in client.send_message(request):
                if response.WhichOneof("payload") != "task":
                    continue
                final = response.task
                if state_name(final.status.state) not in ("submitted", "working"):
                    break
        except A2AError as err:
            # callee 가 거부한 사유를 그대로 살린다 — 전부 A2A_003 으로 뭉개면
            # allowlist 위반(설정 문제)과 peer 실패(운영 문제)가 구분되지 않는다
            raise self._refusal(callee, err) from err
        except httpx.HTTPError as err:
            raise unreachable(self.agent, callee, cause=type(err).__name__) from err

        return self._result(task, callee, final)

    def _refusal(self, callee: str, err: A2AError) -> MalkuthError:
        """peer 가 돌려준 거부를 프레임워크 에러로 옮긴다.

        SDK 는 원격 에러를 문자열로만 실어 오므로 코드 표기를 되읽는다 —
        구조화된 채널이 없다.
        """
        text = str(err)
        for code in (ErrorCode.A2A_004, ErrorCode.A2A_005):
            if f"a2a:{code}" in text:
                return a2a_error(code, text, caller=self.agent, callee=callee, retryable=False)
        return task_rejected(self.agent, callee, reason=text)

    def _result(self, task: TaskRequest, callee: str, final: pb.Task | None) -> TaskResult:
        """스트림 결과를 프레임워크 표현으로 옮긴다."""
        if final is None:
            # 종료 상태 없이 끝난 스트림을 성공으로 접으면 빈 결과가 state 로 흘러간다
            raise submit_failed(self.agent, callee, reason="no terminal task state")

        name = state_name(final.status.state)
        if name == "completed":
            return TaskResult.completed(task, output=read_output(final))
        raise task_rejected(self.agent, callee, state=name)

    async def _client(self, callee: str, *, token: str, headers: Mapping[str, str]) -> Client:
        """peer 별 클라이언트 — 토큰을 헤더에 실어 둔다."""
        if callee not in self._clients:
            address = self.addresses.get(callee)
            if address is None:
                raise unreachable(self.agent, callee, reason="peer address was not injected")
            http = httpx.AsyncClient(
                timeout=self.timeout_s, headers=self.call_headers(token, headers)
            )
            factory = ClientFactory(ClientConfig(httpx_client=http, streaming=True))
            self._clients[callee] = await factory.create_from_url(address)
        return self._clients[callee]

    async def close(self) -> None:
        """열어둔 peer 클라이언트를 정리한다."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


__all__ = [
    "CALLER_HEADER",
    "STATE_NAMES",
    "TOKEN_HEADER",
    "SdkPeerTransport",
    "build_message",
    "read_output",
    "state_name",
]
