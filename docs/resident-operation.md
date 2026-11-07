# 常駐運用・更新・互換性

Core/GUIは同じ`devdash-python`パッケージ（0.2.1）、ROS Bridgeは独立した`devdash-roslaunch`パッケージ（0.1.1）です。
Core/GUI IPCとROS Bridge IPCはそれぞれ世代番号1を持ち、パッケージ版番号の一致を要求しません。
GUI、Core、Workloadのライフサイクルは独立しています。インストール・テンプレート出力だけでは、サービスのenable、linger、GUI自動起動登録を行いません。

## ファイル配置

| 内容 | テンプレートの配置 |
| --- | --- |
| リリースごとの専用venv | `~/.local/share/devdash/versions/0.2.1/` |
| 使用版のsymlink | `~/.local/share/devdash/current` |
| 設定 | `~/.config/devdash/config.yaml` |
| Core unit | `~/.config/systemd/user/devdash-core.service` |
| GUI自動起動 | `~/.config/autostart/devdash.desktop` |
| Core Socket/ロック | `%t/devdash-python/core.sock` / `core.lock`（通常`/run/user/UID`以下） |
| GUI二重起動ロック | `$XDG_RUNTIME_DIR/devdash-gui/`（CoreのRuntimeDirectoryとは別） |
| Manual Gate状態 | `~/.local/state/devdash-python/approvals.json` |
| Coreログ | user journal、`journalctl --user -u devdash-core.service` |

常駐テンプレートではパスを上記に固定しています。開発用CLIのXDG_STATE_HOME等の既定値を変える場合は、unit側の引数も明示的に変更します。
設定ファイルにはunitやコンテナへの参照を記述し、WorkloadのExecStart等は既存のsystemd unitが正本です。

## バージョン固定インストール

UbuntuのシステムPythonを使用する例です。Core専用venvにROSの`--system-site-packages`は使いません。

```bash
sudo apt-get install -y python3-venv python3-tk
# レビュー済みのソースを選ぶ。別の版を使う場合は以下の版番号も合わせる。
git checkout v0.2.1
uv build --wheel
uv export --locked --no-dev --extra gui --no-emit-project \
  --output-file dist/devdash-0.2.1-requirements.txt
DEVDASH_RELEASE="$HOME/.local/share/devdash/versions/0.2.1"
uv venv --python /usr/bin/python3 "$DEVDASH_RELEASE"
uv pip install --python "$DEVDASH_RELEASE/bin/python" --require-hashes \
  -r dist/devdash-0.2.1-requirements.txt
uv pip install --python "$DEVDASH_RELEASE/bin/python" --no-deps \
  dist/devdash_python-0.2.1-py3-none-any.whl
"$DEVDASH_RELEASE/bin/python" -I -m devdash --version
mkdir -p "$HOME/.local/share/devdash"
ln -sfn "$DEVDASH_RELEASE" "$HOME/.local/share/devdash/current"
```

wheelとハッシュ付きrequirementsを一緒に保管します。venvは作成したパスから移動せず、稼働中のvenvへ上書きインストールしません。
Coreだけならexportの`--extra gui`を外せます。デスクトップ側でも同じvenvを使う運用ではgui extraを含めます。
0.2.1の依存・ソースを固定する手順であり、OS側のPython/Tcl/Tkを独自配布する仕組みは追加しません。

## Coreのuser service

まず設定を用意し、参照するunitを確認します。Coreの開始はRobotの開始ではありません。
同じSocketを使う手動Coreがあれば、先にそのCoreだけを明示的に終了します（Workloadの停止は不要です）。

```bash
mkdir -p "$HOME/.config/devdash" "$HOME/.config/systemd/user"
# サンプルを参考に config.yaml を作成・編集する
"$HOME/.local/share/devdash/current/bin/devdash" validate "$HOME/.config/devdash/config.yaml"
"$HOME/.local/share/devdash/current/bin/devdash" template core \
  > "$HOME/.config/systemd/user/devdash-core.service"
systemctl --user daemon-reload
systemctl --user enable --now devdash-core.service
systemctl --user status devdash-core.service
```

テンプレートは`Restart=on-failure`でCore自身の再起動をsystemdへ委譲します。GUIやgraphical-session.targetへの依存はありません。
Pythonの`-I`と環境変数の除外で、ROSのPYTHONPATH/PYTHONHOMEやDISPLAYに依存せず起動します。
user busは`%t/bus`を使用し、単独CLIでもDBUS_SESSION_BUS_ADDRESSがなければruntimeのbusを参照します。
Socketは0700ディレクトリ内の0600です。RuntimeDirectoryは停止後も保持し、起動競合で既存Coreのロックファイルを消さないようにします。通常終了時のSocket削除と異常終了後の再取得はCoreがロック下で行います。ログはjournalへ出します。

ログイン前からuser managerを起動したい場合だけ、次を明示的に実行します。

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

