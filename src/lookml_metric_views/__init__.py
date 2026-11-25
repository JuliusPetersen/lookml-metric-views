"""Turn a Looker (LookML) project into Databricks Unity Catalog metric views, one per explore,
with a field-by-field report of what translated exactly and what needs a person."""

from .emit import write_outputs, summary
from .loader import load_project
from .translate import translate_project

__all__ = ["convert", "load_project", "translate_project", "write_outputs", "summary"]
__version__ = "0.1.0"


def convert(files, dialect="databricks", catalog="main", schema="looker", table_map=None, source="",
            timezone_conversion=False):
    """Convert {path: lookml text} into {output path: text}. Used by the CLI and the web page."""
    project = load_project(files)
    results = translate_project(project, dialect=dialect, table_map=table_map,
                                timezone_conversion=timezone_conversion)
    out = {}
    write_outputs(results, project.warnings, out, catalog=catalog, schema=schema, source=source)
    return out, results, project
