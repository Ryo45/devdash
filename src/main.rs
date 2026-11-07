use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};
use devdash::{
    engine::{Action, Engine},
    ipc::{self, Request},
    model::{Config, Scope},
    runtime::Backends,
};
use std::{path::PathBuf, sync::Arc};
#[derive(Parser)]
#[command(version, about = "Native workload sequencer (systemd / Docker)")]
struct Args {
    #[arg(long, global = true)]
    socket: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Clone, Copy, ValueEnum)]
enum Op {
    Start,
    Stop,
    Restart,
    Reinitialize,
}
impl From<Op> for Action {
    fn from(v: Op) -> Self {
        match v {
            Op::Start => Self::Start,
            Op::Stop => Self::Stop,
            Op::Restart => Self::Restart,
            Op::Reinitialize => Self::Reinitialize,
        }
    }
}
#[derive(Subcommand)]
enum Command {
    /// Run headless core; closing clients does not stop gates or services.
    Daemon {
        #[arg(long, default_value = "devdash.yaml")]
        config: PathBuf,
        #[arg(long)]
        state: Option<PathBuf>,
    },
    Validate {
        config: PathBuf,
    },
    Status,
    Group {
        #[arg(value_enum)]
        action: Op,
        target: String,
    },
    Workload {
        #[arg(value_enum)]
        action: Op,
        target: String,
    },
    Child {
        #[arg(value_enum)]
        action: Op,
        parent: String,
        child: String,
        #[arg(long)]
        epoch: String,
    },
    Approve {
        gate: String,
    },
    Cancel,
    Scan {
        #[arg(long, default_value = "user")]
        scope: Scope,
    },
    Import {
        unit: String,
        #[arg(long, default_value = "user")]
        scope: Scope,
    },
    Check {
        group: String,
        #[arg(long, value_enum, default_value = "restart")]
        action: Op,
    },
    Logs {
        workload: String,
    },
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let socket = args.socket.unwrap_or_else(ipc::socket_path);
    let request = match args.command {
        Command::Validate { config } => {
            Config::load(&config)?;
            println!("Configuration valid");
            return Ok(());
        }
        Command::Daemon { config, state } => {
            let state = state.unwrap_or_else(|| {
                std::env::var_os("XDG_STATE_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                            .join(".local/state")
                    })
                    .join("devdash/approvals.json")
            });
            let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
                .trim()
                .to_string();
            let engine = Engine::new(
                Config::load(&config)?,
                Arc::new(Backends::connect().await?),
                Some(state),
                boot,
            )?;
            println!("Core listening at {}", socket.display());
            return ipc::serve(engine, &socket, config).await;
        }
        Command::Status => Request::Snapshot,
        Command::Group { action, target } => Request::Operate {
            target,
            group: true,
            action: action.into(),
        },
        Command::Workload { action, target } => Request::Operate {
            target,
            group: false,
            action: action.into(),
        },
        Command::Child {
            action,
            parent,
            child,
            epoch,
        } => Request::OperateChild {
            parent,
            child,
            epoch,
            action: action.into(),
        },
        Command::Approve { gate } => Request::Approve { gate },
        Command::Cancel => Request::Cancel,
        Command::Scan { scope } => Request::Scan { scope },
        Command::Import { unit, scope } => Request::Import { unit, scope },
        Command::Check { group, action } => Request::Inspect {
            group,
            action: action.into(),
        },
        Command::Logs { workload } => Request::Logs { workload },
    };
    println!(
        "{}",
        serde_json::to_string_pretty(&ipc::request(&socket, request).await?.data)?
    );
    Ok(())
}
