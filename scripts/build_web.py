"""Copy the Python package into web/py and precompute the TPC-H example for the page.

    python scripts/build_web.py
"""

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from lookml_metric_views import convert  # noqa: E402
from lookml_metric_views.cli import read_folder  # noqa: E402

src = os.path.join(ROOT, "src", "lookml_metric_views")
dst = os.path.join(ROOT, "web", "py", "lookml_metric_views")
shutil.rmtree(dst, ignore_errors=True)
os.makedirs(dst)
names = sorted(n for n in os.listdir(src) if n.endswith(".py") and n not in ("cli.py", "__main__.py"))
for n in names:
    shutil.copy(os.path.join(src, n), os.path.join(dst, n))
with open(os.path.join(ROOT, "web", "py", "manifest.json"), "w") as fh:
    json.dump({"files": names}, fh)

# The page shows a precomputed run on the repository's own TPC-H example (MIT). Third-party
# projects are only converted live in the visitor's browser and never stored here.
files = read_folder(os.path.join(ROOT, "examples", "tpch"))
out, _, _ = convert(files, dialect="databricks", catalog="workspace", schema="lookml_test",
                    source="examples/tpch")
os.makedirs(os.path.join(ROOT, "web", "data"), exist_ok=True)
with open(os.path.join(ROOT, "web", "data", "example_tpch.json"), "w") as fh:
    json.dump(out, fh)
print("copied", names, "and wrote example_tpch.json")
