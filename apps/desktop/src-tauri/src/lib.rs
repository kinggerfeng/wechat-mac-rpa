use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;
use tauri::Manager;

/// The single declaration of the local API port.
///
/// It used to be a `const` here and a literal in the frontend, and the two
/// disagreed: this file spawned uvicorn on 8767 while `api/client.ts`
/// defaulted to 8768. A dev shell then talked to whatever was already
/// listening — typically the installed RPAStudio.app — so the frontend the
/// developer believed they were driving was a different engine, on the same
/// `rpa.db`, writing to the same `data/logs/desktop-api.log`. The lock kept
/// two schedulers from running, but the numbers on screen came from the other
/// process.
///
/// Plain text, not JSON, so no parser is needed on either side. The frontend
/// reads it with Vite's `?raw` at build time; `tests/test_api_port.py` fails
/// if either side drifts.
// Relative to this file, not to src-tauri/: lib.rs sits in src-tauri/src/, so
// this is apps/desktop/api-port.txt. One `../` short of it resolves to a file
// that does not exist, and the crate then fails to compile rather than to
// warn — so `tests/test_api_port.py` resolves this path for real.
const API_PORT_DECLARATION: &str = include_str!("../../api-port.txt");

static API_PORT: OnceLock<u16> = OnceLock::new();

fn api_port() -> u16 {
    *API_PORT.get_or_init(|| {
        API_PORT_DECLARATION
            .trim()
            .parse()
            .expect("apps/desktop/api-port.txt must hold a port number")
    })
}

struct BackendProcess(Mutex<Option<Child>>);

impl BackendProcess {
    fn project_root() -> Result<PathBuf, String> {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(2)
            .map(PathBuf::from)
            .ok_or_else(|| "无法定位项目根目录".to_string())
    }

    fn python(root: &PathBuf) -> PathBuf {
        let configured = std::env::var_os("WECHAT_RPA_PYTHON").map(PathBuf::from);
        configured
            .filter(|path| path.is_file())
            .or_else(|| {
                let venv_python = root.join(".venv/bin/python");
                venv_python.is_file().then_some(venv_python)
            })
            .unwrap_or_else(|| PathBuf::from("python3"))
    }

    fn start(&self) -> Result<String, String> {
        let mut child = self.0.lock().map_err(|_| "后端进程锁已损坏")?;
        if let Some(process) = child.as_mut() {
            if process.try_wait().map_err(|err| err.to_string())?.is_none() {
                return Ok("already_running".to_string());
            }
            *child = None;
        }

        let address = SocketAddr::from(([127, 0, 0, 1], api_port()));
        if TcpStream::connect_timeout(&address, Duration::from_millis(150)).is_ok() {
            return Ok("already_running".to_string());
        }

        let root = Self::project_root()?;
        let python = Self::python(&root);
        let log_dir = root.join("data/logs");
        fs::create_dir_all(&log_dir).map_err(|err| err.to_string())?;
        let api_log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_dir.join("desktop-api.log"))
            .map_err(|err| err.to_string())?;
        let api_error_log = api_log.try_clone().map_err(|err| err.to_string())?;
        let process = Command::new(python)
            .args([
                "-m",
                "uvicorn",
                "rpa.backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                &api_port().to_string(),
            ])
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(Stdio::from(api_log))
            .stderr(Stdio::from(api_error_log))
            .spawn()
            .map_err(|err| format!("无法启动 Python API：{err}"))?;
        let pid = process.id();
        *child = Some(process);

        for _ in 0..40 {
            if TcpStream::connect_timeout(&address, Duration::from_millis(100)).is_ok() {
                return Ok(format!("started:{pid}"));
            }
            std::thread::sleep(Duration::from_millis(100));
        }

        Err("Python API 启动超时，请检查 Python 环境和日志".to_string())
    }

    fn stop(&self) {
        let Ok(mut child) = self.0.lock() else {
            return;
        };
        if let Some(mut process) = child.take() {
            let _ = request_stop_bot();
            let _ = Command::new("/bin/kill")
                .args(["-TERM", &process.id().to_string()])
                .status();
            let _ = process.wait();
        }
    }
}

fn request_stop_bot() -> Result<(), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], api_port()));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(1))
        .map_err(|err| err.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(17)))
        .map_err(|err| err.to_string())?;
    stream
        .write_all(b"POST /api/bot/stop HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        .map_err(|err| err.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|err| err.to_string())?;
    Ok(())
}

#[tauri::command]
fn start_backend(backend: tauri::State<'_, BackendProcess>) -> Result<String, String> {
    backend.start()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend = BackendProcess(Mutex::new(None));
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(backend)
        .invoke_handler(tauri::generate_handler![start_backend])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                window.state::<BackendProcess>().stop();
            }
        })
        .setup(|app| {
            app.state::<BackendProcess>()
                .start()
                .map_err(std::io::Error::other)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
