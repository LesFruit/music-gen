use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use anyhow::{Context, Result, anyhow, bail};
use clap::{Parser, Subcommand, ValueEnum};
use serde::Deserialize;

#[derive(Parser, Debug)]
#[command(name = "control-panel")]
#[command(about = "Control remote GPU/Podman nodes over Tailscale + SSH")]
struct Cli {
    /// Path to config file (defaults to ~/.config/music-gen/control-panel.toml)
    #[arg(long)]
    config: Option<PathBuf>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// List configured nodes
    Nodes,

    /// Run podman ps on a node
    PodmanPs {
        #[arg(long)]
        node: String,
    },

    /// Check GPU visibility on a node (WSL-aware)
    GpuCheck {
        #[arg(long)]
        node: String,
    },

    /// Sync local files/dir to remote node via rsync
    Sync {
        #[arg(long)]
        node: String,
        #[arg(long)]
        local: PathBuf,
        #[arg(long)]
        remote: String,
        #[arg(long, default_value_t = false)]
        delete: bool,
    },

    /// Deploy MusicGen bundle and start remote service
    MusicgenDeploy {
        #[arg(long)]
        node: String,
        #[arg(long, default_value = "../../deployment/musicgen")]
        local: PathBuf,
        #[arg(long, default_value = "/srv/music-gen")]
        remote: String,
        #[arg(long, default_value = "facebook/musicgen-small")]
        model_id: String,
        #[arg(long)]
        device: Option<String>,
        #[arg(long, default_value_t = 256)]
        max_new_tokens: usize,
        #[arg(long, default_value = "/host/d/Music/music-gen")]
        out_dir: String,
        #[arg(long, default_value = "/host/d/Music/suno")]
        suno_dir: String,
        #[arg(long, default_value = "/host/d/Music/music-gen")]
        musicgen_dir: String,
    },

    /// Run a MusicGen generation test request
    MusicgenTest {
        #[arg(long)]
        node: String,
        #[arg(long, default_value = "uplifting electronic melody with warm pads")]
        prompt: String,
        #[arg(long, default_value_t = 128)]
        max_new_tokens: usize,
        #[arg(long, default_value_t = 3.0)]
        guidance_scale: f32,
    },

    /// Execute arbitrary command on a node (everything after -- is the remote command)
    Exec {
        #[arg(long)]
        node: String,
        #[arg(trailing_var_arg = true, required = true)]
        cmd: Vec<String>,
    },

    /// Control a configured service
    Service {
        #[arg(long)]
        name: String,
        #[arg(long)]
        action: ServiceAction,
        #[arg(long, default_value_t = 200)]
        tail: usize,
    },

