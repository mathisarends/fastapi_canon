import shutil
from pathlib import Path
from textwrap import dedent

import pytest

from fastapi_canon._checks.analysis import _Flow
from fastapi_canon._checks.contracts import _check_error_contracts, _Checker, _Report
from fastapi_canon._checks.source import _Definition, _Source
from fastapi_canon.check_error_contracts import _main

PRELUDE = """
from fastapi_canon import CanonRouter, Error, ErrorRegistry

class Missing(Exception):
    pass

missing = Error(Missing, status=404, code="missing", title="Missing")
registry = ErrorRegistry(errors=[missing])
router = CanonRouter(error_registry=registry)
"""


def analyze(tmp_path: Path, source: str, **modules: str) -> _Report:
    (tmp_path / "api.py").write_text(dedent(source), encoding="utf-8")
    for name, content in modules.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(content), encoding="utf-8")
    return _check_error_contracts([tmp_path])


def codes(report: _Report) -> list[str]:
    return [
        item.code for operation in report.operations for item in operation.diagnostics
    ]


def test_missing_contract_has_call_path_and_raise_location(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Service:
    async def complete(self) -> None:
        raise Missing()

@router.post('/completion')
async def complete(service: Service) -> None:
    await service.complete()
""",
    )
    assert codes(report) == ["CAN001"]
    (diagnostic,) = report.operations[0].diagnostics
    assert diagnostic.trace == ("api.complete", "api.Service.complete")
    assert "missing" in diagnostic.message
    assert (tmp_path / "api.py").read_text().splitlines()[
        diagnostic.line - 1
    ].strip() == "raise Missing()"


@pytest.mark.parametrize("shared", [False, True])
def test_shared_and_local_contracts(tmp_path: Path, shared: bool) -> None:
    prelude = (
        PRELUDE.replace(
            "router = CanonRouter(error_registry=registry)",
            "router = CanonRouter(error_registry=registry, raises=[missing])",
        )
        if shared
        else PRELUDE
    )
    decorator = "@router.get('/')" if shared else "@router.get('/', raises=[missing])"
    report = analyze(
        tmp_path,
        prelude
        + f"""
{decorator}
def endpoint() -> None:
    raise Missing
""",
    )
    assert not codes(report)
    assert report.operations[0].complete


def test_unregistered_exception(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Conflict(Exception):
    pass

@router.get('/')
def endpoint() -> None:
    raise Conflict()
""",
    )
    assert codes(report) == ["CAN002"]


def test_import_aliases_reexports_and_dishka_services(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        """
from fastapi_canon import CanonRouter as Router
from dishka.integrations.fastapi import FromDishka
from app import Service, missing, registry

router = Router(error_registry=registry, raises=[missing])

@router.get('/')
async def endpoint(service: FromDishka[Service]) -> None:
    await service.run()
""",
        **{
            "app/__init__.py": "from .services import Service\nfrom .errors import missing, registry",
            "app/errors.py": PRELUDE,
            "app/services.py": """
from .errors import Missing
from .helper import fail as helper

class Service:
    async def run(self) -> None:
        helper()
""",
            "app/helper.py": """
from .errors import Missing

def fail() -> None:
    raise Missing()
""",
        },
    )
    assert len(report.operations) == 1
    assert not codes(report)


def test_constructor_injection_and_abstract_port(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from abc import ABC, abstractmethod

class Repository(ABC):
    @abstractmethod
    async def save(self) -> None: ...

class Service:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def run(self) -> None:
        await self._repository.save()
        raise Missing()

@router.post('/')
async def endpoint(service: Service) -> None:
    await service.run()
""",
    )
    assert set(codes(report)) == {"CAN001", "CAN003"}
    assert any(
        item.message == "abstract implementation"
        for item in report.operations[0].diagnostics
    )


@pytest.mark.parametrize(
    "handler",
    [
        "except Missing: pass",
        "except Exception: pass",
        "except (ValueError, Missing): pass",
    ],
)
def test_caught_exceptions_do_not_escape(tmp_path: Path, handler: str) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + f"""
def helper() -> None:
    raise Missing()

@router.get('/')
def endpoint() -> None:
    try:
        helper()
    {handler}
""",
    )
    assert not codes(report)


def test_exception_translation_and_reraise(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Internal(Exception): pass

def helper() -> None:
    raise Internal()

@router.get('/')
def endpoint() -> None:
    try:
        helper()
    except Internal:
        raise Missing() from None

@router.get('/again')
def again() -> None:
    try:
        helper()
    except Internal:
        raise
""",
    )
    assert codes(report) == ["CAN001", "CAN002"]
    assert "Internal" not in report.operations[0].diagnostics[0].message


