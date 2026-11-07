# systemd統合と運用設計

## 変更した構成

実行エンジンは従来どおり、Groupを順序付きStepsへ展開し、逆順Stop／正順Startを行います。
依存グラフは実行順序の決定に使用しません。

- **Workload**: 論理IDとは別に、`scope + unit名`でmanager上の対象を識別。
- **Backend**: system/user busを区別。接続できないmanagerの理由を保持して表示。
- **Inspection**: D-BusのUnitプロパティを読み、選択Groupとの矛盾と影響経路を報告。
- **Monitor**: systemdイベントと状態の観測。直近200件だけをメモリに保持。
- **GUI**: scope、操作エラー、診断、検査未完了／古い診断、Gate不整合、観測イベントを表示。

Discoveryは手動Scan、Resolutionは登録済みunitの再解決、Inspectionは依存経路の調査です。
Inspectionが参照した未登録unitを設定へ追加しません。

## system/userと認可

```bash
cargo run -- scan --scope user
cargo run -- scan --scope system
cargo run -- import robot-sensors.service --scope system
cargo run -- check robot --action restart
```

同名unitを両managerからImportできます。論理IDが衝突した場合はscopeをIDへ付加します。
同名であっても、一方への操作を他方のmanagerへ送ることはありません。
Import後の設定反映には、従来どおりCore再起動が必要です。

Start/Stop/Restartはsystemd D-Busへ委譲します。D-Bus要求のAllowInteractiveAuthを有効にし、
既存のPolkitが認可と必要な認証を担当します。認証エージェントや管理ポリシーがない場合は、
元のD-Busエラー、scope、unit、操作、Polkitのmanage-units認可が必要な旨を表示します。
管理者が既存のPolkit運用に沿って認可してください。devdashはPolkitルールを自動生成しません。

GUI／Coreをrootで動かす必要はありません。sudoを呼ぶヘルパーや特権デーモンも実装していません。
journalも利用者の権限で読みます。journalctlが返すアクセス制限のヒントをログ欄へ表示します。
Polkitでサービス操作が認可されても、journalの読み取り権限が得られるとは限りません。

## 依存診断の基準

| 関係 | 解析する意味 | 主な診断 |
| --- | --- | --- |
| Wants | 弱い起動要求の伝播 | Gateを越える要求は警告 |
| Requires / BindsTo | 強い起動要求、および逆方向の停止影響 | 未評価Gateより後のWorkloadへ起動要求が到達する構成はエラー |
| PartOf | 対象からこのunitへのStop/Restart伝播 | 選択Group外への影響を警告。逆方向の起動依存とは扱わない |
| After / Before | 同時にキューへ入ったジョブの順序 | Stepsとの食い違いを警告。起動要求は生成しない |
| Conflicts | 片方のStartが他方をStopし得る | Group内で同時稼働を意図する組合せはエラー、外側への停止は警告 |
| Requisite | 既に起動済みである必要。起動要求は伝播しない | Stepsで後に配置された前提への警告、逆方向の停止影響 |
| Upholds | systemdによる継続的な起動 | 選択Groupの停止が維持されない可能性を警告 |
| PropagatesStopTo | 明示的な停止伝播 | Group外へ到達する影響を警告 |

RequiredBy、WantedBy、BoundBy、ConsistsOf、ConflictedBy、RequisiteOf、UpheldBy、
StopPropagatedFromも読み、意味の異なる関係を混同せずに逆方向を調べます。
未登録target等を経由する間接的な経路も対象です。診断には影響unitと経路を添えます。
unit別名はD-BusのIdで正規化します。ジョブの完了通知も正規名で購読し、ジョブパスで照合します。

**エラーは実際のActive状態の予言ではなく、構成上の要求の矛盾を拒否するものです。**
例えば、Gate前のWorkloadがGate後のWorkloadをRequiresしていれば、devdashがGateを待つ前に
systemdへ後者の起動要求が渡ります。条件判定で後者が起動しない可能性があっても、この要求順序の矛盾を拒否します。
Wantsは弱い要求であるため警告とし、実際の稼働は観測側でも確認します。

Group操作はエラーがあると、個々のサービス操作を発行する前に失敗します。
警告／検査未完了だけなら操作を続けられます。通常Restartでは承認済みManual Gateを再利用し、
Reinitializeでは承認をリセットする前提で検査します。個別Workload操作はGate・Group検査を迂回します。

