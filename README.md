# bounded_subprocess

[![PyPI - Version](https://img.shields.io/pypi/v/bounded-subprocess.svg)](https://pypi.org/project/bounded-subprocess)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/bounded-subprocess.svg)](https://pypi.org/project/bounded-subprocess)

The `bounded-subprocess` module runs a subprocess with several bounds:

1. The subprocess runs in a Linux session, so the process and all its children
   can be killed;
2. The subprocess runs with a given timeout; and
3. The parent captures a bounded amount of output from the subprocess and
   discards the rest.

Note that the subprocess is not isolated: it can use the network, the filesystem,
or create new sessions.

## Installation

```console
python3 -m pip install bounded-subprocess
```

## License

`bounded-subprocess` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