認可が必要な環境ではシステム管理者に設定を依頼します。devdashがsudoや独自の昇格デーモンを起動することはありません。
解除もユーザーの判断で`loginctl disable-linger "$USER"`を実行します。他のuser serviceにも関わる設定なので、devdashのアンインストールで自動解除はしません。
PCそのものの再起動・ログアウトを使うlinger試験は行わず、専用unitでの動作と設定例を用意しています。

## GUIのXDG Autostart

```bash
mkdir -p "$HOME/.config/autostart"
"$HOME/.local/share/devdash/current/bin/devdash" template autostart \
  > "$HOME/.config/autostart/devdash.desktop"
# 今すぐ開く場合
"$HOME/.local/share/devdash/current/bin/python" -I -m devdash.gui
```

`template autostart`は現在ユーザーのhomeを使った絶対パスを出力します。desktopファイルはHOMEや`~`のシェル展開を前提にしません。
ログイン時にGUIだけが開始され、Coreへ接続します。Core不在・再起動中も終了せず再接続を待ちます。
通信断中の表示はstaleとし、操作・Gate承認を無効化します。復旧時は現在状態を読み直します。
同じSocketのGUIは既定で一つです。重複起動はメッセージを出して終了し、既存ウィンドウやCoreを終了させません。
必要なら`devdash-gui --new-window`で追加ウィンドウを明示的に開けます。既存ウィンドウを前面化する独自IPCは追加していません。
自動起動の解除は、このdesktopファイルを削除するか`Hidden=true`を設定します。

## 更新とロールバック

1. 別版のwheelと固定requirementsから、新しい`versions/<version>`にvenvを作ります。既存のvenv、ROS workspace、Dockerイメージは変更しません。
2. 新しいvenvの`python -I -m devdash --version`と`validate`を確認します。必要なら設定と承認ファイルを別名でバックアップします。
3. GUIの進行中操作を確認します。できればGroup操作が完了してからGUIを閉じます。Coreを途中で止めた操作は成否不明として扱い、自動再開しません。
4. Coreを止め、`current`を切り替え、CoreとGUIを起動します。

```bash
# DEVDASH_RELEASEには事前に検証した新しいvenvの絶対パスを設定
systemctl --user stop devdash-core.service
ln -sfn "$DEVDASH_RELEASE" "$HOME/.local/share/devdash/current"
systemctl --user start devdash-core.service
"$HOME/.local/share/devdash/current/bin/python" -I -m devdash.gui
```

管理対象へのStop/Restartは発行しません。テンプレートにも管理対象へのPartOf/BindsTo等を追加しません。
既存のsystemd依存関係自体を無効化する仕組みではないため、CoreとWorkloadのunitは独立した構成にします。
テンプレートを変更した版では差分を確認してunitを更新し、`daemon-reload`を行います。版更新だけならunitのExecStartは同じです。

ロールバックは、保管した旧venvを実行して版番号・設定互換性を確認し、上記と同じ停止→symlink切替→開始を行います。
承認ファイルは版ごとに分けず、同じboot IDなら引き継ぎます。OS再起動後の承認はリセットします。過去のシーケンス結果は復元しません。
0.1.0以前の無世代IPCへ戻す場合、Core/GUIも対応する組を選び、Bridgeの互換性を別途確認してください。新版との混在操作は拒否されます。

## ROS Bridgeの独立更新

BridgeはROS側のPythonへインストールします。Core/GUIのvenvへlaunch/rclpyを導入しません。
Jazzyを使用するホストの例です。Dockerなら同じ手順をROSイメージのビルド側で行います。

```bash
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages "$HOME/.local/share/devdash-roslaunch/versions/0.1.1"
# bridge-v0.1.1等で固定したソースからBridgeだけをビルド
uv build --wheel --project bridges/roslaunch --out-dir "$PWD/dist"
"$HOME/.local/share/devdash-roslaunch/versions/0.1.1/bin/pip" install --no-deps \
  dist/devdash_roslaunch-0.1.1-py3-none-any.whl
"$HOME/.local/share/devdash-roslaunch/versions/0.1.1/bin/devdash-roslaunch" --version
```

Bridgeが使うlaunch/rclpyはROS側から供給します。ラッパーの起動パスを変える場合は、対象のlaunch unitを利用者が計画的に変更します。
Bridge自身の更新でlaunch Workloadを再起動する必要がある場合も、Core更新とは別の明示的操作です。
Core/GUIの更新ごとにBridge更新は不要です。0.1.1はBridge IPC世代1に対応し、同世代を維持するCoreと独立に利用できます。
無世代だったBridge 0.1.0は、子要素の操作に新しい互換性確認を使うため一度更新が必要です。親のsystemd/Docker Workload操作はBridgeの版に依存しません。

## 互換性と結果不明

