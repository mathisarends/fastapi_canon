from importlib.metadata import metadata

import fastapi_canon.error


def test_package_is_importable() -> None:
    assert fastapi_canon.error.__name__ == "fastapi_canon.error"


def test_package_declares_mit_license() -> None:
    package = metadata("fastapi-canon")

    assert package["License-Expression"] == "MIT"
    assert "License :: OSI Approved :: MIT License" in package.get_all("Classifier", [])