## 検査の範囲と鮮度

起動時、手動Check、Group操作直前に検査します。登録済みunitの消失・復帰時にも診断を更新します。操作直前はキャッシュを信用せずD-Busから再取得します。
Reloadingイベントを受けたら既存診断をstaleにして再検査します。イベント取りこぼしも表示して再検査します。
承認変更も診断を無効化します。Core側の無効化世代とBackend側のmanager世代を取得前後で確認し、
古い取得結果で新しいキャッシュを上書きしません。操作前検査が既知のstaleになった場合は操作を拒否します。

操作前は選択Group配下のunitだけを探索開始点にし、依存経路と逆方向プロパティから外部への影響を調べます。
部分Groupから外への起動伝播は警告し、関係する親Group等の未承認Manual Gateも表示します。
親のGateを部分Group操作の新たな必須条件にはしません。バックグラウンドの全体検査では全登録unitから取得を共有します。
診断の`requested_units`／`uninspected_units`に要求対象と取得できなかった対象を記録します。

調査は256回のunit参照と6秒の時間予算、各unit最大2秒に制限しています。
未取得プロパティ、接続・権限エラー、制限到達はInspection incompleteとして表示します。
OnFailure／OnSuccess、socket等のTriggers／TriggeredByがある場合も、動的な発火を再現しない旨を表示します。

以下は完全には判定できません。

- Condition／Assert、起動失敗、状態に応じてsystemdが弱い依存ジョブを落とす場合。
- 未登録unitを含むConflictsと停止伝播の複合的な全トランザクション。
- socket・timer・path・D-Bus activation、OnFailure／OnSuccess等の動的起動全般。
- 検査後または検査中の並行した設定変更や、他クライアントとの操作競合。
- 取得上限より先の経路、managerの再起動・接続切断中の状態。

systemdトランザクションの完全な再実装、安全性保証、unit設定の自動修正は行いません。
manager接続とScanは各5秒、状態取得はCore側で全体5秒に制限します。状態取得用Proxyはプロパティキャッシュを持たず、現在値を問い合わせます。

操作中の再読み込みは警告しますが、サービスの自動停止やロールバックは行いません。

## 予期しない状態変化の観測

JobRemoved、UnitNew、UnitRemoved、Reloadingを購読し、登録済みunitに関係するものを記録します。
UnitRemovedだけでファイル削除とは判断せず、名前による再解決でNot Foundを確認します。
ActiveState／SubStateは1秒間隔で取得します。InvocationIDの変化から、ポーリング間の再起動も観測できます。

scopeごとに観測用と制御用で別のD-Bus接続を使います。購読の準備後にSubscribeし、初期状態取得前からイベントを受信します。
広域readerは待機時間や状態問い合わせを挟まず、ポーリングは別タスクで行います。
readerのタスクはBackendが所有し、Backend破棄時に終了します。

自分のジョブはmethod発行前から相関を開始し、返信とJobRemovedの到着順に依存せずjob pathで照合します。
相関待ちのバッファは256件です。超過時は曖昧な履歴を正常と扱わず、そのscopeの監視を利用不能にします。
managerの名前所有変更・切断時も、保留操作を「結果不明」のFailedとして終了し、Busyを解放します。
発行済みジョブをキャンセルしたり、サービスを復元したりはしません。
監視を失ったscopeへの新しい操作は拒否し、1秒から最大10秒の間隔で再接続・再Subscribeします。
監視準備後に新規操作を受け付けます。旧世代の保留操作やGate待機は結果不明で終了し、自動再開しません。
復旧後の現在状態を新しい基準値とし、履歴欠落を保持します。詳細は[再接続とGUIテスト](reconnection-and-gui-tests.md)を参照してください。

- 起動直後の状態は基準値とし、過去の操作を推測しません。
- 自分の要求とジョブパスを可能な範囲で照合します。
- 状態変化については直近5秒の要求とも照合し、因果関係は未確認と表示します。同じ期間の別操作を完全に区別できません。
- 対応しない変化は「devdashの要求と対応しない／発生元不明」と表示します。外部ユーザーの操作と断定しません。
- Group操作中の対応しないジョブ・状態変化、unit消失、未承認Manual Gateの後続稼働を警告します。
- Gate不整合は継続中に同じ警告を繰り返しません。一時的なUnknownを解消とは扱いません。観測復帰時は空白を記録し、その間の変化を推測しません。
- InvocationIDは取得できた値どうしを比較し、欠落だけで再起動とは扱いません。
- ログは200件で打ち切ります。D-Busイベントの取りこぼしは明示します。UnitNew／UnitRemovedの情報ログはunitごとに5秒に1件までとし、状態変化やジョブ結果は抑制しません。
- イベントは種別・scope・受信時刻・相関の確度・job path／resultを保持し、GUIでも受信時刻を表示します。
- Snapshotの`monitoring`と`monitor_gap`で、manager監視不能と履歴欠落を保持します。GUI上部にも表示します。

