"""从构建解释器提取 Tcl/Tk 9 嵌入式资源，供冻结程序启动。"""

import sys
import tkinter as tk
from pathlib import Path


def main():
    target = Path(sys.argv[1])
    root = tk.Tk()
    root.withdraw()
    try:
        for variable, name in (("tcl_library", "tcl"), ("tk_library", "tk")):
            source = root.tk.call("set", variable)
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            root.tk.call("file", "copy", "-force", source, str(destination))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
