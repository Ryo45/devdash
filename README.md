# devdash — Stage方式の比較実装

`feature/staged-startup`ブランチの **Python + ttkbootstrap / Tkinter**版です。比較元のCoordinator方式は`main`の`d143e97`に残っています。このブランチをmainへマージする判断はまだしていません。

Stageは開発者が明示操作する順序付きリストです。選択StageのWorkloadだけにStart/Stop/Restartを要求し、結果を追跡します。後続Stageは自動起動しません。Nextは「選択Stageの次へのStart要求」で、承認ラッチや永続的な現在位置ではありません。

実行管理・必須依存・起動Readyはsystemd / Docker / Supervisor / ROS 2 launchが所有します。devdashはSupervisor、自動復旧、依存DAG実行、独自のReady待機を実装しません。Execution、現在のHealth/Readiness、操作結果は別々に表示します。

[比較レポート](docs/stage-comparison.md)／[生成・移行・適用手順](docs/stage-configuration.md)／[常駐運用](docs/resident-operation.md)

## セットアップと比較実行

Ubuntu 22.04 / 24.04、Python 3.10以上を対象とします。検証環境はUbuntu 24.04 / Python 3.12.3です。GUIには`python3-tk`とディスプレイが必要です。

```bash
uv sync --locked --extra gui --group dev --python /usr/bin/python3
uv run devdash --version                 # 0.3.0.dev0 / Core IPC 2
uv run devdash validate examples/robot.yaml
```

uvなしの場合は専用venvへ`pip install -e '.[gui]'`します。ROS環境とCore/GUIのvenvは分離します。Bridgeは独立した0.1.1 / IPC 1のままです。

比較中は既存main版CoreとSocket・venvを分けてください。以下は生成だけで、systemd設定の適用やサービス起動は行いません。

```bash
uv run devdash generate examples/robot.yaml ./generated-robot        # 一覧・差分・内容をプレビュー
uv run devdash generate examples/robot.yaml ./generated-robot --write
uv run devdash --socket "$XDG_RUNTIME_DIR/devdash-stage/core.sock" \
  daemon --config ./generated-robot/current/devdash.yaml
# 別ターミナル
uv run devdash-gui --socket "$XDG_RUNTIME_DIR/devdash-stage/core.sock"
```

`examples/robot.yaml`のunit参照を実環境に合わせてください。明示依存のdrop-inをまだsystemdへ適用していない場合、該当StageのStartはInspectionで拒否します。生成結果をレビューして明示適用する手順は[設定手順](docs/stage-configuration.md)を参照してください。unit本体のExecStartやRestartポリシーは生成し直しません。

旧Core/GUIとはIPCが非互換です。旧クライアントがStage版Coreへ操作を強行することはありません。既存のリリースタグは変更しません。

## 設定と責務

```yaml
workloads:
  camera:   {backend: systemd, scope: user, unit: camera.service}
  imu:      {backend: systemd, scope: user, unit: imu.service}
  detector: {backend: systemd, scope: user, unit: detector.service}
stages:
  - id: sensors
    name: Sensors
    workloads: [camera, imu]
    description: Check camera images and IMU data
    checks: [Verify images and IMU before explicitly starting Perception]
  - id: perception
    name: Perception
    workloads: [detector]
dependencies:
  detector: [camera, imu]
```

Stage順序と必須依存は別です。上記の明示依存だけから`Requisite=camera.service imu.service`と`After=...`を、所有するdrop-inへ生成します。前提unitを自動起動せず、起動済みであることをsystemdが要求します。依存は同じmanagerのsystemd Workload同士、前段Stageから後段Stageに限ります。同一Stage内の必須依存、逆行、循環、重複所属は拒否します。

既存unitのWants/Requires/PartOf/BindsTo等は保持します。Inspectionで起動の前倒し・停止伝播・順序矛盾・Conflicts・未適用依存を診断します。既知の矛盾と選択unitの取得失敗はStage操作を拒否します。動的トリガーなど未解析の間接作用は警告として残し、完全な安全性を保証しません。

| Lifecycle | 内部観測 | 任意の子操作 |
| --- | --- | --- |
| systemd user/system | systemd | host Supervisor / ROS Bridge |
| systemd user/system | `observer.docker`のDocker API | コンテナ内Supervisor / ROS Bridge |
| Docker Engine（既存直接管理） | Docker API | コンテナ内Supervisor / ROS Bridge |

`backend`は親Lifecycle、`controller`は内部Supervisor/Bridgeです。名称を混在させません。HTTPは`observer.http: {url: ..., expected_status: 200, timeout_ms: 1500}`で読み取り観測できます。HTTP/Docker Healthが不明・異常でも、Coreは起動Readyを推測したり再起動したりしません。

[systemd + Docker](examples/systemd-docker.yaml)、[Docker直接管理](examples/docker-supervisor.yaml)、[ROS Bridge](examples/roslaunch-bridge.yaml)、[HTTP観測](examples/http-observer.yaml)、[system manager](examples/system-robot.yaml)、[systemd側Health待機PoC](examples/systemd-docker-health/)を参照してください。

