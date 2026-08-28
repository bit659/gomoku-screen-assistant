from __future__ import annotations

import argparse
import tkinter as tk

from gomoku_assistant.ui import GomokuApp
from gomoku_assistant.windows import enable_dpi_awareness


def main() -> None:
    parser = argparse.ArgumentParser(description="五子棋屏幕识别与落子助手")
    parser.add_argument("--smoke-test", action="store_true", help="创建窗口后立即退出，用于安装验证")
    args = parser.parse_args()

    enable_dpi_awareness()
    root = tk.Tk()
    app = GomokuApp(root)
    if args.smoke_test:
        root.update_idletasks()
        root.update()
        root.after(80, app.close)
    root.mainloop()


if __name__ == "__main__":
    main()