- Core/GUIはインストール済みdistributionメタデータを表示し、SnapshotにCore版・IPC世代・起動ごとのinstance IDを付けます。
- Core/GUIのパッケージ版が違ってもIPC世代1なら接続し、GUIへ版違いを表示します。非互換なIPCでは操作せず、理由を表示して待機します。
- 操作前のSnapshot確認と操作要求は同じUnix Socket接続で行います。旧Coreの無世代応答には操作を送りません。
- 古いCore instance IDを使う要求は拒否します。操作送信後に応答が失われた場合は結果不明とし、自動再送しません。
- Core再起動をGUIが検出した場合、前の操作が進行中なら結果不明と明示します。新Coreのrunは空で、以前の成功を推測しません。
- Bridgeも版・IPC世代・既存のepochを表示します。Core側と一回限りのRPCクライアント側の両方で確認し、非互換や古いepochで子操作を実行しません。
- Bridgeの互換性確認と子操作も同じ接続です。非互換Bridgeの情報は表示しますが、子要素の操作能力は提供しません。

接続復旧は新しい観測基準を得る処理です。未観測・unknown・Not Foundを正常稼働へ置き換えません。
監視していなかった期間の操作元や中断ジョブの結果を推測せず、サービスの状態だけを再取得します。

## 今回の検証と制約

Ubuntu 24.04 / Python 3.12.3で全189テストを検証しています。内訳はGUI 2、実systemd 4、専用Docker 1、それ以外の182ケースです。
常駐化時に既存118ケースへ追加した33ケースは次を検証します。さらに0.2.1で、[自己管理防止と競合](self-protection.md)の38ケースを追加しています。

- 起動済みWorkloadを持つ疑似managerに、実Coreプロセスが起動・再起動時のStart/Stopを発行しないこと。
- Gate待機中・発行済みジョブ待機中にCoreをSIGTERM/SIGKILLし、成否を復元せず、次回の観測から再開すること。
- GUIが通信断から復旧し、Coreの新しいinstance、版違い、非互換IPCを表示すること。GUI終了後もGateを承認できること。
- 旧Coreへの操作送信を避けること、古いCore instance IDを拒否すること、受付後の通信断で結果不明とし再送しないこと。
- ROS不要の実Bridge Socket処理と一回限りのRPCクライアントで、非互換・古いepochの子操作を拒否すること。
- 配布unitを一時名で起動し、Core強制終了をsystemdが自動再起動すること。別の専用Workload unitのPID/InvocationIDは通常停止・クラッシュ・再起動を通じて同じであること。
- Core/GUI wheelの同一版と、Bridgeの独立wheelを別venvへ導入し、ROSやGUIなしでCoreの版確認・テンプレート出力ができること。

実unitの有効化登録、linger設定、自動起動ファイルの配置はテスト中も変更していません。実OS再起動、実デスクトップログインを伴うAutostart、対話Polkit認証、Ubuntu 22.04は未検証です。
ROSノードは今回起動していません。既存JazzyテストのRPC要求は世代番号へ対応させていますが、実ROSでの再実行は別途必要です。
自動更新、GUIウィンドウの前面化、ネットワーク越しRPCは追加していません。

## トラブルシューティング

```bash
systemctl --user status devdash-core.service
journalctl --user -u devdash-core.service -b --no-pager -n 100
"$HOME/.local/share/devdash/current/bin/devdash" --version
"$HOME/.local/share/devdash/current/bin/devdash" status
```

| 症状 | 確認すること |
| --- | --- |
| Coreが開始できない | `current`の実体、venvの絶対パス、設定の`validate`、journal。Coreは設定エラーを正常状態として開始しない |
| Coreは動くがGUIがない | Tkinter、DISPLAY/Xwayland、desktopファイルのExec/TryExec、GUIロック。同じvenvで`python -m tkinter`を確認 |
| GUIが切断・stale | Coreの状態、同じSocketパスとUIDか。復旧を待つかGUIを開き直す。操作の自動再送はしない |
| IPC世代が非互換 | `current`と稼働中Core/GUIの版を確認し、対応する版でCore/GUIを明示的に再起動 |
| system unitを操作できない | Coreの実行UIDとPolkit認可。user serviceは対話シェルとは認可コンテキストが異なり、対話認証agentを使えるとは限らない。拒否理由を確認し、必要なポリシーは管理者が設定する |
| Dockerだけunknown | Core実行ユーザーからsocketへアクセスできるか、Dockerが動作しているか。rootへの自動昇格はしない |
| Bridgeの子だけ操作できない | Bridge版・IPC世代・epoch・children_error。旧Bridgeは一度0.1.1以降へ更新。通常のlaunch ActionのStart/Restartは引き続き非対応 |
| 承認ファイルが壊れている | journalを確認し、ファイルを保管してから利用者が明示的に初期化する。自動的に承認済み扱いにはしない |

lingerの起動動作は[loginctl 249](https://www.freedesktop.org/software/systemd/man/249/loginctl.html)、
GUI自動起動は[XDG Autostart](https://specifications.freedesktop.org/autostart/latest/)、
Execの引用は[Desktop Entry仕様](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html)を参照しています。
