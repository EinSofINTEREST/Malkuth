"""HTTP surface for the Memory Service.

에이전트는 이 표면을 통해서만 메모리에 닿는다 — **컨테이너에 DB 자격증명을
주지 않기 위해서다** (09 Access Enforcement 1). 저장소 자격증명은 이 프로세스
안에만 있고, 에이전트는 runtime 이 발급한 불투명 토큰만 제시한다.

토큰은 서버가 보관한다: 페이로드를 클라이언트에 넘기면 space 목록과 mode 를
위조할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.memory.entry import MemoryEntry
from malkuth.modules.memoryset import ChunkSpec, MemoryKind

if TYPE_CHECKING:
    from malkuth.memory.index import IndexRegistry
    from malkuth.memory.recall import Recall
    from malkuth.memory.service import AccessToken, MemoryService

MEMORY_TOKEN_ENV = "MALKUTH_MEMORY_TOKEN"  # noqa: S105 — env 키 이름이지 값이 아니다
MEMORY_URL_ENV = "MALKUTH_MEMORY_URL"
"""agentd 가 서비스에 닿기 위해 받는 값 — secrets 와 같은 주입 경로다."""

DEFAULT_SCAN_LIMIT = 500


def unauthorized(reason: str) -> MalkuthError:
    """토큰 없는/알 수 없는 요청 — space 존재 여부조차 알려주지 않는다."""
    return MalkuthError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_001,
        message=reason,
    )


@dataclass
class TokenRegistry:
    """Opaque memory tokens the service hands out.

    발급한 토큰을 서비스가 기억합니다 — 페이로드를 클라이언트에 넘기면
    space 목록과 mode 를 위조할 수 있습니다.
    """

    _tokens: dict[str, AccessToken] = field(default_factory=dict, init=False)

    def issue(self, access: AccessToken) -> str:
        """Mint an opaque token for one agent's access.

        Args:
            access: The assembled access token.

        Returns:
            The opaque string the agent presents.
        """
        secret = token_urlsafe(32)
        self._tokens[secret] = access
        return secret

    def resolve(self, secret: str | None) -> AccessToken:
        """Look up the access behind a presented token.

        Raises:
            MalkuthError: MEMORY/``MEM_001`` if the token is absent or unknown.
        """
        if not secret:
            raise unauthorized("memory token is required")
        access = self._tokens.get(secret)
        if access is None:
            raise unauthorized("memory token is not recognised")
        return access

    def forget(self, secret: str) -> None:
        """토큰을 폐기한다 — 그룹 이동이나 재배포 시 즉시 무효화한다."""
        self._tokens.pop(secret, None)


class SearchRequest(BaseModel):
    """검색 요청."""

    model_config = ConfigDict(frozen=True)

    query: str
    spaces: tuple[str, ...] | None = None
    k: int = 6
    scan: int = DEFAULT_SCAN_LIMIT


class AppendRequest(BaseModel):
    """항목 추가 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    entry: MemoryEntry


class ReadRequest(BaseModel):
    """space 훑기 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    limit: int = 100
    kinds: tuple[MemoryKind, ...] | None = None


class LatestRequest(BaseModel):
    """정정 체인의 최신 항목 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    entry_id: str


