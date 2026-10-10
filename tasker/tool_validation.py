from __future__ import annotations

from typing import Any


class ToolValidationError(
    ValueError
):
    pass


def _matches_type(
    value: Any,
    expected: str,
) -> bool:

    if expected == "string":
        return isinstance(value, str)

    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(
                value,
                bool,
            )
        )

    if expected == "number":
        return (
            isinstance(
                value,
                (int, float),
            )
            and not isinstance(
                value,
                bool,
            )
        )

    if expected == "boolean":
        return isinstance(
            value,
            bool,
        )

    if expected == "object":
        return isinstance(
            value,
            dict,
        )

    if expected == "array":
        return isinstance(
            value,
            list,
        )

    if expected == "null":
        return value is None

    raise ToolValidationError(
        f"unsupported schema type: "
        f"{expected}"
    )


def validate_tool_value(
    value: Any,
    schema: dict,
    *,
    path: str = "$",
) -> None:

    if not schema:
        return

    expected = schema.get(
        "type"
    )

    if expected is not None:

        if isinstance(
            expected,
            list,
        ):

            if not any(
                _matches_type(
                    value,
                    str(item),
                )
                for item
                in expected
            ):
                raise ToolValidationError(
                    f"{path}: invalid type"
                )

        elif not _matches_type(
            value,
            str(expected),
        ):

            raise ToolValidationError(
                f"{path}: expected "
                f"{expected}, got "
                f"{type(value).__name__}"
            )

    if "enum" in schema:

        allowed = schema["enum"]

        if value not in allowed:

            raise ToolValidationError(
                f"{path}: value "
                f"{value} not in enum"
            )

    if isinstance(
        value,
        dict,
    ):

        properties = (
            schema.get(
                "properties",
                {},
            )
            or {}
        )

        required = (
            schema.get(
                "required",
                [],
            )
            or []
        )

        for name in required:

            if name not in value:

                raise ToolValidationError(
                    f"{path}.{name}: "
                    "required"
                )

        if (
            schema.get(
                "additionalProperties"
            )
            is False
        ):

            extra = (
                set(value)
                - set(properties)
            )

            if extra:

                raise ToolValidationError(
                    f"{path}: unknown fields "
                    f"{sorted(extra)}"
                )

        for name, child in (
            properties.items()
        ):

            if name not in value:
                continue

            validate_tool_value(
                value[name],
                child,
                path=(
                    f"{path}.{name}"
                ),
            )

    if isinstance(
        value,
        list,
    ):

        item_schema = schema.get(
            "items"
        )

        if isinstance(
            item_schema,
            dict,
        ):

            for index, item in (
                enumerate(value)
            ):

                validate_tool_value(
                    item,
                    item_schema,
                    path=(
                        f"{path}[{index}]"
                    ),
                )


def validate_tool_input(
    input_data: dict,
    schema: dict,
) -> None:

    if not schema:
        return

    validate_tool_value(
        input_data,
        schema,
    )