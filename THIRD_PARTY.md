# Windows 免安装包的第三方组件

免安装包包含官方 Python 3.14.10 嵌入式运行时，许可证随 `.runtime/LICENSE.txt` 提供。Python 对应源码可从该版本发布页下载。[Python 3.14.10](https://www.python.org/downloads/release/python-31410/)

Qt 6.12.0、PySide6 和 Shiboken6 来自 PyPI 的 Qt for Python 社区版 wheel。包内保留动态链接的 Qt 库、wheel 元数据和上游声明，第三方许可文本位于 `licenses/`。可使用兼容版本替换 `.runtime/pkgs` 中的 Qt 库。[Qt 源码](https://download.qt.io/archive/qt/6.12/6.12.0/) · [Qt for Python 源码](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0) · [Qt for Python 第三方声明](https://doc.qt.io/qtforpython-6/licenses.html)

其余 Python 依赖的版本和 SHA-256 在 `requirements-windows-x64.lock` 中，组件声明随各自的 `.dist-info` 目录提供。UniCampusHelper 的源码与许可见仓库根目录。[UniCampusHelper](https://github.com/zhuangzztong-bit/UniCampusHelper)
