# On-demand Plan版の常駐運用・比較起動・更新

このブランチはCore/GUI `0.4.0.dev1`、Core IPC **3**です。ROS Bridgeは独立した`0.1.1` / IPC 1です。mainのCoordinator版IPC 1、Stage版IPC 2とは操作互換性がありません。既存ブランチ・タグは変更せず、比較用venv・設定・Socketを分けてください。

## 導入と常駐Core

専用venvにこのブランチの確認済みcommitからwheelを導入します。ROS環境やDockerイメージにCore/GUIを組み込む必要はありません。

```bash
uv sync --locked --extra gui --group dev --python /usr/bin/python3
uv build --wheel
uv export --locked --no-dev --extra gui --no-emit-project -o plan-requirements.txt
uv venv --python /usr/bin/python3 "$HOME/.local/share/devdash-missing/current"
uv pip install --python "$HOME/.local/share/devdash-missing/current/bin/python" \
  --require-hashes -r plan-requirements.txt
uv pip install --python "$HOME/.local/share/devdash-missing/current/bin/python" --no-deps \
  dist/devdash_python-0.4.0.dev1-py3-none-any.whl
```

`devdash template core`はテンプレートを標準出力へ出すだけです。実行パスを上記venvへ、Socket / RuntimeDirectory / configを比較用へ置き換えて、**利用者が明示的に**`~/.config/systemd/user/devdash-missing-core.service`等へ保存してください。例では`RuntimeDirectory=devdash-missing`、`--socket "%t/devdash-missing/core.sock"`、`--config <生成出力>/current/devdash.yaml`を使います。

```bash
# 内容・パスをレビューして配置した後だけ
systemctl --user daemon-reload
systemctl --user enable --now devdash-missing-core.service
# ログイン前から起動する場合だけ。devdashは設定しません。
loginctl enable-linger "$USER"
```

CoreはGUI・デスクトップセッション・Approval状態ファイルを要求しません。Restart=on-failureはsystemdへ委譲します。Core開始・終了・SIGKILL後の再起動でWorkloadのStart/Stop/Restartを発行しません。Socketは同一ユーザーの0700ディレクトリ内0600、Core二重起動はlockで拒否します。

## GUI自動起動

`devdash template autostart`のvenvパスとSocketを比較用へ変更し、利用者がXDG Autostartへ明示配置します。GUI終了でCoreを止めません。GUIがCore未起動・再起動中でも待機・再接続し、通信断中はstale表示と操作無効化を行います。同じSocketのGUIは既定一つで、`--new-window`で明示的に複数にできます。

```bash
"$HOME/.local/share/devdash-missing/current/bin/devdash-gui" \
  --socket "$XDG_RUNTIME_DIR/devdash-missing/core.sock"
```

## 更新・Rollback・設定再読込

比較ブランチは細かいタグを付けずcommitで固定します。新旧venvを別ディレクトリに保管し、Core/GUIは同じwheelの版を使います。BridgeはROS側の環境で独立更新できます。

進行中の操作・Planを完了または中止し、発行済みジョブの結果を確認してGUIを閉じます。外部systemctlで比較用Coreだけを止め、venvの参照先を切り替えてCoreを再開します。Rollbackは保管した互換性のあるvenvへ戻す同じ操作です。IPC 1・2の旧版を同じSocketへ混在させず、設定モデルも対応するものへ戻してください。

設定の編集・生成・drop-in適用・Core再読込は[設定手順](stage-configuration.md)の通り分離します。Core再起動・設定再読込は過去のStage成功を復元しません。古いApprovalファイルは読み書きしません。必要なら利用者がmain比較用に保管してください。

PlanはCoreプロセス内の一回限りの実行です。GUI終了・切断中は継続し、再接続で現在のRunを表示します。Core再起動ではRun、preview、Checkpoint、確認tokenを失い、自動再開しません。現在Activeという観測を過去のPlan成功と扱いません。通常終了・異常終了でも管理対象へのStop/Restartは発行しません。

## 問題の確認

```bash
systemctl --user status devdash-missing-core.service
journalctl --user -u devdash-missing-core.service -b
devdash --version
devdash --socket "$XDG_RUNTIME_DIR/devdash-missing/core.sock" status
```

版、Socket、Core instance、managerの接続、Polkit理由、Docker socket権限、Inspection診断を確認します。Stageの失敗と現在のWorkload状態は別です。要求受付は完了ではなく、通信断で結果が不明な操作は自動再送しません。前段Activeは現在の機能的Healthを保証しません。
