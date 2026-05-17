from typing import Any, Coroutine, ParamSpec, TypeAlias, TypeVar, Callable, Protocol, TYPE_CHECKING, Hashable
if TYPE_CHECKING:
    from bogobot_core import BotCore

P = ParamSpec("P")
R = TypeVar("R", covariant=True)
T = TypeVar("T")
K = TypeVar("K", bound=Hashable)

class ObjectWithCommandDecorator(Protocol[P, R]):
    def command(self, *args: P.args, **kwargs: P.kwargs) -> Callable[['BotCore._Setup._Callable'], R]:
        ...

Coro: TypeAlias = Coroutine[Any, Any, T]
