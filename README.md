# devdash v0.1

Linuxのsystemd system/user serviceと既存Dockerコンテナを、階層Groupと順序付きStepsで操作するRust製ネイティブアプリです。
systemd／Docker／既存Supervisorが実行管理を担当し、devdashは親Workloadの操作順序と明示的なReadiness Gateを管理します。
GUIを閉じても、独立したCoreプロセスがGate待機とシーケンスを維持します。

## セットアップ

Ubuntu 24.04で検証。Ubuntu 22.04も対象としていますが未検証です。systemd操作にはmanagerとD-Busセッションが必要です。接続できたsystem/user managerを個別に利用します。Dockerだけの構成ではsystemdへの接続を必須にしません。
GUIにはX11またはWaylandと、GPUIのVulkan描画に対応した環境が必要です。

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential pkg-config cmake clang libclang-dev \
  libwayland-dev libxkbcommon-dev libxkbcommon-x11-dev \
  libvulkan-dev libegl1-mesa-dev libfontconfig1-dev libfreetype-dev \
  libxcb1-dev libxcb-xkb-dev libxcb-randr0-dev libxcb-shape0-dev \
  libxcb-xfixes0-dev libxcb-sync-dev libxcb-render0-dev libxcb-shm0-dev \
  libx11-dev libx11-xcb-dev libasound2-dev libssl-dev

# rustup導入済みなら、rust-toolchain.tomlの指定に従って取得されます。
rustup toolchain install 1.95.0 --profile minimal --component rustfmt,clippy
cargo build --locked --features gui --bins -j 4
```

Rust 1.95.0、GPUI Kit **=0.8.1**、Kitが指定するGPUI snapshot **=0.3.10**を使用します。
Core/CLIのみなら `cargo build --locked --bin devdash` でGUI開発ライブラリは不要です。
`Cargo.lock`を配布して依存関係を固定します。

## 起動

設定内のunitは、あらかじめsystemdに存在するものを参照します。devdashが通常操作でunitを生成することはありません。

```bash
cargo run --locked -- validate examples/robot.yaml

# ターミナル1: 独立したCore
cargo run --locked -- daemon --config examples/robot.yaml

# ターミナル2: GUIクライアント
cargo run --locked --features gui --bin devdash-gui
```

デフォルトSocketは `$XDG_RUNTIME_DIR/devdash/core.sock`、承認状態は
`$XDG_STATE_HOME/devdash/approvals.json`（未設定時は `~/.local/state/devdash/approvals.json`）です。
`--socket /path/to/core.sock` はCLIとGUI両方で指定できます。
Coreの状態ファイルは `daemon --state /path/to/approvals.json` で変更できます。
同じSocketのCore二重起動は拒否し、クラッシュ後の残存Socketは所有ロック取得後に処理します。

CoreはCtrl+CまたはSIGTERMで終了できます。サービスはそのまま維持します。
Core再起動後にシーケンスを自動再開することはありません。

## ダミーサービスで試す

以下は同梱の **devdash-demo-*.serviceのみ** を登録します。
実機のrobotサービスの代わりに、何も処理しないsleepプロセスを起動します。

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/devdash-demo-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

`examples/robot.yaml`でCoreを起動し、CLIまたはGUIから操作してください。

```bash
cargo run -- group start robot
# camera / imu起動後、sensors_readyで待機
cargo run -- status
cargo run -- approve sensors_ready
# detector、plannerが順に起動

cargo run -- group restart perception  # detectorだけ停止・起動
cargo run -- group restart robot       # 逆順停止→正順起動、Manual承認再利用
cargo run -- group reinitialize robot  # Manual承認を消去→Restart→Gate再待機
cargo run -- cancel                    # 待機解除、未実行Stepをスキップ
cargo run -- workload restart detector # BackendのRestart、Gateを迂回
cargo run -- logs detector
cargo run -- group stop robot
```

デモの後片付け（Core終了後に実行）：

```bash
systemctl --user stop devdash-demo-camera.service devdash-demo-imu.service \
  devdash-demo-detector.service devdash-demo-planner.service
rm ~/.config/systemd/user/devdash-demo-camera.service \
   ~/.config/systemd/user/devdash-demo-imu.service \
   ~/.config/systemd/user/devdash-demo-detector.service \
   ~/.config/systemd/user/devdash-demo-planner.service
systemctl --user daemon-reload
```

GUIは左のツリーでGroupまたはWorkloadを選択します。中央でStart/Stop/Restart、
GroupではReinitializeも使えます。Gate待機中はApprove Readyが表示され、Cancel sequenceで中断できます。
下部に選択サービスのjournal最新150行を表示します。Refreshで更新します。
シーケンスの結果とStepごとのCompleted/Failed/Skipped、操作エラーを中央で確認できます。

## 設定

[robot.yaml](examples/robot.yaml) と [http-gate.yaml](examples/http-gate.yaml) が完全なサンプルです。

```yaml
workloads:
  camera: {backend: systemd, scope: user, unit: camera.service}
  imu: {backend: systemd, unit: imu.service}
  detector: {backend: systemd, unit: detector.service}
