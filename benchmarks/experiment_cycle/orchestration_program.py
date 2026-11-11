"""Load a small, explicitly bounded orchestration program."""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

DEFAULT_PATH = Path(__file__).with_name("orchestration.py")
MAX_SOURCE_BYTES = 64 * 1024
MAX_COMMON_BYTES = 16 * 1024


@dataclass(frozen=True)
class Program:
    source: str
    common: str
    run: Callable[..., Awaitable]


class _Validator(ast.NodeVisitor):
    """Accept only the expression and statement forms in the candidate language."""

    allowed = (
        ast.Assign,
        ast.Expr,
        ast.If,
        ast.Return,
        ast.Await,
        ast.Call,
        ast.Attribute,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.List,
        ast.Dict,
        ast.Subscript,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.JoinedStr,
        ast.FormattedValue,
        ast.keyword,
    )

    def generic_visit(self, node):
        if not isinstance(node, self.allowed):
            raise ValueError(f"Unsupported orchestration syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Assign(self, node):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError("Assignments require one local name")
        if node.targets[0].id in {"team", "COMMON"}:
            raise ValueError("Cannot reassign team or COMMON")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == "team":
            raise ValueError("team is only available for method calls")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        raise ValueError("Attributes are only available as team method calls")

    def visit_Call(self, node):
        raise ValueError("Calls must be awaited team methods")

    def visit_Await(self, node):
        call = node.value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "team"
            and call.func.attr in {"call", "message", "execute"}
            and all(keyword.arg is not None for keyword in call.keywords)
        ):
            raise ValueError(
                "Only team.call, team.message and team.execute may be awaited"
            )
        for arg in call.args:
            self.visit(arg)
        for keyword in call.keywords:
            self.visit(keyword.value)

    def visit_Dict(self, node):
        if any(key is None for key in node.keys):
            raise ValueError("Dictionary unpacking is not supported")
        self.generic_visit(node)

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.USub) and not (
            isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float)
        ):
            raise ValueError("Negative numbers must be literals")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if type(node.value) not in (str, int, float, bool, type(None)):
            raise ValueError("Only JSON literals are supported")


def load_orchestration(path=None):
    """Validate and compile a candidate without exposing Python builtins."""
    with Path(path if path is not None else DEFAULT_PATH).open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Orchestration file exceeds size limit")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ValueError("Invalid orchestration source") from error
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if (
        len(body) != 2
        or not isinstance(body[0], ast.Assign)
        or not isinstance(body[1], ast.AsyncFunctionDef)
    ):
        raise ValueError("Expected COMMON followed by orchestrate")
    assignment, function = body
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "COMMON"
        or not isinstance(assignment.value, ast.Constant)
        or not isinstance(assignment.value.value, str)
    ):
        raise ValueError("COMMON must be a string literal")
    common = assignment.value.value
    if (
        not common.strip()
        or "\x00" in common
        or len(common.encode("utf-8")) > MAX_COMMON_BYTES
    ):
        raise ValueError("Invalid COMMON")
    args = function.args
    if (
        function.name != "orchestrate"
        or function.decorator_list
        or function.returns
        or function.type_params
        or len(args.args) != 1
        or args.args[0].arg != "team"
        or args.args[0].annotation
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or args.kw_defaults
    ):
        raise ValueError("Expected async def orchestrate(team)")
    validator = _Validator()
    for statement in function.body:
        validator.visit(statement)
    namespace = {"__builtins__": {}}
    exec(compile(tree, str(path or DEFAULT_PATH), "exec"), namespace)
    return Program(source=source, common=common, run=namespace["orchestrate"])
