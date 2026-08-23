import sys
from pathlib import Path

# python/ holds flat scripts rather than an installed package, so it is not on
# sys.path when pytest runs from the repo root. Adding it here keeps the scripts
# importable without turning the pipeline into a package or adding a config file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
