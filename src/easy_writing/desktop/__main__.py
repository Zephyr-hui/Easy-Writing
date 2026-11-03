"""桌面入口，供 Python 与 Windows 发布程序共用。"""

import argparse
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from easy_writing.desktop.backend import default_data_dir, expected_error
from easy_writing.desktop.window import DesktopWindow


def main():
    parser = argparse.ArgumentParser(prog="EasyWriting")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--smoke-test", type=Path, metavar="RESULT_JSON")
    parser.add_argument("--smoke-samples", type=Path)
    parser.add_argument("--smoke-env", type=Path)
    parser.add_argument("--smoke-python", type=Path)
    parser.add_argument("--smoke-online", action="store_true")
    args = parser.parse_args()
    root = None
    try:
        root = tk.Tk()
        if args.smoke_test:
            root.withdraw()
        window = DesktopWindow(root, args.data_dir)
        if args.smoke_test:
            from easy_writing.desktop.smoke import run
            return run(window, args)
        root.mainloop()
        return 0
    except Exception as error:
        if args.smoke_test:
            detail = str(error) if isinstance(error, tk.TclError) else expected_error(error)
            args.smoke_test.write_text(json.dumps({"ok": False, "error_type": type(error).__name__,
                                                   "error": detail}, ensure_ascii=False), encoding="utf-8")
        elif root is not None:
            messagebox.showerror("Easy Writing", expected_error(error), parent=root)
        elif sys.stderr is not None:
            print(expected_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