    /// Check local prerequisites and config discovery
    Doctor,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum ServiceAction {
    Status,
    Start,
    Stop,
    Restart,
    Logs,
}

#[derive(Debug, Deserialize)]
struct Config {
    #[serde(default)]
    node: BTreeMap<String, Node>,
    #[serde(default)]
    service: BTreeMap<String, Service>,
}

#[derive(Debug, Deserialize)]
struct Node {
    ssh_user: String,
    host: String,
    workdir: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Service {
    node: String,
    container: String,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("error: {err:#}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();
    let config_path = cli.config.unwrap_or_else(discover_config_path);

    if let Commands::Doctor = cli.command {
        return run_doctor(&config_path);
    }

    let cfg = load_config(&config_path)?;

    match cli.command {
        Commands::Nodes => {
            if cfg.node.is_empty() {
                println!("No nodes configured in {}", config_path.display());
                return Ok(());
            }
            for (name, node) in &cfg.node {
                let workdir = node
                    .workdir
                    .as_ref()
                    .map_or("(none)", std::string::String::as_str);
                println!(
                    "{name}: {}@{} workdir={workdir}",
                    node.ssh_user, node.host
                );
            }
        }
        Commands::PodmanPs { node } => {
            let remote = node_from_cfg(&cfg, &node)?;
            let cmd = remote_wrap(remote, "podman ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'")?;
            run_ssh(remote, &cmd)?;
        }
        Commands::GpuCheck { node } => {
            let remote = node_from_cfg(&cfg, &node)?;
            let cmd = "LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers /usr/lib/wsl/lib/nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv,noheader || nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv,noheader";
            let wrapped = remote_wrap(remote, cmd)?;
            run_ssh(remote, &wrapped)?;
        }
        Commands::Sync {
            node,
            local,
            remote,
            delete,
        } => {
            let remote_node = node_from_cfg(&cfg, &node)?;
            run_rsync(remote_node, &local, &remote, delete)?;
        }
        Commands::Exec { node, cmd } => {
            let remote = node_from_cfg(&cfg, &node)?;
            let remote_cmd = shell_join(&cmd);
            let cmd = remote_wrap(remote, &remote_cmd)?;
            run_ssh(remote, &cmd)?;
        }
        Commands::MusicgenDeploy {
            node,
            local,
            remote,
            model_id,
            device,
            max_new_tokens,
            out_dir,
            suno_dir,
            musicgen_dir,
        } => {
            let remote_node = node_from_cfg(&cfg, &node)?;
            run_rsync(remote_node, &local, &remote, true)?;
            let mut env_prefix = format!(
                "MODEL_ID={} MAX_NEW_TOKENS={} OUT_DIR={} SUNO_DIR={} MUSICGEN_DIR={}",
                shell_escape(&model_id),
                max_new_tokens,
                shell_escape(&out_dir),
                shell_escape(&suno_dir),
                shell_escape(&musicgen_dir),
            );
            if let Some(dev) = device {
                env_prefix.push_str(&format!(" DEVICE={}", shell_escape(&dev)));
            }
            let start_cmd = format!(
                "mkdir -p {remote} && chmod +x {remote}/run_remote.sh && {env_prefix} bash {remote}/run_remote.sh && curl -s http://127.0.0.1:8010/health",
                remote = shell_escape(&remote),
            );
            let wrapped = remote_wrap(remote_node, &start_cmd)?;
            run_ssh(remote_node, &wrapped)?;
        }
        Commands::MusicgenTest {
            node,
            prompt,
            max_new_tokens,
            guidance_scale,
        } => {
            let remote = node_from_cfg(&cfg, &node)?;
            let payload = format!(
                "{{\"prompt\":\"{}\",\"max_new_tokens\":{},\"guidance_scale\":{}}}",
                json_escape(&prompt),
                max_new_tokens,
                guidance_scale
            );
            let cmd = format!(
                "curl -s -X POST http://127.0.0.1:8010/generate -H 'content-type: application/json' -d {}",
                shell_escape(&payload)
            );
            let wrapped = remote_wrap(remote, &cmd)?;
            run_ssh(remote, &wrapped)?;
        }
        Commands::Service { name, action, tail } => {
            let svc = cfg
                .service
                .get(&name)
                .ok_or_else(|| anyhow!("unknown service '{name}'"))?;
            let remote = node_from_cfg(&cfg, &svc.node)?;

            let cmd = match action {
                ServiceAction::Status => {
                    format!("podman ps -a --filter name={} --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'", shell_escape(&svc.container))
                }
                ServiceAction::Start => format!("podman start {}", shell_escape(&svc.container)),
                ServiceAction::Stop => format!("podman stop {}", shell_escape(&svc.container)),
                ServiceAction::Restart => {
                    format!("podman restart {}", shell_escape(&svc.container))
                }
                ServiceAction::Logs => {
                    format!("podman logs --tail {} {}", tail, shell_escape(&svc.container))
                }
            };

            let wrapped = remote_wrap(remote, &cmd)?;
            run_ssh(remote, &wrapped)?;
        }
    }

    Ok(())
}

fn load_config(path: &Path) -> Result<Config> {
    let text = fs::read_to_string(path)
        .with_context(|| format!("failed to read config: {}", path.display()))?;
    let cfg: Config = toml::from_str(&text)
        .with_context(|| format!("invalid TOML in config: {}", path.display()))?;

    if cfg.node.is_empty() {
        bail!("config has no [node.<name>] entries: {}", path.display());
    }

    Ok(cfg)
}

fn default_config_path() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".config/music-gen/control-panel.toml")
}

fn discover_config_path() -> PathBuf {
    if let Some(from_env) = std::env::var_os("MUSIC_GEN_CONTROL_PANEL_CONFIG") {
        return PathBuf::from(from_env);
    }

    let home_default = default_config_path();
    if home_default.exists() {
        return home_default;
    }

    let repo_local = PathBuf::from("control-panel.toml");
    if repo_local.exists() {
        return repo_local;
    }

    home_default
}

fn run_doctor(config_path: &Path) -> Result<()> {
    println!("Config path: {}", config_path.display());
    println!("Config exists: {}", config_path.exists());

    for cmd in ["ssh", "tailscale", "podman"] {
        let status = Command::new("sh")
            .arg("-lc")
            .arg(format!("command -v {cmd} >/dev/null"))
            .status()
            .with_context(|| format!("failed to check command '{cmd}'"))?;
        println!("{cmd}: {}", if status.success() { "ok" } else { "missing" });
    }

    Ok(())
}

fn node_from_cfg<'a>(cfg: &'a Config, name: &str) -> Result<&'a Node> {
    cfg.node
        .get(name)
        .ok_or_else(|| anyhow!("unknown node '{name}'"))
}

