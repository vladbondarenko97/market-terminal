# Kronos (vendored)

Model code from https://github.com/shiyu-coder/Kronos (MIT, see LICENSE), commit
67b630e67f6a18c9e9be918d9b4337c960db1e9a, directory `model/`. Changed: relative import (`from .module import *`),
and the tqdm progress bar (verbose mode) removed so tqdm is not a dependency. Weights are not in the repository:
`scripts/fetch_kronos.py` downloads them. Needs `pip install -r requirements-kronos.txt`.