def test_mro_uses_most_specific_registered_mapping(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE.replace("registry = ErrorRegistry(errors=[missing])", "").replace(
            "router = CanonRouter(error_registry=registry)", ""
        )
        + """
class Specific(Missing): pass
specific = Error(Specific, status=409, code='specific', title='Specific')
registry = ErrorRegistry(errors=[missing, specific])
router = CanonRouter(error_registry=registry, raises=[missing])

@router.get('/')
def endpoint() -> None:
    raise Specific()
""",
    )
    assert codes(report) == ["CAN001"]
    assert "api.specific" in report.operations[0].diagnostics[0].message


def test_base_mapping_covers_unregistered_subclass(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Specific(Missing): pass

@router.get('/', raises=[missing])
def endpoint() -> None:
    raise Specific()
""",
    )
    assert not codes(report)


def test_fastapi_dependencies_and_annotated_aliases(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from typing import Annotated
from fastapi import Depends

def authentication() -> str:
    raise Missing()

def nested(identity: Annotated[str, Depends(authentication)]) -> str:
    return identity

Identity = Annotated[str, Depends(nested)]

@router.get('/')
def endpoint(identity: Identity) -> str:
    return identity
""",
    )
    assert codes(report) == ["CAN001"]
    assert report.operations[0].diagnostics[0].trace == ("api.authentication",)


@pytest.mark.parametrize("location", ["router", "operation"])
def test_router_and_operation_dependencies(tmp_path: Path, location: str) -> None:
    declarations = (
        "router = CanonRouter(error_registry=registry, dependencies=[Depends(authentication)])"
        if location == "router"
        else ""
    )
    options = (
        ", dependencies=[Depends(authentication)]" if location == "operation" else ""
    )
    report = analyze(
        tmp_path,
        (
            PRELUDE.replace("router = CanonRouter(error_registry=registry)", "")
            if location == "router"
            else PRELUDE
        )
        + f"""
from fastapi import Depends

def authentication() -> None:
    raise Missing()

{declarations}
@router.get('/'{options})
def endpoint() -> None:
    pass
""",
    )
    assert codes(report) == ["CAN001"]


@pytest.mark.parametrize(
    "body",
    ["external()", "raise make_exception()", "while ready():\n        pass", "yield 1"],
)
def test_incomplete_analysis_is_explicit(tmp_path: Path, body: str) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + f"""
@router.get('/')
def endpoint():
    {body}
""",
    )
    assert "CAN003" in codes(report)
    assert report.incomplete == 1


def test_recursion_is_not_reported_as_complete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
def helper():
    helper()

@router.get('/')
def endpoint():
    helper()
""",
    )
    assert codes(report) == ["CAN003"]


def test_nested_function_body_is_not_executed(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
@router.get('/')
def endpoint():
    def unused():
        raise Missing()
    return None
""",
    )
    assert not codes(report)


def test_dynamic_contracts_remain_incomplete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
@router.get('/', raises=build_errors())
def endpoint():
    pass
""",
    )
    assert codes(report) == ["CAN003"]


def test_no_application_code_is_imported(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    analyze(
        tmp_path,
        PRELUDE
        + f"""
open({str(marker)!r}, 'w').close()

@router.get('/')
def endpoint():
    pass
""",
    )
    assert not marker.exists()


def test_cli_warning_strict_and_json_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analyze(
        tmp_path,
        PRELUDE
        + """
@router.get('/')
def endpoint():
    external()
    raise Missing()
