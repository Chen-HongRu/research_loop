"""Pydantic output contracts projected to the existing CLI JSON interface."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, create_model


class OutputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


def choices(*values):
    """Literal values with JSON type identity (True must not match integer 1)."""

    def exact_type(value):
        if not any(type(value) is type(item) for item in values):
            raise ValueError("Choice has the wrong JSON type")
        return value

    return Annotated[Literal[values], BeforeValidator(exact_type)]


class OutputSchema(dict):
    """A CLI schema and its originating model, never reconstructed from JSON."""

    def __init__(self, model: type[OutputModel]):
        self.model = model
        generated = model.model_json_schema()
        definitions = generated.pop("$defs", {})

        def inline(value):
            if isinstance(value, list):
                return [inline(item) for item in value]
            if not isinstance(value, dict):
                return value
            if "$ref" in value:
                target = definitions[value["$ref"].removeprefix("#/$defs/")]
                value = {**target, **{k: v for k, v in value.items() if k != "$ref"}}
            result = {key: inline(item) for key, item in value.items()}
            if "const" in result:
                result["enum"] = [result.pop("const")]
            return result

        super().__init__(inline(generated))

    def validate(self, value):
        self.model.model_validate(value, strict=True)
        # Retained JSON receipts keep their original numeric representation.
        return value


def with_fields(schema: OutputSchema, **fields) -> OutputSchema:
    """Derive an output model before generating its schema or validating it."""
    definitions = {
        name: (field.annotation, deepcopy(field))
        for name, field in schema.model.model_fields.items()
    }
    definitions.update({name: (annotation, ...) for name, annotation in fields.items()})
    return OutputSchema(
        create_model(schema.model.__name__, __base__=OutputModel, **definitions)
    )


def omit_fields(schema: OutputSchema, *names: str) -> OutputSchema:
    fields = {
        name: (field.annotation, deepcopy(field))
        for name, field in schema.model.model_fields.items()
        if name not in names
    }
    return OutputSchema(
        create_model(schema.model.__name__, __base__=OutputModel, **fields)
    )