fn remote_wrap(node: &Node, cmd: &str) -> Result<String> {
    if let Some(dir) = &node.workdir {
        if dir.trim().is_empty() {
            bail!("workdir cannot be empty if provided");
        }
        Ok(format!("cd {} && {}", shell_escape(dir), cmd))
    } else {
        Ok(cmd.to_string())
    }
}

fn run_ssh(node: &Node, remote_cmd: &str) -> Result<()> {
    let target = format!("{}@{}", node.ssh_user, node.host);
    let status = Command::new("ssh")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg(&target)
        .arg(remote_cmd)
        .status()
        .with_context(|| format!("failed to start ssh to {target}"))?;

    if !status.success() {
        bail!("ssh command failed on {target} with status {status}");
    }
    Ok(())
}

fn run_rsync(node: &Node, local: &Path, remote_path: &str, delete: bool) -> Result<()> {
    if !local.exists() {
        bail!("local path does not exist: {}", local.display());
    }
    let target = format!("{}@{}:{}", node.ssh_user, node.host, remote_path);
    let mut cmd = Command::new("rsync");
    cmd.arg("-az");
    if delete {
        cmd.arg("--delete");
    }
    cmd.arg(format!("{}/", local.display())).arg(&target);
    let status = cmd
        .status()
        .with_context(|| format!("failed to start rsync to {target}"))?;
    if !status.success() {
        bail!("rsync failed to {target} with status {status}");
    }
    Ok(())
}

fn shell_join(parts: &[String]) -> String {
    parts
        .iter()
        .map(|s| shell_escape(s))
        .collect::<Vec<_>>()
        .join(" ")
}

fn shell_escape(raw: &str) -> String {
    if raw.is_empty() {
        return "''".to_string();
    }

    if raw
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "-._/:=@".contains(c))
    {
        return raw.to_string();
    }

    let mut out = String::with_capacity(raw.len() + 2);
    out.push('\'');
    for ch in raw.chars() {
        if ch == '\'' {
            out.push_str("'\\''");
        } else {
            out.push(ch);
        }
    }
    out.push('\'');
    out
}

fn json_escape(raw: &str) -> String {
    raw.replace('\\', "\\\\").replace('\"', "\\\"")
}