groups:
  robot:
    name: Robot
    steps:
      - group: sensors
      - gate: sensors_ready
      - workload: detector
  sensors:
    steps:
      - parallel: [camera, imu]
gates:
  sensors_ready: {type: manual}
```

`name`は任意、scope省略時はuserです。system scopeも操作できます。managerが異なる同名unitは別Workloadです。
各Stepは`workload` / `group` / `parallel` / `gate`の**一つだけ**を指定します。
ParallelにはWorkload IDの配列を指定します。参照不正、循環、複数親、配置重複、空のParallel、
未対応Backend、未知の設定フィールド、同じunitの重複登録を検証します。
未配置Workloadは単独操作用として登録できます。

HTTP Gateの条件は明示したHTTP statusとの一致です。Redirectは追跡しません。
`timeout_ms`はリクエストとリトライを含むGate全体の上限、`interval_ms`は失敗後の待ち時間です。
Start/Restartごとに再評価し、Cancelで待機を解除します。

Manual Gate承認は、同じboot IDではCoreの再起動後も再利用します。
OS再起動後はリセットします。Reinitializeは選択Group配下の承認だけをリセットします。
状態ファイルを読み書きできないときや破損時はエラーを報告し、承認を成功扱いしません。

ExecStart、環境変数、WorkingDirectory、Restart等はunitファイルに記述してください。
devdashのYAMLには複製しません。

## Scan / Import

GUIのScan user / Scan system、またはCLIで手動実行します。停止中のunitファイルとロード済みunitを候補に含めます。
Scanだけでは設定を変更しません。

```bash
cargo run -- scan
cargo run -- import my-existing.service
cargo run -- scan --scope system
cargo run -- import robot-sensors.service --scope system
cargo run -- check robot --action restart
```

Importは選択したmanagerの既存serviceへのWorkload参照をYAMLに保存します。同名unitのIDが衝突する場合は`system:` / `user:`をIDの先頭に付けます。
既存設定のバックアップを拡張子`.yaml.bak`に保存してから置換します。
**Import後はCoreを再起動して設定を読み直してください。** 再起動は実行中シーケンスの終了後に行ってください。
取り込んだWorkloadは、YAMLのGroup Stepsへ必要に応じて配置できます。
YAMLはSerdeによる再出力のため、Import時にコメントや元の書式は保持しません。
テンプレートunitはYAMLで具体的なインスタンス名を指定してください。

登録済みWorkloadだけを1秒間隔で再解決・監視します。未知のunitの常時Discoveryは行いません。
削除・再作成されたunitも同じ名前から再解決し、見つからない場合はNot Foundとして設定に保持します。
ActiveState / SubState、利用可能なMainPID / MemoryCurrent / CPUUsageNSecを表示します。
CPUはsystemdが提供する累積CPU時間で、使用率ではありません。Accounting無効時などは指標が空欄になります。

## 操作の意味

- Startは正順、Stopは逆順。停止ではGateをスキップします。
- Restartは選択Group内の逆順Stop完了後、正順Startを実行します。
- Parallelは全操作の結果を待ってから次へ進みます。一つ失敗しても他の発行済み操作を追跡します。
- systemd JobRemovedを要求発行前に購読し、返されたジョブパスの結果が`done`の場合だけ成功とします。
- Cancelは新しいStepを開始しません。発行済みジョブは取り消さず追跡し、その間はCancellingです。
- 失敗すると後続をスキップします。ロールバック・Reconciliation・独自の自動再起動は行いません。
- 全体で一度に一操作です。個別操作も含め、実行中の競合操作を拒否します。
- systemd自身の既存依存設定によってGroup外のunitが影響を受ける可能性はあります。

## 構成

- `src/model.rs`: 型、YAML、検証、階層展開、Import
- `src/engine.rs`: GUI非依存Core、Steps、Gate、実行結果、キャンセル、承認永続化
- `src/systemd.rs`: D-Busジョブ追跡、登録unitの再解決、オンデマンドScan、journal
- `src/systemd_events.rs`: 独立接続でのmanager監視、要求とジョブの相関、監視喪失の検出
- `src/runtime.rs` / `src/docker.rs`: systemd／Dockerの振り分け、Engine APIとcontrollerへの委譲
- `src/children.rs`: OSプロセス・論理サービス・Componentと操作能力
- `bridges/roslaunch`: 独立したROS 2 Jazzy Bridge。通常プロセスの停止、ComponentManagerの個別操作
- `src/inspection.rs`: 型付き依存関係と読み取り専用のGroup整合性診断
- `src/monitor.rs`: 状態・systemdイベントの観測、200件までのイベントログと警告
- `src/ipc.rs`: 所有者限定Unix Socket、JSON一要求一応答、状態キャッシュ
- `src/main.rs`: ヘッドレスCoreとCLI
- `src/bin/gui.rs`: GPUI KitのTree/Buttonを使う独立GUI。TokioワーカーがIPCを処理

汎用RPC、外部HTTP API、独自Supervisor、プラグイン機構は使用していません。
HTTPクライアントのTLSはrustls、GUIは機能フラグでCoreビルドから分離しています。

## テスト

```bash
cargo fmt --all -- --check
cargo test --locked --no-default-features
cargo check --locked --features gui --all-targets
cargo clippy --locked --features gui --all-targets -- -D warnings

