"""Read operator-authored SQLite Study and application configuration."""

from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.constructor import BaseConstructor, DuplicateKeyError, SafeConstructor
from ruamel.yaml.error import YAMLError

from .operation_contract import json_value


class _ConfigConstructor(SafeConstructor):
    # Skip YAML 1.1 merge expansion, which also disables duplicate-key checks
    # for explicit fields in a merged mapping.
    construct_mapping = BaseConstructor.construct_mapping


def load_config(path: Path) -> dict:
    path = Path(path)
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("Configuration files must use .yaml or .yml")
    loader = YAML(typ="safe", pure=True)
    loader.Constructor = _ConfigConstructor
    loader.version = (1, 2)
    loader.allow_duplicate_keys = False
    with path.open(encoding="utf-8") as stream:
        try:
            value = loader.load(stream)
        except YAMLError as exc:
            # Parser messages may include values from private application config.
            mark = getattr(exc, "problem_mark", None)
            where = (
                f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            )
            reason = (
                "duplicate mapping key"
                if isinstance(exc, DuplicateKeyError)
                else "invalid YAML"
            )
            raise ValueError(f"{path.name}: {reason}{where}") from None
        except AssertionError:
            # ruamel rejects unsupported version directives with AssertionError.
            raise ValueError("Configuration requires YAML 1.2") from None
    if loader.version != (1, 2):
        raise ValueError("Configuration requires YAML 1.2")
    if not isinstance(value, dict):
        raise ValueError("Expected a YAML configuration mapping")
    return json_value(value)
