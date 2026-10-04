"""Data objects are `@dataclass(frozen=True, slots=True)` (metalm coding.md,
"Data objects"); a mutable one is allowed only by name here, with the reason
mutation is the point."""

import dataclasses
import importlib
import pkgutil

import corpuslm

# name → why it stays mutable (or keeps a __dict__)
ALLOWED: dict[str, str] = {}


def _dataclasses():
    for info in pkgutil.walk_packages(corpuslm.__path__, "corpuslm."):
        module = importlib.import_module(info.name)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and dataclasses.is_dataclass(obj)
                and obj.__module__ == module.__name__
            ):
                yield f"{module.__name__}.{obj.__qualname__}", obj


def test_every_data_object_is_frozen_with_slots():
    found = dict(_dataclasses())
    assert "corpuslm.model.Segment" in found
    bad = [
        name
        for name, cls in found.items()
        if name not in ALLOWED
        and not (cls.__dataclass_params__.frozen and "__slots__" in cls.__dict__)
    ]
    assert bad == []


def test_the_allowed_list_names_only_real_classes():
    assert set(ALLOWED) <= dict(_dataclasses()).keys()