外部操作を禁止せず、観測に応じた修復、再起動、停止は一切行いません。
短い中間状態や、監視が途切れた期間のすべてを記録できる監査ログではありません。

## ROS 2 launch

`ros2 launch`を起動するsystemd service全体が一つのWorkloadです。
内部のノードやlaunchファイルを解析せず、ノードのSupervisorも作りません。
観測結果はWorkloadのObservationに載せるため、将来のノード観測をライフサイクル操作から分離できます。

[system設定例](../examples/system-robot.yaml)と[unit参考例](../examples/systemd/robot-sensors.service.example)を用意しました。
unit例のユーザー、ROSインストール、overlay、launch packageは実機に合わせて変更してください。
特にKillMode、KillSignal、TimeoutStopSecはunit側で定義します。

systemd統合の初回検証ではROS 2環境を使わず、専用user service内で親プロセスから子プロセス2つを生成し、
KillMode=control-groupによるStopで全PIDが終了することを確認しました。
ROS 2固有の終了ハンドラーや実機のデバイス解放は、実機での追加検証が必要です。後続の[ROS Bridgeと実Jazzy検証](container-processes-and-roslaunch.md)では、通常Actionの停止とComponentManagerの個別操作を追加しています。

## 検証

Ubuntu 24.04.2 / Rust 1.95.0で合計57テストが成功しました（通常54件＋実systemd3件）。別途GUI E2E 9シナリオも成功しました。
GUIを含むビルド、clippy（警告をエラー扱い）、rustfmtも成功しました。
X11 / Xephyr上で完成版のscope表示、診断表示、system/user Scanの切り替えを確認しました。
[初期の表示確認画面](devdash.png)と、[実際のGUI操作テスト・画面](reconnection-and-gui-tests.md)を保存しています。GUI E2Eでは専用ダミーuser unitのみを操作しました。
テスト専用unitの稼働・登録が残っていないことも確認済みです。

```bash
cargo test --locked --no-default-features
cargo test --locked --test systemd -- --ignored --nocapture
cargo clippy --locked --features gui --all-targets -- -D warnings
cargo build --locked --features gui --bins -j 4
```

- 従来Core: 19件。system scopeの旧読み取り専用制限を更新し、Backendへ操作が到達することを検証。
- 依存解析・監視: 19件。Gate迂回、停止方向、Afterのみ、Conflicts、未登録中間unit、同名別manager、再検査、警告抑制、操作前検査中のCancelによる承認保持、古い診断の公開防止、部分Group外への起動影響、観測空白など。
- 相関の単体テスト: 3件。返信と完了の両到着順、未対応ジョブの排出、相関バッファ超過。
- 一時D-Busのイベント統合: 11件。250ms遅延返信、owner喪失とCancel、64件を越えるバースト、初期状態取得中のイベント、遅いポーリングと操作中Reloading、manager交代・バス再起動・初期不在からの復旧・Gate待機の非再開・metadata通知抑制。サービス自動起動を無効にした専用バスを使用。
- IPC: 1件。独立Core、承認、Scan/Import、競合拒否など。
- 認可: 1件。一時D-Busをsystem busの代わりに使い、AllowInteractiveAuth、AccessDeniedの説明、再試行しないことを検証。
- 実systemd: 3件。従来シーケンス／Cancelと別名unit、依存変更・外部ジョブ・Gate不整合・削除再追加・子プロセス終了、両managerの読み取り。

systemd統合テストは共有managerへdaemon-reloadを発行するため、テスト間で直列化しています。

専用user unit以外のサービスは起動・停止・設定変更しません。
system managerの実サービスについてはScanと状態取得だけを行い、操作の認可失敗は一時D-Busで検証します。
実機のPolkit認証画面と、認可されたsystemサービス操作の実行は未検証です。
