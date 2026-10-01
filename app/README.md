# WeChat Mac RPA Desktop

The desktop shell currently runs from the repository checkout and starts the local FastAPI service as a managed Python process.

## Development

From the repository root, install the Python runtime dependencies into `.venv`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then start the desktop app:

```sh
cd app
npm install
npm run tauri dev
```

The desktop control API listens on `127.0.0.1:8767`; the existing admin site remains on port `8766`. The Rust bridge uses `.venv/bin/python` by default. Set `WECHAT_RPA_PYTHON` to use another Python interpreter.

This source-checkout bridge is for development. Bundling the Python runtime and project assets into a distributable app remains part of the release phase.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Vue - Official](https://marketplace.visualstudio.com/items?itemName=Vue.volar) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)
