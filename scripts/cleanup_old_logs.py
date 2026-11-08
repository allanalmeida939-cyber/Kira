from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from services.kira_log_manager import cleanup_legacy_logs, ensure_log_directories, rotate_logs, write_readme


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        args.dry_run = True

    ensure_log_directories()
    write_readme()
    summary = cleanup_legacy_logs(dry_run=not args.apply)
    removed_rotation = rotate_logs()

    mode = "DRY_RUN" if not args.apply else "APPLY"
    print(f"[{mode}] arquivos_para_apagar={summary['deleted_count']}")
    print(f"[{mode}] arquivos_preservados={summary['preserved_count']}")
    print(f"[{mode}] removidos_por_rotacao={removed_rotation}")
    print("pastas_preservadas:")
    for p in summary["allowed_roots"]:
        print(f"- {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
