"""Load a LookML project into plain Python structures.

Input is a mapping of file paths to file contents, so the same code runs on a local folder,
on files fetched from GitHub, and in the browser (Pyodide).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import lkml

FIELD_LISTS = ("dimensions", "dimension_groups", "measures", "filters", "parameters", "sets")


@dataclass
class Explore:
    name: str
    model: str
    raw: dict
    path: str


@dataclass
class Project:
    views: Dict[str, dict] = field(default_factory=dict)
    explores: List[Explore] = field(default_factory=list)
    warnings: List[Tuple[str, str]] = field(default_factory=list)  # (path, message)
    files: int = 0
    constants: Dict[str, str] = field(default_factory=dict)


def _model_name(path: str) -> Optional[str]:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(".model.lkml") or base.endswith(".model.lookml"):
        return base.split(".model.")[0]
    return None


def _merge_view(base: dict, child: dict) -> dict:
    """Child fields override base fields with the same name; other keys override too."""
    merged = copy.deepcopy(base)
    for key, value in child.items():
        if key in FIELD_LISTS:
            existing = {f.get("name"): f for f in merged.get(key, [])}
            for item in value:
                name = item.get("name")
                if name in existing:
                    updated = dict(existing[name])
                    updated.update(item)
                    existing[name] = updated
                else:
                    existing[name] = item
            merged[key] = list(existing.values())
        elif key not in ("name", "extends__all", "extends", "extension"):
            merged[key] = value
    return merged


def _extends_of(view: dict) -> List[str]:
    names: List[str] = []
    for group in view.get("extends__all", []) or []:
        names.extend(group)
    if isinstance(view.get("extends"), list):
        names.extend(view["extends"])
    return names


CONSTANT = re.compile(r"@\{\s*(\w+)\s*\}")


def _read_constants(files: Dict[str, str], project: Project) -> None:
    """Collect `constant:` values from manifest files. Looker substitutes @{name} as text."""
    for path, text in files.items():
        if path.replace("\\", "/").rsplit("/", 1)[-1] != "manifest.lkml":
            continue
        try:
            parsed = lkml.load(text)
        except Exception as exc:
            project.warnings.append((path, f"Could not parse the manifest: {exc}"))
            continue
        for const in parsed.get("constants", []) or []:
            if const.get("name") and isinstance(const.get("value"), str):
                project.constants[const["name"]] = const["value"]


def load_project(files: Dict[str, str]) -> Project:
    project = Project()
    _read_constants(files, project)
    used_constants = set()

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name in project.constants:
            used_constants.add(name)
            return project.constants[name]
        return match.group(0)
    raw_views: Dict[str, dict] = {}
    refinements: List[Tuple[str, dict]] = []
    explores: List[Tuple[str, dict, str]] = []
    other_explores: List[Tuple[dict, str]] = []

    for path in sorted(files):
        if not (path.endswith(".lkml") or path.endswith(".lookml")):
            continue
        if ".dashboard." in path.rsplit("/", 1)[-1]:
            continue  # LookML dashboards are YAML documents, not part of the semantic model
        project.files += 1
        text = CONSTANT.sub(substitute, files[path]) if project.constants else files[path]
        try:
            parsed = lkml.load(text)
        except Exception as exc:  # lkml raises SyntaxError and others
            project.warnings.append((path, f"Could not parse this file: {exc}"))
            continue
        for view in parsed.get("views", []) or []:
            name = view.get("name", "")
            if name.startswith("+"):
                refinements.append((name[1:], view))
            else:
                if name in raw_views:
                    project.warnings.append((path, f"View '{name}' is defined twice; the later file wins."))
                raw_views[name] = view
        model = _model_name(path)
        for explore in parsed.get("explores", []) or []:
            if model:
                explores.append((model, explore, path))
            else:
                other_explores.append((explore, path))

    # Resolve extends (with a guard against cycles), then apply refinements in file order.
    resolved: Dict[str, dict] = {}

    def resolve(name: str, stack: Tuple[str, ...] = ()) -> Optional[dict]:
        if name in resolved:
            return resolved[name]
        if name not in raw_views:
            return None
        if name in stack:
            project.warnings.append(("", f"View '{name}' extends itself in a cycle."))
            return raw_views[name]
        view = raw_views[name]
        result: dict = {}
        for parent in _extends_of(view):
            parent_view = resolve(parent, stack + (name,))
            if parent_view is None:
                project.warnings.append(("", f"View '{name}' extends unknown view '{parent}'."))
                continue
            result = _merge_view(result, parent_view) if result else copy.deepcopy(parent_view)
        result = _merge_view(result, view) if result else copy.deepcopy(view)
        result["name"] = name
        # "extension: required" belongs to the view that declares it, not to views that extend it.
        if view.get("extension"):
            result["extension"] = view["extension"]
        else:
            result.pop("extension", None)
        resolved[name] = result
        return result

    for name in raw_views:
        resolve(name)
    for name, refinement in refinements:
        if name in resolved:
            resolved[name] = _merge_view(resolved[name], refinement)
            resolved[name]["name"] = name
        else:
            project.warnings.append(("", f"Refinement '+{name}' has no base view."))

    project.views = {n: v for n, v in resolved.items() if v.get("extension") != "required"}
    if used_constants:
        project.warnings.append(("manifest.lkml", "Replaced the LookML constants " + ", ".join(sorted(used_constants))
                                 + " with their values from the manifest. Projects installed from the "
                                 "Marketplace often override them, so check them."))

    for model, explore, path in explores:
        if explore.get("name", "").startswith("+"):
            project.warnings.append((path, f"Explore refinement '{explore['name']}' is not applied yet."))
            continue
        project.explores.append(Explore(explore["name"], model, explore, path))
    for explore, path in other_explores:
        if explore.get("name", "").startswith("+"):
            continue
        project.explores.append(Explore(explore["name"], "", explore, path))
    return project