## GUI / CLI操作

GUIはStage順にWorkloadと内部要素を表示します。Stageを選ぶと説明・確認事項・対象・現在の操作結果を表示し、Nextボタンには次のStageと対象Workloadを明記します。Stageを直接選択してStartできます。現在activeという観測だけで過去のStage操作を成功と扱いません。

```bash
# 各コマンドは要求受付。完了・部分成功・エラーはstatusで確認します。
devdash stage start sensors
devdash status
devdash next sensors                 # perceptionへ明示Start。自動カーソルなし
devdash stage restart perception     # 対象だけBackend Restart。後続は再実行しない
devdash workload stop camera
devdash cancel                      # 発行済みジョブは取消・ロールバックしない
devdash check perception
devdash logs detector
```

比較用Socketを使う場合は各コマンドの先頭に`--socket "$XDG_RUNTIME_DIR/devdash-stage/core.sock"`を付けてください。Stageメンバーは独立した要求として並列発行し、一部失敗しても発行済み結果をすべて追跡します。既にactiveなunitへのStartはsystemdに委譲し、不必要なRestartは発行しません。全体停止は必要なStageを逆順で明示Stopしてください。

GUI終了・Core終了はWorkload停止ではありません。Core復旧時は新しい観測基準を取得し、過去の操作は再実行・成功推定しません。通信断はstaleとして表示し、GUI操作を無効にして復旧を待ちます。Core/GUIの版・接続・互換性も表示します。

## Configuration Editor / Generator

GUIのConfiguration Editor、または`devdash edit ./robot-source.yaml`で開きます。

- Workload追加・削除・既存unitのScan候補選択、Lifecycle/Observer/Child Controller設定。
- Stage追加・削除・並び替え、Workload割り当て、説明と確認事項。
- 必須依存の指定、検証、ライブ依存Inspection。
- 生成ファイル一覧・追加/変更/削除のdiff、Gitディレクトリへの保存。

フォーム編集はApplyで編集モデルへ反映し、Saveは編集元だけを保存します。Generateは同じモデルから実行YAMLとdrop-inの世代を作り、検証後に`current`を原子的に切り替えます。保存・生成でCoreは再読込されず、systemdのdaemon-reloadやLifecycle要求も発行しません。

Scan / Importは編集候補または編集元へ反映します。生成済みYAMLを直接書き換えません。

```bash
devdash scan user
devdash source-import ./robot-source.yaml user camera.service
devdash generate ./robot-source.yaml ./generated --inspect          # 任意のライブ検査
devdash generate ./robot-source.yaml ./generated --write
# 必要なdrop-inを手動レビュー・適用してから
devdash reload                                                     # Core設定の明示再読込だけ
```

再読込中に古い観測が戻っても破棄し、変更後のStageへ古い操作成功を流用しません。操作中の再読込は拒否します。Generatorの所有ファイルを手編集した場合は上書きを拒否し、無関係なファイルは管理しません。

## 旧設定からの移行

```bash
devdash migrate old-coordinator.yaml                               # 変換候補と警告だけ
devdash migrate old-coordinator.yaml --output new-stage-source.yaml
```

旧Workload定義を保持し、Parallelは同一Stage、逐次Stepは別Stage、Manual Gateは開発者向けチェックへ変換する候補を提示します。必須依存は推測せず、編集元で明示してください。HTTP/container_ready Gateは条件を移行レポートへ保持し、Ready委譲の確認なしに出力しません。必要なunit側対応を確認後だけ`--ack-readiness-delegation`で新規ファイルへ保存できます。旧ファイルを上書きしません。

## 検証

```bash
uv run pytest -q tests/python -m 'not integration and not gui'
# ディスプレイと実user manager、専用イメージがある環境のみ
DEVDASH_GUI_TEST=1 DEVDASH_SYSTEMD_TEST=1 DEVDASH_DOCKER_TEST=1 uv run pytest -q tests/python
uv run ruff check devdash tests/python examples/systemd-docker-health
uv run ruff format --check devdash tests/python examples/systemd-docker-health
uv build --wheel
```

Dockerの専用fixtureが未作成なら、明示的に一度ビルドします。

```bash
docker build -t devdash-supervisor-test:4.3.0 tests/fixtures/supervisor
```

テストは一意な`devdash-py-it-*` user unitと専用コンテナだけを操作し、既存サービスやlingerを変更しません。system managerの実テストは読み取りのみで、認可拒否は疑似D-Busでも検証します。ROSの実環境固有テストは従来の`tools/run_roslaunch_tests.py`を利用します。

旧テストの対応・削除した状態管理・9シナリオの操作比較・既知の制限は[比較レポート](docs/stage-comparison.md)にまとめています。MIT / Apache-2.0、GUIは引き続きttkbootstrap / Tkinterです。Rust + GPUI版は`archive/rust-gpui`に保存しています。
