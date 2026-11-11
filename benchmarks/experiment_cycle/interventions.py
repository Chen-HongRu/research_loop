"""Check that a candidate implements its assigned Cactus architecture intervention."""

import ast

# Parameter order, number of positional parameters and constructor defaults.
_SIGNATURES = {
    "Conv2d": (
        (
            "in_channels",
            "out_channels",
            "kernel_size",
            "stride",
            "padding",
            "dilation",
            "groups",
            "bias",
            "padding_mode",
            "device",
            "dtype",
        ),
        9,
        {
            "stride": 1,
            "padding": 0,
            "dilation": 1,
            "groups": 1,
            "bias": True,
            "padding_mode": "zeros",
            "device": None,
            "dtype": None,
        },
    ),
    "ReLU": (("inplace",), 1, {"inplace": False}),
    "GELU": (("approximate",), 1, {"approximate": "none"}),
    "SiLU": (("inplace",), 1, {"inplace": False}),
    "MaxPool2d": (
        ("kernel_size", "stride", "padding", "dilation", "return_indices", "ceil_mode"),
        6,
        {
            "stride": None,
            "padding": 0,
            "dilation": 1,
            "return_indices": False,
            "ceil_mode": False,
        },
    ),
    "AdaptiveAvgPool2d": (("output_size",), 1, {}),
    "AdaptiveMaxPool2d": (
        ("output_size", "return_indices"),
        2,
        {"return_indices": False},
    ),
    "Flatten": (("start_dim", "end_dim"), 2, {"start_dim": 1, "end_dim": -1}),
    "Linear": (
        ("in_features", "out_features", "bias", "device", "dtype"),
        3,
        {"bias": True, "device": None, "dtype": None},
    ),
    "BatchNorm2d": (
        (
            "num_features",
            "eps",
            "momentum",
            "affine",
            "track_running_stats",
            "device",
            "dtype",
        ),
        5,
        {
            "eps": 1e-5,
            "momentum": 0.1,
            "affine": True,
            "track_running_stats": True,
            "device": None,
            "dtype": None,
        },
    ),
    "GroupNorm": (
        ("num_groups", "num_channels", "eps", "affine", "device", "dtype"),
        4,
        {"eps": 1e-5, "affine": True, "device": None, "dtype": None},
    ),
    "Dropout": (("p", "inplace"), 2, {"p": 0.5, "inplace": False}),
}
_BOOL_FIELDS = {
    "bias",
    "inplace",
    "return_indices",
    "ceil_mode",
    "affine",
    "track_running_stats",
}
_INT_FIELDS = {
    "in_channels",
    "out_channels",
    "groups",
    "in_features",
    "out_features",
    "num_features",
    "num_groups",
    "num_channels",
    "start_dim",
    "end_dim",
}
_PAIR_FIELDS = {"kernel_size", "stride", "padding", "dilation", "output_size"}


def _layer(call):
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "nn"
        and call.func.attr in _SIGNATURES
    ):
        raise ValueError("Unsupported layer constructor")
    name = call.func.attr
    parameters, positional, defaults = _SIGNATURES[name]
    if len(call.args) > positional:
        raise ValueError(f"Too many positional arguments for {name}")
    values = dict(defaults)
    provided = set()
    for key, node in zip(parameters, call.args):
        try:
            values[key] = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError) as error:
            raise ValueError("Layer arguments must be literals") from error
        provided.add(key)
    for keyword in call.keywords:
        if keyword.arg not in parameters or keyword.arg in provided:
            raise ValueError(f"Invalid or duplicate argument for {name}")
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError) as error:
            raise ValueError("Layer arguments must be literals") from error
        provided.add(keyword.arg)
    if set(values) != set(parameters):
        raise ValueError(f"Missing argument for {name}")
    for key, value in values.items():
        if key in _PAIR_FIELDS:
            if value is None and key == "stride" and name == "MaxPool2d":
                value = values["kernel_size"]
            if type(value) is int:
                value = (value, value)
            elif (
                isinstance(value, (tuple, list))
                and len(value) == 2
                and all(type(x) is int for x in value)
            ):
                value = tuple(value)
            else:
                raise ValueError(f"Invalid {key} for {name}")
        elif key in _BOOL_FIELDS and type(value) is not bool:
            raise ValueError(f"Invalid {key} for {name}")
        elif key in _INT_FIELDS and type(value) is not int:
            raise ValueError(f"Invalid {key} for {name}")
        elif type(value) not in (int, float, bool, str, type(None)):
            raise ValueError(f"Invalid {key} for {name}")
        values[key] = value
    return name, values


def _layers(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError) as error:
        raise ValueError("Invalid Python source") from error
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tiny_cnn"
    ]
    if (
        len(functions) != 1
        or len(functions[0].body) != 1
        or not isinstance(functions[0].body[0], ast.Return)
    ):
        raise ValueError("Expected one tiny_cnn with a single return")
    sequence = functions[0].body[0].value
    if not (
        isinstance(sequence, ast.Call)
        and isinstance(sequence.func, ast.Attribute)
        and isinstance(sequence.func.value, ast.Name)
        and sequence.func.value.id == "nn"
        and sequence.func.attr == "Sequential"
        and not sequence.keywords
    ):
        raise ValueError("tiny_cnn must return nn.Sequential")
    return [_layer(layer) for layer in sequence.args]


_BASELINE = """def tiny_cnn():
    return nn.Sequential(
        nn.Conv2d(3, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
        nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Linear(64, 1))
"""


def verify_intervention(baseline_source, candidate_source, intervention_id):
    """Reject candidates that do not implement exactly their assigned intervention."""
    if type(intervention_id) is not int or not 1 <= intervention_id <= 10:
        raise ValueError("Intervention ID must be an integer from 1 to 10")
    baseline = _layers(baseline_source)
    if baseline != _layers(_BASELINE):
        raise ValueError("Baseline tiny_cnn does not match the expected architecture")
    expected = _layers(_BASELINE)
    if intervention_id in (1, 2):
        name = "GELU" if intervention_id == 1 else "SiLU"
        expected = [
            _layer(ast.parse(f"nn.{name}()", mode="eval").body)
            if layer[0] == "ReLU"
            else layer
            for layer in expected
        ]
    elif intervention_id in (3, 4):
        expanded = []
        for layer in expected:
            expanded.append(layer)
            if layer[0] == "Conv2d":
                channels = layer[1]["out_channels"]
                code = (
                    f"nn.BatchNorm2d({channels})"
                    if intervention_id == 3
                    else f"nn.GroupNorm(8, {channels})"
                )
                expanded.append(_layer(ast.parse(code, mode="eval").body))
        expected = expanded
    elif intervention_id == 5:
        expected[8] = _layer(ast.parse("nn.AdaptiveMaxPool2d(1)", mode="eval").body)
    elif intervention_id == 6:
        expected.insert(10, _layer(ast.parse("nn.Dropout(0.2)", mode="eval").body))
    elif intervention_id in (7, 8):
        width = 96 if intervention_id == 7 else 32
        expected[3][1]["out_channels"] = width
        expected[6][1]["in_channels"] = width
        expected[6][1]["out_channels"] = width
        expected[10][1]["in_features"] = width
    elif intervention_id == 9:
        expected[0][1].update(kernel_size=(5, 5), padding=(2, 2))
    else:
        expected[8:8] = [
            _layer(ast.parse(code, mode="eval").body)
            for code in ("nn.Conv2d(64, 64, 3, padding=1)", "nn.ReLU()")
        ]
    if _layers(candidate_source) != expected:
        raise ValueError("Candidate does not match the assigned intervention")