""",
    )
    assert _main([str(tmp_path)]) == 0
    assert "1 contract gaps; 1 incomplete operations" in capsys.readouterr().out
    assert _main([str(tmp_path), "--strict"]) == 1
    assert _main([str(tmp_path), "--require-complete", "--format", "json"]) == 1
    assert '"complete": false' in capsys.readouterr().out


def test_cli_syntax_error_and_empty_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main([str(tmp_path), "--require-complete"]) == 1
    assert "No supported CanonRouter operations" in capsys.readouterr().out
    (tmp_path / "broken.py").write_text("def (", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        _main([str(tmp_path)])
    assert error.value.code == 2


def test_duplicate_module_roots_rejected(tmp_path: Path) -> None:
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "api.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate module"):
        _check_error_contracts([tmp_path / "one", tmp_path / "two"])


def test_reference_service_detects_removed_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    shutil.copytree(root / "examples/reference-service/src", tmp_path / "src")
    before = _check_error_contracts([tmp_path / "src"])
    assert len(before.operations) == 6
    assert before.gaps == 0
    assert before.incomplete > 0
    router = tmp_path / "src/app/features/tasks/presentation/router.py"
    router.write_text(
        router.read_text("utf-8").replace("    raises=[task_not_found],\n", ""),
        encoding="utf-8",
    )
    after = _check_error_contracts([tmp_path / "src"])
    assert after.gaps == 1
    finding = next(
        item
        for operation in after.operations
        for item in operation.diagnostics
        if item.code == "CAN001"
    )
    assert "task_not_found" in finding.message


def test_handler_translation_from_implicit_failure(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
@router.get('/')
def endpoint(value: str):
    try:
        return int(value)
    except ValueError:
        raise Missing()
""",
    )
    assert "CAN001" in codes(report)


@pytest.mark.parametrize(
    "statement",
    [
        "router = build_router()",
        "registry = build_registry(); router = CanonRouter(error_registry=registry)",
        "registry.errors.append(missing)",
    ],
)
def test_dynamic_and_mutated_declarations_are_incomplete(
    tmp_path: Path, statement: str
) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + f"""
{statement}
@router.get('/')
def endpoint(): pass
""",
    )
    assert report.incomplete == 1


def test_nested_route_factory_is_reported(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        """
from fastapi_canon import CanonRouter
def create_router():
    router = CanonRouter()
    @router.get('/')
    def endpoint(): pass
    return router
""",
    )
    assert codes(report) == ["CAN003"]


def test_unawaited_coroutine_is_not_an_http_error(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
async def later():
    raise Missing()

@router.get('/')
def endpoint():
    return later()
""",
    )
    assert codes(report) == ["CAN003"]


def test_shadowed_helper_does_not_use_global_binding(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
def callback(): pass

@router.get('/')
def endpoint(callback):
    callback()
""",
    )
    assert codes(report) == ["CAN003"]


def test_inherited_method_dispatch_is_incomplete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Base:
    def run(self): self.fail()
    def fail(self): pass

class Specific(Base):
    def fail(self): raise Missing()

@router.get('/')
def endpoint(service: Specific):
    service.run()
""",
    )
    assert report.incomplete == 1


def test_constructor_and_property_exceptions(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Value:
    def __init__(self): raise Missing()

class Service:
    @property
    def value(self): raise Missing()

@router.get('/')
def endpoint():
    return Value()

@router.get('/property')
def property_endpoint(service: Service):
    return service.value
""",
    )
    assert codes(report) == ["CAN001", "CAN001"]
    assert report.operations[0].diagnostics[0].trace == (
        "api.endpoint",
        "api.Value.__init__",
    )


def test_lazy_dependency_alias(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from typing import Annotated
from fastapi import Depends
def authentication(): raise Missing()
type Identity = Annotated[str, Depends(authentication)]

@router.get('/')
def endpoint(identity: Identity): pass
""",
    )
    assert codes(report) == ["CAN001"]


def test_cyclic_contract_sequence_is_incomplete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
errors = [*errors]
@router.get('/', raises=errors)
def endpoint(): pass
""",
    )
    assert report.incomplete == 1