def create_app(
    service: MemoryService,
    recall: Recall,
    tokens: TokenRegistry,
    *,
    indexer: IndexRegistry | None = None,
    chunk: ChunkSpec | None = None,
) -> FastAPI:
    """Build the Memory Service HTTP app.

    메모리 서비스의 HTTP 표면을 만듭니다.

    Args:
        service: The framework-side memory gateway.
        recall: Hybrid search over the space indexes.
        tokens: Opaque token registry.
        indexer: Index registry the write path feeds — 없으면 저장만 하고
            색인하지 않습니다 (읽기 전용 배치용).
        chunk: Chunking policy for indexed entries.

    Returns:
        The FastAPI application.
    """
    spec = chunk or ChunkSpec()
    app = FastAPI(title="Malkuth Memory Service")

    @app.exception_handler(MalkuthError)
    async def _structured(_request: Request, err: MalkuthError) -> JSONResponse:
        """구조화 에러를 상태코드로 옮긴다.

        접근 거부(``MEM_001``)는 **401** 이다 — 클라이언트가 거부와 장애를
        구분해야 재시도 판단이 갈린다.
        """
        if err.code == ErrorCode.MEM_001:
            http_status = status.HTTP_401_UNAUTHORIZED
        elif err.retryable:
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            http_status = status.HTTP_400_BAD_REQUEST
        return JSONResponse(status_code=http_status, content=err.payload().model_dump(mode="json"))

    def granted(request: Request) -> AccessToken:
        """Bearer 토큰을 실제 접근 권한으로 바꾼다.

        헤더를 직접 읽는다 — ``Depends`` 를 인자 기본값에 두면 lint 가 막고,
        ``Annotated`` 로만 쓰면 FastAPI 가 dataclass 를 쿼리 파라미터로 읽는다.
        """
        header = request.headers.get("authorization", "")
        prefix = "bearer "
        presented = header[len(prefix) :] if header.lower().startswith(prefix) else None
        return tokens.resolve(presented)

    @app.post("/v1/search")
    async def search(request: SearchRequest, http_request: Request) -> list[dict[str, Any]]:
        """Search the spaces this agent may read.

        선언되지 않은 space 요청은 별칭 해석에서 ``MEM_001`` 로 거부됩니다.
        """
        token = granted(http_request)
        aliases = list(request.spaces or [space.alias for space in token.spaces])
        resolved = [_space_id(token, alias) for alias in aliases]
        entries = {
            entry.entry_id: entry
            for alias in aliases
            for entry in service.read(token, alias, limit=request.scan)
        }
        found = recall.search(request.query, spaces=resolved, k=request.k, entries=entries)
        return [
            {
                "entry": scored.entry.model_dump(mode="json"),
                "space": scored.space,
                "score": scored.score,
            }
            for scored in found
        ]

    @app.post("/v1/append")
    async def append(request: AppendRequest, http_request: Request) -> dict[str, Any]:
        """rw 권한이 있는 space 에만 추가합니다.

        색인은 **저장과 분리된 큐**로 넘깁니다 (09 Write Path) — 여기서
        embedding 을 기다리면 append 지연이 모델 호출만큼 길어집니다.
        """
        token = granted(http_request)
        stored = service.append(token, request.space, request.entry)
        if indexer is not None:
            # 저장만 하고 색인하지 않으면 그 기억은 검색에서 영원히 사라진다
            indexer.submit(stored, spec)
        return stored.model_dump(mode="json")

    @app.post("/v1/read")
    async def read(request: ReadRequest, http_request: Request) -> list[dict[str, Any]]:
        """선언된 space 를 최신순으로 읽습니다 — 검색 없이 훑는 창구."""
        token = granted(http_request)
        found = service.read(token, request.space, limit=request.limit, kinds=request.kinds)
        return [entry.model_dump(mode="json") for entry in found]

    @app.post("/v1/latest")
    async def latest(request: LatestRequest, http_request: Request) -> dict[str, Any] | None:
        """``supersedes`` 체인의 최신 항목 — 정정된 기억을 그대로 읽지 않기 위해서."""
        token = granted(http_request)
        found = service.latest(token, request.space, request.entry_id)
        return found.model_dump(mode="json") if found is not None else None

    @app.get("/v1/spaces")
    async def spaces(http_request: Request) -> list[dict[str, str]]:
        """이 토큰이 닿을 수 있는 space — 에이전트가 자기 범위를 확인한다."""
        token = granted(http_request)
        return [
            {"alias": space.alias, "scope": str(space.scope), "mode": str(space.mode)}
            for space in token.spaces
        ]

    return app


def _space_id(token: AccessToken, alias: str) -> str:
    """별칭 해석이 곧 경계 검사다 — 미선언 별칭은 ``MEM_001``."""
    space = token.resolve(alias)
    if space is None:
        raise unauthorized("memory space is not declared for this agent")
    return space.space_id


__all__ = [
    "MEMORY_TOKEN_ENV",
    "MEMORY_URL_ENV",
    "AppendRequest",
    "LatestRequest",
    "ReadRequest",
    "SearchRequest",
    "TokenRegistry",
    "create_app",
]
