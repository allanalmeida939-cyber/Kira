from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    root_dir = Path(__file__).resolve().parent.parent
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    root_launcher = root_dir / "kira_app.py"
    runpy.run_path(str(root_launcher), run_name="__main__")