# 明示的に実行: 専用runtime user unitを生成してテスト、終了時に停止・登録解除
cargo test --locked --test systemd -- --ignored --nocapture

# GPUIの実クリックから操作・承認・部分Restart・Cancelまで。専用unitを後片付けする。
cargo build --locked --features gui --bins -j 4
python3 tools/gui_e2e.py --run
```

CoreテストはFake BackendとローカルHTTPサーバーを使用し、実systemdとGUIに依存しません。
IPCテストはローカルUnix Socketを使用します。
実systemdテストは一意な`devdash-it-<pid>-<time>-*`だけを操作します。
Gate、承認、Perceptionだけの再起動、Robot全体再起動、途中キャンセル、Scan、
起動ジョブの失敗、Not Foundを検証します。既存サービスを停止・変更しません。

## v0.1の制約

- Ubuntu 22.04、Wayland、別GPUでの互換性は未検証です。
- 状態は1秒間隔で取得し、JobRemoved / UnitNew / UnitRemoved / Reloadingも購読します。manager喪失時はscopeごとに再接続・再Subscribeします。
- manager喪失時は保留操作・Gate待機を結果不明として終了し、監視復旧までそのscopeの追加操作を拒否します。復旧後も中断した操作を再発行・再開しません。
- Importと手動YAML編集の反映にはCore再起動が必要です。
- journalは最新150行の取得と手動更新です。ログ追従・検索・巨大ログの仮想化は未実装です。
- Core再起動後に操作履歴は保持しません。Manual承認だけを永続化します。
- system操作の認可はPolkitへ委譲します。認証エージェント／ポリシーが必要です。独自の権限昇格サービスはありません。
- Dockerは既存コンテナの操作と監視、supervisordは内部サービスの個別操作に対応します。通常のROS launchプロセスはBridge経由の監視・停止のみで、任意ActionのStart／Restartは非対応です。
- Docker Scan／Import、s6、SSH、GPU監視、Web UI、unit生成Wizardは未実装です。
- 意図的にロールバック、途中再開、systemd依存関係生成、独自Restartループを持ちません。

## 技術調査とライセンス

GPUI Kitの[公式リポジトリ](https://github.com/longbridge/gpui-kit)、
[Getting Started](https://gpui-kit.com/docs/getting-started/)、
[Tree](https://gpui-kit.com/component/tree/)、
[実際の依存設定](https://github.com/longbridge/gpui-kit/blob/main/crates/kit/Cargo.toml)、
[GPUI](https://github.com/zed-industries/zed/tree/main/crates/gpui)を参照しました。
Kitはcomponentとassetsのみ有効にし、WebView、Shell、Speech、Tree-sitter、Syntectは有効にしていません。
Kit自体の必須依存にはテキスト・画像処理等が含まれるため、GUIビルドはCoreより大きくなります。
Rust 1.95.0はGPUI snapshotが使用する`std::hint::cold_path`に合わせた選択です。

systemdは[公式D-Bus仕様](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)を参照しています。

本プロジェクトはMIT OR Apache-2.0。GPUI/KitはApache-2.0、主要なRust依存はMIT/Apache系です。
フォント等を同梱する際には各配布物のライセンスも確認してください。

実施結果と画面は[検証記録](docs/validation.md)にあります。

## systemd統合と運用

[追加設計・検証記録](docs/systemd-operations.md)に、依存診断の拒否／警告基準、
解析範囲、認可、外部操作の観測、ROS 2 launchの扱いを記載しています。
GUIやCoreをrootで起動する必要はありません。
依存診断とイベント追跡の[セルフレビューと修正結果](docs/self-review-systemd.md)も保存しています。

[manager再接続とGUI操作テスト](docs/reconnection-and-gui-tests.md)に、接続世代の扱い、再試行、GUIテストの要件と画面を記載しています。

## コンテナ内部とROS 2 launch

[設計判断・対応範囲・検証結果](docs/container-processes-and-roslaunch.md)にまとめています。
[Docker設定例](examples/docker-supervisor.yaml)と[ROS Bridge導入方法](bridges/roslaunch/README.md)を用意しました。
内部サービスはGroupのStepsに追加せず、一覧と個別操作だけを提供します。SupervisorなしのOSプロセスは監視のみです。

ROS 2の通常Actionは一度しか実行できないため、個別Restartを有効と偽りません。
既存のComposable NodeはUnloadのみ、明示したManagedComponentsは元のLoadNode要求を保持してLoad／Unload／Reloadできます。
Coreは現在Snapshotをポーリングし、Bridgeのwatch通知を常時購読する対応は後続です。
