"""Display a native path dialog only after the user presses a protected settings browse button."""

import json
import sys


def main():
    """Return the explicitly selected path on stdout; cancellation returns an empty string."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        path = filedialog.askdirectory(parent=root, title='选择游戏数据目录') if sys.argv[1] == 'directory' else filedialog.askopenfilename(parent=root, title='选择游戏数据文件')
        sys.stdout.buffer.write(json.dumps(path, ensure_ascii=False).encode('utf-8'))
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