def test_recursive_type_alias_is_incomplete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from typing import Annotated
type Service = Annotated[Service, 'metadata']
@router.get('/')
def endpoint(service: Service): service.run()
""",
    )
    assert report.incomplete == 1


def test_nested_function_defaults_execute(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
def helper(): raise Missing()
@router.get('/')
def endpoint():
    def unused(value=helper()): pass
""",
    )
    assert codes(report) == ["CAN001"]


def test_mutated_contract_does_not_guess_missing_errors(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
shared = []
shared.append(missing)
another_router = CanonRouter(error_registry=registry, raises=shared)
@another_router.get('/')
def endpoint(): raise Missing()
""",
    )
    assert codes(report) == ["CAN003"]


@pytest.mark.parametrize("initially_declared", [False, True])
def test_reassigned_annotated_contract_is_incomplete(
    tmp_path: Path, initially_declared: bool
) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + f"""
shared: list = {"[missing]" if initially_declared else "[]"}
@router.get('/', raises=shared)
def endpoint(): raise Missing()
shared: list = {"[]" if initially_declared else "[missing]"}
""",
    )
    assert report.gaps == 0
    assert report.incomplete == 1
    assert codes(report) == ["CAN003"]
    assert (
        "reassigned module binding: shared"
        in report.operations[0].diagnostics[0].message
    )
    assert _main([str(tmp_path), "--strict", "--require-complete"]) == 1


@pytest.mark.parametrize("explicit_errors", [False, True])
def test_expanded_registry_is_incomplete_without_guessed_gaps(
    tmp_path: Path, explicit_errors: bool
) -> None:
    configuration = (
        'config = {"type_base": "https://example.test/problems"}\n'
        "registry = ErrorRegistry(errors=[missing], **config)"
        if explicit_errors
        else 'config = {"errors": [missing]}\nregistry = ErrorRegistry(**config)'
    )
    report = analyze(
        tmp_path,
        PRELUDE.replace("registry = ErrorRegistry(errors=[missing])", configuration)
        + """
@router.get('/', raises=[missing])
def endpoint(): raise Missing()
""",
    )
    assert report.gaps == 0
    assert report.incomplete == 1
    assert codes(report) == ["CAN003"]
    assert (
        "expanded ErrorRegistry keyword arguments"
        in report.operations[0].diagnostics[0].message
    )
    assert _main([str(tmp_path), "--strict"]) == 0
    assert _main([str(tmp_path), "--strict", "--require-complete"]) == 1


def test_cli_help_names_the_error_contract_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as result:
        _main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "python -m fastapi_canon.check_error_contracts" in output
    assert "CanonRouter raises declarations" in output


@pytest.mark.parametrize("declared", [True, False])
def test_direct_router_policy_contracts(tmp_path: Path, declared: bool) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + f"""
from fastapi_canon import RouterPolicy
policy = RouterPolicy(error_registry=registry, raises={"[missing]" if declared else "[]"})
secured = CanonRouter(policy=policy)
@secured.get('/')
def endpoint(): raise Missing()
""",
    )
    assert codes(report) == ([] if declared else ["CAN001"])


def test_policy_dependencies_and_local_registry_combine(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from fastapi import Depends
from fastapi_canon import RouterPolicy
class Conflict(Exception): pass
conflict = Error(Conflict, status=409, code='conflict', title='Conflict')
local_registry = ErrorRegistry(errors=[conflict])
def authentication(): raise Missing()
policy = RouterPolicy(error_registry=registry, dependencies=[Depends(authentication)])
secured = CanonRouter(policy=policy, error_registry=local_registry, raises=[conflict])
@secured.get('/')
def endpoint(): raise Conflict()
""",
    )
    assert codes(report) == ["CAN001"]
    assert "api.missing" in report.operations[0].diagnostics[0].message


def test_dynamic_policy_is_incomplete(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
secured = CanonRouter(policy=build_policy())
@secured.get('/')
def endpoint(): raise Missing()
""",
    )
    assert codes(report) == ["CAN003"]


def test_composition_policy_is_incomplete_instead_of_guessing(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from fastapi_canon import Composition, RouterPolicy
composition = Composition(router_policy=RouterPolicy(error_registry=registry, raises=[missing]))
@router.get('/')
def endpoint(): raise Missing()
""",
    )
    assert codes(report) == ["CAN003"]
    assert "composition policy mounting" in report.operations[0].diagnostics[0].message


