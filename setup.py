from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class OptionalBuildExt(build_ext):
    """The C decoder is a speed-up. If no compiler is available the package still
    installs and falls back to the pure-Python decoder."""

    def run(self):
        try:
            super().run()
        except Exception as e:  # noqa: BLE001
            print(f"warning: C extension not built ({e}); using the pure-Python decoder")

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as e:  # noqa: BLE001
            print(f"warning: {ext.name} not built ({e}); using the pure-Python decoder")


setup(
    ext_modules=[Extension("photolifeboat._scan", ["src/photolifeboat/_scan.c"])],
    cmdclass={"build_ext": OptionalBuildExt},
)
