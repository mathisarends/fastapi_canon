import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[3] / "fastapi_canon" / "error"


def source_modules() -> list[Path]:
    modules = sorted(SOURCE_ROOT.glob("*.py"))
    assert modules, f"No product modules found in {SOURCE_ROOT}"
    return modules


def test_product_modules_use_modern_annotation_syntax() -> None:
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        assert ast.get_docstring(tree, clean=False) is None, path
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "typing"
            and any(alias.name in {"Generic", "TypeVar"} for alias in node.names)
            for node in ast.walk(tree)
        ), path


def test_product_modules_do_not_hide_modules_or_loggers() -> None:
    assert {path.name for path in source_modules() if path.name.startswith("_")} == {
        "__init__.py"
    }

    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(
            isinstance(node, ast.Name) and node.id == "_logger"
            for node in ast.walk(tree)
        ), path