def test_shared_helpers_are_analyzed_once_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyze(
        tmp_path,
        PRELUDE
        + """
def shared(): raise Missing()
@router.get('/first')
def first(): shared()
@router.get('/second', raises=[missing])
def second(): shared()
""",
    )
    checker = _Checker(_Source.read([tmp_path]))
    calls: list[str] = []
    original = checker.analyzer._analyze_function

    def counted(function: _Definition, stack: tuple[str, ...]) -> _Flow:
        calls.append(function.name)
        return original(function, stack)

    monkeypatch.setattr(checker.analyzer, "_analyze_function", counted)
    report = checker.check()
    assert calls.count("api.shared") == 1
    assert codes(report) == ["CAN001"]
    assert not report.operations[1].diagnostics


def test_cached_function_does_not_acquire_route_dependencies(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from fastapi import Depends
def authenticate(): raise Missing()
@router.get('/first', dependencies=[Depends(authenticate)])
def first(): pass
@router.get('/second')
def second(): first()
""",
    )
    assert codes(report) == ["CAN001"]
    assert not report.operations[1].diagnostics


def test_cached_call_graph_preserves_uncertain_modules(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from outer import run
@router.get('/first')
def first(): run()
@router.get('/second')
def second(): run()
""",
        **{
            "outer.py": "from inner import work\ndef run(): work()\n",
            "inner.py": "state = 1\nstate = 2\ndef work(): pass\n",
        },
    )
    assert len(report.operations) == report.incomplete == 2
    assert all(
        "reassigned module binding" in operation.diagnostics[0].message
        for operation in report.operations
    )


def test_cached_inheritance_preserves_uncertain_modules(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        """
from fastapi_canon import CanonRouter
from errors import registry
router = CanonRouter(error_registry=registry)
@router.get('/first')
def first(): pass
@router.get('/second')
def second(): pass
""",
        **{
            "base.py": "state = 1\nstate = 2\nclass Base(Exception): pass\n",
            "errors.py": """
from fastapi_canon import Error, ErrorRegistry
from base import Base
class Missing(Base): pass
missing = Error(Missing, status=404, code='missing', title='Missing')
registry = ErrorRegistry(errors=[missing])
""",
        },
    )
    assert len(report.operations) == report.incomplete == 2
    assert report.gaps == 0


def test_recursive_summaries_preserve_each_entry_context(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
def a(): b()
def b():
    a()
    raise Missing()
@router.get('/first')
def first(): a()
@router.get('/second')
def second(): b()
""",
    )
    assert report.incomplete == report.gaps == 2
    paths = [
        next(item.trace for item in operation.diagnostics if item.code == "CAN001")
        for operation in report.operations
    ]
    assert paths == [("api.first", "api.a", "api.b"), ("api.second", "api.b")]


def test_summary_retains_first_error_paths_and_order(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
class Conflict(Exception): pass
def shared(flag: bool):
    if flag:
        raise Missing()
    if flag:
        raise Conflict()
    raise Missing()
@router.get('/')
def endpoint(): shared(True)
""",
    )
    assert codes(report) == ["CAN001", "CAN002"]
    first = report.operations[0].diagnostics[0]
    assert (tmp_path / "api.py").read_text("utf-8").splitlines()[
        first.line - 2
    ].strip() == "if flag:"


def test_separate_runs_recheck_changed_helpers(tmp_path: Path) -> None:
    report = analyze(
        tmp_path,
        PRELUDE
        + """
from helper import work
@router.get('/')
def endpoint(): work()
""",
        **{"helper.py": "def work(): pass\n"},
    )
    assert not codes(report)
    (tmp_path / "helper.py").write_text(
        "from api import Missing\ndef work(): raise Missing()\n", encoding="utf-8"
    )
    assert codes(_check_error_contracts([tmp_path])) == ["CAN001"]
