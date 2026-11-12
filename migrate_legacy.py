"""Import old JSON history without editing or deleting either source file."""
from pathlib import Path
import storage

if __name__ == "__main__":
    root = Path(__file__).parent
    storage.initialize()
    storage.import_legacy(root / "run_history.json", root / "manual_verification.json")
    print(f"Imported legacy records; database now has {len(storage.list_runs(100000))} runs")
